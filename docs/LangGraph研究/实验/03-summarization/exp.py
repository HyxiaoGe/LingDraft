"""实验 03：middleware 钩子的执行顺序；SummarizationMiddleware 和 ContextEditingMiddleware 挂到灵稿的对话上会怎样。

在进程里跑灵稿的运行器（假模型、假网关，读页正文放大到 6000 字），图换成“我们的 4 个 middleware + 要试的那个”，
其余照 agent.build_agent。

- order：在我们的 4 个前后各插一个只打日志的 middleware，跑一轮“搜 → 读 → 答”，看各个钩子按什么顺序跑。
- baseline：不压缩，连说 4 轮。看每次发给模型的消息里，开头和之前某次调用一样的有多少（DeepSeek 的缓存按前缀命中）。
- summarize：加 SummarizationMiddleware（消息到 14 条就压，留最近 6 条），同样 4 轮。看状态、页面、写摘要的模型拿到了什么。
- summarize-cut：留得很少（2 条），一轮里就压（只压一次），这一轮用户的话也被压掉。
- context-edit：加 ContextEditingMiddleware（估算超过 3000 token 就把旧的工具结果换成 [cleared]，留最近 2 个），同样 4 轮。
- cache <sqlite>：真实对话里每次调用命中缓存多少；最长的那个对话压一次，送去写摘要的还剩多少（只读，传检查点库的一份拷贝）。

用法（在 backend 下）：
  PYTHONPATH=. LINGDRAFT_FAKE=1 uv run python "../docs/LangGraph研究/实验/03-summarization/exp.py" order baseline summarize summarize-cut context-edit
"""

import dataclasses
import sys
import tempfile
import uuid
from pathlib import Path

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ClearToolUsesEdit, ContextEditingMiddleware, SummarizationMiddleware
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages.utils import count_tokens_approximately

from app.api.research_runner import ResearchRunner
from app.api.runner import LocalRunRegistry
from app.graph.research import agent
from app.graph.research.graph import memory_checkpointer
from app.graph.research.state import ResearchState
from app.research_llm import FakeResearchPlanner, FakeToolModel, fake_gateway_client

TMP = Path(tempfile.mkdtemp(prefix="lg-sum-"))
TID = "t"
QUESTIONS = ["杭州有什么好玩的", "西湖边上有什么好吃的", "灵隐寺要门票吗", "住哪里方便"]


def tokens(messages) -> int:
    return count_tokens_approximately(messages)


# ---- 搭图、跑一轮 ----

def big_gateway():
    gw = fake_gateway_client()
    _read = gw.read

    def read(url, **kw):
        r = _read(url, **kw)
        return dataclasses.replace(r, content=(r.content * 200)[:6000]) if r.ok else r

    gw.read = read
    return gw


class FreshIds(FakeToolModel):
    """假模型的消息 id、工具调用 id 是按消息条数编的。压缩以后消息变少，新 id 会和留下来的旧消息撞上，add_messages 按 id 覆盖、
    不追加，图就一直绕圈（压缩实验第一次就是这样卡死的）。真模型的 id 是随机的，这里也加个随机后缀。"""

    def _message(self, messages):
        m = super()._message(messages)
        sfx = uuid.uuid4().hex[:8]
        return m.model_copy(update={"id": f"{m.id}-{sfx}",
                                    "tool_calls": [{**tc, "id": f"{tc['id']}-{sfx}"} for tc in m.tool_calls],
                                    "invalid_tool_calls": [{**tc, "id": f"{tc['id']}-{sfx}"} for tc in m.invalid_tool_calls]})


class Recorder(AgentMiddleware):
    """放在最里面：记下每次真正发给模型的消息（外面的 middleware 改完以后的）。"""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[list] = []

    def wrap_model_call(self, request, handler):
        self.calls.append(list(request.messages))
        return handler(request)


class Chat:
    """像页面那样一句一句说（同 tests/test_research.py 的 Chat），图换成加了 extra 的。"""

    def __init__(self, extra=(), first=()) -> None:
        self.planner = FakeResearchPlanner(big_gateway())
        self.planner._model = FreshIds()  # 建图之前换，同 tests/research_helpers.py
        saver = memory_checkpointer()
        self.r = ResearchRunner(planner=self.planner, checkpointer=saver, data_dir=TMP / "data")
        self.rec = Recorder()
        self.r.graph = create_agent(  # 照 agent.build_agent
            model=self.planner.agent_model(), tools=self.planner.tools(), state_schema=ResearchState,
            context_schema=agent.ChatContext, checkpointer=saver, name="research",
            middleware=[*first, agent.TurnMiddleware(self.planner), agent.ContentGuard(self.planner), agent.ToolProgressMiddleware(),
                        agent.InvalidToolCallRetryMiddleware(agent.INVALID_TOOL_CALL_RETRIES), *extra, self.rec])
        self.r.runs = LocalRunRegistry()

    def say(self, text: str) -> list[dict]:
        self.r.accept(TID, text)
        key = self.r._run_key(TID)
        self.r.runs.admit(key)
        self.r.runs.started(key)
        try:
            evs = list(self.r._graph_events(TID, None))
        finally:
            self.r.runs.release(key)
        return evs + list(self.r._after_run(TID))

    def view(self) -> dict:
        return self.r.state_view(TID)

    def messages(self) -> list:
        return self.r.graph.get_state({"configurable": {"thread_id": TID}}).values["messages"]


def show_view(c: Chat) -> None:
    v = c.view()
    print(f"  页面：{len(v['turns'])} 轮，来源 {len(v['sources'])} 条")
    for t in v["turns"]:
        kinds = [i["type"] if i["type"] != "step" else i["kind"] for i in t["items"]]
        print(f"    用户：{(t['user'] or '')[:24]!r:28} 条目 {kinds}  记录 {'有' if t['record'] else '无'}")


def show_messages(c: Chat) -> None:
    ms = c.messages()
    print(f"  状态里的消息 {len(ms)} 条：" + " ".join(
        "摘要" if m.additional_kwargs.get("lc_source") == "summarization" else m.type for m in ms))


# ---- 缓存：每次调用开头和之前哪次一样 ----

def same(a, b) -> bool:
    return a.type == b.type and a.content == b.content and getattr(a, "tool_calls", None) == getattr(b, "tool_calls", None)


def prefix(a: list, b: list) -> int:
    n = 0
    for x, y in zip(a, b):
        if not same(x, y):
            break
        n += 1
    return n


def key(m) -> tuple:
    return m.type, str(m.content), str(getattr(m, "tool_calls", None))


def cache_report(calls: list[list]) -> None:
    """DeepSeek 的缓存按前缀命中：每次调用从头数，和之前某次调用一样的那段能命中，之后的都算没命中。
    没命中的再分两种：之前的调用里发过、只是开头对不上了的（本来能省的），和第一次发的（省不了）。都按估算 token。
    上一次调用的回复会作为这一次的下一条消息出现，真实数据里它也能命中，所以整份对上上一次时再多算一条。"""
    total = resent = new = 0
    seen: set = set()
    for i, c in enumerate(calls):
        best = max((prefix(c, p) for p in calls[:i]), default=0)
        if i and best == len(calls[i - 1]) and len(c) > best:
            best += 1
        total += tokens(c)
        for m in c[best:]:
            if key(m) in seen:
                resent += tokens([m])
            else:
                new += tokens([m])
        seen |= {key(m) for m in c}
    print(f"  调模型 {len(calls)} 次，发出去共约 {total} token。没命中：之前发过、开头对不上的约 {resent}，第一次发的约 {new}")


# ---- 各个实验 ----

class Trace(AgentMiddleware):
    """只打日志：每个钩子进来时记一笔，wrap 的两个记进和出。"""

    def __init__(self, tag: str, log: list) -> None:
        super().__init__()
        self.tag, self.log = tag, log

    @property
    def name(self) -> str:
        return f"Trace{self.tag}"

    def before_agent(self, state, runtime):
        self.log.append(f"{self.tag}.before_agent")

    def before_model(self, state, runtime):
        self.log.append(f"{self.tag}.before_model")

    def after_model(self, state, runtime):
        self.log.append(f"{self.tag}.after_model")

    def after_agent(self, state, runtime):
        self.log.append(f"{self.tag}.after_agent")

    def wrap_model_call(self, request, handler):
        self.log.append(f"{self.tag}.wrap_model_call 进")
        r = handler(request)
        self.log.append(f"{self.tag}.wrap_model_call 出")
        return r

    def wrap_tool_call(self, request, handler):
        self.log.append(f"{self.tag}.wrap_tool_call 进（{request.tool_call['name']}）")
        r = handler(request)
        self.log.append(f"{self.tag}.wrap_tool_call 出")
        return r


def order():
    log: list[str] = []
    c = Chat(first=[Trace("A", log)], extra=[Trace("Z", log)])
    nodes = list(c.r.graph.get_graph().nodes)
    print("图里的节点：" + " → ".join(n for n in nodes if n not in ("__start__", "__end__")))
    # 节点跑完时运行器发 node 事件（只发改了 state 的）；这里直接看 updates，每个节点跑完记一笔
    c.r.accept(TID, "杭州有什么好玩的")
    for mode, chunk in c.r.graph.stream(None, {"configurable": {"thread_id": TID}}, context=agent.ChatContext(images=c.r.images, check_stop=lambda: None),
                                        stream_mode=["updates"], subgraphs=False):
        for node in chunk:
            log.append(f"  —— 节点 {node} 跑完")
    print("\n".join(log))


def run_four(c: Chat) -> list[dict]:
    evs = []
    for q in QUESTIONS:
        evs += c.say(q)
    return evs


def baseline():
    c = Chat()
    run_four(c)
    show_messages(c)
    show_view(c)
    cache_report(c.rec.calls)


class RecordingSummarizer(FakeListChatModel):
    """写摘要的假模型：记下拿到的提示词。"""

    prompts: list[str] = []

    def _call(self, messages, stop=None, run_manager=None, **kwargs):
        self.prompts.append(messages[-1].content)
        return super()._call(messages, stop, run_manager, **kwargs)


class WatchedSummarization(SummarizationMiddleware):
    """看清楚每次压了哪些、送去写摘要的又是哪些（默认只送最后 4000 token）。"""

    @property
    def name(self) -> str:
        return "SummarizationMiddleware"

    def _trim_messages_for_summary(self, messages):
        out = super()._trim_messages_for_summary(messages)
        print(f"  [压缩] 要压掉 {len(messages)} 条（约 {tokens(messages)} token），送去写摘要的只有最后 {len(out)} 条（约 {tokens(out)} token）："
              + " ".join(m.type for m in out))
        return out


class OnceSummarization(WatchedSummarization):
    """只压一次。留得太少时，留下的消息里没有这一轮做过的搜索，假模型就再搜一遍，消息一多又压，压完又搜，一直绕下去
    （第一次跑一轮压了 2500 次）。真模型会看摘要，不一定这样，但这一轮做到哪儿确实丢了。只压一次，看压完以后什么样。"""

    done: bool = False

    def _should_summarize(self, messages, total_tokens):
        if self.done or not super()._should_summarize(messages, total_tokens):
            return False
        self.done = True
        return True


def summarize(keep=6, questions=QUESTIONS):
    summarizer = RecordingSummarizer(responses=["SUMMARY-TEXT：用户在问杭州旅游，已查过景点。"] * 10)
    cls = WatchedSummarization if keep > 2 else OnceSummarization
    mw = cls(model=FakeToolModel(), trigger=("messages", 14 if keep > 2 else 4), keep=("messages", keep), summarizer=summarizer)
    c = Chat(extra=[mw])
    evs = []
    for q in questions:
        evs += c.say(q)
    nodes = [e["data"]["node"] for e in evs if e["event"] == "node"]
    print(f"  压缩节点发了 node 事件 {nodes.count('SummarizationMiddleware.before_model')} 次；"
          f"摘要的字混进正文流：{'是' if any('SUMMARY-TEXT' in str(e['data']) for e in evs if e['event'] == 'delta') else '否'}")
    show_messages(c)
    show_view(c)
    if keep > 2:
        cache_report(c.rec.calls)
    else:
        searches = [sum(1 for m in call if m.type == "ai" for tc in m.tool_calls if tc["name"] == "search_web") for call in c.rec.calls]
        print(f"  第一轮每次调模型时，它看到自己已经搜过几次：{searches[:6]}；第一轮的记录：{c.view()['turns'][0]['record']}")


def summarize_cut():
    summarize(keep=2, questions=QUESTIONS[:2])


def context_edit():
    mw = ContextEditingMiddleware(edits=[ClearToolUsesEdit(trigger=3000, keep=2)])
    c = Chat(extra=[mw])
    run_four(c)
    seen = [sum(1 for m in call if m.type == "tool" and m.content == "[cleared]") for call in c.rec.calls]
    print(f"  每次调用里被换成 [cleared] 的工具结果：{seen}")
    show_messages(c)
    print(f"  状态里还是 [cleared] 的：{sum(1 for m in c.messages() if m.type == 'tool' and m.content == '[cleared]')} 条")
    show_view(c)
    cache_report(c.rec.calls)


def cache(path: str):
    from app.graph.research.graph import open_sqlite_checkpointer, thread_ids

    saver = open_sqlite_checkpointer(path)
    g = agent.build_agent(FakeResearchPlanner(), saver)
    first_turn = later_first = later = total = avoid_lo = avoid_hi = 0
    for tid in thread_ids(saver):
        turns = []  # 每轮每次调模型：(输入, 命中缓存, 输出)
        for m in g.get_state({"configurable": {"thread_id": tid}}).values.get("messages", []):
            if m.type == "human":
                turns.append([])
            elif m.type == "ai" and m.usage_metadata and turns:
                u = m.usage_metadata
                turns[-1].append((u["input_tokens"], (u.get("input_token_details") or {}).get("cache_read", 0), u["output_tokens"]))
        turns = [t for t in turns if t]
        for k, t in enumerate(turns):
            total += sum(i for i, _, _ in t)
            later += sum(i - c for i, c, _ in t[1:])
            i0, c0, _ = t[0]
            if k == 0:
                first_turn += i0 - c0
                continue
            later_first += i0 - c0
            # 前缀不变的话，上一轮最后一次调用的输入（再加它的回复）都该命中；思考是不是也发回去不确定，所以给个范围
            pin, _, pout = turns[k - 1][-1]
            avoid_lo += max(0, min(pin, i0) - c0)
            avoid_hi += max(0, min(pin + pout, i0) - c0)
    print(f"  真实对话：输入共 {total} token，没命中 {first_turn + later_first + later}。其中第一轮第一次调用 {first_turn}，"
          f"之后每轮第一次调用 {later_first}（上一轮发过、本该命中的 {avoid_lo}～{avoid_hi}），一轮里后面的调用 {later}")
    # 最长的那个对话：按默认留 20 条、留 6 条各压一次，送去写摘要的还剩多少（默认只送最后 4000 token）
    longest = max(thread_ids(saver), key=lambda t: len(g.get_state({"configurable": {"thread_id": t}}).values.get("messages", [])))
    ms = g.get_state({"configurable": {"thread_id": longest}}).values["messages"]
    for keep in (20, 6):
        mw = SummarizationMiddleware(model=FakeToolModel(), trigger=("messages", 1), keep=("messages", keep))
        cut = mw._determine_cutoff_index(ms)
        part = ms[:cut]
        sent = mw._trim_messages_for_summary(part)
        print(f"  最长的对话（{len(ms)} 条），留 {keep} 条：要压掉 {len(part)} 条约 {tokens(part)} token（其中用户的话 {sum(m.type == 'human' for m in part)} 句），"
              f"送去写摘要的 {len(sent)} 条约 {tokens(sent)} token（用户的话 {sum(m.type == 'human' for m in sent)} 句）；"
              f"摘要：{mw._create_summary(part)[:60]!r}")


if __name__ == "__main__":
    args = sys.argv[1:]
    while args:
        w = args.pop(0)
        print(f"\n===== {w} =====")
        if w == "cache":
            cache(args.pop(0))
        else:
            {"order": order, "baseline": baseline, "summarize": summarize, "summarize-cut": summarize_cut,
             "context-edit": context_edit}[w]()
