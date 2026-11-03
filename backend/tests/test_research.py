"""对话 agent：多轮对话、流式事件、停止和重新生成、内容审核、检查点往返、来源。只用假模型（FakeToolModel 的关键字见它的说明）。"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.messages.tool import tool_call_chunk
from langgraph.checkpoint.memory import InMemorySaver

from app import aio, research_llm
from app.api import research_runner
from app.api.research_runner import NothingToRegenerate, NotIdle, ResearchRunner, StreamBuffer
from app.api.runner import RunConflict
from app.api.runner import LocalRunRegistry
from app.graph.research import history, prompts
from app.graph.research import agent as agent_mod
from app.graph.research.agent import CHINA_TZ, WEEKDAYS, system_prompt, turn_started
from app.graph.research.graph import make_serde, memory_checkpointer, open_sqlite_checkpointer
from app.graph.research.sources import cited, collect_sources, url_key
from app.graph.research.state import Turn
from app.model_guard import ModelUnavailable
from app.research_llm import FAKE_BLOCKED, THINKING_MS, FakeResearchPlanner, FakeToolModel, LiveResearchPlanner
from app.tools.gateway import GatewayAuthError, GatewayClient
from app.tools.research_tools import make_research_tools


class Chat:
    """像页面那样一句一句说：写进检查点 → 跑到这一轮结束。不经过排队和运行记录，等这一轮跑完拿到事件。
    运行占位用自己的一份：停止靠它传话（stop_on 在收到某个事件时点停止）。会画图的要给 tmp_path，图存在那里。"""

    def __init__(self, tmp_path: Path | None = None, planner: FakeResearchPlanner | None = None, saver=None, tid: str = "t1") -> None:
        self.planner = planner or FakeResearchPlanner()
        self.saver = saver or memory_checkpointer()
        self.r = ResearchRunner(planner=self.planner, checkpointer=self.saver, data_dir=tmp_path / "data" if tmp_path else None)
        self.r.runs = LocalRunRegistry()
        self.tid = tid
        self.events: list[dict] = []

    def say(self, text: str, stop_on=None) -> dict:
        self.r.accept(self.tid, text)
        return self.run(stop_on)

    def regenerate(self) -> dict:
        self.r.regenerate_input(self.tid)
        return self.run()

    def run(self, stop_on=None, stop_first: bool = False) -> dict:
        key = self.r._run_key(self.tid)
        self.r.runs.admit(key)
        if stop_first:
            self.r.stop(self.tid)
        self.r.runs.started(key)
        self.events = []

        def out(ev: dict) -> None:  # 在对话的事件循环里调：点停止和页面一样，马上取消这一轮
            self.events.append(ev)
            if stop_on and stop_on(ev):
                self.r.stop(self.tid)

        try:
            self.r.run_turn(self.tid, None, out)
        finally:
            self.r.runs.release(key)
        self.events += self.r.suggest(self.tid)  # 这一轮结束之后起推荐问题
        return self.view()

    def view(self) -> dict:
        return self.r.state_view(self.tid)

    def values(self) -> dict:
        return self.r.graph.get_state({"configurable": {"thread_id": self.tid}}).values


def _sqlite(path: Path):
    return aio.run(open_sqlite_checkpointer(path))


@pytest.fixture
def chat() -> Chat:
    return Chat()


def _kinds(turn: dict) -> list[str]:
    return [i["type"] if i["type"] != "step" else f"step:{i['kind']}" for i in turn["items"]]


def _turn_messages(chat: Chat) -> list:
    msgs = chat.values()["messages"]
    return msgs[history.last_human(msgs) + 1:]


def _answered(msgs: list) -> bool:
    """每个工具调用都有结果：下一次发给模型的历史才合法。"""
    calls = {tc["id"] for m in msgs if isinstance(m, AIMessage) for tc in m.tool_calls}
    return calls <= {m.tool_call_id for m in msgs if isinstance(m, ToolMessage)}


# ---- 一轮 ----


def test_first_turn_searches_reads_and_replies(chat):
    v = chat.say("查一下 AI 海报工具的局部修改")
    assert v["status"] == "idle" and v["title"] == "标题：查一下 AI 海" and v["error"] is None
    (turn,) = v["turns"]
    assert turn["user"] == "查一下 AI 海报工具的局部修改"
    # 思考、步骤、正文按发生的先后穿插
    assert _kinds(turn) == ["thinking", "step:search", "thinking", "step:read", "thinking", "text"]
    reply = turn["items"][-1]["text"]
    assert reply.startswith("结论：")
    rec = turn["record"]
    assert rec["outcome"] == "ok" and len(rec["suggestions"]) == 3 and rec["ended_at"]
    assert rec["usage"]["model_calls"] == 3 and rec["usage"]["cached_tokens"] == 180 and rec["usage"]["searches"] == 1 and rec["usage"]["reads"] == 1
    # 来源：搜到 3 条，读到第 1 条的正文；正文引用的两个 id 都在里面
    assert sorted(s["excerpt_kind"] for s in v["sources"].values()) == ["body", "summary", "summary"]
    assert len(cited(reply)) == 2 and set(cited(reply)) <= set(v["sources"])


def test_events_stream_title_steps_and_text(chat):
    chat.say("查一下局部修改")
    names = [e["event"] for e in chat.events]
    assert names.index("title") < names.index("step") and "node" in names
    steps = [e["data"]["step"] for e in chat.events if e["event"] == "step"]
    # 每个工具开始一次、完成一次，两次 id 一样
    assert [(s["kind"], s["pending"]) for s in steps] == [("search", True), ("search", False), ("read", True), ("read", False)]
    assert steps[0]["id"] == steps[1]["id"]
    deltas = [e["data"] for e in chat.events if e["event"] == "delta"]
    assert {d["kind"] for d in deltas} == {"thinking", "text"}
    # 实时片段的消息 id 和存下来的对得上：页面按 id 把片段叠在已存的 state 上
    stored = {i["id"] for i in chat.view()["turns"][0]["items"] if i["type"] in ("thinking", "text")}
    assert {d["id"] for d in deltas} == stored
    text = "".join(d["text"] for d in deltas if d["kind"] == "text")
    assert text == chat.view()["turns"][0]["items"][-1]["text"]


def test_finished_steps_carry_their_sources(chat):
    """一轮跑完才存检查点，执行中页面从事件拿来源：做完的那一步带着它得到的来源，和最终状态里的一致（读页之后的用正文开头）。"""
    v = chat.say("查一下局部修改")
    search, read = [e["data"] for e in chat.events if e["event"] == "step" and not e["data"]["step"]["pending"]]
    assert list(search["sources"]) == search["step"]["source_ids"] and len(search["sources"]) == 3
    assert {s["excerpt_kind"] for s in search["sources"].values()} == {"summary"}
    (sid,) = read["step"]["source_ids"]
    assert read["sources"] == {sid: v["sources"][sid]} and v["sources"][sid]["excerpt_kind"] == "body"
    assert read["sources"][sid]["title"] == search["sources"][sid]["title"]  # 标题沿用搜索时的
    for other in set(search["sources"]) - {sid}:
        assert search["sources"][other] == v["sources"][other]
    pending = [e["data"] for e in chat.events if e["event"] == "step" and e["data"]["step"]["pending"]]
    assert all("sources" not in p for p in pending)


def test_phases_say_what_the_gaps_wait_for(chat):
    """没有字和步骤的空档，页面靠阶段说明在等什么：每次调模型之前、这一轮结束后起推荐问题。起标题不占空档（和回答同时跑）。"""
    chat.say("查一下局部修改")
    assert [e["data"]["phase"] for e in chat.events if e["event"] == "phase"] == ["model", "model", "model", "suggest"]
    chat.say("直接答：再说一句")
    assert [e["data"]["phase"] for e in chat.events if e["event"] == "phase"] == ["model", "suggest"]


def test_simple_question_answers_directly(chat):
    v = chat.say("直接答：新加坡现在是什么季节？")
    (turn,) = v["turns"]
    assert _kinds(turn) == ["thinking", "text"] and v["sources"] == {} and v["status"] == "idle"
    assert len(turn["record"]["suggestions"]) == 3


def test_later_turns_continue_the_conversation(chat):
    chat.say("查一下局部修改")
    v = chat.say("直接答：为什么不稳定？")
    assert len(v["turns"]) == 2 and v["turns"][1]["user"] == "直接答：为什么不稳定？"
    assert len(v["sources"]) == 3  # 上一轮的来源还在
    assert [t for name, t in chat.planner.calls if name == "title"] == ["查一下局部修改"]  # 标题只在第一轮起
    assert [q for name, q in chat.planner.calls if name == "suggest"] == ["查一下局部修改", "直接答：为什么不稳定？"]


def test_no_reply_is_recorded(chat):
    v = chat.say("查一下 不回复")
    assert v["status"] == "idle" and v["turns"][0]["record"]["outcome"] == "no_reply" and v["turns"][0]["record"]["suggestions"] == []


def test_title_runs_alongside_the_answer():
    """起标题和回答同时跑：模型不等标题起好就开始。起好的标题发给页面一次、写进 state；之后几轮不再起。"""
    p = FakeResearchPlanner()
    model_called = threading.Event()

    class Seen:  # 放进假模型的闸门：模型一开始调用就记下来，不拦
        def wait(self, timeout):
            model_called.set()

    p._model.gates.append(Seen())
    orig, saw_model = p.title, []

    async def title(text):
        saw_model.append(await asyncio.to_thread(model_called.wait, 5))  # 回答要是等着标题，这里等不到模型
        return await orig(text)

    p.title = title
    chat = Chat(planner=p)
    v = chat.say("查一下局部修改")
    assert saw_model == [True] and v["title"] == "标题：查一下局部修改"
    assert [e["data"]["title"] for e in chat.events if e["event"] == "title"] == [v["title"]]
    assert chat.say("直接答：再说一句")["title"] == v["title"] and saw_model == [True]


def test_title_falls_back_to_the_words():
    """照 Fusion：起标题失败用原话，超过 30 个字截掉、加省略号。"""
    p = FakeResearchPlanner()
    p.fail_title = True
    text = "直接答：这是一个超过三十个字的问题，用来测试起标题失败时用原话、截断以后加上省略号"
    assert Chat(planner=p).say(text)["title"] == text[:30] + "..."


def test_long_title_is_cut():
    """模型没照要求写短：去掉首尾空白，超过 30 个字截掉。"""
    p = FakeResearchPlanner()
    async def title(text):
        return " " + "一个很长的标题" * 10 + "\n"

    p.title = title
    assert Chat(planner=p).say("直接答：一句话")["title"] == ("一个很长的标题" * 10)[:30]


def test_slow_title_falls_back_and_the_late_one_is_dropped(monkeypatch):
    """第一次调模型结束后再等一会儿还没起好：用原话，还在起的不要了（取消）；不会再发、再改。"""
    monkeypatch.setattr(agent_mod, "TITLE_WAIT", 0.2)
    p = FakeResearchPlanner()
    gate = threading.Event()
    orig, done = p.title, threading.Event()

    async def title(text):
        try:
            await asyncio.to_thread(gate.wait, 5)
            return await orig(text)
        finally:
            done.set()  # 取消了也走到这里

    p.title = title
    chat = Chat(planner=p)
    v = chat.say("直接答：一句话")
    gate.set()
    assert done.wait(5)
    time.sleep(0.1)
    assert v["title"] == "直接答：一句话" and chat.view()["title"] == "直接答：一句话"
    assert [e["data"]["title"] for e in chat.events if e["event"] == "title"] == ["直接答：一句话"]


def test_live_title_keeps_what_the_model_wrote():
    """标题原样用模型给的（只去掉首尾空白）：书名号、引号是标题的一部分，代码不删。"""
    p = LiveResearchPlanner.__new__(LiveResearchPlanner)
    p._titler = FakeListChatModel(responses=[" 《三体》里的“黑暗森林” \n"])
    assert aio.run(p.title("三体里的黑暗森林是什么意思")) == "《三体》里的“黑暗森林”"


def test_system_prompt_dates_are_beijing_with_weekday_anchors():
    """日期按北京时间（服务器在 UTC 也一样），带星期；今天是周六时，这周末就是今天和明天。"""
    sat = system_prompt(datetime(2026, 10, 31, 3, 0, tzinfo=UTC))
    assert "[Current date] 2026-10-31 (Saturday), Asia/Shanghai." in sat
    assert "Tomorrow is 2026-11-01 (Sunday)." in sat
    assert "This Saturday is 2026-10-31 (Saturday) and this Sunday is 2026-11-01 (Sunday)." in sat
    assert "Next Saturday is 2026-11-07 (Saturday) and next Sunday is 2026-11-08 (Sunday)." in sat
    # UTC 周六晚上 17 点，北京已经是周日凌晨：这周末还是 10-31、11-01
    sun = system_prompt(datetime(2026, 10, 31, 17, 0, tzinfo=UTC))
    assert "[Current date] 2026-11-01 (Sunday)" in sun and "This Saturday is 2026-10-31 (Saturday)" in sun


def test_dates_follow_when_this_turn_started():
    """取的是最新这句话那一轮的开始时间，不是上一轮的，也不是现在。"""
    from types import SimpleNamespace

    turns = [Turn(id="h0", started_at="2026-10-30T01:00:00+00:00"), Turn(id="h1", started_at="2026-10-31T01:02:00+00:00")]
    req = SimpleNamespace(messages=[HumanMessage(content="早", id="h0"), AIMessage(content="早"), HumanMessage(content="x", id="h1")],
                          state={"turns": turns})
    assert turn_started(req) == datetime(2026, 10, 31, 9, 2, tzinfo=CHINA_TZ)


def test_time_of_day_comes_from_the_tool_not_the_request():
    """几点钟不接在发给模型的消息上，之前发过的内容不变，前缀缓存才接得上；要用时模型调 get_current_time，
    取的是北京时间，页面上不显示这一步，也不进步骤流。"""
    seen: list[list] = []

    class Recording(FakeToolModel):
        def _message(self, messages):
            seen.append(messages)
            return super()._message(messages)

    p = FakeResearchPlanner()
    p._model = Recording()
    chat = Chat(planner=p)
    chat.say("直接答 第一句")
    v = chat.say("现在几点 直接答")
    assert [m.content for m in seen[-1] if isinstance(m, HumanMessage)] == ["直接答 第一句", "现在几点 直接答"]
    result = next(m for m in chat.values()["messages"] if isinstance(m, ToolMessage) and m.name == "get_current_time")
    data = json.loads(result.content)
    got = datetime.strptime(data["now"], "%Y-%m-%d %H:%M").replace(tzinfo=CHINA_TZ)
    assert data["timezone"] == "Asia/Shanghai" and abs((datetime.now(CHINA_TZ) - got).total_seconds()) < 120
    assert data["weekday"] == WEEKDAYS[got.weekday()]
    assert _kinds(v["turns"][-1]) == ["thinking", "thinking", "text"]
    assert not [e for e in chat.events if e["event"] == "step"]


def test_invalid_tool_call_is_retried_with_the_error(chat):
    v = chat.say("坏JSON 直接答")
    msgs = chat.values()["messages"]
    bad = next(m for m in msgs if isinstance(m, AIMessage) and m.invalid_tool_calls)
    # 无效的调用在下一次请求里仍是 tool_calls：重发前先有一条带解析错误的结果，接口才不报错
    res = next(m for m in msgs if isinstance(m, ToolMessage) and m.tool_call_id == bad.invalid_tool_calls[0]["id"])
    assert res.status == "error" and "not valid JSON" in res.content
    assert msgs.index(res) == msgs.index(bad) + 1
    assert v["status"] == "idle" and v["turns"][0]["items"][-1]["text"] == "直接回答：这个问题不用查。"
    assert not [i for i in v["turns"][0]["items"] if i["type"] == "step"]
    # 重发的那次调用，页面说的是在让模型重写，不是普通的等模型
    assert [e["data"]["phase"] for e in chat.events if e["event"] == "phase"] == ["model", "retry", "suggest"]


def test_invalid_tool_call_gives_up_after_retries(chat):
    v = chat.say("坏JSON 一直坏")
    msgs = chat.values()["messages"]
    calls = [tc["id"] for m in msgs if isinstance(m, AIMessage) for tc in m.invalid_tool_calls]
    answered = {m.tool_call_id for m in msgs if isinstance(m, ToolMessage)}
    assert len(calls) == 3 and set(calls) <= answered  # 重试两次；最后一次的也补了结果，下一轮的历史合法
    assert v["status"] == "idle" and v["turns"][0]["record"]["outcome"] == "no_reply"
    v = chat.say("直接答：再说一遍")
    assert v["turns"][-1]["record"]["outcome"] == "ok"


def test_dangling_calls_get_error_results():
    ai = AIMessage(content="", tool_calls=[{"name": "search_web", "args": {"query": "q"}, "id": "c1", "type": "tool_call"},
                                           {"name": "read_page", "args": {"url": "u"}, "id": "c2", "type": "tool_call"}])
    done = ToolMessage(content="[]", name="search_web", tool_call_id="c1")
    (fix,) = history.dangling([HumanMessage("x"), ai, done])
    assert fix.tool_call_id == "c2" and fix.status == "error" and fix.artifact is None
    (stopped,) = history.dangling([HumanMessage("x"), ai, done], stopped=True)
    assert stopped.artifact == {"stopped": True} and stopped.content == prompts.render("agent.stopped_call")
    assert history.step_of(ai.tool_calls[1], stopped).message == "停止了，没有做完"


def _with_gateway(planner: FakeResearchPlanner, handler) -> FakeResearchPlanner:
    gw = GatewayClient("http://gw", "k", scene="research", transport=httpx.MockTransport(handler))
    planner._gateway, planner._tools = gw, make_research_tools(gw)
    return planner


def test_search_outage_goes_to_the_agent():
    """搜索服务出错变成给 agent 的工具结果：这一步显示失败，这一轮照常结束。"""
    p = _with_gateway(FakeResearchPlanner(), lambda req: httpx.Response(502, text="bad gateway"))
    v = Chat(planner=p).say("比较两款笔记软件")
    step = next(i for i in v["turns"][0]["items"] if i["type"] == "step")
    assert step["kind"] == "search" and not step["ok"] and "502" in step["message"]
    assert v["status"] == "idle" and v["turns"][0]["items"][-1]["text"] == "没查到。"


def test_search_auth_error_fails_the_run():
    """密钥问题不交给 agent：这次运行失败、错误记在检查点里，改好配置后可以重试。"""
    c = Chat(planner=_with_gateway(FakeResearchPlanner(), lambda req: httpx.Response(401, json={"error": "unauthorized"})))
    with pytest.raises(GatewayAuthError):
        c.say("比较两款笔记软件")
    v = c.view()
    assert v["status"] == "failed" and "401" in v["error"]


# ---- 停止 ----


def _first(event: str, node: str | None = None, kind: str | None = None):
    """停止的时机：收到第一个这样的事件时点停止。"""
    def match(ev: dict) -> bool:
        d = ev["data"]
        return ev["event"] == event and (node is None or d.get("node") == node) and (kind is None or d.get("kind") == kind)
    return match


def test_stop_while_the_finished_turn_is_being_saved_counts_as_answered():
    """点停止正好落在这一轮答完、存检查点的时候：LangGraph 让存检查点在后台接着做，等它存完再看，这一轮算答完，答完的不被冲掉。"""

    class SlowSaver(InMemorySaver):
        armed = False

        async def aput(self, *a, **kw):
            if self.armed:  # 这一轮最后一次存检查点（DURABILITY 是 exit，只在结束时存）：存的时候点停止，连点两次
                self.armed = False
                c.r.stop(c.tid)
                c.r.stop(c.tid)
                await asyncio.sleep(0.3)
            return await super().aput(*a, **kw)

    saver = SlowSaver(serde=make_serde())
    c = Chat(saver=saver)
    c.r.accept(c.tid, "直接答：说说看")
    saver.armed = True
    v = c.run()
    (turn,) = v["turns"]
    assert turn["record"]["outcome"] == "ok" and turn["items"][-1]["text"] == "直接回答：这个问题不用查。"
    assert c.r.graph.get_state({"configurable": {"thread_id": c.tid}}).next == ()


def test_stop_while_writing_keeps_what_was_streamed(chat, monkeypatch):
    """正文流到一半点停止：模型的流马上断开，已经流到页面的思考和正文存成这一轮的回复，思考用时也存下来。"""
    orig, once = FakeToolModel._chunks, [True]

    def chunks(self, msg):
        for c in orig(self, msg):
            yield c
            if c.additional_kwargs.get("reasoning_content"):
                time.sleep(0.05)  # 想了一会儿才开始写正文
            if c.content and once:  # 第一段正文发出去以后，用户点了停止
                once.clear()
                chat.r.stop(chat.tid)

    monkeypatch.setattr(FakeToolModel, "_chunks", chunks)
    monkeypatch.setattr(research_llm, "FAKE_CHUNK_GAP_S", 0.02)  # 吐出来的片段先到页面，再取消
    v = chat.say("直接答：说说看")
    full = "直接回答：这个问题不用查。"
    (turn,) = v["turns"]
    assert v["status"] == "idle" and turn["record"]["outcome"] == "stopped" and turn["record"]["suggestions"] == []
    assert _kinds(turn) == ["thinking", "text"] and turn["items"][-1]["text"] == full[:len(full) // 2]
    assert [n for n, _ in chat.planner.calls if n == "suggest"] == []
    (partial,) = _turn_messages(chat)
    assert partial.response_metadata[history.STOPPED] and partial.additional_kwargs["reasoning_content"]
    assert turn["items"][0]["ms"] == partial.response_metadata[THINKING_MS] >= 30  # 重新打开也是“思考了 N 秒”
    assert turn["record"]["usage"]["model_calls"] == 0  # 没跑完的那次不知道用量，不算
    assert chat.events[-1] == {"event": "node", "data": {"node": "TurnMiddleware.after_agent"}}
    v = chat.say("直接答：接着说")  # 停下以后可以接着说
    assert v["turns"][-1]["record"]["outcome"] == "ok" and len(v["turns"]) == 2


def test_stop_while_thinking_keeps_the_thought(chat, monkeypatch):
    """还在想、正文一个字没写就点停止：想到的和想了多久存下来，停下前后页面上看到的一样（子 agent 的本来就这样）。
    这半截不发给模型：下一轮模型收到的和它没存时一样。"""
    orig_chunks, orig_message, once, seen = FakeToolModel._chunks, FakeToolModel._message, [True], []

    def chunks(self, msg):
        for c in orig_chunks(self, msg):
            yield c
            if c.additional_kwargs.get("reasoning_content") and once:  # 思考吐出去、正文还没开始，用户点了停止
                once.clear()
                time.sleep(0.05)
                chat.r.stop(chat.tid)

    def message(self, messages):
        seen.append([m.id for m in messages])
        return orig_message(self, messages)

    monkeypatch.setattr(FakeToolModel, "_chunks", chunks)
    monkeypatch.setattr(FakeToolModel, "_message", message)
    monkeypatch.setattr(research_llm, "FAKE_CHUNK_GAP_S", 0.02)
    v = chat.say("直接答：说说看")
    (turn,) = v["turns"]
    assert v["status"] == "idle" and turn["record"]["outcome"] == "stopped" and _kinds(turn) == ["thinking"]
    (partial,) = _turn_messages(chat)
    assert partial.response_metadata[history.STOPPED] and partial.content == "" and history.is_stopped_thought(partial)
    assert turn["items"][0]["text"] == partial.additional_kwargs["reasoning_content"] and turn["items"][0]["ms"] >= 30
    assert turn["record"]["usage"]["model_calls"] == 0
    seen.clear()
    v = chat.say("直接答：接着说")
    assert v["turns"][-1]["record"]["outcome"] == "ok" and _kinds(v["turns"][0]) == ["thinking"]
    assert seen and all(partial.id not in ids for ids in seen)


def test_stop_right_after_a_search_keeps_it(chat):
    """页面收到搜索做完马上点停止：那一步可能还没存进检查点，停下以后它还是做完的，来源也在。"""
    v = chat.say("查一下局部修改", stop_on=lambda e: e["event"] == "step" and not e["data"]["step"]["pending"])
    (turn,) = v["turns"]
    (first, *_) = [i for i in turn["items"] if i["type"] == "step"]
    assert turn["record"]["outcome"] == "stopped" and first["ok"] and first["source_ids"]
    assert set(first["source_ids"]) <= v["sources"].keys() and _answered(chat.values()["messages"])


def test_stop_during_a_search_does_not_wait_for_it():
    """搜索开始以后点停止：正在等的搜索马上断开，不等它返回；这一步显示停止了，不再读页、不再回复。"""
    async def slow(req: httpx.Request) -> httpx.Response:
        await asyncio.sleep(10)  # 慢搜索：停止要是等它，这个测试要等 10 秒
        return _search_returns(_hits("内容一"))(req)

    c = Chat(planner=_with_gateway(FakeResearchPlanner(), slow))
    t0 = time.monotonic()
    v = c.say("查一下局部修改", stop_on=lambda ev: ev["event"] == "step" and ev["data"]["step"]["pending"])
    (turn,) = v["turns"]
    step = turn["items"][-1]
    assert time.monotonic() - t0 < 3 and turn["record"]["outcome"] == "stopped" and v["status"] == "idle"
    assert _kinds(turn) == ["thinking", "step:search"] and not step["ok"] and step["message"] == "停止了，没有做完"
    assert v["sources"] == {} and _answered(c.values()["messages"])
    assert c.say("直接答：好")["turns"][-1]["record"]["outcome"] == "ok"


def test_stop_before_the_tools_run_closes_the_calls(chat):
    """模型发出了工具调用、工具还没跑就停了：调用补上停止的结果，下一轮的历史合法；页面上这一步显示停止了。"""
    v = chat.say("查一下局部修改", stop_on=_first("node", node="model"))
    (turn,) = v["turns"]
    step = turn["items"][-1]
    assert _kinds(turn) == ["thinking", "step:search"] and not step["ok"] and step["message"] == "停止了，没有做完"
    assert turn["record"]["outcome"] == "stopped" and v["sources"] == {}
    msgs = chat.values()["messages"]
    assert _answered(msgs) and msgs[-1].content == prompts.render("agent.stopped_call")
    assert chat.say("直接答：好")["turns"][-1]["record"]["outcome"] == "ok"


def test_stop_while_queued_does_not_run(chat):
    chat.r.accept(chat.tid, "查一下局部修改")
    v = chat.run(stop_first=True)
    (turn,) = v["turns"]
    assert turn["items"] == [] and turn["record"]["outcome"] == "stopped" and v["status"] == "idle"
    assert chat.planner.calls == []  # 标题也没起
    assert v["title"] == "查一下局部修改"
    v = chat.say("直接答：好")
    assert v["turns"][-1]["record"]["outcome"] == "ok" and v["title"] == "标题：查一下局部修改"  # 标题按这段对话的第一句起


def test_stop_without_a_run_is_refused(chat):
    chat.say("直接答：好")
    assert chat.r.stop(chat.tid) is False


# ---- 重新生成 ----


def test_regenerate_replaces_the_last_answer(chat):
    chat.say("直接答：先问一句")
    first = chat.say("查一下局部修改")
    n = len(chat.values()["messages"])
    v = chat.regenerate()
    assert len(v["turns"]) == 2 and v["turns"][1]["id"] == first["turns"][1]["id"] and v["turns"][1]["user"] == "查一下局部修改"
    assert _kinds(v["turns"][1]) == _kinds(first["turns"][1]) and v["turns"][1]["record"]["outcome"] == "ok"
    assert v["turns"][0] == first["turns"][0]
    assert v["turns"][1]["sent_at"] == first["turns"][1]["sent_at"]  # 没改那句话，发送时间照旧
    assert len(chat.values()["messages"]) == n  # 旧的回答去掉了，没有叠在后面
    assert sum(isinstance(m, AIMessage) and m.content.startswith("结论") for m in chat.values()["messages"]) == 1
    assert [n for n, _ in chat.planner.calls].count("title") == 1


def test_regenerate_after_stop(chat):
    chat.say("查一下局部修改", stop_on=_first("node", node="model"))
    v = chat.regenerate()
    (turn,) = v["turns"]
    assert turn["record"]["outcome"] == "ok" and _kinds(turn)[-1] == "text"
    assert not any(isinstance(m.artifact, dict) and m.artifact.get("stopped") for m in chat.values()["messages"] if isinstance(m, ToolMessage))


def test_regenerate_refused_when_failed_or_empty():
    c = Chat(planner=_with_gateway(FakeResearchPlanner(), lambda req: httpx.Response(401, json={"error": "unauthorized"})))
    with pytest.raises(NothingToRegenerate):
        c.r.regenerate_input(c.tid)
    with pytest.raises(GatewayAuthError):
        c.say("比较两款笔记软件")
    with pytest.raises(NotIdle):
        c.r.regenerate_input(c.tid)
    with pytest.raises(NotIdle):
        c.r.accept(c.tid, "接着说")


# ---- 改最后一句、改标题、删除 ----


def test_edit_replaces_the_last_question_and_answers_again(chat, monkeypatch):
    chat.say("直接答：先问一句")
    first = chat.say("查一下局部修改")
    assert datetime.fromisoformat(first["turns"][1]["sent_at"]).tzinfo is not None
    monkeypatch.setattr(research_runner, "now", lambda: "2026-11-01T02:03:04+00:00")
    chat.r.regenerate_input(chat.tid, "  直接答：换个问法 ")
    v = chat.run()
    assert len(v["turns"]) == 2 and v["turns"][0] == first["turns"][0]
    turn = v["turns"][1]
    assert turn["id"] == first["turns"][1]["id"] and turn["user"] == "直接答：换个问法" and _kinds(turn) == _kinds(first["turns"][0])
    assert turn["record"]["outcome"] == "ok" and turn["sent_at"] == "2026-11-01T02:03:04+00:00"  # 发送时间记成改的时候
    humans = [history.text_of(m) for m in chat.values()["messages"] if isinstance(m, HumanMessage)]
    assert humans == ["直接答：先问一句", "直接答：换个问法"]  # 旧的那句换掉了，没有叠在后面
    assert v["title"] == first["title"] and [n for n, _ in chat.planner.calls].count("title") == 1


def test_rename_and_delete(tmp_path):
    c = Chat(saver=_sqlite(tmp_path / "r.sqlite"))
    c.say("直接答：先问一句")
    c.r.rename(c.tid, "新标题")
    assert c.view()["title"] == "新标题" and c.view()["status"] == "idle"
    assert c.say("直接答：接着说")["title"] == "新标题"  # 改过的标题不会被重起
    key = c.r._run_key(c.tid)
    c.r.runs.admit(key)  # 像正在回答
    with pytest.raises(RunConflict):
        c.r.rename(c.tid, "又一个标题")
    with pytest.raises(RunConflict):
        c.r.delete(c.tid)
    c.r.runs.release(key)
    c.r.delete(c.tid)
    assert c.view() == {"thread_id": c.tid, "exists": False} and c.r.list_threads() == []
    assert c.r.runs.get(key) is None


def test_rename_refused_when_failed():
    c = Chat(planner=_with_gateway(FakeResearchPlanner(), lambda req: httpx.Response(401, json={"error": "unauthorized"})))
    with pytest.raises(GatewayAuthError):
        c.say("比较两款笔记软件")
    with pytest.raises(NotIdle):
        c.r.rename(c.tid, "新标题")
    assert c.view()["status"] == "failed"  # 出错的那一步还在，可以重试
    c.r.delete(c.tid)
    assert not c.view()["exists"]


# ---- 模型服务：内容审核、不可用 ----


def _search_returns(hits: list[dict]):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/search/search":
            return httpx.Response(200, json={"results": hits})
        return httpx.Response(404, json={"detail": {"kind": "page_not_found", "message": "gone", "retryable": False}})
    return handler


def _hits(*contents: str) -> list[dict]:
    return [{"title": f"结果 {i}", "url": f"https://example.test/{i}", "description": f"摘要 {i}", "content": c} for i, c in enumerate(contents, 1)]


def _search_step(v: dict) -> dict:
    return next(i for i in v["turns"][-1]["items"] if i["type"] == "step" and i["kind"] == "search")


def test_blocked_search_hit_is_dropped_and_the_turn_goes_on():
    """三条搜索结果里一条没通过审核：一条条单独送审，只去掉那一条，接着跑；去掉的写回消息，之后的请求不再带上。"""
    p = _with_gateway(FakeResearchPlanner(), _search_returns(_hits("内容一", f"内容二{FAKE_BLOCKED}", "内容三")))
    c = Chat(planner=p)
    v = c.say("比较两款笔记软件")
    assert v["status"] == "idle" and v["turns"][0]["record"]["outcome"] == "ok"
    probes = [x for name, x in p.calls if name == "blocked"]
    assert len(probes) == 1 and len(probes[0]) == 3
    phases = [e["data"]["phase"] for e in c.events if e["event"] == "phase"]
    assert phases[phases.index("guard") - 1] == "model"  # 拦下以后才说在去掉资料重试
    step = _search_step(v)
    assert step["ok"] and step["hits"] == 2 and step["message"] == "1 条没通过内容审核，已去掉"
    assert "https://example.test/2" not in {s["url"] for s in v["sources"].values()}
    assert not any(FAKE_BLOCKED in str(m.content) for m in c.values()["messages"])
    assert v["turns"][0]["items"][-1]["type"] == "text"


def test_whole_batch_dropped_when_no_single_hit_is_blocked(monkeypatch):
    """单条都能过（合在一起才拦）：这次搜索的结果整个去掉，按搜索失败交给 agent。"""
    p = _with_gateway(FakeResearchPlanner(), _search_returns(_hits("内容一", f"内容二{FAKE_BLOCKED}")))
    async def blocked(texts):
        return [False] * len(texts)

    monkeypatch.setattr(p, "blocked", blocked)
    v = Chat(planner=p).say("比较两款笔记软件")
    step = _search_step(v)
    assert not step["ok"] and step["message"] == history.BLOCKED_NOTE
    assert v["status"] == "idle" and v["turns"][0]["items"][-1]["text"] == "没查到。" and v["sources"] == {}


def test_still_blocked_after_dropping_material_stops_the_turn(chat):
    v = chat.say("比较两款笔记软件 查完就拦")
    turn = v["turns"][0]
    assert _kinds(turn)[-1] == "blocked" and turn["items"][-1]["reason"] == "material"
    assert turn["record"]["outcome"] == "blocked" and turn["record"]["suggestions"] == [] and v["status"] == "idle"


def test_blocked_user_message_stops_the_turn_and_is_left_out_later(chat):
    """用户的话没通过：这一轮停下，页面上留着；之后的请求不带这一轮，不然每次都会被拦。"""
    v = chat.say(f"直接答 {FAKE_BLOCKED}")
    (turn,) = v["turns"]
    assert _kinds(turn) == ["blocked"] and turn["items"][0]["reason"] == "input"
    assert turn["record"]["outcome"] == "blocked" and turn["record"]["usage"]["model_calls"] == 0
    v = chat.say("直接答：换个说法")
    assert len(v["turns"]) == 2 and v["turns"][1]["record"]["outcome"] == "ok"
    assert v["turns"][1]["items"][-1]["text"] == "直接回答：这个问题不用查。"


def test_blocked_output_stops_the_turn_and_drops_what_was_streamed(chat):
    v = chat.say("输出拦截 直接答")
    assert any(e["event"] == "delta" for e in chat.events)  # 思考已经流出去了
    (turn,) = v["turns"]
    assert _kinds(turn) == ["blocked"] and turn["items"][0]["reason"] == "output"


class FlakyModel(FakeToolModel):
    fails: list[int] = []

    def _ready(self, messages):  # 同步、异步调用都先走这里
        if self.fails:
            self.fails.pop()
            raise ModelUnavailable("模型服务暂时不可用（服务端出错），约 3 分钟后再试")
        return super()._ready(messages)


def test_model_unavailable_fails_the_turn_and_retry_continues():
    p = FakeResearchPlanner()
    p._model = FlakyModel(fails=[1])
    c = Chat(planner=p)
    with pytest.raises(ModelUnavailable):
        c.say("直接答")
    v = c.view()
    assert v["status"] == "failed" and v["error"].startswith("模型服务暂时不可用（服务端出错）")
    c.r.retry_input(c.tid)
    v = c.run()
    assert v["status"] == "idle" and v["turns"][0]["items"][-1]["text"] == "直接回答：这个问题不用查。"


# ---- 检查点 ----


def test_sqlite_round_trip_and_continue(tmp_path):
    db = tmp_path / "data" / "r.sqlite"
    c = Chat(saver=_sqlite(db))
    c.say("查一下局部修改")
    c.say("直接答：为什么", stop_on=_first("delta", kind="text"))
    before = c.view()
    again = Chat(saver=_sqlite(db))  # 像服务重启：新的连接、新的 agent
    after = again.view()
    for key in ("title", "turns", "sources", "status"):
        assert after[key] == before[key], key
    v = again.say("直接答：好")
    assert len(v["turns"]) == 3 and v["turns"][-1]["record"]["outcome"] == "ok"


# ---- 流式片段合并 ----


def test_stream_buffer_merges_deltas_and_keeps_the_last_message():
    buf = StreamBuffer()
    out = []
    out += buf.add(AIMessageChunk(content="", id="m1", additional_kwargs={"reasoning_content": "想"}))
    for part in ("一", "二", "三"):
        out += buf.add(AIMessageChunk(content=part, id="m1"))
    out += buf.flush()
    deltas = [e["data"] for e in out if e["event"] == "delta"]
    assert [(d["kind"], d["text"]) for d in deltas] == [("thinking", "想"), ("text", "一二三")]  # 80ms 内的片段按种类并成一条
    assert buf.flush() == []
    p = buf.partial()
    assert p.id == "m1" and p.content == "一二三" and p.additional_kwargs == {"reasoning_content": "想"} and p.response_metadata[history.STOPPED]
    buf.add(AIMessageChunk(content="四", id="m2"))
    assert buf.partial().content == "四" and buf.partial().additional_kwargs == {}  # 只记最后一条
    assert StreamBuffer().partial() is None


def test_stream_buffer_sends_thinking_time_when_thinking_ends():
    """想完了（下一个片段是正文或工具调用），思考那一段带上后端计的用时，排在正文前面；思考那段已经发出去了，就另起一条只带用时的。
    页面以它为准：中途打开对话时前面的事件一下子补发过来，页面按收到的时间算会算短。"""
    buf = StreamBuffer()
    first = buf.add(AIMessageChunk(content="", id="m1", additional_kwargs={"reasoning_content": "想"})) + buf.flush()
    assert [e["data"] for e in first] == [{"id": "m1", "kind": "thinking", "text": "想"}]  # 还在想，没有用时
    time.sleep(0.03)
    call = AIMessageChunk(content="", id="m1", tool_call_chunks=[tool_call_chunk(name="search_web", args="", id="c1", index=0)])
    out = [e["data"] for e in buf.add(call) + buf.flush()]
    assert len(out) == 1 and out[0]["kind"] == "thinking" and out[0]["text"] == "" and out[0]["ms"] >= 30  # 调工具也算想完了

    buf.add(AIMessageChunk(content="", id="m2", additional_kwargs={"reasoning_content": "再想"}))
    buf.add(AIMessageChunk(content="写", id="m2"))
    buf.add(AIMessageChunk(content="完", id="m2"))
    out = [e["data"] for e in buf.flush()]
    assert [(d["kind"], d["text"], "ms" in d) for d in out] == [("thinking", "再想", True), ("text", "写完", False)]  # 只带一次


# ---- 来源 ----


def _search(hits) -> ToolMessage:
    return ToolMessage(content=json.dumps(hits, ensure_ascii=False), name="search_web", tool_call_id="1", artifact={"retrieved_at": "2026-10-30"})


def _read(data, cid="2") -> ToolMessage:
    return ToolMessage(content=json.dumps(data, ensure_ascii=False), name="read_page", tool_call_id=cid)


def test_collect_sources_prefers_body_excerpt():
    msgs = [_search([{"id": "s111111", "url": "https://a.test/1", "title": "A", "description": "摘要", "published_at": "2026-01-01"}]),
            _read({"ok": True, "id": "s111111", "url": "https://a.test/1", "title": "A", "content": "正文" * 300}),
            _read({"ok": False, "url": "https://a.test/2", "error": "x"}, "3")]
    (s,) = collect_sources(msgs)
    assert s.id == "s111111" and s.excerpt_kind == "body" and len(s.excerpt) == 400 and s.published_at == "2026-01-01"
    assert s.retrieved_at == "2026-10-30"


def test_read_without_body_keeps_search_excerpt():
    msgs = [_search([{"id": "s111111", "url": "https://a.test/1", "title": "A", "description": "摘要"}]),
            _read({"ok": True, "id": "s111111", "url": "https://a.test/1", "content": ""}),
            _read({"ok": True, "id": "s222222", "url": "https://a.test/2", "title": "B", "content": " "}, "3")]
    (s,) = collect_sources(msgs)
    assert s.excerpt == "摘要" and s.excerpt_kind == "summary"


def test_url_variants_get_one_id():
    """读页时网址只是写法不同（域名大小写、锚点、默认端口、编码）：还是搜到的那个来源。"""
    from langchain.tools import ToolRuntime

    def handler(req):
        if req.url.path == "/search/search":
            return httpx.Response(200, json={"results": [{"url": "https://Example.test/文档/a", "title": "A"},
                                                         {"url": "https://example.test/%E6%96%87%E6%A1%A3/a#top", "title": "A 重复"}]})
        return httpx.Response(200, json={"title": "A", "content": "正文"})

    tools = {t.name: t for t in make_research_tools(GatewayClient("http://gw", "k", scene="t", transport=httpx.MockTransport(handler)))}

    def call(name, msgs, **args):
        rt = ToolRuntime(state={"messages": msgs}, context=None, config={}, stream_writer=lambda _: None, tool_call_id="c", store=None)
        return aio.run(tools[name].ainvoke({"name": name, "args": {**args, "runtime": rt}, "id": f"c{len(msgs)}", "type": "tool_call"}))

    found = call("search_web", [], query="q")
    hits = json.loads(found.content)
    assert hits[0]["id"] == hits[1]["id"]
    page = call("read_page", [found], url="https://example.test:443/%e6%96%87%e6%a1%a3/a")
    assert json.loads(page.content)["id"] == hits[0]["id"]
    (s,) = collect_sources([found, page])
    assert s.url == "https://Example.test/文档/a" and s.excerpt_kind == "body"


def test_different_pages_are_not_merged():
    """可能是不同页面的写法不合并：误合并会把 B 的正文挂到 A 的网址和标题上。"""
    pairs = [
        ("https://a.test/x?id=contract%26version=1", "https://a.test/x?id=contract&version=1"),
        ("https://a.test/customer%2Fcontract", "https://a.test/customer/contract"),
        ("http://docs.test:443/a", "http://docs.test/a"),
        ("https://www.a.test/a", "https://a.test/a"), ("http://a.test/a", "https://a.test/a"), ("https://a.test/a/", "https://a.test/a"),
        ("https://a.test/a?x=1", "https://a.test/a?x=2"), ("https://a.test/A", "https://a.test/a"),
    ]
    for a, b in pairs:
        assert url_key(a) != url_key(b), (a, b)
