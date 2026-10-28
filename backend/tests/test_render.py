from __future__ import annotations

from app.graph.poster.state import Element, StyleOption, empty_facts
from app.render import checks, renderer

STYLE = StyleOption(id="s", name="x", palette=["#000000", "#111111", "#f5c542", "#ffffff"], mood="m", description="d")


def _facts(offer="5 折"):
    f = empty_facts()
    f["offer"] = f["offer"].model_copy(update={"value": offer})
    f["date_range"] = f["date_range"].model_copy(update={"value": "11.20 至 11.27"})
    f["product_name"] = f["product_name"].model_copy(update={"value": "蓝牙耳机"})
    return f


def _elements(headline="黑五预热"):
    return [
        Element(id="background", kind="background", slot="background"),
        Element(id="product_image", kind="product_image", slot="product_image", status="confirmed"),
        Element(id="headline", kind="headline", slot="headline", content=headline),
        Element(id="subhead", kind="subhead", slot="subhead", content="副标题"),
        Element(id="offer", kind="offer", slot="offer", bound_fact="offer"),
        Element(id="date", kind="date", slot="date", bound_fact="date_range"),
        Element(id="cta", kind="cta", slot="cta", content="立即抢购"),
    ]


def test_bound_text_comes_from_fact_not_content():
    els = _elements()
    els[4] = els[4].model_copy(update={"content": "9 折"})  # 即使 content 被写入也不生效
    html = renderer.render_html(els, _facts(), STYLE, "bold_sale")
    assert "5 折" in html and "9 折" not in html


def test_render_layout_and_checks(tmp_path):
    res = renderer.render(_elements(), _facts(), STYLE, "bold_sale", tmp_path, 1)
    assert res.layout["offer"]["text"] == "5 折"
    results = checks.run_checks(_elements(), _facts(), res.layout, [])
    assert all(r.passed for r in results), results


def test_overflow_detected_and_turned_into_constraint(tmp_path):
    res = renderer.render(_elements(headline="这是一个长到不可能放进标题框的标题" * 3), _facts(), STYLE, "bold_sale", tmp_path, 1)
    results = checks.run_checks(_elements(), _facts(), res.layout, [])
    by = {r.name: r for r in results}
    assert by["overflow"].passed is False and "headline" in by["overflow"].slots
    assert any("缩短" in c for c in checks.constraints_from(results))


def test_compliance_issues_from_model_become_constraint():
    """合规与否由模型判断；代码只把问题记到对应槽位，转成重写约束。"""
    from app.llm import CopyIssue

    layout = {"headline": {"text": "全网最低价来了", "overflow": False}}
    results = checks.run_checks(_elements(), _facts(), layout, [CopyIssue(slot="headline", problem="绝对化用语")])
    by = {r.name: r for r in results}
    assert by["copy_compliance"].passed is False and by["copy_compliance"].slots == ["headline"]
    assert any("headline：绝对化用语" in c for c in checks.constraints_from(results))


def test_compliance_check_not_done_is_reported_not_retried():
    results = checks.run_checks(_elements(), _facts(), {}, None)
    by = {r.name: r for r in results}
    assert by["copy_compliance"].passed is False and by["copy_compliance"].slots == []
    assert checks.constraints_from(results) == []


def test_free_copy_is_rendered_text_of_unbound_text_slots():
    layout = {"headline": {"text": "黑五预热"}, "offer": {"text": "5 折"}, "cta": {"text": "立即抢购"}, "subhead": {"text": " "}}
    assert checks.free_copy(_elements(), layout) == {"headline": "黑五预热", "cta": "立即抢购"}
