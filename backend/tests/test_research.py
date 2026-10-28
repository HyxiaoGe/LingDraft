"""调研场景离线测试：假模型按固定脚本调工具，网关用 MockTransport。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

from app.cli import interrupt_view, pending_interrupt, research_resume_payload
from app.graph.research.agent import (
    ASK_BLOCKED,
    DANGLING_NOTE,
    SKIP_MESSAGE,
    UNSHOWN_MESSAGE,
    answer_dangling_calls,
    ask_resume,
    build_agent,
    collect_sources,
    final_answer_text,
    is_ask_interrupt,
)
from app.graph.research.graph import build_graph, memory_checkpointer, open_sqlite_checkpointer
from app.graph.research.state import MAX_ASKS, MAX_SEARCHES
from app.research_llm import FakeResearchPlanner

REQUEST = "AI 海报工具在局部修改上的现状（2026）"


def cfg(tid: str) -> dict:
    return {"configurable": {"thread_id": tid}}


def intr(result: dict) -> dict | None:
    ints = result.get("__interrupt__") or []
    return ints[0].value if ints else None


@pytest.fixture
def planner() -> FakeResearchPlanner:
    return FakeResearchPlanner()


@pytest.fixture
def graph(planner, tmp_path: Path):
    return build_graph(planner, memory_checkpointer(), tmp_path / "out")


def resume(graph, tid: str, payload: dict) -> dict:
    """像 CLI / 接口层那样：先看挂起的是不是 agent 提问，再转换回传格式。"""
    pending = pending_interrupt(graph, cfg(tid))
    return graph.invoke(Command(resume=research_resume_payload(pending, payload)), cfg(tid))


def run_to_review(graph, tid: str = "t1", answer: dict | None = None) -> dict:
    r = graph.invoke({"request": REQUEST}, cfg(tid))
    p = intr(r)
    assert p["type"] == "research.scope"
    assert p["scope"]["question"] and [q["id"] for q in p["questions"]] == ["q1"] and p["questions"][0]["why"]
    r = resume(graph, tid, {"answers": {"q1": "改文字"}})
    p = intr(r)
    assert is_ask_interrupt(p)
    view = interrupt_view(p)
    assert view["type"] == "research.ask" and view["options"] == ["包含", "不包含"] and view["why"]
    r = resume(graph, tid, answer or {"answer": "包含"})
    p = intr(r)
    assert p["type"] == "research.review"
    return p


def test_full_flow_traceable_and_exported(graph, tmp_path):
    p = run_to_review(graph)
    assert p["version"] == 1 and p["answer_how"] == "agent"
    assert all(c["passed"] for c in p["checks"]), p["checks"]
    sources = {s["id"]: s for s in p["sources"]}
    claims = p["claims"]
    # 整理步骤按候选答案分了两节；每条论断都能回指到系统选用的来源；事实 / 推断可辨
    assert [s["id"] for s in p["sections"]] == ["c1", "c2"]
    assert len(claims) == 3 and {c["outline_id"] for c in claims} == {"c1", "c2"}
    for c in claims:
        assert c["source_ids"] and all(sources[s]["status"] == "adopted" for s in c["source_ids"])
    assert {c["kind"] for c in claims} == {"fact", "inference"}
    # 读过正文的来源用正文片段，其他是搜索摘要
    assert {s["excerpt_kind"] for s in sources.values()} == {"body", "summary"}
    # 无来源的论断被丢弃并记录；挂在它上面的句子也从正文移除，不能变成“不需要引用的过渡句”
    assert any("丢弃无来源论断" in n for n in p["notes"])
    assert "没有来源的论断应被丢弃" not in p["report"] and any("一起从正文移除" in n for n in p["notes"])
    # 报告里推断句带标记并带引用标号；来源表完整
    report = p["report"]
    assert "推断：" in report and "[S1]" in report and "## 来源" in report
    # 动态提问的回答是用户确认的前提，进报告单独一节、不进假设；澄清的回答进了范围
    assert "## 用户确认的前提" in report and "范围是否包含 PPT 工具？ → 包含" in report
    assert not any("包含" in a for a in p["assumptions"])
    assert "改文字" in report

    # 校验过就已经导出，不等批准；论断状态照实导出（系统交付不等于人工确认）
    exp = p["exported"]
    assert exp and exp["version"] == 1
    pkg = Path(exp["dir"])
    assert {f.name for f in pkg.iterdir()} == {"manifest.json", "report.md", "items.json", "sources.json"}
    items = json.loads((pkg / "items.json").read_text())
    kinds = {i["kind"] for i in items}
    assert {"scope", "outline", "claim.fact", "claim.inference"} <= kinds
    assert all(i["status"] == "draft" for i in items if i["kind"].startswith("claim."))
    r = resume(graph, "t1", {"action": "approve"})
    assert r["approved"] and r["exported"]["version"] == 1 and graph.get_state(cfg("t1")).next == ()
    adopted = json.loads((pkg / "sources.json").read_text())
    assert adopted and all(s["status"] == "adopted" for s in adopted)
    manifest = json.loads((pkg / "manifest.json").read_text())
    assert manifest["scenario"] == "research" and manifest["thread_id"] == "t1"


def test_exclude_source_orphans_dependent_claim(graph):
    p = run_to_review(graph)
    fact = next(c for c in p["claims"] if c["kind"] == "fact" and c["outline_id"] == "c1")
    sid = fact["source_ids"][0]
    r = resume(graph, "t1", {"action": "exclude_source", "target": sid})
    p2 = intr(r)
    assert p2["type"] == "research.review" and p2["version"] == 2
    src = next(s for s in p2["sources"] if s["id"] == sid)
    assert src["status"] == "excluded"
    orphan = next(c for c in p2["claims"] if c["id"] == fact["id"])
    assert orphan["orphaned"] and orphan["status"] == "draft" and orphan["source_ids"] == []
    assert any(fact["id"] in n and "失去来源支撑" in n for n in p2["notes"])
    # 报告里不再出现这条论断，校验仍全部通过；只重写了受影响的节
    assert fact["statement"] not in p2["report"]
    assert all(c["passed"] for c in p2["checks"]), p2["checks"]
    # 改完立刻重新导出，第 2 版覆盖第 1 版
    assert p2["exported"]["version"] == 2
    items = json.loads(Path(p2["exported"]["items"]).read_text())
    assert fact["id"] not in {i["id"] for i in items}


def test_edit_claim_marks_user_edited_and_rewrites(graph, planner):
    p = run_to_review(graph)
    target = p["claims"][0]
    r = resume(graph, "t1", {"action": "edit_claim", "target": target["id"], "content": "用户改过的论断"})
    p2 = intr(r)
    c = next(c for c in p2["claims"] if c["id"] == target["id"])
    assert c["status"] == "user_edited" and c["statement"] == "用户改过的论断"
    assert "用户改过的论断" in p2["report"]
    assert [x for x in planner.calls if x[0] == "write"][-1][1] == target["outline_id"]


def test_rewrite_section_with_instruction(graph, planner):
    run_to_review(graph)
    r = resume(graph, "t1", {"action": "rewrite_section", "target": "c2", "instruction": "更简短"})
    p = intr(r)
    assert p["version"] == 2 and "更简短" in p["report"]
    writes = [x for x in planner.calls if x[0] == "write"]
    assert writes[-1] == ("write", "c2", "更简短")
    # 初稿没有调用重写：直接拼装整理结果
    assert len(writes) == 1


def test_unclear_answer_is_not_a_confirmed_premise(graph):
    p = run_to_review(graph, answer={"answer": "尚不清楚，先按标注的假设给建议"})
    assert any("尚不清楚" in a and "按模型假设处理" in a for a in p["assumptions"])
    assert "## 用户确认的前提" not in p["report"]


def test_skip_ask_records_assumption(graph):
    p = run_to_review(graph, answer={"skip": True})
    assert any("用户跳过了" in a and "PPT" in a for a in p["assumptions"])
    assert "## 用户确认的前提" not in p["report"]


def test_invalid_scope_rejected(graph):
    graph.invoke({"request": REQUEST}, cfg("t1"))
    with pytest.raises(ValueError, match="不是当前问题"):
        resume(graph, "t1", {"answers": {"nope": "x"}})


def test_simple_question_one_section_no_cards(graph):
    """需求清楚、问题简单：不弹范围卡，agent 不提问，整理成一节，直接到终审。"""
    r = graph.invoke({"request": "最近新加坡的天气情况如何？"}, cfg("t1"))
    p = intr(r)
    assert p["type"] == "research.review"
    v = graph.get_state(cfg("t1")).values
    assert v["scope_confirmed"] and v["clarify"] == [] and v["asks_used"] == 0
    assert [s["id"] for s in p["sections"]] == ["c1"] and len(p["claims"]) == 2


def test_clarify_skip_records_assumption(graph):
    r = graph.invoke({"request": REQUEST}, cfg("t2"))
    r = resume(graph, "t2", {"answers": {"q1": None}})
    assert is_ask_interrupt(intr(r))
    assert any("未回答" in a for a in graph.get_state(cfg("t2")).values["assumptions"])


def test_gap_fill_runs_research_once_more(graph, planner):
    r = graph.invoke({"request": "某题（回补）"}, cfg("t1"))
    p = intr(r)
    assert p["type"] == "research.review"
    v = graph.get_state(cfg("t1")).values
    assert v["gap_rounds"] == 1 and v["gap"] is None
    assert sum(1 for c in planner.calls if c[0] == "consolidate") == 2
    assert any("回研究环节补一轮" in n for n in p["notes"])


def test_budget_exhausted_tool_removed_and_still_answers(graph):
    """模型一直搜：搜够上限后工具被摘掉，模型只能作答；执行层没有多执行一次。"""
    r = graph.invoke({"request": "一直搜（不收尾）"}, cfg("t1"))
    p = intr(r)
    assert p["type"] == "research.review" and p["answer_how"] == "agent"
    v = graph.get_state(cfg("t1")).values
    assert len(v["sources"]) == 3 * MAX_SEARCHES


def test_no_text_triggers_controlled_close_out(graph):
    r = graph.invoke({"request": "一直搜（无答案）"}, cfg("t1"))
    p = intr(r)
    assert p["type"] == "research.review" and p["answer_how"] == "close"
    assert any("没有给出文字答案" in n for n in p["notes"])
    check = next(c for c in p["checks"] if c["name"] == "answer_present")
    assert check["passed"] and "收尾" in check["detail"]
    assert "收尾" in graph.get_state(cfg("t1")).values["answer"]


def test_invalid_tool_call_is_retried(graph):
    r = graph.invoke({"request": "题（坏JSON）"}, cfg("t1"))
    p = intr(r)
    assert p["type"] == "research.review" and p["answer_how"] == "agent" and p["claims"]


def test_resume_across_graph_instances_with_sqlite(planner, tmp_path):
    """中断在子图里发生；换一个进程（新的图实例、同一个 sqlite）也要能接着跑。"""
    db = tmp_path / "cp.sqlite"
    g1 = build_graph(planner, open_sqlite_checkpointer(db), tmp_path / "out")
    r = g1.invoke({"request": REQUEST}, cfg("t1"))
    r = resume(g1, "t1", {"answers": {"q1": "改文字"}})
    assert is_ask_interrupt(intr(r))
    g2 = build_graph(FakeResearchPlanner(), open_sqlite_checkpointer(db), tmp_path / "out")
    assert is_ask_interrupt(pending_interrupt(g2, cfg("t1")))
    r = resume(g2, "t1", {"answer": "包含"})
    assert intr(r)["type"] == "research.review"


def test_build_agent_without_ask_drops_tool(planner):
    agent = build_agent(planner.agent_model(), planner.tools(), "sys", ask_limit=0)
    names = {t.name for t in agent.get_graph().nodes["tools"].data.tools_by_name.values()} if "tools" in agent.get_graph().nodes else set()
    assert "ask_user" not in names


def test_final_answer_text_ignores_limit_message_and_tool_calls():
    msgs = [
        AIMessage(content="答案", ),
        AIMessage(content="", tool_calls=[{"name": "search_web", "args": {}, "id": "1", "type": "tool_call"}]),
        AIMessage(content="Model call limits exceeded: run limit (30/30)"),
    ]
    assert final_answer_text(msgs) == "答案"
    assert final_answer_text(msgs[1:]) is None


def test_collect_sources_prefers_body_excerpt():
    hits = [{"url": "https://a.test/1", "title": "A", "description": "摘要", "published_at": "2026-01-01"}]
    msgs = [
        ToolMessage(content=json.dumps(hits), name="search_web", tool_call_id="1"),
        ToolMessage(content=json.dumps({"ok": True, "url": "https://a.test/1", "title": "A", "content": "正文" * 300}), name="read_page", tool_call_id="2"),
        ToolMessage(content=json.dumps({"ok": False, "url": "https://a.test/2", "error": "x"}), name="read_page", tool_call_id="3"),
    ]
    new, mapping = collect_sources(msgs, [], 1)
    assert [s.id for s in new] == ["s1"] and mapping == {"https://a.test/1": "s1"}
    assert new[0].excerpt_kind == "body" and len(new[0].excerpt) == 400 and new[0].published_at == "2026-01-01"


def test_ask_resume_matches_multiple_requests():
    pending = {"action_requests": [{"name": "ask_user", "args": {"question": "a"}}, {"name": "ask_user", "args": {"question": "b"}}]}
    d = ask_resume("答", pending=pending)["decisions"]
    assert [x["message"] for x in d] == ["答", UNSHOWN_MESSAGE]
    assert ask_resume(None, skip=True)["decisions"] == [{"type": "respond", "message": SKIP_MESSAGE}]


def test_exclude_unreferenced_source_returns_to_review(graph):
    p = run_to_review(graph)
    used = {sid for c in p["claims"] for sid in c["source_ids"]}
    unused = next(s["id"] for s in p["sources"] if s["id"] not in used)
    r = resume(graph, "t1", {"action": "exclude_source", "target": unused})
    p2 = intr(r)
    assert p2["type"] == "research.review" and p2["version"] == 1
    assert next(s for s in p2["sources"] if s["id"] == unused)["status"] == "excluded"
    assert any("没有被任何论断引用" in n for n in p2["notes"])
    assert p2["exported"]["version"] == 1  # 没重写就没重新导出
    r = resume(graph, "t1", {"action": "approve"})
    assert r["exported"]["version"] == 1


def test_batch_tool_calls_count_toward_budget(planner):
    """同一批里发 10 个搜索：只执行到额度为止，其余在执行层拦下。"""
    agent = build_agent(planner.agent_model(), planner.tools(), "sys", ask_limit=0)
    res = agent.invoke({"messages": [("user", "题（连发）")]})
    searches = [m for m in res["messages"] if isinstance(m, ToolMessage) and m.name == "search_web"]
    assert len(searches) == 10
    assert sum(1 for m in searches if m.status != "error") == MAX_SEARCHES
    assert all("额度已用完" in m.content for m in searches if m.status == "error")


def test_ask_budget_enforced_before_card(graph):
    """模型无视绑定列表每轮都问：只出 MAX_ASKS 张问题卡，超额的提问在变成卡之前被改成错误结果。"""
    r = graph.invoke({"request": "题（多问）"}, cfg("t1"))
    cards = 0
    while is_ask_interrupt(intr(r)):
        cards += 1
        assert cards <= MAX_ASKS + 1
        r = resume(graph, "t1", {"answer": f"答 {cards}"})
    assert cards == MAX_ASKS
    p = intr(r)
    assert p["type"] == "research.review"
    v = graph.get_state(cfg("t1")).values
    assert v["asks_used"] == MAX_ASKS
    assert all(c["passed"] for c in p["checks"]), p["checks"]


def test_consolidate_failure_cannot_be_exported(graph):
    r = graph.invoke({"request": "题（整理失败）"}, cfg("t1"))
    p = intr(r)
    assert p["type"] == "research.review" and p["claims"] == []
    failed = {c["name"] for c in p["checks"] if not c["passed"]}
    assert {"report_valid", "question_answered"} <= failed
    assert any("整理失败" in n for n in p["notes"])
    # 无效报告不导出确认物包；“不用改了”只是结束
    assert p["exported"] is None
    r = resume(graph, "t1", {"action": "approve"})
    assert r.get("exported") is None


def test_unanswered_without_gap_text_still_refills(graph, planner):
    r = graph.invoke({"request": "题（没回答）"}, cfg("t1"))
    p = intr(r)
    assert p["type"] == "research.review"
    v = graph.get_state(cfg("t1")).values
    assert v["gap_rounds"] == 1 and v["answered"] is True
    assert sum(1 for c in planner.calls if c[0] == "consolidate") == 2
    assert next(c for c in p["checks"] if c["name"] == "question_answered")["passed"]


def test_close_out_history_answers_dangling_tool_calls():
    msgs = [
        HumanMessage("题"),
        AIMessage(content="", tool_calls=[{"name": "search_web", "args": {}, "id": "ok1", "type": "tool_call"}]),
        ToolMessage(content="[]", name="search_web", tool_call_id="ok1"),
        AIMessage(content="", invalid_tool_calls=[{"name": "search_web", "args": "{bad", "id": "bad1", "error": "x", "type": "invalid_tool_call"}]),
    ]
    out = answer_dangling_calls(msgs)
    assert len(out) == 5 and isinstance(out[-1], ToolMessage) and out[-1].tool_call_id == "bad1" and out[-1].content == DANGLING_NOTE
    assert answer_dangling_calls(msgs[:3]) == msgs[:3]
    assert ASK_BLOCKED != "ask_user"
