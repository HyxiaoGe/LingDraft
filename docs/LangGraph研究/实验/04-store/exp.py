"""实验 04：长期记忆 Store。存取和筛选、语义搜索、挂到灵稿的对话上跨对话记住东西、和检查点的关系、记忆放进提示词对缓存的影响、存 Postgres。

在进程里跑灵稿的运行器（假模型、假网关），图换成"我们的 4 个 middleware + 要试的那个"，加两个记忆工具，其余照 agent.build_agent。
向量用假的：按相邻两个字算哈希（只能看机制，看不出语义好坏）。

- basics：InMemoryStore 的存、取、按条件筛、按前缀列命名空间、覆盖、删除。
- semantic：带向量的 InMemoryStore：哪些字段算向量、一条记忆多个向量时怎么打分、不进索引的怎么排。
- agent：对话 A 里说"记住……"，模型调 remember 存进 Store；对话 B 里问推荐，模型调 recall 查出来。看工具怎么拿到 Store、页面上什么样。
- checkpoint：记忆不在检查点里：重新回答、改了重答、删掉对话，Store 里的记忆都不跟着变。
- inject：不让模型自己查，middleware 每次调模型前按用户的话查出记忆放进系统提示词。中途多一条记忆，看下一次调用的开头还对不对得上。
- postgres：临时起一个 Postgres（pgserver），PostgresStore 建了哪些表、一条记忆存成什么样；带向量的要 pgvector。

用法（在 backend 下）：
  PYTHONPATH=. LINGDRAFT_FAKE=1 uv run python "../docs/LangGraph研究/实验/04-store/exp.py" basics semantic agent checkpoint inject
  PYTHONPATH=. LINGDRAFT_FAKE=1 uv run --with pgserver python "../docs/LangGraph研究/实验/04-store/exp.py" postgres
"""

import json
import sys
import tempfile
import time
import uuid
import zlib
from pathlib import Path

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain.tools import ToolRuntime
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.store.memory import InMemoryStore

from app.api.research_runner import ResearchRunner
from app.api.runner import LocalRunRegistry
from app.graph.research import agent, history
from app.graph.research.graph import memory_checkpointer
from app.graph.research.state import ResearchState
from app.research_llm import FakeResearchPlanner, FakeToolModel

TMP = Path(tempfile.mkdtemp(prefix="lg-store-"))
NS = ("memories", "sean")  # 灵稿没有登录，只有一个用户；有多个用户时第二段换成用户 id
DIMS = 256


def fake_embed(texts: list[str]) -> list[list[float]]:
    """相邻两个字一组，哈希到 256 维里计数。只看字面重合，不懂意思。"""
    out = []
    for t in texts:
        v = [0.0] * DIMS
        for a, b in zip(t, t[1:]):
            v[zlib.crc32((a + b).encode()) % DIMS] += 1.0
        out.append(v)
    return out


def store_with_index() -> InMemoryStore:
    return InMemoryStore(index={"dims": DIMS, "embed": fake_embed, "fields": ["content"]})


# ---- 记忆工具：工具从 runtime.store 拿到 Store ----

@tool("remember", description="Save one long-term memory about the user, such as a preference or a fact they asked you to remember.")
def remember(content: str, runtime: ToolRuntime) -> str:
    key = uuid.uuid4().hex[:8]
    runtime.store.put(NS, key, {"content": content, "thread": runtime.config["configurable"]["thread_id"]})
    return json.dumps({"ok": True, "id": key})


@tool("recall", description="Search long-term memories about the user.")
def recall(query: str, runtime: ToolRuntime) -> str:
    items = runtime.store.search(NS, query=query, limit=3)
    return json.dumps([{"id": i.key, "content": i.value["content"], "score": round(i.score or 0, 2)} for i in items], ensure_ascii=False)


class MemoryModel(FakeToolModel):
    """话里有"记住"：调 remember 存冒号后面那段，存完回一句；有"推荐"：先 recall，再照常搜、读、答。id 加随机后缀（同 03 的 FreshIds）。"""

    def _decide(self, messages):
        i = next(i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], HumanMessage))
        text, turn = str(messages[i].content), messages[i + 1:]
        names = [tc["name"] for m in turn if m.type == "ai" for tc in m.tool_calls]
        sfx = uuid.uuid4().hex[:6]
        if "记住" in text:
            if "remember" not in names:
                return "", [{"name": "remember", "args": {"content": text.split("：", 1)[-1]}, "id": f"call_remember_{sfx}"}]
            return "好的，记住了。", []
        if "推荐" in text and "recall" in self.bound and "recall" not in names:
            return "", [{"name": "recall", "args": {"query": text}, "id": f"call_recall_{sfx}"}]
        content, calls = super()._decide(messages)
        return content, [{**c, "id": f"{c['id']}-{sfx}"} for c in calls]

    def _message(self, messages):
        m = super()._message(messages)
        return m.model_copy(update={"id": f"{m.id}-{uuid.uuid4().hex[:6]}"})


class Recorder(AgentMiddleware):
    """放在最里面：记下每次真正发给模型的系统提示词和消息。"""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[list] = []

    def wrap_model_call(self, request, handler):
        self.calls.append([request.system_message, *request.messages])
        return handler(request)


class Chat:
    """像页面那样一句一句说（同 tests/test_research.py 的 Chat）。几个对话共用一个 Store、一个检查点库。"""

    def __init__(self, store, tools=(remember, recall), extra=()) -> None:
        self.planner = FakeResearchPlanner()
        self.planner._model = MemoryModel()
        self.saver = memory_checkpointer()
        self.store = store
        self.r = ResearchRunner(planner=self.planner, checkpointer=self.saver, data_dir=TMP / "data")
        self.rec = Recorder()
        self.r.graph = create_agent(  # 照 agent.build_agent，多了 store 和记忆工具
            model=self.planner.agent_model(), tools=[*self.planner.tools(), *tools], state_schema=ResearchState,
            context_schema=agent.ChatContext, checkpointer=self.saver, store=store, name="research",
            middleware=[agent.TurnMiddleware(self.planner), agent.ContentGuard(self.planner), agent.ToolProgressMiddleware(),
                        agent.InvalidToolCallRetryMiddleware(agent.INVALID_TOOL_CALL_RETRIES), *extra, self.rec])
        self.r.runs = LocalRunRegistry()

    def _run(self, tid: str) -> None:
        key = self.r._run_key(tid)
        self.r.runs.admit(key)
        self.r.runs.started(key)
        try:
            list(self.r._graph_events(tid, None))
        finally:
            self.r.runs.release(key)
        list(self.r._after_run(tid))

    def say(self, tid: str, text: str) -> dict:
        self.r.accept(tid, text)
        self._run(tid)
        return self.r.state_view(tid)

    def regenerate(self, tid: str, text: str | None = None) -> dict:
        self.r.regenerate_input(tid, text)
        self._run(tid)
        return self.r.state_view(tid)


def kinds(turn: dict) -> list[str]:
    return [i["type"] if i["type"] != "step" else i["kind"] for i in turn["items"]]


def memories(store) -> list[str]:
    return [i.value["content"] for i in store.search(NS, limit=50)]


# ---- 实验 ----

def basics():
    s = InMemoryStore()
    s.put(("memories", "sean"), "a", {"content": "不吃辣", "kind": "food", "weight": 3})
    s.put(("memories", "sean"), "b", {"content": "喜欢安静的地方", "kind": "travel", "weight": 1})
    s.put(("memories", "guest"), "a", {"content": "素食", "kind": "food", "weight": 2})
    s.put(("docs", "sean", "notes"), "n1", {"content": "旅行计划草稿"})
    print("  取一条：", s.get(("memories", "sean"), "a").value)
    print("  按前缀搜 ('memories',)：", [(i.namespace, i.key) for i in s.search(("memories",))])
    print("  再加条件 kind=food：", [(i.namespace[-1], i.value["content"]) for i in s.search(("memories",), filter={"kind": "food"})])
    print("  条件用比较 weight>=2：", [i.value["content"] for i in s.search(("memories",), filter={"weight": {"$gte": 2}})])
    print("  列命名空间：", s.list_namespaces(), "；只列前两层：", s.list_namespaces(max_depth=2))
    first = s.get(("memories", "sean"), "a")
    time.sleep(0.01)
    s.put(("memories", "sean"), "a", {"content": "不吃辣，微辣可以"})
    again = s.get(("memories", "sean"), "a")
    print(f"  同一个 key 再存：整条换掉 {again.value}；created_at 变了：{again.created_at != first.created_at}，updated_at 变了：{again.updated_at != first.updated_at}")
    s.delete(("memories", "sean"), "a")
    print("  删掉以后取：", s.get(("memories", "sean"), "a"))
    try:
        s.put(("memories", "sean"), "t", {"content": "x"}, ttl=1)
    except NotImplementedError as e:
        print("  InMemoryStore 设过期：", type(e).__name__, str(e)[:60])


def semantic():
    s = store_with_index()
    s.put(NS, "a", {"content": "不吃辣，微辣可以", "note": "去年说的"})
    s.put(NS, "b", {"content": "喜欢安静的地方，不爱排队"})
    s.put(NS, "c", {"content": "对花生过敏"})
    s.put(NS, "d", {"content": "住在杭州西湖区"}, index=False)  # 不进索引：取得到，按意思搜不到
    s.put(NS, "e", {"content": "周末想去吃辣的火锅", "tags": ["吃辣", "火锅"]}, index=["content", "tags[*]"])  # 一条存三个向量
    print("  每条存了几个向量：", {k: list(v) for k, v in s._vectors[NS].items()})
    for q in ("吃辣的菜", "排队的地方", "杭州西湖"):
        r = s.search(NS, query=q, limit=5)
        print(f"  搜 {q!r}：", [(i.value["content"][:8], None if i.score is None else round(i.score, 2)) for i in r])
    print("  不带 query 就不算分：", [(i.key, i.score) for i in s.search(NS, limit=3)])


def agent_mode():
    store = store_with_index()
    c = Chat(store)
    va = c.say("A", "记住：我不吃辣，微辣可以")
    print("  对话 A：", kinds(va["turns"][-1]), "回复：", va["turns"][-1]["items"][-1]["text"])
    print("  Store 里：", [(i.key, i.value) for i in store.search(NS)])
    vb = c.say("B", "推荐几道杭州菜，吃辣的菜不要")
    msgs = c.r.graph.get_state({"configurable": {"thread_id": "B"}}).values["messages"]
    got = next(m for m in msgs if m.type == "tool" and m.name == "recall")
    print("  对话 B：", kinds(vb["turns"][-1]), "；recall 查到：", got.content)
    print("  页面上记忆工具这一步不显示（history.STEP_TOOLS 里没有），来源只算搜索和读页：", len(vb["sources"]), "条")


def checkpoint():
    store = store_with_index()
    c = Chat(store)
    c.say("A", "记住：我不吃辣")
    print("  说完：", memories(store))
    c.regenerate("A")
    print("  重新回答一次：", memories(store))
    c.regenerate("A", "记住：我能吃一点辣")
    print("  改了那句话重答：", memories(store))
    print("  对话里现在只有一轮：", [t["user"] for t in c.r.state_view("A")["turns"]])
    c.r.delete("A")
    print("  删掉对话 A：", memories(store), "；检查点里还有 A 吗：", bool(list(c.saver.list({"configurable": {"thread_id": "A"}}))))


class InjectMemories(AgentMiddleware):
    """每次调模型前，按这一轮用户的话查记忆，接在系统提示词后面（LangMem 文档里的写法）。放在 TurnMiddleware 里面，系统提示词已经换好了。"""

    def wrap_model_call(self, request, handler):
        msgs = request.messages
        text = str(msgs[history.last_human(msgs)].content)
        found = request.runtime.store.search(NS, query=text, limit=3)
        note = "\n".join(f"- {i.value['content']}" for i in found)
        sys_text = f"{request.system_message.content}\n\n[Memories about the user]\n{note or '(none)'}"
        return handler(request.override(system_message=SystemMessage(sys_text)))


def same(a, b) -> bool:
    return a.type == b.type and a.content == b.content and getattr(a, "tool_calls", None) == getattr(b, "tool_calls", None)


def prefix(a: list, b: list) -> int:
    n = 0
    for x, y in zip(a, b):
        if not same(x, y):
            break
        n += 1
    return n


MANY = ["不吃辣，微辣可以", "对花生过敏", "住在杭州西湖区", "喜欢安静的地方", "周末常去灵隐寺爬山"]


def inject():
    """同一个对话说三轮，看每轮第一次调用和上一轮最后一次调用的开头对不对得上。三种做法：
    - 系统提示词、记忆少：1 条，第 3 轮之前别处新记了一条；
    - 系统提示词、记忆多：5 条，按这一轮的话查前 3 条，中途不新增；
    - recall 工具：记忆不进系统提示词，模型自己查，查到的是一条工具结果；第 3 轮之前同样新记一条。"""
    cases = (
        ("系统提示词、记忆少", MANY[:1], (), [InjectMemories()], True),
        ("系统提示词、记忆多", MANY, (), [InjectMemories()], False),
        ("recall 工具", MANY[:1], (remember, recall), [], True),
    )
    for label, initial, tools, extra, add_later in cases:
        store = store_with_index()
        for i, text in enumerate(initial):
            store.put(NS, f"m{i}", {"content": text})
        c = Chat(store, tools=tools, extra=extra)
        starts = []
        for n, text in enumerate(("推荐杭州好吃的", "推荐西湖边上的", "推荐灵隐寺附近的")):
            if n == 2 and add_later:
                store.put(NS, "new", {"content": "对花生过敏"})  # 比如别的对话里新记了一条
            starts.append(len(c.rec.calls))
            c.say("T", text)
        calls = c.rec.calls
        print(f"  [{label}]")
        for n in (1, 2):
            prev, cur = calls[starts[n] - 1], calls[starts[n]]
            print(f"    第 {n + 1} 轮第一次调用：开头对得上 {prefix(cur, prev)} 条（共 {len(cur)} 条），系统提示词一样：{same(cur[0], prev[0])}")
        if extra:
            for n in range(3):
                print(f"    第 {n + 1} 轮放进去的记忆：", calls[starts[n]][0].content.rsplit("]\n", 1)[-1].replace("\n", " "))


def postgres():
    import pgserver
    import psycopg
    from langgraph.store.postgres import PostgresStore

    srv = pgserver.get_server(TMP / "pg")
    url = srv.get_uri()
    with PostgresStore.from_conn_string(url) as s:
        s.setup()
        s.put(NS, "a", {"content": "不吃辣"})
        print("  不带向量：", [i.value for i in s.search(NS, filter={"content": "不吃辣"})])
    with psycopg.connect(url) as c:
        print("  建的表：", [r[0] for r in c.execute("select tablename from pg_tables where schemaname='public' order by 1")])
        print("  一行：", c.execute("select prefix, key, value, expires_at from store").fetchone())
        print("  有没有 pgvector：", c.execute("select name from pg_available_extensions where name='vector'").fetchall())
    try:
        with PostgresStore.from_conn_string(url, index={"dims": DIMS, "embed": fake_embed, "fields": ["content"]},
                                            ttl={"default_ttl": 60 * 24 * 30, "refresh_on_read": True}) as s:
            s.setup()
            s.put(NS, "b", {"content": "对花生过敏"})
            print("  带 query 搜（a 是开向量之前存的，没有向量）：", [(i.key, round(i.score, 2)) for i in s.search(NS, query="花生过敏")])
            print("  不带 query：", [(i.key, i.score) for i in s.search(NS)])
            with psycopg.connect(url) as c:
                print("  建的表：", [r[0] for r in c.execute("select tablename from pg_tables where schemaname='public' order by 1")])
                print("  过期时间：", c.execute("select key, ttl_minutes, expires_at is not null from store order by key").fetchall())
    except Exception as e:  # noqa: BLE001
        print("  带向量：", type(e).__name__, str(e)[:160])
    srv.cleanup()


MODES = {"basics": basics, "semantic": semantic, "agent": agent_mode, "checkpoint": checkpoint, "inject": inject, "postgres": postgres}

if __name__ == "__main__":
    for name in sys.argv[1:]:
        print(f"\n===== {name} =====")
        MODES[name]()
