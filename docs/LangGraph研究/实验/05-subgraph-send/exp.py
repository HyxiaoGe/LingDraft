"""实验 05：Send 并行分发、子图、多 agent。都用假模型、假网关。

- send：纯 StateGraph 的 map-reduce。Send 出去的节点拿到的是什么、是不是并行跑、结果按什么顺序合并、没有 reducer 会怎样、max_concurrency、Send 自带的超时。
- send-fail：三个分支里一个失败：失败时等不等还在跑的那个、哪些分支的结果存下来了、接着跑时哪些重跑。
- agent-parallel：灵稿的运行器，模型一次回复同时搜两个（真实数据里常见）。工具是不是并行跑、进度事件的顺序、页面上什么样。
- subgraph：子图当节点。checkpointer 取 None 和 True 的区别；共享一个带 reducer 的字段会怎样；检查点的命名空间；子图里 interrupt 再恢复，哪些节点重跑。
- subagent：灵稿的运行器，主 agent 有个工具，工具里调另一个 create_agent（研究员）。子 agent 的正文会不会流进主回答、存在哪、页面上什么样、停止能不能停下来。
- handoff：两个 create_agent 当父图的两个节点，一个的工具返回 Command(graph=Command.PARENT, goto=…) 把对话交给另一个。下一句话从哪个开始。

用法（在 backend 下）：
  PYTHONPATH=. LINGDRAFT_FAKE=1 uv run python "../docs/LangGraph研究/实验/05-subgraph-send/exp.py" send send-fail agent-parallel subgraph subagent handoff
"""

import operator
import sys
import tempfile
import threading
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Annotated, TypedDict

import httpx
from langchain.agents import AgentState, create_agent
from langchain.tools import ToolRuntime
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt

from app.api.research_runner import ResearchRunner
from app.api.runner import LocalRunRegistry
from app.graph.research import agent
from app.graph.research.graph import memory_checkpointer
from app.graph.research.state import ResearchState
from app.research_llm import FakeResearchPlanner, FakeToolModel, fake_gateway_client

TMP = Path(tempfile.mkdtemp(prefix="lg-send-"))


def sfx() -> str:
    return uuid.uuid4().hex[:6]


def ns_counts(saver, tid: str) -> dict:
    """这个对话的检查点按命名空间数一数；命名空间里的任务 id 换成 …。"""
    c = Counter()
    for t in saver.list({"configurable": {"thread_id": tid}}):
        ns = t.config["configurable"].get("checkpoint_ns", "")
        c["|".join(p.split(":")[0] + (":…" if ":" in p else "") for p in ns.split("|")) if ns else "(父图)"] += 1
    return dict(c)


# ---------------- send ----------------

class MapState(TypedDict):
    items: list[float]
    results: Annotated[list, operator.add]


def work(arg: dict) -> dict:
    """Send 过来的节点：参数是 Send 带的那个 dict，不是整个状态。"""
    time.sleep(arg["delay"])
    return {"results": [{"i": arg["i"], "thread": threading.current_thread().name, "got": sorted(arg)}]}


def map_graph(state_cls=MapState, node=work, **send_kw):
    def fan_out(state):
        return [Send("work", {"i": i, "delay": d}, **send_kw) for i, d in enumerate(state["items"])]

    g = StateGraph(state_cls)
    g.add_node("work", node)
    g.add_conditional_edges(START, fan_out, ["work"])
    g.add_edge("work", END)
    return g


def send():
    saver = InMemorySaver()
    graph = map_graph().compile(checkpointer=saver)
    cfg = {"configurable": {"thread_id": "S"}}
    t0 = time.monotonic()
    out = graph.invoke({"items": [0.3, 0.1, 0.2], "results": []}, cfg)
    print(f"  三个分支各睡 0.3/0.1/0.2 秒，一共用了 {time.monotonic() - t0:.2f} 秒")
    print("  结果的顺序（i）：", [r["i"] for r in out["results"]], "；跑在哪个线程：", [r["thread"] for r in out["results"]])
    print("  节点拿到的参数里有哪些键：", out["results"][0]["got"])
    print("  检查点：", ns_counts(saver, "S"), "；最后一个检查点的步数：", graph.get_state(cfg).metadata["step"])

    t0 = time.monotonic()
    map_graph().compile().invoke({"items": [0.3, 0.1, 0.2], "results": []}, {"max_concurrency": 1})
    print(f"  max_concurrency=1：{time.monotonic() - t0:.2f} 秒")

    class NoReducer(TypedDict):
        items: list[float]
        results: list

    try:
        map_graph(NoReducer).compile().invoke({"items": [0.01, 0.01], "results": []})
    except Exception as e:  # noqa: BLE001
        print("  results 没有 reducer：", type(e).__name__, str(e).splitlines()[0][:110])

    try:
        t0 = time.monotonic()
        map_graph(timeout=0.15).compile().invoke({"items": [0.05, 0.4], "results": []})
    except Exception as e:  # noqa: BLE001
        print(f"  Send(timeout=0.15)，一个分支要 0.4 秒：{time.monotonic() - t0:.2f} 秒后", type(e).__name__, str(e)[:90])


# ---------------- send-fail ----------------

def send_fail():
    calls, done_at = Counter(), {}
    fail = {"on": True}
    t0 = time.monotonic()

    def flaky(arg):
        calls[arg["i"]] += 1
        time.sleep(arg["delay"])
        if arg["i"] == 1 and fail["on"]:
            raise RuntimeError("第 1 个分支出错")
        done_at[arg["i"]] = round(time.monotonic() - t0, 2)
        return {"results": [arg["i"]]}

    saver = InMemorySaver()
    graph = map_graph(node=flaky).compile(checkpointer=saver)
    cfg = {"configurable": {"thread_id": "F"}}
    try:
        graph.invoke({"items": [0.05, 0.1, 0.4], "results": []}, cfg)
    except RuntimeError as e:
        print(f"  {time.monotonic() - t0:.2f} 秒后抛出：{e}；各分支做完的时刻：{done_at}")
    snap = graph.get_state(cfg)
    print("  状态里的 results：", snap.values.get("results"), "；next：", snap.next)
    for t in snap.tasks:
        print(f"    任务 {t.name}{list(t.path)[:2]}：error={type(t.error).__name__ if t.error else None}，result={t.result}")
    writes = saver.get_tuple(cfg).pending_writes
    print("  存下来的 pending writes：", [(w[1], w[2]) for w in writes])
    fail["on"] = False
    out = graph.invoke(None, cfg)
    print("  接着跑：results =", out["results"], "；各分支一共跑了几次：", dict(calls))


# ---------------- 灵稿的运行器 ----------------

class SlowTransport(httpx.BaseTransport):
    """搜索慢一点，记下每次搜索在哪个线程、什么时候开始结束。"""

    def __init__(self, inner, delay: float, log: list) -> None:
        self.inner, self.delay, self.log = inner, delay, log

    def handle_request(self, request):
        t0 = time.monotonic()
        if request.url.path == "/search/search":
            time.sleep(self.delay)
            self.log.append((threading.current_thread().name, round(t0, 2), round(time.monotonic(), 2)))
        return self.inner.handle_request(request)


class Chat:
    """像页面那样一句一句说（同 04 的 Chat），记下运行器发出的事件。"""

    def __init__(self, model, tools=(), slow: float = 0.0) -> None:
        self.search_log: list = []
        gw = fake_gateway_client()
        if slow:
            gw._client._transport = SlowTransport(gw._client._transport, slow, self.search_log)
        self.planner = FakeResearchPlanner(gw)
        self.planner._model = model
        self.saver = memory_checkpointer()
        self.r = ResearchRunner(planner=self.planner, checkpointer=self.saver, data_dir=TMP / "data")
        self.r.graph = create_agent(  # 照 agent.build_agent，多了要试的工具
            model=self.planner.agent_model(), tools=[*self.planner.tools(), *tools], state_schema=ResearchState,
            context_schema=agent.ChatContext, checkpointer=self.saver, name="research",
            middleware=[agent.TurnMiddleware(self.planner), agent.ContentGuard(self.planner), agent.ToolProgressMiddleware(),
                        agent.InvalidToolCallRetryMiddleware(agent.INVALID_TOOL_CALL_RETRIES)])
        self.r.runs = LocalRunRegistry()
        self.events: list[dict] = []

    def say(self, tid: str, text: str) -> dict:
        self.r.accept(tid, text)
        key = self.r._run_key(tid)
        self.r.runs.admit(key)
        self.r.runs.started(key)
        try:
            self.events = list(self.r._graph_events(tid, None))
        finally:
            self.r.runs.release(key)
        list(self.r._after_run(tid))
        return self.r.state_view(tid)


def kinds(turn: dict) -> list[str]:
    return [i["type"] if i["type"] != "step" else i["kind"] for i in turn["items"]]


def streamed_text(events: list[dict]) -> str:
    return "".join(e["data"]["text"] for e in events if e["event"] == "delta" and e["data"]["kind"] == "text")


class ParallelModel(FakeToolModel):
    """第一次回复同时发两个搜索（真实数据里多半是这样），然后读一页、回答。"""

    def _decide(self, messages):
        i = next(i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], HumanMessage))
        turn = messages[i + 1:]
        if not any(m.type == "ai" and m.tool_calls for m in turn):
            q = str(messages[i].content)[:10]
            return "", [{"name": "search_web", "args": {"query": f"{q} 甲", "count": 3}, "id": f"call_s1_{sfx()}"},
                        {"name": "search_web", "args": {"query": f"{q} 乙", "count": 3}, "id": f"call_s2_{sfx()}"}]
        content, calls = super()._decide(messages)
        return content, [{**c, "id": f"{c['id']}-{sfx()}"} for c in calls]

    def _message(self, messages):
        m = super()._message(messages)
        return m.model_copy(update={"id": f"{m.id}-{sfx()}"})


def agent_parallel():
    c = Chat(ParallelModel(), slow=0.5)
    t0 = time.monotonic()
    view = c.say("P", "杭州 美食")
    print(f"  这一轮用了 {time.monotonic() - t0:.2f} 秒（两次搜索各 0.5 秒）")
    print("  两次搜索：", [(name, round(a - t0, 2), round(b - t0, 2)) for name, a, b in c.search_log])
    steps = [(e["data"]["step"]["kind"], e["data"]["step"]["text"][-1:], "开始" if e["data"]["step"].get("pending") else "做完")
             for e in c.events if e["event"] == "step"]
    print("  进度事件（种类、搜索词最后一个字、开始还是做完）：", steps)
    print("  页面这一轮：", kinds(view["turns"][-1]), "；来源", len(view["sources"]), "条")


# ---------------- subgraph ----------------

class Q(TypedDict):
    question: str
    answer: str


class SubQ(TypedDict):
    question: str
    answer: str
    seen: Annotated[list, operator.add]  # 只有子图有


def remember_q(state: SubQ):
    return {"seen": [state["question"]]}


def answer_q(state: SubQ):
    return {"answer": f"这个子图见过 {len(state['seen'])} 个问题：{state['seen']}"}


def sub_q(checkpointer):
    g = StateGraph(SubQ)
    g.add_node("remember", remember_q)
    g.add_node("answer", answer_q)
    g.add_edge(START, "remember")
    g.add_edge("remember", "answer")
    g.add_edge("answer", END)
    return g.compile(checkpointer=checkpointer)


class Notes(TypedDict):
    notes: Annotated[list, operator.add]


def subgraph():
    for mode in (None, True):
        saver = InMemorySaver()
        g = StateGraph(Q)
        g.add_node("sub", sub_q(mode))
        g.add_edge(START, "sub")
        g.add_edge("sub", END)
        parent = g.compile(checkpointer=saver)
        cfg = {"configurable": {"thread_id": "G"}}
        a1 = parent.invoke({"question": "第一问"}, cfg)["answer"]
        a2 = parent.invoke({"question": "第二问"}, cfg)["answer"]
        print(f"  子图 checkpointer={mode}：第一次「{a1}」；第二次「{a2}」")
        print("    检查点：", ns_counts(saver, "G"))

    # 父图和子图共用一个 operator.add 的字段
    def add_note(state):
        return {"notes": [f"note{len(state['notes']) + 1}"]}

    sg = StateGraph(Notes)
    sg.add_node("add", add_note)
    sg.add_edge(START, "add")
    sg.add_edge("add", END)
    g = StateGraph(Notes)
    g.add_node("sub", sg.compile())
    g.add_edge(START, "sub")
    g.add_edge("sub", END)
    p = g.compile(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "N"}}
    p.invoke({"notes": []}, cfg)
    out = p.invoke({"notes": []}, cfg)
    print("  共用 operator.add 字段，说两次以后父图的 notes：", out["notes"])

    # 子图里 interrupt
    runs = Counter()

    class S(TypedDict):
        x: str

    def counted(name, fn=None):
        def node(state):
            runs[name] += 1
            return fn(state) if fn else {}
        return node

    def ask(state):
        runs["ask"] += 1
        reply = interrupt("要继续吗？")
        return {"x": f"用户说：{reply}"}

    inner = StateGraph(S)
    inner.add_node("prep", counted("prep"))
    inner.add_node("ask", ask)
    inner.add_node("finish", counted("finish"))
    inner.add_edge(START, "prep")
    inner.add_edge("prep", "ask")
    inner.add_edge("ask", "finish")
    inner.add_edge("finish", END)
    inner_c = inner.compile()

    def wrapper(state):
        runs["wrapper"] += 1
        return inner_c.invoke(state)

    for how, node in (("子图直接当节点", inner_c), ("在函数节点里调子图", wrapper)):
        runs.clear()
        g = StateGraph(S)
        g.add_node("before", counted("before"))
        g.add_node("sub", node)
        g.add_edge(START, "before")
        g.add_edge("before", "sub")
        g.add_edge("sub", END)
        saver = InMemorySaver()
        p = g.compile(checkpointer=saver)
        cfg = {"configurable": {"thread_id": "I"}}
        chunks = list(p.stream({"x": ""}, cfg, stream_mode="updates", subgraphs=True))
        intr = [ns for ns, ch in chunks if "__interrupt__" in ch]
        snap = p.get_state(cfg, subgraphs=True)
        sub_state = snap.tasks[0].state
        print(f"  [{how}] 停下时：父图 next={snap.next}；子图 next={sub_state.next if sub_state else None}；interrupt 从哪层冒出来：{intr}")
        out = p.invoke(Command(resume="好"), cfg)
        print(f"    恢复后：{out['x']}；各节点跑了几次：{dict(runs)}")


# ---------------- subagent ----------------

class SubModel(FakeToolModel):
    """研究员：照常搜、读、答；每次回复的 id 带随机后缀。"""

    def _decide(self, messages):
        content, calls = super()._decide(messages)
        return (f"研究员：{content}" if content else ""), [{**c, "id": f"{c['id']}-{sfx()}"} for c in calls]

    def _message(self, messages):
        m = super()._message(messages)
        return m.model_copy(update={"id": f"{m.id}-{sfx()}"})


class BossModel(FakeToolModel):
    """主 agent：先把问题交给研究员，拿到结果再回答。"""

    def _decide(self, messages):
        i = next(i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], HumanMessage))
        turn = messages[i + 1:]
        if not any(m.type == "tool" and m.name == "ask_researcher" for m in turn):
            return "", [{"name": "ask_researcher", "args": {"question": str(messages[i].content)}, "id": f"call_ask_{sfx()}"}]
        return "主 agent：按研究员查到的，答案是可以用。", []

    def _message(self, messages):
        m = super()._message(messages)
        return m.model_copy(update={"id": f"{m.id}-{sfx()}"})


STOP = {"in_sub": False, "now": False}


def make_researcher(planner, with_progress: bool):
    return create_agent(model=SubModel(), tools=planner.tools(), name="researcher",
                        middleware=[agent.ToolProgressMiddleware()] if with_progress else [])


def subagent():
    holder = {}

    @tool("ask_researcher", description="Hand a research question to the researcher agent and get its findings.")
    def ask_researcher(question: str, runtime: ToolRuntime) -> str:
        if STOP["in_sub"]:
            STOP["now"] = True  # 研究员一开始调模型就该停
        out = holder["researcher"].invoke({"messages": [HumanMessage(question)]})
        return out["messages"][-1].content

    for with_progress in (False, True):
        c = Chat(BossModel(), tools=[ask_researcher])
        holder["researcher"] = make_researcher(c.planner, with_progress)
        view = c.say("A", "灵稿能不能用 LangGraph")
        text = streamed_text(c.events)
        print(f"  [研究员{'带' if with_progress else '不带'}进度 middleware]")
        print("    流到页面的正文：", text)
        print("    进度事件：", [e["data"]["step"].get("kind") for e in c.events if e["event"] == "step"])
        msgs = c.r.graph.get_state({"configurable": {"thread_id": "A"}}).values["messages"]
        print("    主 agent 状态里的消息：", [m.type + (f":{m.name}" if m.type == "tool" else "") for m in msgs])
        print("    页面这一轮：", kinds(view["turns"][-1]), "；来源", len(view["sources"]), "条；最后一项：", view["turns"][-1]["items"][-1].get("text", "")[:40])
        print("    检查点：", ns_counts(c.saver, "A"))

    # 打开 subgraphs=True 直接看图的流：研究员的片段和进度从哪层来、langgraph_node 是什么
    c.r.accept("C", "灵稿能不能用 LangGraph")
    ctx = agent.ChatContext(images=c.r.images, check_stop=lambda: None)
    seen = Counter()
    for ns, mode, chunk in c.r.graph.stream(None, c.r._cfg("C"), context=ctx, stream_mode=["messages", "custom"], subgraphs=True):
        layer = "子 agent" if ns else "主 agent"
        if mode == "messages" and chunk[0].content:
            seen[(layer, "正文片段", chunk[1].get("langgraph_node"))] += 1
        elif mode == "custom" and chunk.get("type") == "step":
            seen[(layer, "进度", chunk["step"]["kind"])] += 1
    print("  打开 subgraphs=True 以后（哪层、什么、langgraph_node 或种类 → 几次）：", dict(seen))

    c = Chat(BossModel(), tools=[ask_researcher])
    holder["researcher"] = make_researcher(c.planner, False)
    c.r._stop_requested = lambda tid: STOP["now"]
    STOP["in_sub"] = True
    try:
        view = c.say("B", "灵稿能不能用 LangGraph")
        print("  研究员干活时点停止：", [e["event"] for e in c.events][-3:], "；页面这一轮：", kinds(view["turns"][-1]))
    except Exception as e:  # noqa: BLE001
        print("  研究员干活时点停止：抛到外面", type(e).__name__, str(e)[:100])
    finally:
        STOP.update(in_sub=False, now=False)


# ---------------- handoff ----------------

class TriageModel(FakeToolModel):
    def _decide(self, messages):
        if not any(m.type == "tool" for m in messages[-1:]):
            return "", [{"name": "transfer_to_expert", "args": {}, "id": f"call_transfer_{sfx()}"}]
        return "前台：已转接。", []

    def _message(self, messages):
        m = super()._message(messages)
        return m.model_copy(update={"id": f"{m.id}-{sfx()}"})


class ExpertModel(FakeToolModel):
    def _decide(self, messages):
        last_human = next(m for m in reversed(messages) if isinstance(m, HumanMessage))
        return f"专家：回答「{last_human.content}」", []

    def _message(self, messages):
        m = super()._message(messages)
        return m.model_copy(update={"id": f"{m.id}-{sfx()}"})


class Team(AgentState):
    active: str


def handoff():
    @tool("transfer_to_expert", description="Transfer the conversation to the expert agent.")
    def transfer_to_expert(runtime: ToolRuntime) -> Command:
        call = runtime.state["messages"][-1]  # 发起这次调用的那条模型消息
        done = ToolMessage("Transferred to expert.", tool_call_id=runtime.tool_call_id, name="transfer_to_expert")
        return Command(graph=Command.PARENT, goto="expert", update={"messages": [call, done], "active": "expert"})

    triage = create_agent(model=TriageModel(), tools=[transfer_to_expert], name="triage")
    expert = create_agent(model=ExpertModel(), tools=[], name="expert")

    def route(state: Team):
        return state.get("active") or "triage"

    for how, router in (("每句话都从前台开始", lambda s: "triage"), ("按上次在谁那里", route)):
        g = StateGraph(Team)
        g.add_node("triage", triage, destinations=("expert",))
        g.add_node("expert", expert)
        g.add_conditional_edges(START, router, ["triage", "expert"])
        g.add_edge("triage", END)
        g.add_edge("expert", END)
        saver = InMemorySaver()
        team = g.compile(checkpointer=saver)
        cfg = {"configurable": {"thread_id": "H"}}
        team.invoke({"messages": [HumanMessage("第一句")]}, cfg)
        out = team.invoke({"messages": [HumanMessage("第二句")]}, cfg)
        seq = [(m.type, (m.content or "")[:12] or ",".join(tc["name"] for tc in getattr(m, "tool_calls", []))) for m in out["messages"]]
        print(f"  [{how}] 两句话以后的消息：", seq)
        print("    检查点：", ns_counts(saver, "H"))


MODES = {"send": send, "send-fail": send_fail, "agent-parallel": agent_parallel, "subgraph": subgraph,
         "subagent": subagent, "handoff": handoff}

if __name__ == "__main__":
    for name in sys.argv[1:]:
        print(f"\n===== {name} =====")
        MODES[name]()
