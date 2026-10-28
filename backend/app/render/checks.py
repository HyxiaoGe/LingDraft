"""渲染后的校验：事实逐字比对、溢出是确定性的；文案合规（绝对化用语、数字承诺、和事实冲突）交给模型判断。"""
from __future__ import annotations

from app.graph.poster.state import TEXT_KINDS, CheckResult, Element, Fact


def free_copy(elements: list[Element], layout: dict) -> dict[str, str]:
    """要做合规检查的文案：不绑定事实的文本槽位上实际渲染出的文字。"""
    out = {}
    for e in elements:
        if e.kind in TEXT_KINDS and not e.bound_fact and e.slot in layout:
            text = (layout[e.slot].get("text") or "").strip()
            if text:
                out[e.slot] = text
    return out


def run_checks(elements: list[Element], facts: dict[str, Fact], layout: dict, copy_issues: list | None) -> list[CheckResult]:
    """copy_issues 是模型合规检查报告的问题（slot, problem）；None 表示检查没能完成。"""
    by_slot = {e.slot: e for e in elements}
    results: list[CheckResult] = []

    # 1) 事实绑定元素：渲染出的文本必须与已确认事实逐字一致
    mismatches, bad = [], []
    for slot, info in layout.items():
        e = by_slot.get(slot)
        if not e or not e.bound_fact:
            continue
        expected = (facts[e.bound_fact].value or "").strip()
        if info.get("text", "") != expected:
            mismatches.append(f"{e.id}: 渲染='{info.get('text')}' 事实='{expected}'")
            bad.append(slot)
    results.append(CheckResult(name="fact_match", passed=not mismatches, detail="; ".join(mismatches), slots=bad))

    # 2) 文案合规：价格 / 折扣 / 日期只能从事实槽位展示，否则事实改了文案会留下旧数字；不能用绝对化用语
    if copy_issues is None:
        results.append(CheckResult(name="copy_compliance", passed=False, detail="合规检查没能完成，请自己看一下文案"))
    else:
        results.append(CheckResult(
            name="copy_compliance", passed=not copy_issues,
            detail="; ".join(f"{i.slot}：{i.problem}" for i in copy_issues), slots=[i.slot for i in copy_issues],
        ))

    # 3) 溢出：自动缩字到下限后仍放不下
    overflow = [slot for slot, info in layout.items() if info.get("overflow")]
    results.append(CheckResult(name="overflow", passed=not overflow, detail="溢出槽位: " + ", ".join(overflow) if overflow else "", slots=overflow))
    return results


def constraints_from(results: list[CheckResult]) -> list[str]:
    """把失败项转成下一轮生成的约束说明。"""
    out = []
    for r in results:
        if r.passed:
            continue
        if r.name == "overflow":
            out.append(f"文案过长导致溢出（{r.detail}），请明显缩短对应槽位的文案")
        elif r.name == "copy_compliance" and r.slots:
            out.append(f"文案不合规（{r.detail}），请改写对应槽位：不用绝对化用语，价格 / 折扣 / 日期已有独立槽位，不要写进文案")
        elif r.name == "fact_match":
            out.append(f"事实文本不一致（{r.detail}）")
    return out
