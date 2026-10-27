"""调研场景离线测试：假模型按固定脚本调工具，网关用 MockTransport。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from app.cli import interrupt_view, pending_interrupt, research_resume_payload
from app.graph.research.agent import (
    SKIP_MESSAGE,
    UNSHOWN_MESSAGE,
    ask_resume,
    build_agent,
    collect_sources,
    is_ask_interrupt,
)
from app.graph.research.graph import build_graph, memory_checkpointer, open_sqlite_checkpointer
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
    assert p["type"] == "research.outline"
    r = resume(graph, tid, {"action": "approve"})
    p = intr(r)
    assert p["type"] == "research.review"
    return p


def test_full_flow_traceable_and_exported(graph, tmp_path):
    p = run_to_review(graph)
    assert p["version"] == 1
    assert all(c["passed"] for c in p["checks"]), p["checks"]
    sources = {s["id"]: s for s in p["sources"]}
    claims = p["claims"]
    # 每条论断都能回指到已采纳的来源；事实 / 推断可辨
    assert len(claims) == 4 and {c["outline_id"] for c in claims} == {"c1", "c2"}
    for c in claims:
        assert c["source_ids"] and all(sources[s]["status"] == "adopted" for s in c["source_ids"])
    assert {c["kind"] for c in claims} == {"fact", "inference"}
    # 读过正文的来源用正文片段，其他是搜索摘要
    kinds = {s["excerpt_kind"] for s in sources.values()}
    assert kinds == {"body", "summary"}
    # 无来源的论断被丢弃并记录
    assert any("丢弃无来源论断" in n for n in p["notes"])
    # 报告里推断句带标记并带引用标号；来源表完整
    report = p["report"]
    assert "推断：" in report and "[S1]" in report and "## 来源" in report
    # 动态提问的回答进了假设
    assert any("包含" in a for a in p["assumptions"])

    r = resume(graph, "t1", {"action": "approve"})
    exp = r["exported"]
    pkg = Path(exp["dir"])
    assert {f.name for f in pkg.iterdir()} == {"manifest.json", "report.md", "items.json", "sources.json"}
    items = json.loads((pkg / "items.json").read_text())
    kinds = {i["kind"] for i in items}
    assert {"scope", "outline", "claim.fact", "claim.inference"} <= kinds
    assert all(i["status"] == "confirmed" for i in items if i["kind"].startswith("claim."))
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
    # 报告里不再出现这条论断，校验仍全部通过；只重写了受影响的章
    assert fact["statement"] not in p2["report"]
    assert all(c["passed"] for c in p2["checks"]), p2["checks"]
    assert "重写 c1" in p2["title"] or p2["version"] == 2
    r = resume(graph, "t1", {"action": "approve"})
    items = json.loads(Path(r["exported"]["items"]).read_text())
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


def test_outline_edit_researches_new_section_only(graph, planner):
    tid = "t1"
    r = graph.invoke({"request": REQUEST}, cfg(tid))
    r = resume(graph, tid, {"answers": {"q1": "改文字"}})
    r = resume(graph, tid, {"answer": "包含"})
    p = intr(r)
    assert p["type"] == "research.outline"
    nodes = [{"id": "c1", "title": "局部修改机制（改）", "question": "怎么做？"}, {"id": None, "title": "新章", "question": "新问题？"}]
    r = resume(graph, tid, {"action": "edit", "nodes": nodes})
    p = intr(r)
    assert p["type"] == "research.outline"
    ids = [n["id"] for n in p["nodes"]]
    assert ids == ["c1", "c3"] and p["nodes"][0]["title"] == "局部修改机制（改）"
    # c1 的论断保留，c2 的论断被丢掉，c3 新调研出论断
    assert {c["outline_id"] for n in p["nodes"] for c in n["claims"]} == {"c1", "c3"}
    assert all(n["claims"] for n in p["nodes"])


def test_skip_ask_records_assumption(graph):
    p = run_to_review(graph, answer={"skip": True})
    assert any(SKIP_MESSAGE in a for a in p["assumptions"])


def test_invalid_scope_rejected(graph):
    graph.invoke({"request": REQUEST}, cfg("t1"))
    with pytest.raises(ValueError, match="不是当前问题"):
        resume(graph, "t1", {"answers": {"nope": "x"}})


def test_clear_request_skips_scope_card(graph):
    """需求清楚就不弹范围卡：第一个中断直接是调研中的提问。"""
    r = graph.invoke({"request": "最近新加坡的天气情况如何？"}, cfg("t1"))
    assert is_ask_interrupt(intr(r))
    v = graph.get_state(cfg("t1")).values
    assert v["scope_confirmed"] and v["clarify"] == [] and v["scope"].extra is None


def test_clarify_answer_lands_in_scope_and_skip_records_assumption(graph):
    p = run_to_review(graph)
    assert "改文字" in p["report"]
    r = graph.invoke({"request": REQUEST}, cfg("t2"))
    r = resume(graph, "t2", {"answers": {"q1": None}})
    assert is_ask_interrupt(intr(r))
    assert any("未回答" in a for a in graph.get_state(cfg("t2")).values["assumptions"])


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
    assert intr(r)["type"] == "research.outline"
    r = resume(g2, "t1", {"action": "approve"})
    assert intr(r)["type"] == "research.review"


def test_build_agent_without_ask_drops_tool(planner):
    agent = build_agent(planner.agent_model(), planner.tools(), "sys", allow_ask=False)
    names = {t.name for t in agent.get_graph().nodes["tools"].data.tools_by_name.values()} if "tools" in agent.get_graph().nodes else set()
    assert "ask_user" not in names


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
    r = resume(graph, "t1", {"action": "approve"})
    assert r["exported"]
