"""调研 agent：多轮对话、提问、写报告和改报告、出版本、流式事件、检查点往返。只用假模型（FakeToolModel 的关键字见它的说明）。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

from app.api.research_runner import NotIdle, ResearchRunner, StaleInterrupt, StreamBuffer
from app.graph.research import history
from app.graph.research.agent import system_prompt
from app.graph.research.answers import InvalidAnswer, ask_resume
from app.graph.research.graph import memory_checkpointer, open_sqlite_checkpointer
from app.graph.research.report import report_blocks, run_checks, to_markdown
from app.graph.research.sources import collect_sources, url_key
from app.graph.research.state import Claim, OutlineNode, SectionText, Sentence, Source
from app.research_llm import ClaimDraft, ConsolidateResult, FakeResearchPlanner, FakeToolModel, SectionDraft, SentenceDraft, TimeIssue
from app.tools.gateway import GatewayAuthError, GatewayClient
from app.tools.research_tools import make_research_tools

ASK = "范围包含 PPT 工具吗？"


class Chat:
    """像页面那样一句一句说：写进检查点 → 跑到这一轮结束或停在提问上。不经过执行器，事件同步拿到。"""

    def __init__(self, tmp_path: Path, planner: FakeResearchPlanner | None = None, saver=None, tid: str = "t1") -> None:
        self.planner = planner or FakeResearchPlanner()
        self.saver = saver or memory_checkpointer()
        self.r = ResearchRunner(planner=self.planner, checkpointer=self.saver, out_dir=tmp_path / "out")
        self.tid = tid
        self.events: list[dict] = []

    def say(self, text: str, quote: dict | None = None, action: dict | None = None) -> dict:
        self.r.accept(self.tid, text, quote, action)
        return self.run(None)

    def run(self, inp) -> dict:
        self.events = list(self.r._graph_events(self.tid, inp))
        return self.view()

    def answer(self, payload: dict) -> dict:
        it = self.r.pending(self.tid)
        return self.run(self.r.resume_input(self.tid, payload, it.id))

    def view(self) -> dict:
        return self.r.state_view(self.tid)

    def values(self) -> dict:
        return self.r.graph.get_state({"configurable": {"thread_id": self.tid}}).values


@pytest.fixture
def chat(tmp_path) -> Chat:
    return Chat(tmp_path)


def _kinds(turn: dict) -> list[str]:
    return [i["type"] if i["type"] != "step" else f"step:{i['kind']}" for i in turn["items"]]


def _labels(report: dict) -> dict[str, str]:
    return {b["source_id"]: b["label"] for b in report["blocks"] if b["type"] == "source"}


def _sentences(report: dict) -> list[str]:
    return [b["text"] for b in report["blocks"] if b["type"] == "sentence"]


# ---- 一轮 ----


def test_first_turn_researches_writes_v1_and_replies(chat, tmp_path):
    v = chat.say("调研一下 AI 海报工具的局部修改")
    assert v["status"] == "idle" and v["title"] == "标题：调研一下 AI" and v["interrupt"] is None
    (turn,) = v["turns"]
    assert turn["user"] == {"text": "调研一下 AI 海报工具的局部修改"}
    # 思考、步骤、正文按发生的先后穿插
    assert _kinds(turn) == ["thinking", "step:search", "thinking", "step:read", "thinking", "step:report", "thinking", "text"]
    assert turn["items"][-1]["text"].startswith("结论：")
    rec = turn["record"]
    assert rec["version"] == 1 and rec["outcome"] == "ok" and len(rec["suggestions"]) == 3 and rec["ended_at"]
    assert rec["usage"]["model_calls"] == 4 and rec["usage"]["cached_tokens"] == 240 and rec["usage"]["searches"] == 1
    r = v["report"]
    assert r["version"] == 1 and not r["draft"] and r["title"] == "局部修改调研"
    assert v["versions"] == [{"n": 1, "note": "第一版", "turn": turn["id"]}]
    assert to_markdown(r["blocks"]) == (tmp_path / "out" / "t1" / "report_v1.md").read_text()
    # 草稿里引用了不存在的来源：那句连同论断一起去掉；没有来源的句子留作过渡句，并记成假设
    assert "引用不存在的来源。" not in _sentences(r) and "没有来源的说法。" in _sentences(r) and r["assumptions"] == ["没有来源的说法。"]
    # 推断句的标记由代码按论断类型加
    kind = {c["id"]: c["kind"] for c in r["claims"]}
    sents = [b for b in r["blocks"] if b["type"] == "sentence" and b["claim_id"]]
    assert sents and all(b["text"].startswith("推断：") == b["inference"] == (kind[b["claim_id"]] == "inference") for b in sents)
    assert {c["name"]: c["passed"] for c in r["checks"]}["report_valid"]
    # 来源：搜到 3 条，读到第 1 条的正文；报告引用了 2 条
    status = {s["id"]: (s["status"], s["excerpt_kind"]) for s in v["sources"].values()}
    assert sorted(status.values()) == [("adopted", "body"), ("adopted", "summary"), ("candidate", "summary")]


def test_events_stream_title_steps_text_and_writing(chat):
    chat.say("调研一下局部修改")
    names = [e["event"] for e in chat.events]
    assert names.index("title") < names.index("step") and "writing" in names and "node" in names
    steps = [e["data"]["step"] for e in chat.events if e["event"] == "step"]
    # 每个工具开始一次、完成一次，两次 id 一样
    assert [(s["kind"], s["pending"]) for s in steps] == [("search", True), ("search", False), ("read", True), ("read", False),
                                                           ("report", True), ("report", False)]
    assert steps[0]["id"] == steps[1]["id"] and steps[-1]["message"] == "整理成 2 节、3 条论断"
    writing = [e["data"] for e in chat.events if e["event"] == "writing"]
    assert writing and writing[-1]["tool"] == "write_report" and writing[-1]["chars"] > 0
    deltas = [e["data"] for e in chat.events if e["event"] == "delta"]
    assert {d["kind"] for d in deltas} == {"thinking", "text"}
    # 实时片段的消息 id 和存下来的对得上：页面按 id 把片段叠在已存的 state 上
    stored = {i["id"] for i in chat.view()["turns"][0]["items"] if i["type"] in ("thinking", "text")}
    assert {d["id"] for d in deltas} == stored
    text = "".join(d["text"] for d in deltas if d["kind"] == "text")
    assert text == chat.view()["turns"][0]["items"][-1]["text"]


def test_simple_question_answers_without_report(chat):
    v = chat.say("直接答：新加坡现在是什么季节？")
    (turn,) = v["turns"]
    assert _kinds(turn) == ["thinking", "text"] and turn["record"]["version"] is None
    assert v["report"] is None and v["versions"] == [] and v["status"] == "idle"
    assert len(turn["record"]["suggestions"]) == 3


def test_later_turns_keep_report_unless_changed(chat):
    chat.say("调研一下局部修改")
    before = chat.view()["report"]
    v = chat.say("直接答：为什么不稳定？")
    assert len(v["turns"]) == 2 and v["turns"][1]["record"]["version"] is None
    assert v["report"]["blocks"] == before["blocks"] and len(v["versions"]) == 1
    assert [t for _, t in chat.planner.calls if _ == "title"] == ["调研一下局部修改"]  # 标题只在第一轮起


def test_no_reply_is_recorded(chat):
    v = chat.say("调研 不回复")
    assert v["status"] == "idle" and v["turns"][0]["record"]["outcome"] == "no_reply" and v["turns"][0]["record"]["suggestions"] == []
    assert v["report"]["version"] == 1  # 报告照样落成一版


def test_title_falls_back_to_the_words(tmp_path):
    p = FakeResearchPlanner()
    p.fail_title = True
    text = "直接答：这是一个超过二十四个字的问题，用来测试起标题失败时的截断"
    assert Chat(tmp_path, p).say(text)["title"] == text[:24]


def test_system_prompt_has_today_and_methods():
    from datetime import date

    text = system_prompt()
    assert date.today().isoformat() in text and "load_skill" in text


# ---- 按钮和选中 ----


def test_exclude_source_drops_sentences_and_renumbers(chat):
    v1 = chat.say("调研一下局部修改")["report"]
    first = next(sid for sid, n in _labels(v1).items() if n == "1")
    only_first = [c["statement"] for c in v1["claims"] if c["source_ids"] == [first]]
    v = chat.say("这条来源不靠谱", action={"type": "exclude_source", "source_id": first})
    turn = v["turns"][-1]
    assert turn["user"] == {"text": "这条来源不靠谱", "action": {"type": "exclude_source", "source_id": first}}
    assert turn["record"]["version"] == 2 and v["versions"][-1]["note"] == "排除了一条来源"
    r = v["report"]
    assert r["excluded"] == [first] and v["sources"][first]["status"] == "excluded"
    assert only_first and not any(s in t for s in only_first for t in _sentences(r))  # 只靠它的句子删了
    assert first not in _labels(r) and list(_labels(r).values()) == ["1"]  # 重新从 1 编
    assert all(first not in c["source_ids"] for c in r["claims"])
    assert {c["name"]: c["passed"] for c in r["checks"]}["no_excluded_sources"]


def test_exclude_uncited_source_makes_no_version(chat):
    v1 = chat.say("调研一下局部修改")
    uncited = next(s["id"] for s in v1["sources"].values() if s["status"] == "candidate")
    v = chat.say("", action={"type": "exclude_source", "source_id": uncited})
    assert v["turns"][-1]["record"]["version"] is None and len(v["versions"]) == 1
    assert v["report"]["excluded"] == [uncited] and v["report"]["blocks"] == v1["report"]["blocks"]
    with pytest.raises(InvalidAnswer, match="排除过"):
        chat.r.accept("t1", "", None, {"type": "exclude_source", "source_id": uncited})


def test_claim_the_model_rewords_is_not_marked_as_yours(tmp_path):
    """用户选中一句说太绝对，模型自己用 edit_claim 改了说法：这不是用户的话，不显示“你改过”。"""

    class Rewords(FakeToolModel):
        def _decide(self, messages):
            if "太绝对" in history.text_of(messages[history.last_human(messages)]):
                done = any(isinstance(m, ToolMessage) and m.name == "edit_claim" for m in messages[history.last_human(messages):])
                if not done:
                    return "", [{"name": "edit_claim", "args": {"claim_id": "k1", "statement": "模型改的说法。", "change": "改得不那么绝对"}, "id": "e1"}]
                return "改好了。", []
            return super()._decide(messages)

    p = FakeResearchPlanner()
    p._model = Rewords()
    chat = Chat(tmp_path, p)
    r1 = chat.say("调研一下局部修改")["report"]
    k1 = next(c for c in r1["claims"] if c["id"] == "k1")
    r = chat.say("这里说得太绝对了", quote={"text": k1["statement"], "claim_ids": ["k1"], "section_ids": [k1["outline_id"]]})["report"]
    claim = next(c for c in r["claims"] if c["id"] == "k1")
    assert claim["statement"] == "模型改的说法。" and claim["status"] == "draft"


def test_edit_claim_replaces_the_sentence(chat):
    r1 = chat.say("调研一下局部修改")["report"]
    k = r1["claims"][0]
    v = chat.say("改一下", action={"type": "edit_claim", "claim_id": k["id"], "statement": "局部修改基本可用。"})
    r = v["report"]
    claim = next(c for c in r["claims"] if c["id"] == k["id"])
    assert claim["statement"] == "局部修改基本可用。" and claim["status"] == "user_edited"
    assert "局部修改基本可用。" in _sentences(r) and k["statement"] not in " ".join(_sentences(r))
    assert v["turns"][-1]["record"]["version"] == 2


def test_rewrite_section_changes_only_that_section(chat):
    r1 = chat.say("调研一下局部修改")["report"]
    first, second = r1["outline"]
    v = chat.say("重写", action={"type": "rewrite_section", "section_id": first["id"]})
    r = v["report"]
    assert [n["id"] for n in r["outline"]] == [first["id"], second["id"]] and r["outline"][0]["title"] == "重写的节"
    assert [s["text"] for s in r["sections"][first["id"]]["sentences"]] == ["重写后的说法。"]
    assert r["sections"][second["id"]] == r1["sections"][second["id"]]
    assert v["versions"][-1]["note"] == "重写了一节"


class SharedClaim(FakeResearchPlanner):
    """整理时一条论断用在两节里（结论节复述正文的发现）。"""

    def consolidate(self, requests, draft, sources, user_facts=(), report=None, part=None):
        if part is not None:
            return super().consolidate(requests, draft, sources, user_facts, report, part)
        sid = sources[0].id
        return ConsolidateResult(
            sections=[SectionDraft(title="结论", question="?", sentences=[SentenceDraft(text="X 的成功率是 90%。", claim_ref="k1")]),
                      SectionDraft(title="细节", question="?", sentences=[SentenceDraft(text="实测中 X 的成功率达到 90%。", claim_ref="k1")])],
            claims=[ClaimDraft(ref="k1", statement="X 成功率 90%", kind="fact", source_ids=[sid], confidence="high")],
            assumptions=[], time_issues=[])


def test_rewriting_one_section_keeps_claims_other_sections_cite(tmp_path):
    chat = Chat(tmp_path, SharedClaim())
    r1 = chat.say("调研一下局部修改")["report"]
    first, second = r1["outline"]
    r = chat.say("重写", action={"type": "rewrite_section", "section_id": first["id"]})["report"]
    detail = next(b for b in r["blocks"] if b["type"] == "sentence" and b["text"] == "实测中 X 的成功率达到 90%。")
    assert detail["claim_id"] == "k1" and detail["marks"] == ["1"]  # 还挂着来源，不是变成了过渡句
    assert next(c for c in r["claims"] if c["id"] == "k1")["outline_id"] == second["id"]
    assert all(c["passed"] for c in r["checks"])


def test_edit_claim_used_in_two_sections_tells_the_model_about_the_other(tmp_path):
    chat = Chat(tmp_path, SharedClaim())
    chat.say("调研一下局部修改")
    r = chat.say("改一下", action={"type": "edit_claim", "claim_id": "k1", "statement": "X 的成功率约 90%。"})["report"]
    assert _sentences(r)[:2] == ["X 的成功率约 90%。", "实测中 X 的成功率达到 90%。"]
    result = next(m for m in reversed(chat.values()["messages"]) if isinstance(m, ToolMessage) and m.name == "edit_claim")
    assert "还有 1 句也引用了它" in result.content and "实测中 X 的成功率达到 90%。" in result.content


def test_check_fails_when_a_sentence_points_at_a_missing_claim():
    src = Source(id="s000001", url="https://a.test/1", title="A", retrieved_at="2026-10-30", status="adopted")
    claims = [Claim(id="k1", statement="a", kind="fact", source_ids=[src.id], outline_id="c1")]
    sections = {"c1": SectionText(outline_id="c1", sentences=[Sentence(text="a", claim_id="k1"), Sentence(text="b", claim_id="k9")])}
    check = next(c for c in run_checks(sections, claims, [src]) if c.name == "claims_sourced")
    assert not check.passed and "论断不存在：k9" in check.detail


def test_report_change_without_a_note_is_still_published(tmp_path):
    """模型改了报告却没写改动说明：照样出版、跑校验，说明用这一步的结果代替。"""

    class NoNote(FakeToolModel):
        def _decide(self, messages):
            content, calls = super()._decide(messages)
            return content, [{**c, "args": {**c["args"], "change": ""}} if isinstance(c["args"], dict) and "change" in c["args"] else c
                             for c in calls]

    p = FakeResearchPlanner()
    p._model = NoNote()
    chat = Chat(tmp_path, p)
    v1 = chat.say("调研一下局部修改")
    assert [x["n"] for x in v1["versions"]] == [1] and v1["versions"][0]["note"].startswith("整理成 2 节")
    first = next(sid for sid, n in _labels(v1["report"]).items() if n == "1")
    v = chat.say("", action={"type": "exclude_source", "source_id": first})
    assert v["turns"][-1]["record"]["version"] == 2 and not v["report"]["draft"] and v["report"]["checks"]
    assert first not in Path(chat.values()["versions"][-1].path).read_text()


def test_quote_goes_to_the_model_and_is_kept_for_the_page(chat):
    r1 = chat.say("调研一下局部修改")["report"]
    k = r1["claims"][0]
    quote = {"text": k["statement"], "claim_ids": [k["id"]], "section_ids": [k["outline_id"]]}
    chat.say("直接答：这里说得太绝对了", quote=quote)
    human = [m for m in chat.values()["messages"] if isinstance(m, HumanMessage)][-1]
    assert k["statement"] in human.content and k["id"] in human.content  # 模型看到选中的句子和它的 id
    assert history.said(human) == {"text": "直接答：这里说得太绝对了", "quote": quote}  # 页面显示原话和选中


@pytest.mark.parametrize("quote, action, match", [
    ({"text": "x", "claim_ids": ["k99"], "section_ids": []}, None, "不在当前报告"),
    ({"text": "x", "claim_ids": [], "section_ids": ["c99"]}, None, "不在当前报告"),
    (None, {"type": "exclude_source", "source_id": "s000000"}, "来源不存在"),
    (None, {"type": "edit_claim", "claim_id": "k1"}, "statement"),
    (None, {"type": "edit_claim", "claim_id": "k99", "statement": "x"}, "论断不存在"),
    (None, {"type": "rewrite_section", "section_id": "c99"}, "章节不存在"),
    (None, {"type": "publish"}, "type"),
])
def test_bad_quote_or_action_rejected_before_anything_changes(chat, quote, action, match):
    chat.say("调研一下局部修改")
    n = len(chat.values()["messages"])
    with pytest.raises(InvalidAnswer, match=match):
        chat.r.accept("t1", "看看", quote, action)
    assert len(chat.values()["messages"]) == n


def test_empty_message_rejected(chat):
    with pytest.raises(InvalidAnswer):
        chat.r.accept("t1", "  ")


# ---- 提问 ----


def test_ask_waits_then_answer_continues(chat):
    v = chat.say("问一下 调研局部修改")
    assert v["status"] == "waiting" and v["interrupt"]["type"] == "research.ask" and v["interrupt"]["title"] == ASK
    assert [e["event"] for e in chat.events].count("interrupt") == 1
    ask = [i for i in v["turns"][0]["items"] if i["type"] == "ask"]
    assert ask[0]["question"] == ASK and ask[0]["pending"] and not v["turns"][0]["record"]["ended_at"]
    with pytest.raises(NotIdle):
        chat.r.accept("t1", "接着说")
    v = chat.answer({"answer": "包含"})
    assert v["status"] == "idle" and v["report"]["version"] == 1
    (turn,) = v["turns"]  # 回答不另起一轮
    ask = next(i for i in turn["items"] if i["type"] == "ask")
    assert ask["answer"] == "包含" and not ask["pending"] and not ask["skipped"]
    assert _kinds(turn)[:2] == ["thinking", "ask"] and turn["record"]["version"] == 1
    assert {"type": "item", "text": f"{ASK} → 包含"} in v["report"]["blocks"]  # 用户的回答写进报告


def test_skipped_ask_is_kept(chat):
    chat.say("问一下 直接答")
    v = chat.answer({"skip": True})
    ask = next(i for i in v["turns"][0]["items"] if i["type"] == "ask")
    assert ask["skipped"] and ask["answer"] is None and v["status"] == "idle"


def test_two_asks_in_one_batch_only_first_shown(chat):
    v = chat.say("双问 直接答")
    assert v["interrupt"]["title"] == "同批第 1 问？"
    v = chat.answer({"answer": "A"})
    asks = [i for i in v["turns"][0]["items"] if i["type"] == "ask"]
    assert [(a["question"], a["answer"]) for a in asks] == [("同批第 1 问？", "A")] and v["status"] == "idle"


@pytest.mark.parametrize("bad", [{"action": "approve"}, {}, {"answer": "包含", "skip": True}, {"answer": "   "}, {"skip": False}])
def test_malformed_answer_rejected(chat, bad):
    chat.say("问一下 直接答")
    it = chat.r.pending("t1")
    with pytest.raises(InvalidAnswer):
        chat.r.resume_input("t1", bad, it.id)
    assert chat.r.pending("t1").id == it.id


def test_stale_interrupt_rejected(chat):
    chat.say("问一下 直接答")
    with pytest.raises(StaleInterrupt):
        chat.r.resume_input("t1", {"answer": "包含"}, "not-this-card")
    chat.answer({"answer": "包含"})
    with pytest.raises(StaleInterrupt):
        chat.r.resume_input("t1", {"answer": "包含"}, None)


def test_ask_resume_matches_multiple_requests():
    pending = {"action_requests": [{"name": "ask_user", "args": {"question": "a"}}, {"name": "ask_user", "args": {"question": "b"}}]}
    d = ask_resume("答", pending=pending)["decisions"]
    assert [json.loads(x["message"]).get("answer") for x in d] == ["答", None] and json.loads(d[1]["message"])["unshown"] is True
    (skip,) = ask_resume(None, skip=True)["decisions"]
    assert skip["type"] == "respond" and json.loads(skip["message"])["skipped"] is True


# ---- 工具出错、模型出错 ----


def test_two_report_writes_in_one_batch_only_first_runs(chat):
    v = chat.say("调研 连写")
    results = [m for m in chat.values()["messages"] if isinstance(m, ToolMessage) and m.name == "write_report"]
    assert [m.status for m in results] == ["success", "error"] and v["report"]["version"] == 1
    # 被拦下的那个是系统替模型纠错：实时没报进度，存下来的对话里也不列
    assert [i["kind"] for i in v["turns"][0]["items"] if i["type"] == "step"] == ["search", "read", "report"]
    assert [e["data"]["step"]["kind"] for e in chat.events if e["event"] == "step"].count("report") == 2


def test_consolidate_failure_is_a_tool_error_not_a_failed_run(tmp_path):
    p = FakeResearchPlanner()
    p.fail_consolidate = True
    v = Chat(tmp_path, p).say("调研一下局部修改")
    step = next(i for i in v["turns"][0]["items"] if i["type"] == "step" and i["kind"] == "report")
    assert not step["ok"] and "整理失败" in step["message"]
    assert v["status"] == "idle" and v["report"] is None and v["versions"] == []


def test_invalid_tool_call_is_retried_with_the_error(chat):
    v = chat.say("坏JSON 直接答")
    msgs = chat.values()["messages"]
    bad = next(m for m in msgs if isinstance(m, AIMessage) and m.invalid_tool_calls)
    # 无效的调用在下一次请求里仍是 tool_calls：重发前先有一条带解析错误的结果，接口才不报错
    res = next(m for m in msgs if isinstance(m, ToolMessage) and m.tool_call_id == bad.invalid_tool_calls[0]["id"])
    assert res.status == "error" and "不是合法的 JSON" in res.content
    assert msgs.index(res) == msgs.index(bad) + 1
    assert v["status"] == "idle" and v["turns"][0]["items"][-1]["text"] == "直接回答：这个问题不用查。"
    assert not [i for i in v["turns"][0]["items"] if i["type"] == "step"]


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
    assert fix.tool_call_id == "c2" and fix.status == "error"


def _with_gateway(planner: FakeResearchPlanner, handler) -> FakeResearchPlanner:
    gw = GatewayClient("http://gw", "k", scene="research", transport=httpx.MockTransport(handler))
    planner._gateway, planner._tools = gw, make_research_tools(gw)
    return planner


def test_search_outage_goes_to_the_agent(tmp_path):
    """搜索服务出错变成给 agent 的工具结果：这一步显示失败，这一轮照常结束。"""
    p = _with_gateway(FakeResearchPlanner(), lambda req: httpx.Response(502, text="bad gateway"))
    v = Chat(tmp_path, p).say("比较两款笔记软件")
    step = next(i for i in v["turns"][0]["items"] if i["type"] == "step")
    assert step["kind"] == "search" and not step["ok"] and "502" in step["message"]
    assert v["status"] == "idle" and v["turns"][0]["items"][-1]["text"] == "没查到。"


def test_search_auth_error_fails_the_run(tmp_path):
    """密钥问题不交给 agent：这次运行失败、错误记在检查点里，改好配置后可以重试。"""
    c = Chat(tmp_path, _with_gateway(FakeResearchPlanner(), lambda req: httpx.Response(401, json={"error": "unauthorized"})))
    with pytest.raises(GatewayAuthError):
        c.say("比较两款笔记软件")
    v = c.view()
    assert v["status"] == "failed" and "401" in v["error"]


# ---- 检查点 ----


def test_sqlite_round_trip_and_continue(tmp_path):
    db = tmp_path / "data" / "r.sqlite"
    c = Chat(tmp_path, saver=open_sqlite_checkpointer(db))
    c.say("问一下 调研局部修改")
    c.answer({"answer": "包含"})
    before = c.view()
    again = Chat(tmp_path, saver=open_sqlite_checkpointer(db))  # 像服务重启：新的连接、新的 agent
    after = again.view()
    for key in ("title", "turns", "sources", "report", "versions", "status"):
        assert after[key] == before[key], key
    v = again.say("重写", action={"type": "rewrite_section", "section_id": after["report"]["outline"][0]["id"]})
    assert v["report"]["version"] == 2 and len(v["turns"]) == 2


def test_report_view_does_not_depend_on_the_file(chat, tmp_path):
    chat.say("调研一下局部修改")
    (tmp_path / "out" / "t1" / "report_v1.md").unlink()
    v = chat.view()
    assert v["report"]["version"] == 1 and v["report"]["blocks"]
    v = chat.say("重写", action={"type": "rewrite_section", "section_id": v["report"]["outline"][0]["id"]})
    assert (tmp_path / "out" / "t1" / "report_v2.md").exists()


# ---- 流式片段合并 ----


def test_stream_buffer_merges_deltas_and_counts_report_chars():
    buf = StreamBuffer()
    out = []
    for part in ("一", "二", "三"):
        out += buf.add(AIMessageChunk(content=part, id="m1"))
    out += buf.add(AIMessageChunk(content="", id="m1", tool_call_chunks=[
        {"name": "write_report", "args": '{"draft": "## 一节', "id": "w1", "index": 0, "type": "tool_call_chunk"}]))
    out += buf.flush()
    deltas = [e["data"] for e in out if e["event"] == "delta"]
    assert "".join(d["text"] for d in deltas) == "一二三" and len(deltas) == 1  # 80ms 内的片段并成一条
    (w,) = [e["data"] for e in out if e["event"] == "writing"]
    assert w["tool"] == "write_report" and w["chars"] == len('{"draft": "## 一节')
    assert buf.flush() == []


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


def test_url_variants_get_one_id(chat):
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
        return tools[name].invoke({"name": name, "args": {**args, "runtime": rt}, "id": f"c{len(msgs)}", "type": "tool_call"})

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


# ---- 报告的编号和校验 ----


def test_sources_numbered_by_first_appearance():
    """编号按正文里第一次出现的先后（节按大纲顺序），不按来源 id；没被正文引用的来源不编号。"""
    sources = [Source(id=f"s{i}", url=f"https://{i}.test/", status="adopted") for i in range(1, 6)]
    claims = [Claim(id="k1", statement="a", kind="fact", source_ids=["s4", "s2"], outline_id="c2"),
              Claim(id="k2", statement="b", kind="fact", source_ids=["s5"], outline_id="c1"),
              Claim(id="k3", statement="c", kind="fact", source_ids=["s1", "s2"], outline_id="c2")]
    sections = {"c2": SectionText(outline_id="c2", sentences=[Sentence(text="a", claim_id="k1"), Sentence(text="c", claim_id="k3")]),
                "c1": SectionText(outline_id="c1", sentences=[Sentence(text="b", claim_id="k2")])}
    outline = [OutlineNode(id="c1", title="一", question="?"), OutlineNode(id="c2", title="二", question="?")]
    blocks = report_blocks("T", outline, sections, claims, sources, [])
    assert [b["marks"] for b in blocks if b["type"] == "sentence" and b.get("claim_id")] == [["1"], ["2", "3"], ["3", "4"]]
    assert [(b["label"], b["source_id"]) for b in blocks if b["type"] == "source"] == [("1", "s5"), ("2", "s4"), ("3", "s2"), ("4", "s1")]


def test_untitled_source_listed_once():
    sources = [Source(id="s1", url="https://a.test/x.pdf"), Source(id="s2", url="https://b.test/", title="B 站")]
    claims = [Claim(id="k1", statement="a", kind="fact", source_ids=["s1", "s2"], outline_id="c1")]
    sections = {"c1": SectionText(outline_id="c1", sentences=[Sentence(text="a", claim_id="k1")])}
    blocks = report_blocks("T", [OutlineNode(id="c1", title="一", question="?")], sections, claims, sources, [])
    assert [ln for ln in to_markdown(blocks).splitlines() if ln.startswith("[")] == ["[1] https://a.test/x.pdf", "[2] B 站 — https://b.test/"]


def test_time_issues_judged_by_model(tmp_path):
    """来源在不在时间范围里由整理的模型判断；校验只看正文用到的，说明里写报告里的编号。"""
    p = FakeResearchPlanner()
    c = Chat(tmp_path, p)
    first = c.say("调研一下局部修改")["report"]
    sid = next(iter(_labels(first)))
    p.time_issues = [TimeIssue(source_id=sid, reason="发布于 2023 年，不在 2026 年内"), TimeIssue(source_id="s000000", reason="不存在")]
    r = c.say("重写", action={"type": "rewrite_section", "section_id": first["outline"][0]["id"]})["report"]
    check = next(x for x in r["checks"] if x["name"] == "scope_time")
    assert not check["passed"] and check["detail"] == f"[{_labels(r)[sid]}]：发布于 2023 年，不在 2026 年内；"
    assert r["time_issues"] == {sid: "发布于 2023 年，不在 2026 年内"}


def test_time_check_ignores_uncited_sources():
    sources = [Source(id="s1", url="u1", title="t", retrieved_at="x", status="adopted"), Source(id="s2", url="u2")]
    claims = [Claim(id="k1", statement="a", kind="fact", source_ids=["s1"], outline_id="c1")]
    sections = {"c1": SectionText(outline_id="c1", sentences=[Sentence(text="a", claim_id="k1")])}
    assert {c.name: c for c in run_checks(sections, claims, sources, {"s2": "太旧"})}["scope_time"].passed
    res = {c.name: c for c in run_checks(sections, claims, sources, {"s1": "太旧"}, {"s1": "1"})}
    assert not res["scope_time"].passed and res["scope_time"].detail == "[1]：太旧；"
