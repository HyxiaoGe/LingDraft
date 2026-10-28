"""报告的确定性拼装与校验。模型只写句子，编号、引用标记、来源表都在这里生成。"""

from __future__ import annotations

from app.graph.research.state import CheckResult, Claim, OutlineNode, Scope, SectionText, Source

INFERENCE_PREFIX = "推断："


def referenced_source_ids(sections: dict[str, SectionText], claims: dict[str, Claim]) -> list[str]:
    seen: list[str] = []
    for sec in sections.values():
        for s in sec.sentences:
            if s.claim_id and s.claim_id in claims:
                for sid in claims[s.claim_id].source_ids:
                    if sid not in seen:
                        seen.append(sid)
    return seen


def cited_claim_ids(sections: dict[str, SectionText], claims: list[Claim]) -> set[str]:
    """正文里真正用到的论断（失去来源的不算）。"""
    usable = {c.id for c in claims if c.source_ids and not c.orphaned}
    return {s.claim_id for sec in sections.values() for s in sec.sentences if s.claim_id in usable}


def source_status(sources: list[Source], sections: dict[str, SectionText], claims: list[Claim]) -> list[Source]:
    """按当前这版报告重算来源状态：正文引用到的是系统选用，用户排除的保持排除，其余（包括以前引用、现在没用上的）是候选。"""
    cited = cited_claim_ids(sections, claims)
    used = {sid for c in claims if c.id in cited for sid in c.source_ids}
    return [s if s.status == "excluded" else s.model_copy(update={"status": "adopted" if s.id in used else "candidate"})
            for s in sources]


def assemble(
    scope: Scope,
    outline: list[OutlineNode],
    sections: dict[str, SectionText],
    claims: list[Claim],
    sources: list[Source],
    assumptions: list[str],
    user_answers: list[str] = (),
    answered: bool = True,
    open_gap: str | None = None,
) -> str:
    by_claim = {c.id: c for c in claims}
    by_source = {s.id: s for s in sources}
    order = referenced_source_ids(sections, by_claim)
    num = {sid: i + 1 for i, sid in enumerate(order)}

    lines = [f"# {scope.question}", ""]
    if not answered:  # 没答完也照常交付，但报告自己要说清楚，下载出去的文件也一样
        lines += ["> 注意：这份报告没有完全回答研究问题" + (f"，还缺：{open_gap}" if open_gap else "") + "。", ""]
    bounds = [f"{label}：{val}" for label, val in (
        ("时间", scope.time_range), ("地域", scope.region), ("信源语言", scope.lang), ("不含", scope.exclude),
        ("补充", scope.extra),
    ) if val]
    if bounds:
        lines += ["范围：" + "；".join(bounds), ""]
    for node in outline:
        sec = sections.get(node.id)
        if sec is None:
            continue
        lines += [f"## {node.title}", ""]
        for s in sec.sentences:
            text = s.text.strip()
            claim = by_claim.get(s.claim_id) if s.claim_id else None
            if claim is not None:
                if claim.kind == "inference" and not text.startswith(INFERENCE_PREFIX):
                    text = INFERENCE_PREFIX + text
                marks = "".join(f"[S{num[sid]}]" for sid in claim.source_ids if sid in num)
                text = f"{text}{marks}"
            lines.append(text)
        lines.append("")
    if user_answers:
        lines += ["## 用户的回答", ""] + [f"- {a}" for a in user_answers] + [""]
    if assumptions:
        lines += ["## 假设与取舍", ""] + [f"- {a}" for a in assumptions] + [""]
    lines += ["## 来源", ""]
    for sid in order:
        src = by_source.get(sid)
        if src is None:
            continue
        meta = []
        if src.published_at:
            meta.append(f"发布 {src.published_at[:10]}")
        if src.retrieved_at:
            meta.append(f"抓取 {src.retrieved_at[:10]}")
        lines.append(f"[S{num[sid]}] {src.title or src.url} — {src.url}" + (f"（{'，'.join(meta)}）" if meta else ""))
    return "\n".join(lines).rstrip() + "\n"


def answer_check(answer: str | None, how: str) -> CheckResult:
    """没有有效候选答案就不能算成功；受控收尾得到的答案要标出来。"""
    if not answer:
        return CheckResult(name="answer_present", passed=False, detail="调研没有产出答案；")
    return CheckResult(name="answer_present", passed=True, detail="由受控收尾补得；" if how == "close" else "")


def report_check(sections: dict[str, SectionText], claims: list[Claim], answered: bool, open_gap: str | None = None) -> list[CheckResult]:
    """交付物本身怎么样：至少一句挂着有来源的论断；整理步骤认为问题被回答了。只是说明，不拦交付。"""
    linked = bool(cited_claim_ids(sections, claims))
    missing = "整理步骤判断候选答案没有回答研究问题" + (f"，还缺：{open_gap}" if open_gap else "") + "；"
    return [
        CheckResult(name="report_valid", passed=linked, detail="" if linked else "报告里没有任何带来源的论断；"),
        CheckResult(name="question_answered", passed=answered, detail="" if answered else missing),
    ]


def run_checks(
    sections: dict[str, SectionText], claims: list[Claim], sources: list[Source], scope: Scope
) -> list[CheckResult]:
    by_claim = {c.id: c for c in claims}
    by_source = {s.id: s for s in sources}
    used_claims = [by_claim[s.claim_id] for sec in sections.values() for s in sec.sentences if s.claim_id in by_claim]
    results: list[CheckResult] = []

    unsourced = [c.id for c in used_claims if not c.source_ids or any(by_source.get(sid) is None for sid in c.source_ids)]
    not_adopted = [c.id for c in used_claims if any(by_source[sid].status != "adopted" for sid in c.source_ids if sid in by_source)]
    results.append(CheckResult(name="claims_sourced", passed=not unsourced and not not_adopted,
                               detail=_join("无来源", unsourced) + _join("来源未采纳", not_adopted)))

    excluded = [c.id for c in used_claims if any(by_source.get(sid) and by_source[sid].status == "excluded" for sid in c.source_ids)]
    results.append(CheckResult(name="no_excluded_sources", passed=not excluded, detail=_join("引用了已排除来源", excluded)))

    unmarked = [
        s.claim_id for sec in sections.values() for s in sec.sentences
        if s.claim_id in by_claim and by_claim[s.claim_id].kind == "inference" and not s.text.strip().startswith(INFERENCE_PREFIX)
    ]
    results.append(CheckResult(name="inference_marked", passed=not unmarked, detail=_join("推断未标记", unmarked)))

    incomplete = [sid for sid in referenced_source_ids(sections, by_claim)
                  if sid in by_source and not (by_source[sid].url and by_source[sid].title and by_source[sid].retrieved_at)]
    results.append(CheckResult(name="sources_complete", passed=not incomplete, detail=_join("来源信息不全", incomplete)))

    orphaned = [c.id for c in used_claims if c.orphaned]
    results.append(CheckResult(name="no_orphaned_claims", passed=not orphaned, detail=_join("失去来源支撑", orphaned)))

    year = _year(scope.time_range)
    stale = [sid for sid in referenced_source_ids(sections, by_claim)
             if year and sid in by_source and by_source[sid].published_at and _year(by_source[sid].published_at) and _year(by_source[sid].published_at) < year - 1]
    results.append(CheckResult(name="scope_time", passed=not stale, detail=_join(f"发布时间早于范围（{year}）", stale)))
    return results


def _join(label: str, ids: list[str]) -> str:
    return f"{label}：{', '.join(ids)}；" if ids else ""


def _year(text: str | None) -> int | None:
    if not text:
        return None
    import re

    m = re.search(r"(20\d{2})", text)
    return int(m.group(1)) if m else None
