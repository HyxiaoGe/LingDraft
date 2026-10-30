"""调研方法（技能）：目录解析、load_skill 工具，以及读过的方法怎么带到整理。"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app.graph.research import skills
from app.graph.research.history import collect_skills
from app.tools.research_tools import load_skill
from tests.test_research import Chat


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


def test_picked_method_reaches_consolidate_and_later_turns(tmp_path):
    c = Chat(tmp_path)
    v = c.say("调研向量库选型")
    steps = [i for i in v["turns"][0]["items"] if i["type"] == "step"]
    assert steps[0]["kind"] == "skill" and steps[0]["text"] == "对比与选型" and steps[0]["ok"]
    guide = skills.catalog()["comparison"].report.strip()
    assert [x["report"] for n, x in c.planner.calls if n == "consolidate"] == [guide]
    # 下一轮改报告不用再读一遍方法，整理照样按它的报告结构
    c.say("重写", action={"type": "rewrite_section", "section_id": v["report"]["outline"][0]["id"]})
    assert [x["report"] for n, x in c.planner.calls if n == "consolidate"] == [guide, guide]


def test_unknown_method_is_a_failed_step_and_plain_consolidate(tmp_path):
    c = Chat(tmp_path)
    v = c.say("调研 错方法")
    (step,) = [i for i in v["turns"][0]["items"] if i["type"] == "step" and i["kind"] == "skill"] or [None]
    assert step is None  # 不存在的方法没有中文名，不列成一步
    assert [x["report"] for n, x in c.planner.calls if n == "consolidate"] == [None]
    assert v["report"]["version"] == 1
