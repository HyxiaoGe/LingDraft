"""报告的确定性拼装与校验。模型只写句子，编号、引用标记、来源表都在这里生成。"""

from __future__ import annotations

from app.graph.research.state import CheckResult, Claim, OutlineNode, Scope, SectionText, Source

INFERENCE_PREFIX = "推断："  # 推断句的标记：写句子时按论断类型由代码加上，模型不写


def referenced_source_ids(sections: dict[str, SectionText], claims: dict[str, Claim]) -> list[str]:
    seen: list[str] = []
    for sec in sections.values():
        for s in sec.sentences:
            if s.claim_id and s.claim_id in claims:
                for sid in claims[s.claim_id].source_ids:
                    if sid not in seen:
                        seen.append(sid)
    return seen


def source_label(sid: str) -> str:
    """来源在报告里的标注就是它的 id（s16 → S16）：版本之间不变，和来源页上显示的一致，用户说“排除 S16”不会有歧义。"""
    return sid.upper()


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


def report_blocks(
    scope: Scope,
    outline: list[OutlineNode],
    sections: dict[str, SectionText],
    claims: list[Claim],
    sources: list[Source],
    assumptions: list[str],
    user_answers: list[str] = (),
    answered: bool = True,
    open_gap: str | None = None,
) -> list[dict]:
    """报告的结构化内容：页面按它渲染，Markdown 也由它生成。每句是不是推断、挂哪些来源都来自论断，不看句子文字。"""
    by_claim = {c.id: c for c in claims}
    by_source = {s.id: s for s in sources}
    order = referenced_source_ids(sections, by_claim)

    blocks: list[dict] = [{"type": "title", "text": scope.question}]
    if not answered:  # 没答完也照常交付，但报告自己要说清楚，下载出去的文件也一样
        blocks.append({"type": "notice", "text": "注意：这份报告没有完全回答研究问题" + (f"，还缺：{open_gap}" if open_gap else "") + "。"})
    bounds = [f"{label}：{val}" for label, val in (
        ("时间", scope.time_range), ("地域", scope.region), ("信源语言", scope.lang), ("不含", scope.exclude),
        ("补充", scope.extra),
    ) if val]
    if bounds:
        blocks.append({"type": "scope", "text": "范围：" + "；".join(bounds)})
    for node in outline:
        sec = sections.get(node.id)
        if sec is None:
            continue
        blocks.append({"type": "heading", "text": node.title})
        for s in sec.sentences:
            claim = by_claim.get(s.claim_id) if s.claim_id else None
            blocks.append({
                "type": "sentence", "text": s.text.strip(), "claim_id": claim.id if claim else None,
                "inference": bool(claim and claim.kind == "inference"),
                "marks": [source_label(sid) for sid in claim.source_ids if sid in by_source] if claim else [],
            })
    for title, items in (("用户的回答", user_answers), ("假设与取舍", assumptions)):
        if items:
            blocks.append({"type": "heading", "text": title})
            blocks += [{"type": "item", "text": a} for a in items]
    blocks.append({"type": "heading", "text": "来源"})
    for sid in order:
        src = by_source.get(sid)
        if src is None:
            continue
        meta = []
        if src.published_at:
            meta.append(f"发布 {src.published_at[:10]}")
        if src.retrieved_at:
            meta.append(f"抓取 {src.retrieved_at[:10]}")
        blocks.append({"type": "source", "label": source_label(sid), "source_id": sid, "title": src.title or src.url, "url": src.url, "meta": "，".join(meta)})
    return blocks


def to_markdown(blocks: list[dict]) -> str:
    out: list[str] = []
    for b in blocks:
        t = b["type"]
        if t == "title":
            out += [f"# {b['text']}", ""]
        elif t == "notice":
            out += [f"> {b['text']}", ""]
        elif t == "scope":
            out += [b["text"], ""]
        elif t == "heading":
            if out and out[-1] != "":
                out.append("")
            out += [f"## {b['text']}", ""]
        elif t == "sentence":
            out.append(b["text"] + "".join(f"[{m}]" for m in b["marks"]))
        elif t == "item":
            out.append(f"- {b['text']}")
        elif t == "source":
            out.append(f"[{b['label']}] {b['title']} — {b['url']}" + (f"（{b['meta']}）" if b["meta"] else ""))
    return "\n".join(out).rstrip() + "\n"


def assemble(*args, **kwargs) -> str:
    return to_markdown(report_blocks(*args, **kwargs))


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
    sections: dict[str, SectionText], claims: list[Claim], sources: list[Source], time_issues: dict[str, str] | None = None
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

    incomplete = [sid for sid in referenced_source_ids(sections, by_claim)
                  if sid in by_source and not (by_source[sid].url and by_source[sid].title and by_source[sid].retrieved_at)]
    results.append(CheckResult(name="sources_complete", passed=not incomplete, detail=_join("来源信息不全", incomplete)))

    orphaned = [c.id for c in used_claims if c.orphaned]
    results.append(CheckResult(name="no_orphaned_claims", passed=not orphaned, detail=_join("失去来源支撑", orphaned)))

    # 来源发布时间在不在用户要的时间范围里由整理步骤的模型判断（范围可能是“近三年”“2020 年以前”这类说法）；这里只看正文用到的
    off = [sid for sid in referenced_source_ids(sections, by_claim) if sid in (time_issues or {})]
    results.append(CheckResult(name="scope_time", passed=not off,
                               detail="；".join(f"{sid}：{time_issues[sid]}" for sid in off) + ("；" if off else "")))
    return results


def _join(label: str, ids: list[str]) -> str:
    return f"{label}：{', '.join(ids)}；" if ids else ""
