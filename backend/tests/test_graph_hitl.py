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

    def always_overflow(elements, facts, layout, copy_issues):
        calls["n"] += 1
        res = real(elements, facts, layout, copy_issues)
        return [r.model_copy(update={"passed": False, "detail": "溢出槽位: headline", "slots": ["headline"]}) if r.name == "overflow" else r for r in res]

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


def test_failure_only_in_slots_model_cannot_rewrite_is_not_retried(graph, planner):
    """只有事实槽位或用户改过的文案出问题时，重写也改不到它们：不重试，直接如实给用户看。"""
    from app.llm import CopyIssue

    run_to_review(graph, "tnr")
    planner.copy_issues = [[CopyIssue(slot="headline", problem="绝对化用语")]]
    r = resume(graph, "tnr", {"action": "manual_edit", "target": "headline", "content": "全网最低"})
    payload = interrupt_of(r)
    assert payload["version"] == 2  # 没有为它重试
    assert not {c["name"]: c for c in payload["checks"]}["copy_compliance"]["passed"]


def test_compliance_issue_in_model_copy_retries_with_constraint(graph, planner):
    from app.llm import CopyIssue

    planner.copy_issues = [[CopyIssue(slot="headline", problem="含绝对化用语")]]
    payload = run_to_review(graph, "tcc")
    composes = [c for c in planner.calls if c[0] == "compose"]
    assert len(composes) == 2 and "headline：含绝对化用语" in composes[1][3][0]
    assert payload["version"] == 2 and all(c["passed"] for c in payload["checks"])
    checked = [c[1] for c in planner.calls if c[0] == "check_copy"]
    assert set(checked[0]) == {"headline", "subhead", "cta"}  # 只查不绑定事实的文案（这个需求没给行动号召，cta 由模型写）


def test_compliance_check_failure_does_not_block(graph, planner, monkeypatch):
    def boom(facts, copy):
        raise RuntimeError("模型挂了")

    monkeypatch.setattr(planner, "check_copy", boom)
    payload = run_to_review(graph, "tcf")
    assert payload["version"] == 1
    c = {c["name"]: c for c in payload["checks"]}["copy_compliance"]
    assert not c["passed"] and "没能完成" in c["detail"]


# ---- 终审时用户直接说的话 ----


def _plan(*ops, reply=None):
    from app.llm import PosterOp, PosterPlan

    full = [PosterOp(**{"target": None, "instruction": None, "content": None, **o}) for o in ops]
    return PosterPlan(ops=full, reply=reply)


def test_instruct_edits_several_elements_in_one_version(graph, planner):
    run_to_review(graph, "ti1")
    planner.review_plans = [_plan(
        {"action": "edit", "target": "headline", "instruction": "更短"},
        {"action": "manual_edit", "target": "subhead", "content": "限时好礼"},
    )]
    r = resume(graph, "ti1", {"action": "instruct", "text": "标题短一点，副标题就写限时好礼"})
    payload = interrupt_of(r)
    assert ("interpret", "标题短一点，副标题就写限时好礼") in planner.calls
    assert payload["version"] == 2 and payload["reply"] is None
    els = {e.id: e for e in _state(graph, "ti1")["elements"]}
    assert els["headline"].content == "黑五来了"
    assert els["subhead"].content == "限时好礼" and els["subhead"].status == "user_edited"
    assert _state(graph, "ti1")["versions"][-1].note == "修改主标题：更短；副标题改成“限时好礼”"
    # 模型收到的是终审卡上的元素视图和用户原话
    assert [c for c in planner.calls if c[0] == "compose"][-1][2] == "更短"


def test_instruct_fact_change_prefills_fact_card(graph, planner):
    run_to_review(graph, "ti2")
    planner.review_plans = [_plan({"action": "change_fact", "target": "offer", "content": "6 折"})]
    r = resume(graph, "ti2", {"action": "instruct", "text": "折扣改成 6 折"})
    payload = interrupt_of(r)
    assert payload["type"] == "confirm_facts"
    offer = {f["key"]: f for f in payload["fields"]}["offer"]
    assert offer["value"] == "6 折" and offer["source"] == "proposed" and offer["previous"] == "5 折"
    assert _state(graph, "ti2")["facts"]["offer"].value == "5 折"  # 还没确认，事实不变
    values = {f["key"]: f["value"] for f in payload["fields"]}
    r = resume(graph, "ti2", {"values": values})
    payload = interrupt_of(r)
    assert payload["type"] == "review" and _state(graph, "ti2")["facts"]["offer"].value == "6 折"
    assert _state(graph, "ti2")["fact_proposals"] == {}


def test_instruct_style_change_and_approve(graph, planner):
    run_to_review(graph, "ti3")
    planner.review_plans = [_plan({"action": "change_style", "instruction": "极简白底"}), _plan({"action": "approve"})]
    before = {e.id: e.content for e in _state(graph, "ti3")["elements"]}
    r = resume(graph, "ti3", {"action": "instruct", "text": "换成极简白底"})
    payload = interrupt_of(r)
    assert payload["type"] == "review" and payload["style"]["name"] == "极简白底"
    assert {e.id: e.content for e in _state(graph, "ti3")["elements"]} == before
    r = resume(graph, "ti3", {"action": "instruct", "text": "可以了"})
    assert r["exported"] and not r.get("__interrupt__")


def test_instruct_mixed_does_copy_first_and_says_what_was_left(graph, planner):
    run_to_review(graph, "ti4")
    planner.review_plans = [_plan(
        {"action": "edit", "target": "headline", "instruction": "更有紧迫感"},
        {"action": "change_fact", "target": "offer", "content": "6 折"},
        {"action": "approve"},
    )]
    r = resume(graph, "ti4", {"action": "instruct", "text": "标题更紧迫，折扣改 6 折，然后就导出"})
    payload = interrupt_of(r)
    assert payload["type"] == "review" and payload["version"] == 2
    assert "改事实这次没做" in payload["reply"]
    assert _state(graph, "ti4")["facts"]["offer"].value == "5 折"


def test_instruct_invalid_ops_are_reported_not_applied(graph, planner):
    run_to_review(graph, "ti5")
    planner.review_plans = [_plan(
        {"action": "edit", "target": "offer", "instruction": "改成 6 折"},  # 事实元素
        {"action": "edit", "target": "logo", "instruction": "大一点"},  # 不存在
        {"action": "edit", "target": "headline"},  # 缺 instruction
    )]
    r = resume(graph, "ti5", {"action": "instruct", "text": "随便说的"})
    payload = interrupt_of(r)
    assert payload["type"] == "review" and payload["version"] == 1
    assert "优惠显示的是事实" in payload["reply"] and "“logo”" in payload["reply"] and "没理解完整" in payload["reply"]
    # 下一次回答后 reply 清空
    planner.review_plans = [_plan({"action": "approve"})]
    resume(graph, "ti5", {"action": "instruct", "text": "导出"})
    assert _state(graph, "ti5")["review_reply"] is None


def test_instruct_unmatched_or_question_returns_reply(graph, planner):
    run_to_review(graph, "ti6")
    planner.review_plans = [_plan(reply="这版标题用的是促销口吻。想改哪一处可以直接说。")]
    r = resume(graph, "ti6", {"action": "instruct", "text": "标题为什么这么写"})
    payload = interrupt_of(r)
    assert payload["type"] == "review" and payload["version"] == 1
    assert payload["reply"].startswith("这版标题")


def test_instruct_model_failure_returns_to_review(graph, planner, monkeypatch):
    run_to_review(graph, "ti7")

    def boom(*a, **k):
        raise RuntimeError("模型挂了")

    monkeypatch.setattr(planner, "interpret_review", boom)
    r = resume(graph, "ti7", {"action": "instruct", "text": "标题短一点"})
    payload = interrupt_of(r)
    assert payload["type"] == "review" and payload["version"] == 1 and "没能处理" in payload["reply"]


def test_instruct_payload_is_validated(graph):
    run_to_review(graph, "ti8")
    with pytest.raises(ValueError):
        resume(graph, "ti8", {"action": "instruct", "text": "  "})
    with pytest.raises(ValueError):
        resume(graph, "ti8", {"action": "instruct", "text": "改标题", "target": "headline"})
    with pytest.raises(ValueError):
        resume(graph, "ti8", {"action": "approve", "text": "好"})


def test_retry_budget_restarts_after_each_user_action(graph, planner):
    """不能重试的失败不消耗重试次数；用户每次操作后重新计数。"""
    from app.llm import CopyIssue

    run_to_review(graph, "trb")
    resume(graph, "trb", {"action": "manual_edit", "target": "subhead", "content": "全网第一"})
    planner.copy_issues = [[CopyIssue(slot="subhead", problem="绝对化")]] * 3
    resume(graph, "trb", {"action": "manual_edit", "target": "subhead", "content": "全网第一好"})
    assert _state(graph, "trb")["retry_count"] == 0  # subhead 是用户改的，不重试也不计数
    planner.copy_issues = [[CopyIssue(slot="headline", problem="绝对化")]]
    r = resume(graph, "trb", {"action": "edit", "target": "headline", "instruction": "更短"})
    composes = [c for c in planner.calls if c[0] == "compose"]
    assert any("headline：绝对化" in x for x in composes[-1][3])  # 模型写的 headline 出问题照样重试
    assert interrupt_of(r)["type"] == "review"
