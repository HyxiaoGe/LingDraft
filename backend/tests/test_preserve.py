"""T2 / T4：局部修改保住已确认内容。"""
from __future__ import annotations

from PIL import Image, ImageChops

from tests.conftest import cfg, interrupt_of, resume, run_to_review


def _elements(graph, tid):
    return {e.id: e for e in graph.get_state(cfg(tid)).values["elements"]}


def _diff_outside_bbox(png_a: str, png_b: str, bbox: list[float]) -> int:
    a, b = Image.open(png_a).convert("RGB"), Image.open(png_b).convert("RGB")
    diff = ImageChops.difference(a, b)
    x, y, w, h = [round(v) for v in bbox]
    # 把目标元素所在区域抹掉，其余区域必须完全一致
    diff.paste((0, 0, 0), (x, y, x + w, y + h))
    return sum(1 for p in diff.convert("L").tobytes() if p)


def test_t2_edit_headline_changes_only_headline(graph, planner):
    run_to_review(graph, "t2")
    before = _elements(graph, "t2")
    v1 = graph.get_state(cfg("t2")).values["versions"][-1]

    r = resume(graph, "t2", {"action": "edit", "target": "headline", "instruction": "标题换成更有紧迫感的"})
    payload = interrupt_of(r)
    assert payload["type"] == "review" and payload["version"] == 2
    after = _elements(graph, "t2")
    v2 = graph.get_state(cfg("t2")).values["versions"][-1]

    # 只有 headline 内容变化；其余元素内容不变并已转为 confirmed
    assert after["headline"].content != before["headline"].content
    assert after["headline"].status == "draft" and after["headline"].version == 2
    for eid, e in after.items():
        if eid == "headline":
            continue
        assert e.content == before[eid].content, eid
        assert e.status in ("confirmed", "user_edited"), eid
    # 模型只收到 headline 作为撰写目标
    last_compose = [c for c in planner.calls if c[0] == "compose"][-1]
    assert last_compose[1] == ("headline",)
    # 像素层面：headline 框以外完全一致
    assert _diff_outside_bbox(v1.png_path, v2.png_path, v2.layout["headline"]["bbox"]) == 0
    assert v2.note.startswith("修改 headline")


def test_t4_manual_edit_is_never_overwritten(graph):
    run_to_review(graph, "t4")
    r = resume(graph, "t4", {"action": "manual_edit", "target": "subhead", "content": "我的副标题"})
    assert interrupt_of(r)["version"] == 2
    assert _elements(graph, "t4")["subhead"].status == "user_edited"
    assert graph.get_state(cfg("t4")).values["versions"][-1].layout["subhead"]["text"] == "我的副标题"

    r = resume(graph, "t4", {"action": "edit", "target": "headline", "instruction": "标题换成更有紧迫感的"})
    assert interrupt_of(r)["version"] == 3
    els = _elements(graph, "t4")
    assert els["subhead"].content == "我的副标题" and els["subhead"].status == "user_edited"

    # 对手工编辑过的元素再让模型改：拒绝并提示
    r = resume(graph, "t4", {"action": "edit", "target": "subhead", "instruction": "换一个"})
    payload = interrupt_of(r)
    assert payload["version"] == 3 and any("手工编辑" in n for n in payload["notes"])


def test_t3_change_style_touches_no_content(graph, planner):
    run_to_review(graph, "t3")
    before = _elements(graph, "t3")
    st1 = graph.get_state(cfg("t3")).values
    assert st1["background_image"] and st1["background_image"].endswith("bg_v1.png")

    r = resume(graph, "t3", {"action": "change_style"})
    payload = interrupt_of(r)
    assert payload["type"] == "choose_style" and "不会改动" in payload["message"]
    r = resume(graph, "t3", {"style_id": "s3"})
    payload = interrupt_of(r)
    assert payload["type"] == "review" and payload["version"] == 2
    st = graph.get_state(cfg("t3")).values
    assert st["style"].id == "s3" and st["versions"][-1].note.startswith("更换风格")
    assert st["background_image"].endswith("bg_v2.png")
    after = _elements(graph, "t3")
    for eid, e in after.items():
        assert e.content == before[eid].content and e.status == before[eid].status and e.version == before[eid].version, eid
    assert st["versions"][-1].layout["headline"]["text"] == st1["versions"][-1].layout["headline"]["text"]
    # 没有再调文案模型；背景按新风格重新生成
    assert [c for c in planner.calls if c[0] == "compose"] == [c for c in planner.calls if c[0] == "compose"][:1]
    assert [c for c in planner.calls if c[0] == "background"][-1][1] == "s3"


def test_t3_change_style_with_instruction(graph, planner):
    run_to_review(graph, "t3b")
    r = resume(graph, "t3b", {"action": "change_style", "instruction": "极简白底"})
    payload = interrupt_of(r)
    assert payload["type"] == "review" and payload["version"] == 2
    assert graph.get_state(cfg("t3b")).values["style"].name == "极简白底"


def test_background_switch_off(planner, out_dir, monkeypatch):
    from app import config as cfg_mod
    from app.graph.poster.graph import build_graph, memory_checkpointer

    monkeypatch.setattr(cfg_mod, "BACKGROUND_ENABLED", False)
    graph = build_graph(planner, memory_checkpointer(), out_dir)
    run_to_review(graph, "tbg")
    st = graph.get_state(cfg("tbg")).values
    assert st["background_image"] is None and not [c for c in planner.calls if c[0] == "background"]
