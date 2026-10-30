"""调研工作台用到的接口：任务列表、存下来的过程记录、进行中这一轮、进度事件、每一版是按什么改的、选中一段再说的话。"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
import httpx
import pytest

from app.api import main as api_main
from app.api.research_runner import ResearchRunner
from app.graph.research.graph import memory_checkpointer, open_sqlite_checkpointer, thread_ids
from app.research_llm import LiveResearchPlanner, ReviewOp, ReviewPlan
from tests.test_api_research import _events, _last, _resume

REQ = "AI 海报工具在局部修改上的现状（2026）"  # 假模型：先问一个澄清问题，调研到一半再问一次


@pytest.fixture
def client(tmp_path):
    api_main.set_research_runner(ResearchRunner(fake=True, data_dir=tmp_path / "data", out_dir=tmp_path / "out"))
    return TestClient(api_main.app)


def _to_review(client) -> dict:
    ev = _events(client.post("/api/research/threads", json={"request": REQ}))
    ev = _events(_resume(client, _last(ev, "state"), {"answers": {"q1": "改文字"}}))
    ev = _events(_resume(client, _last(ev, "state"), {"answer": "包含"}))
    return _last(ev, "state")


def test_list_threads_with_status(client):
    waiting = _last(_events(client.post("/api/research/threads", json={"request": REQ})), "state")
    done = _last(_events(client.post("/api/research/threads", json={"request": "最近新加坡的天气情况如何？"})), "state")
    rows = client.get("/api/research/threads").json()["threads"]
    by_id = {r["thread_id"]: r for r in rows}
    assert set(by_id) == {waiting["thread_id"], done["thread_id"]}
    assert rows[0]["thread_id"] == done["thread_id"]  # 最近有动静的在前
    w = by_id[waiting["thread_id"]]
    assert w["status"] == "waiting" and w["waiting"] == "scope" and w["questions"] == 1 and w["version"] is None
    assert w["title"] == waiting["scope"]["question"] and w["request"] == REQ and w["updated_at"]
    d = by_id[done["thread_id"]]
    assert d["waiting"] == "review" and d["version"] == 1 and d["running_since"] is None


def test_round_steps_are_kept_in_state(client):
    """每轮调研的搜索、读页、提问存进线程状态，刷新或换个页面打开都还在；来源编号和来源列表对得上。"""
    st = _to_review(client)
    assert st["clarify_answers"] == {"q1": "改文字"} and [q["id"] for q in st["clarify"]] == ["q1"]
    assert len(st["rounds"]) == 1 and st["current_round"] is None
    rnd = st["rounds"][0]
    assert rnd["gap"] is None and rnd["answer_how"] == "agent" and rnd["new_sources"] == 3 and rnd["ended_at"]
    kinds = [s["kind"] for s in rnd["steps"]]
    assert "search" in kinds and "read" in kinds and "ask" in kinds
    ids = {s["id"] for s in st["sources"]}
    search = next(s for s in rnd["steps"] if s["kind"] == "search")
    assert search["ok"] and search["hits"] == 3 and search["source_ids"] and set(search["source_ids"]) <= ids
    reads = [s for s in rnd["steps"] if s["kind"] == "read"]
    assert any(r["body"] and r["source_ids"] for r in reads)
    assert all(not r["source_ids"] for r in reads if not r["body"])  # 没读到正文的不算得到了来源
    ask = next(s for s in rnd["steps"] if s["kind"] == "ask")
    assert ask["ok"] and ask["message"] == "包含" and ask["text"]


def test_gap_round_records_its_gap(client):
    st = _last(_events(client.post("/api/research/threads", json={"request": "某题（回补，补查失败）"})), "state")
    assert [(r["gap"], r["answer_how"]) for r in st["rounds"]] == [(None, "agent"), ("缺价格数据", "kept")]


def test_current_round_while_waiting_on_ask(client):
    """停在调研途中的提问上：已经做过的搜索、读页从 agent 的检查点里取，来源编号和这一轮结束后入库的一致。"""
    ev = _events(client.post("/api/research/threads", json={"request": REQ}))
    st = _last(_events(_resume(client, _last(ev, "state"), {"answers": {"q1": "改文字"}})), "state")
    assert st["interrupt"]["type"] == "research.ask" and st["rounds"] == []
    cur = st["current_round"]
    assert cur["gap"] is None and {s["kind"] for s in cur["steps"]} >= {"search", "read"}
    early = {s["url"]: s["id"] for s in cur["sources"]}
    assert early and st["sources"] == []  # 只是先算出来给页面看，没有写进状态

    st = _last(_events(_resume(client, st, {"answer": "包含"})), "state")
    assert {s["url"]: s["id"] for s in st["sources"]}.items() >= early.items()
    assert [s["id"] for s in cur["steps"]] == [s["id"] for s in st["rounds"][0]["steps"]][: len(cur["steps"])]


def test_tool_progress_has_start_and_done(client):
    """每次搜索、读页先报“开始”，做完再报一条带记录的；两条的 id 相同，页面据此合并。"""
    ev = _events(client.post("/api/research/threads", json={"request": REQ}))
    ev = _events(_resume(client, _last(ev, "state"), {"answers": {"q1": "改文字"}}))
    acts = [d for e, d in ev if e == "progress" and d.get("kind") in ("search", "read")]
    starts = [a for a in acts if not a.get("done")]
    dones = {a["id"]: a for a in acts if a.get("done")}
    assert starts and {a["id"] for a in starts} == set(dones)
    search = next(dones[a["id"]] for a in starts if a["kind"] == "search")
    assert search["step"]["hits"] == 3 and len(search["found"]) == 3 and all(f["url"] and not f["body"] for f in search["found"])
    read = next(d for d in dones.values() if d["kind"] == "read" and d["step"]["body"])
    assert read["found"] == [{"url": read["found"][0]["url"], "title": "正文标题", "body": True}]


def test_tool_step_records():
    """失败的读页、出错的搜索照记（带原因）；被额度拦下的、没呈现的提问不记。"""
    import json

    from langchain_core.messages import ToolMessage

    from app.graph.research.agent import tool_step

    def msg(name, content, status="success"):
        return ToolMessage(content=content if isinstance(content, str) else json.dumps(content), name=name, tool_call_id="t", status=status)

    read = {"name": "read_page", "args": {"url": "https://a.test/x"}, "id": "t"}
    gone = tool_step(read, msg("read_page", {"ok": False, "url": "https://a.test/x", "kind": "page_not_found", "message": "gone"}))
    assert not gone.ok and not gone.body and gone.message == "gone" and gone.source_ids == []
    ok = tool_step(read, msg("read_page", {"ok": True, "url": "https://A.test/x#top", "title": "T", "content": "正文"}), {"https://a.test/x": "s4"})
    assert ok.body and ok.title == "T" and ok.source_ids == ["s4"]  # 网址写法不同也对得上来源
    assert tool_step(read, msg("read_page", "额度用完了", status="error")) is None

    search = {"name": "search_web", "args": {"query": "q", "recency_days": 30, "domains": ["a.test"]}, "id": "t"}
    err = tool_step(search, msg("search_web", {"ok": False, "kind": "upstream", "message": "超时"}, status="error"))
    assert not err.ok and err.message == "超时" and err.limits == ["近 30 天", "a.test"]
    assert tool_step(search, msg("search_web", "额度用完了", status="error")) is None

    ask = {"name": "ask_user", "args": {"question": "问？"}, "id": "t"}
    assert tool_step(ask, msg("ask_user", {"unshown": True, "note": "x"})) is None
    skipped = tool_step(ask, msg("ask_user", {"skipped": True, "note": "x"}))
    assert skipped.kind == "ask" and not skipped.ok and skipped.message == ""
    assert tool_step(ask, None) is None


def test_each_version_says_what_it_was_for(client):
    st = _to_review(client)
    assert st["versions"][0]["asked"] == ""  # 初稿
    sid = next(c for c in st["claims"] if c["source_ids"])["source_ids"][0]
    st = _last(_events(_resume(client, st, {"action": "exclude_source", "target": sid})), "state")
    v2 = st["versions"][-1]
    title = next(s["title"] or s["url"] for s in st["sources"] if s["id"] == sid)
    # 写来源标题，不写报告编号（编号每版重新编，换一版就对不上了）
    assert v2["n"] == 2 and v2["asked"] == f"排除来源「{title}」" and v2["understood"] == ""

    api_main.research().planner.review_plans = [
        ReviewPlan(ops=[ReviewOp(action="rewrite_section", target="c1", content=None, instruction="更简短")], reply=None)]
    st = _last(_events(_resume(client, st, {"action": "instruct", "text": "第一节改短"})), "state")
    v3 = st["versions"][-1]
    assert v3["asked"] == "第一节改短" and v3["understood"].startswith("重写「")

    api_main.research().planner.review_plans = [ReviewPlan(ops=[], reply="你说的是哪一节？")]
    st = _last(_events(_resume(client, st, {"action": "instruct", "text": "改改"})), "state")
    assert len(st["versions"]) == 3 and st["interrupt"]["reply"] == "你说的是哪一节？"  # 没对上：不出新版，也不留下“改版原因”

    k = next(c for c in st["claims"] if not c["orphaned"])
    st = _last(_events(_resume(client, st, {"action": "edit_claim", "target": k["id"], "content": "新的说法"})), "state")
    assert st["versions"][-1]["asked"] == f"把「{k['statement'][:40]}{'…' if len(k['statement']) > 40 else ''}」改成：新的说法"
    assert k["id"] not in st["versions"][-1]["asked"]  # 不露内部 id


def test_quote_goes_to_the_model(client):
    """选中一段再说的话：选中的原文和它所在的论断、节一起交给模型理解。"""
    st = _to_review(client)
    sent = next(b for b in st["report_blocks"] if b["type"] == "sentence" and b["claim_id"])
    heading = next(b for b in st["report_blocks"] if b["type"] == "heading" and b.get("section_id"))
    claim = next(c for c in st["claims"] if c["id"] == sent["claim_id"])
    quote = {"text": sent["text"][:10], "claim_ids": [claim["id"]], "section_ids": [claim["outline_id"]]}
    planner = api_main.research().planner
    planner.review_plans = [ReviewPlan(ops=[ReviewOp(action="rewrite_section", target=claim["outline_id"], content=None,
                                                     instruction="写明年份")], reply=None)]
    st = _last(_events(_resume(client, st, {"action": "instruct", "text": "说明是哪年的数据", "quote": quote})), "state")
    assert [c for c in planner.calls if c[0] == "interpret"][-1] == ("interpret", "说明是哪年的数据", quote)
    assert heading["section_id"] == "c1"
    assert st["versions"][-1]["asked"] == f"「{quote['text']}」说明是哪年的数据"


def test_quote_must_point_at_the_card(client):
    st = _to_review(client)
    for bad in ({"text": "x", "claim_ids": ["k999"]}, {"text": "x", "section_ids": ["c9"]}, {"text": ""}):
        r = _resume(client, st, {"action": "instruct", "text": "改一下", "quote": bad})
        assert r.status_code == 400, bad
    r = _resume(client, st, {"action": "exclude_source", "target": st["sources"][0]["id"], "quote": {"text": "x"}})
    assert r.status_code == 400 and "quote" in r.json()["detail"]  # 只有直接说的话能带选中位置


def test_thread_ids_without_reading_checkpoints(tmp_path):
    saver = open_sqlite_checkpointer(tmp_path / "c.sqlite")
    assert thread_ids(saver) == []  # 表还没建
    assert thread_ids(memory_checkpointer()) == []


def test_reply_without_structured_output_raises():
    """模型直接回一段文字、没调结构化输出的工具：要报错交给重试和失败处理，不能返回 None。"""
    from langchain_deepseek import ChatDeepSeek

    def reply(content: str | None, tool_args: dict | None = None):
        msg = {"role": "assistant", "content": content}
        if tool_args is not None:
            msg["tool_calls"] = [{"id": "t1", "type": "function", "function": {"name": "ReviewPlan", "arguments": json.dumps(tool_args)}}]
        return {"id": "x", "object": "chat.completion", "created": 0, "model": "m",
                "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if tool_args else "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}

    answers = [reply("好的，我来改。"), reply(None, {"ops": [], "reply": "没对上"})]
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=answers.pop(0)))
    llm = ChatDeepSeek(model="m", api_key="k", api_base="http://model.test/v1", max_retries=0,
                       http_client=httpx.Client(transport=transport))
    chain = LiveResearchPlanner._structured(llm, ReviewPlan)
    with pytest.raises(RuntimeError, match="没有按结构返回"):
        chain.invoke("改一下")
    assert chain.invoke("改一下") == ReviewPlan(ops=[], reply="没对上")
