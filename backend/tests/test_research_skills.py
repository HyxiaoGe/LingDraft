"""调研方法（技能）：目录解析、load_skill 工具，以及选定的方法怎么带到补查、整理和重写。"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app.graph.research import skills
from app.graph.research.agent import collect_skills
from app.graph.research.graph import build_graph, memory_checkpointer
from app.graph.research.nodes import research_system
from app.graph.research.state import Scope
from app.research_llm import FakeResearchPlanner
from app.tools.research_tools import load_skill


def cfg(tid: str) -> dict:
    return {"configurable": {"thread_id": tid}}


def review_card(result: dict) -> dict:
    p = result["__interrupt__"][0].value
    assert p["type"] == "research.review"
    return p


@pytest.fixture
def planner() -> FakeResearchPlanner:
    return FakeResearchPlanner()


@pytest.fixture
def graph(planner, tmp_path: Path):
    return build_graph(planner, memory_checkpointer(), tmp_path / "out")


def test_catalog_lists_every_skill_with_report():
    cat = skills.catalog()
    assert set(cat) == {"comparison", "landscape", "fact-check"}
    for s in cat.values():
        assert s.title and s.description and "报告结构" in s.report and "论断的写法" in s.report
        assert s.content().startswith(s.method.strip()) and s.report.strip() in s.content()
    lines = skills.catalog_text().splitlines()
    assert len(lines) == 3 and lines[0].startswith("- comparison（对比与选型）：")


def _write_skill(root: Path, name: str, frontmatter: str, report: bool = True) -> None:
    d = root / name
    (d / "references").mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n正文\n", encoding="utf-8")
    if report:
        (d / "references" / "report.md").write_text("## 报告结构\n", encoding="utf-8")


@pytest.mark.parametrize("frontmatter, report, error", [
    ("name: other\ndescription: d\nmetadata:\n  title: 标题", True, "同名"),
    ("name: x\nmetadata:\n  title: 标题", True, "description"),
    ("name: x\ndescription: d", True, "metadata.title"),
    ("name: x\ndescription: d\nmetadata:\n  title: 标题", False, "report.md"),
])
def test_broken_skill_fails_loudly(tmp_path, frontmatter, report, error):
    _write_skill(tmp_path, "x", frontmatter, report)
    with pytest.raises(skills.SkillError, match=error):
        skills.catalog(tmp_path)


def test_load_skill_tool_returns_method_and_report():
    assert load_skill.invoke({"name": "comparison"}) == skills.catalog()["comparison"].content()
    msg = load_skill.invoke({"type": "tool_call", "id": "c1", "name": "load_skill", "args": {"name": "nope"}})
    assert msg.status == "error" and "nope" in msg.content and "comparison" in msg.content


def test_collect_skills_only_counts_successful_loads():
    msgs = [
        AIMessage(content="", tool_calls=[{"name": "load_skill", "args": {"name": "nope"}, "id": "a", "type": "tool_call"},
                                          {"name": "load_skill", "args": {"name": "comparison"}, "id": "b", "type": "tool_call"}]),
        ToolMessage(content="没有", name="load_skill", tool_call_id="a", status="error"),
        ToolMessage(content="方法", name="load_skill", tool_call_id="b"),
        AIMessage(content="", tool_calls=[{"name": "load_skill", "args": {"name": "comparison"}, "id": "c", "type": "tool_call"}]),
        ToolMessage(content="方法", name="load_skill", tool_call_id="c"),
    ]
    assert collect_skills(msgs) == ["comparison"]


def test_system_prompt_lists_catalog_until_a_method_is_picked():
    scope = Scope(question="选哪个？")
    first = research_system(scope, allow_ask=True)
    assert skills.catalog_text() in first and "load_skill" in first
    again = research_system(scope, allow_ask=True, loaded=["comparison"])
    assert skills.catalog()["comparison"].content() in again and skills.catalog_text() not in again


def test_picked_method_reaches_consolidate_and_rewrite(graph, planner):
    p = review_card(graph.invoke({"request": "向量库选型"}, cfg("t1")))
    v = graph.get_state(cfg("t1")).values
    report = skills.catalog()["comparison"].report.strip()
    assert v["skills"] == ["comparison"] and planner.consolidate_reports == [report]
    assert "调研方法：对比与选型" in p["notes"]
    graph.invoke(_rewrite(graph, "t1"), cfg("t1"))
    assert planner.write_inputs[-1]["report"] == report


def test_gap_round_keeps_method_without_reloading(graph, planner):
    review_card(graph.invoke({"request": "向量库选型（回补）"}, cfg("t1")))
    v = graph.get_state(cfg("t1")).values
    assert v["gap_rounds"] == 1 and v["skills"] == ["comparison"]
    assert planner.consolidate_reports == [skills.catalog()["comparison"].report.strip()] * 2
    assert v["notes"].count("调研方法：对比与选型") == 1


def test_no_method_or_unknown_method_keeps_plain_flow(graph, planner):
    review_card(graph.invoke({"request": "最近新加坡的天气情况如何？"}, cfg("t1")))
    review_card(graph.invoke({"request": "错方法的题"}, cfg("t2")))
    assert graph.get_state(cfg("t1")).values["skills"] == [] and graph.get_state(cfg("t2")).values["skills"] == []
    assert planner.consolidate_reports == [None, None]


def _rewrite(graph, tid: str):
    from app.cli import research_resume

    return research_resume(graph, cfg(tid), {"action": "rewrite_section", "target": "c1", "instruction": "更简短"})
