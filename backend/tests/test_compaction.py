"""上下文压缩：发给模型的那份按 state 里的边界压，state 和页面不变；边界只往后挪，短版每轮写一次；取回工具。
只用假模型：触发线调低，读页的正文放大（page_repeat），几轮就过线。"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, messages_to_dict
from langchain_core.tools import ToolException
from pydantic import Field

from app import aio, config
from app.graph.research import compaction as cp
from app.graph.research import history
from app.graph.research.compaction import CompactionMiddleware, compact, recall_source, recall_turn, tokens, turn_order
from app.graph.research.state import Compaction
from app.research_llm import FakeResearchPlanner, FakeToolModel
from tests.test_research import Chat

REMOVED = "[Removed to save space]"
SHORT = "[Short version of my side of turn"


class Recording(FakeToolModel):
    """记下主 agent 每次调模型发出去的消息（序列化后的）。bind_tools 复制后还是同一个 list。"""

    sent: list = Field(default_factory=list)

    def _message(self, messages):
        self.sent.append(messages_to_dict(messages))
        return super()._message(messages)


def tools_tokens() -> float:
    """假模型下主 agent 的工具定义估算多少 token（每次都发，算进估算里）。下面的触发线都在这个基础上加。"""
    from app.graph.research.subagent import make_task_tool
    from app.tools.flyai_mcp import itinerary_tool

    p = FakeResearchPlanner()
    aio.run(p.connect_tools())  # 和运行器里一样，连上 MCP 的工具（搜索、读页、天气……）
    # 和 agent.build_agent 的工具列表一样
    return CompactionMiddleware(p, [*p.tools(), make_task_tool(p), cp.make_recall_tool(), itinerary_tool()]).tools_tokens


TOOLS = tools_tokens()


def planner(page_repeat: int = 60) -> FakeResearchPlanner:
    p = FakeResearchPlanner(page_repeat=page_repeat)
    p._model = Recording()
    return p


@pytest.fixture
def low(monkeypatch):
    """每轮原样约 1500 token、第一层压过约 300，系统提示词约 2400（再加工具定义）：第三轮过线，之前两轮压完回到目标以下。"""
    monkeypatch.setattr(config, "COMPACT_TRIGGER_TOKENS", 6000 + TOOLS)
    monkeypatch.setattr(config, "COMPACT_TARGET_TOKENS", 4500 + TOOLS)


@pytest.fixture
def changes(monkeypatch) -> list[Compaction]:
    """每次压缩边界挪动时 abefore_model 写进 state 的。"""
    out: list[Compaction] = []
    orig = CompactionMiddleware.abefore_model

    async def spy(self, state, runtime):
        res = await orig(self, state, runtime)
        if res:
            out.append(res["compaction"])
        return res

    monkeypatch.setattr(CompactionMiddleware, "abefore_model", spy)
    return out


def ask(chat: Chat, n: int, start: int = 1) -> None:
    for k in range(start, start + n):
        chat.say(f"第{k}个问题：查一下局部修改")


def turn_ids(chat: Chat) -> list[str]:
    return [m.id for m in chat.values()["messages"] if history.is_request(m)]


def texts(call: list[dict]) -> str:
    return "\n".join(json.dumps(m["data"].get("content"), ensure_ascii=False) for m in call)


def is_prefix(a: list, b: list) -> bool:
    return len(a) <= len(b) and b[:len(a)] == a


# ---- 拼发给模型的那份 ----


def _search(cid: str, ids: list[str]) -> ToolMessage:
    hits = [{"id": i, "title": f"标题{i}", "url": f"https://x.test/{i}", "description": "摘要", "content": "长摘要" * 50} for i in ids]
    return ToolMessage(json.dumps(hits, ensure_ascii=False), tool_call_id=cid, name="search_web", id=f"r-{cid}")


def _read(cid: str, sid: str) -> ToolMessage:
    body = {"id": sid, "ok": True, "url": f"https://x.test/{sid}", "title": f"页面{sid}", "content": "正文" * 500, "truncated": False}
    return ToolMessage(json.dumps(body, ensure_ascii=False), tool_call_id=cid, name="read_page", id=f"r-{cid}")


def _ai(mid: str, text: str = "", calls: list[tuple[str, str]] = ()) -> AIMessage:
    return AIMessage(content=text, id=mid, additional_kwargs={"reasoning_content": "想一想" * 20},
                     tool_calls=[{"name": n, "args": {"q": c}, "id": c} for n, c in calls])


def conversation_fixture() -> list:
    """三轮：第一轮搜、读、查天气；第二轮派子任务，用户中途插了一句；第三轮（这一轮）刚搜完。"""
    return [
        HumanMessage("第一问", id="u1"),
        _ai("a1", calls=[("search_web", "c1"), ("get_weather", "c2")]),
        _search("c1", ["s000001", "s000002"]),
        ToolMessage('{"ok": true, "location": "广州"}', tool_call_id="c2", name="get_weather", id="r-c2"),
        _ai("a2", calls=[("read_page", "c3")]),
        _read("c3", "s000001"),
        _ai("a3", text="第一问的回答 [s000001]"),
        HumanMessage("第二问", id="u2"),
        _ai("a4", calls=[("task", "c4")]),
        ToolMessage("子任务的报告 [s000003]", tool_call_id="c4", name="task", id="r-c4"),
        HumanMessage("顺便看看价格", id="g1", additional_kwargs={history.GUIDE: True}),
        _ai("a5", text="第二问的回答 [s000003]"),
        HumanMessage("第三问", id="u3"),
        _ai("a6", calls=[("search_web", "c5")]),
        _search("c5", ["s000004"]),
    ]


def test_layer_one_clears_material_and_reasoning_but_keeps_the_rest():
    msgs = conversation_fixture()
    before = messages_to_dict(msgs)
    sent = compact(msgs, Compaction(cleared_through="u2"), turn_order(msgs))
    assert messages_to_dict(msgs) == before  # 传进来的消息没改
    by_id = {m.id: m for m in sent}
    assert [m.id for m in sent] == [m.id for m in msgs]
    search = by_id["r-c1"].content
    assert search.startswith(REMOVED) and "s000001" in search and "标题s000002" in search and "https://x.test/s000002" in search
    assert "长摘要" not in search
    read = by_id["r-c3"].content
    assert read.startswith(REMOVED) and "s000001" in read and "页面s000001" in read and "正文" not in read
    assert by_id["r-c2"].content == msgs[3].content  # 天气留着
    assert by_id["r-c4"].content == "子任务的报告 [s000003]"  # 子任务的报告留着
    assert all("reasoning_content" not in by_id[i].additional_kwargs for i in ("a1", "a2", "a3", "a4", "a5"))
    assert by_id["a1"].tool_calls == msgs[1].tool_calls  # 工具调用本身留着
    # 这一轮原样
    assert by_id["a6"] is msgs[13] and by_id["r-c5"] is msgs[14]


def test_layer_two_keeps_user_messages_and_puts_the_short_version_after_them():
    msgs = conversation_fixture()
    c = Compaction(cleared_through="u2", summarized_through="u2", summaries={"u1": "短版一 [s000001]", "u2": "短版二"})
    sent = compact(msgs, c, turn_order(msgs))
    assert [m.id for m in sent[:5]] == ["u1", "compact-u1", "u2", "g1", "compact-u2"]
    assert sent[0] is msgs[0] and sent[3] is msgs[10]  # 用户的话（包括插进来的）照原样
    assert sent[1].content.startswith(f"{SHORT} 1:") and sent[1].content.endswith("短版一 [s000001]")
    assert not sent[1].tool_calls and "reasoning_content" not in sent[1].additional_kwargs
    assert "recall with turn 2" in sent[4].content
    assert sent[5:] == msgs[12:]


def test_turn_without_a_short_version_falls_back_to_layer_one_and_long_user_message_is_shortened():
    msgs = conversation_fixture()
    c = Compaction(cleared_through="u2", summarized_through="u2", summaries={"u2": "短版二"}, shortened={"u1": "短的第一问"})
    sent = compact(msgs, c, turn_order(msgs))
    assert sent[0].id == "u1" and sent[0].content.startswith("[This user message was shortened") and sent[0].content.endswith("短的第一问")
    assert [m.id for m in sent[1:6]] == ["a1", "r-c1", "r-c2", "a2", "r-c3"]
    assert sent[2].content.startswith(REMOVED)


def test_turn_numbers_count_blocked_turns():
    """被内容审核停下的那一轮不发给模型，但轮号还算它：说明里写的轮号和 recall 认的一样。"""
    msgs = conversation_fixture()
    blocked = [HumanMessage("被拦的", id="u0"), AIMessage("", id="b0", response_metadata={history.BLOCKED: "input"})]
    msgs = blocked + msgs
    c = Compaction(cleared_through="u2", summarized_through="u1", summaries={"u1": "短版一"})
    sent = compact(history.without_blocked_turns(msgs), c, turn_order(msgs))
    assert sent[1].content.startswith(f"{SHORT} 2:")
    assert json.loads(recall_turn(msgs, 2))["user"] == "第一问"


def test_recall_does_not_bring_back_blocked_turns():
    """被内容审核停下的那一轮，按轮号、按来源 id 都取不回：发给模型就又会被拦。"""
    blocked = [HumanMessage("被拦的", id="u0"), _ai("x0", calls=[("read_page", "k0")]), _read("k0", "s0000bb"),
               AIMessage("", id="b0", response_metadata={history.BLOCKED: "output"})]
    msgs = blocked + conversation_fixture()
    with pytest.raises(ToolException, match="content review"):
        recall_turn(msgs, 1)
    with pytest.raises(ToolException, match="s0000bb"):
        recall_source(msgs, "s0000bb")
    assert json.loads(recall_source(msgs, "s000001"))["id"] == "s000001"


def test_token_estimate_follows_deepseek_ratio():
    assert tokens("abcdefghij") == pytest.approx(3.0)
    assert tokens("中文中文中") == pytest.approx(3.0)


# ---- 跑起来：第一层 ----


def test_crossing_clears_old_turns_once_and_keeps_state_and_page(low, changes):
    p = planner()
    chat = Chat(planner=p)
    ask(chat, 2)
    view_before = chat.view()["turns"]
    msgs_before = messages_to_dict(chat.values()["messages"])
    ask(chat, 1, start=3)
    c = chat.values()["compaction"]
    ids = turn_ids(chat)
    assert len(changes) == 1 and c.cleared_through == ids[1] and not c.summarized_through and not c.summaries
    # state、页面都没变：之前两轮的消息、对话视图原样
    assert messages_to_dict(chat.values()["messages"])[:len(msgs_before)] == msgs_before
    assert chat.view()["turns"][:2] == view_before
    # 过线之后发出去的：之前两轮的读页、搜索换成了说明，这一轮的原样
    last = texts(p._model.sent[-1])
    assert last.count(REMOVED) == 4
    assert "正文：https://example.test" in last  # 这一轮读的页还在
    first_turn = [m for m in p._model.sent[-1] if m["type"] == "ai"][:3]
    assert all("reasoning_content" not in m["data"]["additional_kwargs"] for m in first_turn)


def test_sent_messages_are_a_prefix_of_the_next_call_except_at_crossings(low, changes):
    """前缀缓存：边界不挪时，每次发出去的都是下一次的前缀（逐字节一样）；只在边界挪的那一次断。"""
    p = planner()
    chat = Chat(planner=p)
    ask(chat, 6)
    sent = p._model.sent
    breaks = [i for i in range(len(sent) - 1) if not is_prefix(sent[i], sent[i + 1])]
    assert len(changes) >= 2
    assert len(breaks) == len(changes)
    for i in breaks:  # 断的地方都是又换掉了一些
        assert texts(sent[i + 1]).count(REMOVED) > texts(sent[i]).count(REMOVED)


def test_same_state_gives_the_same_payload(low):
    p = planner()
    chat = Chat(planner=p)
    ask(chat, 3)
    v = chat.values()
    msgs, c = v["messages"], v["compaction"]
    a = messages_to_dict(compact(msgs, c, turn_order(msgs)))
    b = messages_to_dict(compact(msgs, c.model_copy(deep=True), turn_order(list(msgs))))
    assert a == b


def test_no_compaction_below_the_line():
    p = planner()
    chat = Chat(planner=p)
    ask(chat, 3)
    assert "compaction" not in chat.values()
    assert all(REMOVED not in texts(call) for call in p._model.sent)


# ---- 第二层 ----


@pytest.fixture
def summarize_low(monkeypatch):
    """第一层压完还超：目标定得比之前几轮都压过第一层还低，短版按很短估。"""
    monkeypatch.setattr(config, "COMPACT_TRIGGER_TOKENS", 6000 + TOOLS)
    monkeypatch.setattr(config, "COMPACT_TARGET_TOKENS", 3000 + TOOLS)
    monkeypatch.setattr(cp, "SUMMARY_GUESS", 50)


def summarized_turns(p: FakeResearchPlanner) -> list[str]:
    """写过短版的轮（按交来的那一轮里用户说的第几个问题）。"""
    out = []
    for kind, prompt in p.calls:
        if kind == "summarize" and "<turn>" in prompt:
            body = prompt.split("<turn>")[-1]
            out.append(body.split("个问题")[0].split("第")[-1])
    return out


def test_summaries_are_written_once_per_turn(summarize_low, changes):
    p = planner()
    chat = Chat(planner=p)
    ask(chat, 3)
    c = chat.values()["compaction"]
    ids = turn_ids(chat)
    assert c.summarized_through == ids[1] and set(c.summaries) == {ids[0], ids[1]}
    assert sorted(summarized_turns(p)) == ["1", "2"]
    last = p._model.sent[-1]
    assert texts(last).count(SHORT) == 2
    # 之前两轮发出去的只剩用户的话和短版
    head = [(m["type"], m["data"].get("id")) for m in last[1:5]]
    assert head == [("human", ids[0]), ("ai", f"compact-{ids[0]}"), ("human", ids[1]), ("ai", f"compact-{ids[1]}")]
    assert last[1]["data"]["content"] == "第1个问题：查一下局部修改"
    ask(chat, 3, start=4)
    turns = summarized_turns(p)
    assert len(turns) == len(set(turns))  # 没有哪一轮写了两次
    assert chat.values()["compaction"].summaries[ids[0]] == c.summaries[ids[0]]  # 写好的不再改


def test_regenerate_and_stop_keep_compaction(summarize_low):
    p = planner()
    chat = Chat(planner=p)
    ask(chat, 3)
    c = chat.values()["compaction"]
    n = len(summarized_turns(p))
    chat.regenerate()
    assert chat.values()["compaction"] == c and len(summarized_turns(p)) == n
    chat.say("第9个问题：查一下局部修改", stop_on=lambda ev: ev["event"] == "step" and not ev["data"]["step"].get("pending"))
    assert chat.view()["turns"][-1]["record"]["outcome"] == "stopped"
    after = chat.values()["compaction"]
    assert order_of(chat, after.cleared_through) >= order_of(chat, c.cleared_through)
    assert all(after.summaries[k] == v for k, v in c.summaries.items())  # 停下以后写好的短版、边界都在


def test_failed_summary_stops_the_boundary_and_is_retried_later(summarize_low, monkeypatch):
    p = planner()
    chat = Chat(planner=p)
    ask(chat, 2)
    ids = turn_ids(chat)
    state = {"fail": True}
    orig = p.summarize

    async def flaky(prompt):
        if state["fail"] and "第1个问题" in prompt.split("<turn>")[-1]:
            raise RuntimeError("假模型：写短版失败")
        return await orig(prompt)

    p.summarize = flaky
    ask(chat, 1, start=3)
    c = chat.values()["compaction"]
    assert not c.summarized_through and ids[1] in c.summaries and ids[0] not in c.summaries  # 第二轮写好了，边界停在第一轮之前
    assert texts(p._model.sent[-1]).count(SHORT) == 0  # 第一轮没短版，两轮都按第一层发
    state["fail"] = False
    ask(chat, 3, start=4)
    c = chat.values()["compaction"]
    assert ids[0] in c.summaries and order_of(chat, c.summarized_through) >= 1
    assert summarized_turns(p).count("2") == 1  # 第二轮没有重写


def order_of(chat: Chat, tid: str) -> int:
    return turn_order(chat.values()["messages"]).get(tid, -1)


def test_long_user_message_is_shortened_in_summarized_turns(summarize_low, monkeypatch):
    monkeypatch.setattr(cp, "LONG_USER_CHARS", 30)
    p = planner()
    chat = Chat(planner=p)
    chat.say("第1个问题：查一下局部修改。" + "这是用户贴进来的一大段材料。" * 5)
    ask(chat, 2, start=2)
    ids = turn_ids(chat)
    c = chat.values()["compaction"]
    assert ids[0] in c.shortened and c.shortened[ids[0]].startswith("（短的用户消息）")
    first = p._model.sent[-1][1]
    assert first["data"]["id"] == ids[0] and first["data"]["content"].startswith("[This user message was shortened")
    assert chat.view()["turns"][0]["user"].endswith("材料。")  # 页面上还是原话


# ---- 取回 ----


def test_recall_gets_back_a_cleared_page_and_is_not_shown(low):
    p = planner()
    chat = Chat(planner=p)
    ask(chat, 3)
    msgs = chat.values()["messages"]
    first_read = next(m for m in msgs if isinstance(m, ToolMessage) and m.name == "read_page")
    view = chat.say("取回")
    result = [m for m in chat.values()["messages"] if isinstance(m, ToolMessage) and m.name == "recall"]
    assert len(result) == 1 and result[0].content == first_read.content and result[0].status == "success"
    turn = view["turns"][-1]
    assert [i["type"] for i in turn["items"] if i["type"] == "step"] == []  # 页面上不显示取回这一步
    assert turn["record"]["usage"]["searches"] == 0 and turn["record"]["usage"]["reads"] == 0
    assert turn["items"][-1]["text"].startswith("取回了：")


def test_recall_a_turn_gives_the_original_answer(low):
    chat = Chat(planner=planner())
    ask(chat, 3)
    answer = chat.view()["turns"][0]["items"][-1]["text"]
    chat.say("取回第1轮")
    result = next(m for m in chat.values()["messages"] if isinstance(m, ToolMessage) and m.name == "recall")
    data = json.loads(result.content)
    assert data == {"turn": 1, "user": "第1个问题：查一下局部修改", "answer": answer}


def test_recall_lookup_and_errors():
    msgs = conversation_fixture()
    assert json.loads(recall_source(msgs, "s000001"))["content"] == "正文" * 500  # 读过的给读页的结果
    hit = json.loads(recall_source(msgs, "s000002"))  # 只在搜索里出现的给搜索那一条
    assert hit["page_read"] is False and hit["title"] == "标题s000002" and hit["snippet"].startswith("长摘要")
    with pytest.raises(ToolException):
        recall_source(msgs, "s999999")
    assert json.loads(recall_turn(msgs, 2)) == {"turn": 2, "user": "第二问", "user_follow_ups": ["顺便看看价格"], "answer": "第二问的回答 [s000003]"}
    with pytest.raises(ToolException):
        recall_turn(msgs, 4)


def test_recall_finds_pages_read_by_subagents():
    sub = [HumanMessage("查"), _ai("x1", calls=[("read_page", "k1")]), _read("k1", "s0000aa"), AIMessage("报告 [s0000aa]")]
    task = ToolMessage("报告 [s0000aa]", tool_call_id="c9", name="task", artifact={"messages": messages_to_dict(sub)})
    assert json.loads(recall_source([HumanMessage("问"), task], "s0000aa"))["id"] == "s0000aa"


def test_recall_rejects_both_or_neither_argument():
    tool = cp.make_recall_tool()
    runtime = SimpleNamespace(state={"messages": conversation_fixture()})
    for args in ({}, {"source_id": "s000001", "turn": 1}, {"source_id": " "}):
        with pytest.raises(ToolException):
            tool.func(runtime=runtime, **args)
    assert json.loads(tool.func(runtime=runtime, turn=1))["user"] == "第一问"


# ---- 缓存断了记警告 ----


def _usage_ai(inp: int, hit: int) -> AIMessage:
    return AIMessage("x", usage_metadata={"input_tokens": inp, "output_tokens": 1, "total_tokens": inp + 1,
                                          "input_token_details": {"cache_read": hit}})


def test_cache_drop_is_logged(caplog):
    caplog.set_level(logging.WARNING, logger=cp.__name__)
    prev = [HumanMessage("q"), _usage_ai(50000, 0)]
    cp._check_cache(prev, SimpleNamespace(result=[_usage_ai(52000, 49000)]), "")
    assert not caplog.records
    cp._check_cache(prev, SimpleNamespace(result=[_usage_ai(52000, 3000)]), "")
    assert "前缀缓存没接上" in caplog.text
    caplog.clear()
    cp._check_cache([HumanMessage("q"), _usage_ai(5000, 0)], SimpleNamespace(result=[_usage_ai(6000, 0)]), "")  # 太小的不查
    assert not caplog.records
    cp._check_cache(prev, SimpleNamespace(result=[_usage_ai(30000, 3000)]), "u1/")  # 压缩边界这次刚挪过：断是应该的
    assert not caplog.records


def test_cache_check_knows_when_the_boundary_moved(low, changes, caplog):
    """每次调模型记下当时的压缩边界；过线那次不报，之后边界没动的照常查。"""
    caplog.set_level(logging.WARNING, logger=cp.__name__)
    chat = Chat(planner=planner())
    ask(chat, 3)
    marks = [m.response_metadata.get(cp.MARK) for m in chat.values()["messages"] if isinstance(m, AIMessage) and m.usage_metadata]
    c = chat.values()["compaction"]
    assert marks[0] == "" and marks[-1] == f"{c.cleared_through}/{c.summarized_through}" and len(changes) == 1
