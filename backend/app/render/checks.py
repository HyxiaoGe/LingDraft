"""渲染后的确定性校验：事实逐字比对、违禁词、溢出。不调用模型。"""
from __future__ import annotations

from pathlib import Path

from app.graph.poster.state import CheckResult, Element, Fact

_WORDS_FILE = Path(__file__).parent / "forbidden_words.txt"


def load_forbidden_words() -> list[str]:
    return [w.strip() for w in _WORDS_FILE.read_text(encoding="utf-8").splitlines() if w.strip()]


def run_checks(elements: list[Element], facts: dict[str, Fact], layout: dict, forbidden: list[str] | None = None) -> list[CheckResult]:
    forbidden = load_forbidden_words() if forbidden is None else forbidden
    by_slot = {e.slot: e for e in elements}
    results: list[CheckResult] = []

    # 1) 事实绑定元素：渲染出的文本必须与已确认事实逐字一致
    mismatches = []
    for slot, info in layout.items():
        e = by_slot.get(slot)
        if not e or not e.bound_fact:
            continue
        expected = (facts[e.bound_fact].value or "").strip()
        if info.get("text", "") != expected:
            mismatches.append(f"{e.id}: 渲染='{info.get('text')}' 事实='{expected}'")
    results.append(CheckResult(name="fact_match", passed=not mismatches, detail="; ".join(mismatches)))

    # 2) 违禁词（广告法绝对化用语）
    hits = []
    for slot, info in layout.items():
        text = info.get("text", "")
        for w in forbidden:
            if w and w in text:
                hits.append(f"{slot} 含“{w}”")
    results.append(CheckResult(name="forbidden_words", passed=not hits, detail="; ".join(hits)))

    # 3) 溢出：自动缩字到下限后仍放不下
    overflow = [slot for slot, info in layout.items() if info.get("overflow")]
    results.append(CheckResult(name="overflow", passed=not overflow, detail="溢出槽位: " + ", ".join(overflow) if overflow else ""))
    return results


def constraints_from(results: list[CheckResult]) -> list[str]:
    """把失败项转成下一轮生成的约束说明。"""
    out = []
    for r in results:
        if r.passed:
            continue
        if r.name == "overflow":
            out.append(f"文案过长导致溢出（{r.detail}），请明显缩短对应槽位的文案")
        elif r.name == "forbidden_words":
            out.append(f"存在违禁词（{r.detail}），请换用非绝对化表述")
        elif r.name == "fact_match":
            out.append(f"事实文本不一致（{r.detail}）")
    return out
