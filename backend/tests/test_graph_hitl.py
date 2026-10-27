"""T1 / T5 / T6 / T8：HITL 链路本身。"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.graph.poster.graph import build_graph, memory_checkpointer, open_sqlite_checkpointer
from app.llm import FakePlanner
from tests.conftest import REQUEST, cfg, interrupt_of, resume, run_to_review


def _state(graph, thread_id):
    return graph.get_state(cfg(thread_id)).values


def test_t1_facts_confirmed_and_rendered_verbatim(graph, planner):
    payload = run_to_review(graph, "t1")
    st = _state(graph, "t1")
    facts = st["facts"]
    assert facts["offer"].value == "5 折" and facts["offer"].status == "confirmed" and facts["offer"].source == "user"
    layout = st["versions"][-1].layout
    assert layout["offer"]["text"] == "5 折"
    assert layout["date"]["text"] == "11.20 至 11.27"
    assert all(c["passed"] for c in payload["checks"]), payload["checks"]
    # 事实绑定元素不可由模型编辑
    views = {e["id"]: e for e in payload["elements"]}
    assert views["offer"]["editable_by_model"] is False
    assert views["headline"]["editable_by_model"] is True
    # 模型从未收到事实槽位作为撰写目标
    for call in planner.calls:
        if call[0] == "compose":
            assert "offer" not in call[1] and "date" not in call[1]


def test_t1_intake_prefills_facts_without_fabrication(graph):
    r = graph.invoke({"request": "黑五海报，随便做一个", "product_image": None}, cfg("t1b"))
    payload = interrupt_of(r)
    assert payload["type"] == "confirm_facts"
    values = {f["key"]: f["value"] for f in payload["fields"]}
    assert values["offer"] is None and values["date_range"] is None
    # 必填缺失时 resume 校验失败，流程停在原确认点
    with pytest.raises(ValueError, match="必填事实缺失"):
        resume(graph, "t1b", {"values": values})
    assert graph.get_state(cfg("t1b")).next == ("confirm_facts",)


def test_t5_fact_change_must_go_through_confirm(graph, planner):
    run_to_review(graph, "t5")
    v1_headline = {e.id: e.content for e in _state(graph, "t5")["elements"]}["headline"]

    # 试图通过修改指令改优惠：被拒绝，不产生新版本，回到 review 并带提示
    r = resume(graph, "t5", {"action": "edit", "target": "offer", "instruction": "折扣改成 6 折"})
    payload = interrupt_of(r)
    assert payload["type"] == "review" and payload["version"] == 1
    assert any("change_fact" in n for n in payload["notes"])
    assert _state(graph, "t5")["facts"]["offer"].value == "5 折"
    compose_calls_before = len([c for c in planner.calls if c[0] == "compose"])

    # 走正式的事实修改：回到 confirm_facts，预填当前值
    r = resume(graph, "t5", {"action": "change_fact"})
    payload = interrupt_of(r)
    assert payload["type"] == "confirm_facts"
    values = {f["key"]: f["value"] for f in payload["fields"]}
    assert values["offer"] == "5 折"
    values["offer"] = "6 折"
    r = resume(graph, "t5", {"values": values})
    payload = interrupt_of(r)
    assert payload["type"] == "review" and payload["version"] == 2
    st = _state(graph, "t5")
    assert st["versions"][-1].layout["offer"]["text"] == "6 折"
    assert st["versions"][-1].note == "事实更新"
    # 事实更新只重渲染，不调模型；标题保持不变
    assert len([c for c in planner.calls if c[0] == "compose"]) == compose_calls_before
    assert {e.id: e.content for e in st["elements"]}["headline"] == v1_headline


def test_t6_resume_after_process_restart(tmp_path: Path):
    db = tmp_path / "ckpt.sqlite"
    out = tmp_path / "out"
    g1 = build_graph(FakePlanner(), open_sqlite_checkpointer(db), out)
    r = g1.invoke({"request": REQUEST, "product_image": None}, cfg("t6"))
    payload = interrupt_of(r)
    assert payload["type"] == "confirm_facts"
    values = {f["key"]: f["value"] for f in payload["fields"]}
    del g1  # 模拟进程退出

    g2 = build_graph(FakePlanner(), open_sqlite_checkpointer(db), out)
    assert g2.get_state(cfg("t6")).next == ("confirm_facts",)
    r = resume(g2, "t6", {"values": values})
    assert interrupt_of(r)["type"] == "choose_style"
    r = resume(g2, "t6", {"style_id": "s2"})
    payload = interrupt_of(r)
    assert payload["type"] == "review"
    st = g2.get_state(cfg("t6")).values
    assert st["style"].id == "s2" and Path(st["versions"][-1].png_path).exists()


def test_t8_style_in_request_skips_style_question(graph):
    payload = run_to_review(graph, "t8", request=REQUEST + "，极简白底风格")
    st = _state(graph, "t8")
    assert st["style"].id == "s_hint"
    assert payload["version"] == 1


def test_approve_exports_files(graph):
    run_to_review(graph, "ta")
    r = resume(graph, "ta", {"action": "approve"})
    exported = r["exported"]
    assert Path(exported["png"]).exists() and Path(exported["html"]).exists() and Path(exported["brief"]).exists()
    assert all(e.status != "draft" for e in r["elements"])
    html = Path(exported["html"]).read_text(encoding="utf-8")
    assert "5 折" in html and "11.20 至 11.27" in html


def test_check_failure_retries_plan_layout_with_constraints(planner, out_dir, monkeypatch):
    """校验不过 → 带约束回到 plan_layout，最多重试 MAX_CHECK_RETRIES 次后仍进入 review。"""
    from app.render import checks as checks_mod

    calls = {"n": 0}
    real = checks_mod.run_checks

    def always_overflow(elements, facts, layout, forbidden=None):
        calls["n"] += 1
        res = real(elements, facts, layout, forbidden)
        return [r.model_copy(update={"passed": False, "detail": "溢出槽位: headline"}) if r.name == "overflow" else r for r in res]

    monkeypatch.setattr(checks_mod, "run_checks", always_overflow)
    graph = build_graph(planner, memory_checkpointer(), out_dir)
    payload = run_to_review(graph, "tr")
    assert payload["type"] == "review"
    composes = [c for c in planner.calls if c[0] == "compose"]
    assert len(composes) == 3  # 初稿 + 2 次重试
    assert composes[0][3] == () and all("缩短" in composes[i][3][0] for i in (1, 2))
    assert payload["version"] == 3
    assert any(not c["passed"] for c in payload["checks"])  # 问题如实带到 review


def test_custom_style_text_generates_style_without_llm_in_interrupt(graph, planner):
    r = graph.invoke({"request": REQUEST, "product_image": None}, cfg("tc"))
    values = {f["key"]: f["value"] for f in interrupt_of(r)["fields"]}
    r = resume(graph, "tc", {"values": values})
    assert interrupt_of(r)["type"] == "choose_style"
    r = resume(graph, "tc", {"custom": "复古胶片感"})
    assert interrupt_of(r)["type"] == "review"
    st = _state(graph, "tc")
    assert st["style"].name == "复古胶片感"
    assert [c for c in planner.calls if c[0] == "style_options"][-1][1] == "复古胶片感"


def test_cta_fact_added_later_binds_cta_element(graph):
    run_to_review(graph, "tcta")
    assert {e.id: e for e in _state(graph, "tcta")["elements"]}["cta"].bound_fact is None
    r = resume(graph, "tcta", {"action": "change_fact"})
    values = {f["key"]: f["value"] for f in interrupt_of(r)["fields"]}
    values["cta"] = "马上抢"
    r = resume(graph, "tcta", {"values": values})
    payload = interrupt_of(r)
    assert payload["type"] == "review" and payload["version"] == 2
    st = _state(graph, "tcta")
    cta = {e.id: e for e in st["elements"]}["cta"]
    assert cta.bound_fact == "cta" and st["versions"][-1].layout["cta"]["text"] == "马上抢"


def test_manual_edit_of_bound_element_is_refused_without_new_version(graph):
    run_to_review(graph, "tmb")
    r = resume(graph, "tmb", {"action": "manual_edit", "target": "offer", "content": "9 折"})
    payload = interrupt_of(r)
    assert payload["version"] == 1 and any("change_fact" in n for n in payload["notes"])
    assert _state(graph, "tmb")["facts"]["offer"].value == "5 折"
