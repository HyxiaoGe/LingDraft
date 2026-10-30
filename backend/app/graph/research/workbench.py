"""报告工作稿：改报告的四个工具，以及一轮结束时把工作稿落成一版。

工具只改 state 里的工作稿（节、论断、假设、排除的来源），每次都把改完的整份报告返回给 agent，agent 据此决定下一步。
一轮里改了几次，结束时只出一版，版本说明是这一轮各次改动的说明连起来。
草稿由 agent 写，快模型整理成节和论断；整理只能用草稿里引用到、没被排除的来源，挂不上来源的论断丢掉，挂在它上面的句子一起去掉。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import ToolException, tool
from langgraph.types import Command
from pydantic import BaseModel, Field

from app.graph.research import history, prompts, report
from app.graph.research.sources import cited, collect_sources
from app.graph.research.state import Claim, OutlineNode, ReportVersion, ResearchState, SectionText, Sentence, Source

REPORT_TOOLS = ("write_report", "revise_section", "edit_claim", "exclude_source")
NEW_SECTION = "new"


def _arg(tool_name: str, name: str) -> str:
    return prompts.render(f"tools.{tool_name}.{name}")


class WriteArgs(BaseModel):
    title: str = Field(description=_arg("write_report", "title"))
    draft: str = Field(description=_arg("write_report", "draft"))
    change: str = Field(description=_arg("write_report", "change"))


class ReviseArgs(BaseModel):
    section_id: str = Field(description=_arg("revise_section", "section_id"))
    title: str = Field(description=_arg("revise_section", "title"))
    draft: str = Field(description=_arg("revise_section", "draft"))
    change: str = Field(description=_arg("revise_section", "change"))


class EditArgs(BaseModel):
    claim_id: str = Field(description=_arg("edit_claim", "claim_id"))
    statement: str = Field(description=_arg("edit_claim", "statement"))
    change: str = Field(description=_arg("edit_claim", "change"))


class ExcludeArgs(BaseModel):
    source_id: str = Field(description=_arg("exclude_source", "source_id"))
    change: str = Field(description=_arg("exclude_source", "change"))


@dataclass
class Draft:
    """state 里的报告工作稿。"""

    title: str = ""
    outline: list[OutlineNode] = field(default_factory=list)
    sections: dict[str, SectionText] = field(default_factory=dict)
    claims: list[Claim] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    time_issues: dict[str, str] = field(default_factory=dict)
    excluded: list[str] = field(default_factory=list)
    ids: dict[str, int] = field(default_factory=dict)

    @classmethod
    def of(cls, state: ResearchState) -> Draft:
        return cls(title=state.get("report_title", ""), outline=list(state.get("outline", [])), sections=dict(state.get("sections", {})),
                   claims=list(state.get("claims", [])), assumptions=list(state.get("assumptions", [])),
                   time_issues=dict(state.get("time_issues", {})), excluded=list(state.get("excluded", [])), ids=dict(state.get("ids", {})))

    def fields(self) -> dict:
        return {"report_title": self.title, "outline": self.outline, "sections": self.sections, "claims": self.claims,
                "assumptions": self.assumptions, "time_issues": self.time_issues, "excluded": self.excluded, "ids": self.ids}

    def visible_claims(self) -> dict[str, Claim]:
        return {c.id: c for c in self.claims if not c.orphaned}


def _mark(text: str, claim: Claim | None) -> str:
    """推断句的标记由代码按论断类型加上；提示词要求模型不要自己写。"""
    text = text.strip()
    return report.INFERENCE_PREFIX + text if claim is not None and claim.kind == "inference" else text


def sources_of(state: ResearchState) -> list[Source]:
    """这条对话里所有来源，用户排除的标成 excluded，正文引用到的标成 adopted。"""
    d = Draft.of(state)
    return report.source_status(collect_sources(state.get("messages", []), d.excluded), d.sections, d.claims)


def blocks(state: ResearchState) -> list[dict]:
    """当前工作稿的结构化内容（页面按它渲染，Markdown 也由它生成）。还没写过报告是空的。"""
    d = Draft.of(state)
    if not d.outline:
        return []
    return report.report_blocks(d.title, d.outline, d.sections, d.claims, sources_of(state), d.assumptions,
                                history.user_answers(state.get("messages", [])))


# ---- 给 agent 看的整份报告 ----


def view(d: Draft, sources: list[Source], n: int, notes: list[str] = ()) -> str:
    claims = d.visible_claims()
    secs = []
    for node in d.outline:
        lines = []
        for s in d.sections.get(node.id, SectionText(outline_id=node.id, sentences=[])).sentences:
            c = claims.get(s.claim_id) if s.claim_id else None
            lines.append((f"（{c.id}）" if c else "") + s.text + ("".join(f"[{sid}]" for sid in c.source_ids) if c else ""))
        secs.append({"id": node.id, "title": node.title, "sentences": lines})
    by_id = {s.id: s for s in sources}
    numbers = report.source_numbers(d.outline, d.sections, list(claims.values()), sources)
    return prompts.render("workbench.view", n=n, title=d.title, sections=secs, assumptions=d.assumptions,
                          numbers=[(num, sid, by_id[sid].title or by_id[sid].url) for sid, num in numbers.items()],
                          excluded="、".join(d.excluded), notes=list(notes))


def _done(runtime: ToolRuntime, name: str, d: Draft, change: str | None, notes: list[str], summary: str) -> Command:
    """改完的工作稿写回 state，记下改动说明（一轮结束时连成版本说明，有改动就出一版）。
    change 为 None 表示报告内容没变；模型没写说明时用这一步的结果代替，改了报告就一定出版、跑校验。"""
    state = runtime.state
    changes = list(state.get("changes", [])) + ([change.strip() or summary] if change is not None else [])
    n = len(state.get("versions", [])) + 1
    sources = report.source_status(collect_sources(state.get("messages", []), d.excluded), d.sections, d.claims)
    msg = ToolMessage(content=view(d, sources, n, notes), name=name, tool_call_id=runtime.tool_call_id, artifact={"summary": summary})
    return Command(update={**d.fields(), "changes": changes, "messages": [msg]})


# ---- 整理：草稿 → 节和论断 ----


def _consolidate(planner, state: ResearchState, draft: str, part: str | None) -> tuple[object, set[str], list[str]]:
    """返回 (整理结果, 可用的来源 id, 说明)。整理只拿到草稿里引用到、没被排除的来源。"""
    msgs = state.get("messages", [])
    usable = {s.id: s for s in collect_sources(msgs, state.get("excluded", [])) if s.status != "excluded"}
    ids = cited(draft)
    notes = []
    unknown = [sid for sid in ids if sid not in usable]
    if unknown:
        notes.append(f"草稿里这些来源 id 不在搜索、读页结果里（或已被排除），挂在上面的内容没有用上：{'、'.join(unknown)}")
    try:
        res = planner.consolidate(history.requests(msgs), draft, [usable[sid] for sid in ids if sid in usable],
                                  history.user_answers(msgs), history.report_guide(msgs), part)
    except Exception as e:  # noqa: BLE001
        raise ToolException(prompts.render("workbench.failed", error=f"{type(e).__name__}: {e}")) from e
    return res, set(usable), notes


def _assemble(res, known: set[str], ids: dict[str, int], section_id: str | None = None):
    """整理结果 → (节, 正文, 论断, 说明, 用到的编号)。section_id 给了就把整理出的内容并成这一节。

    挂不上已知来源的论断丢掉；句子挂在被丢掉的论断上就一起去掉，不能悄悄变成“不需要引用的过渡句”。
    """
    ids, notes = dict(ids), []
    drafts = list(res.sections)
    if section_id is not None and drafts:
        drafts = [drafts[0].model_copy(update={"sentences": [s for sd in drafts for s in sd.sentences]})]
    nodes = []
    for sd in drafts:
        if section_id is None:
            ids["c"] = ids.get("c", 0) + 1
        nodes.append(OutlineNode(id=section_id or f"c{ids['c']}", title=sd.title.strip(), question=sd.question.strip()))
    section_of: dict[str, str] = {}
    for node, sd in zip(nodes, drafts):
        for s in sd.sentences:
            if s.claim_ref:
                section_of.setdefault(s.claim_ref, node.id)
    claims, ref_to_id = [], {}
    for cd in res.claims:
        if cd.ref not in section_of:
            continue  # 正文没用到的论断不进报告
        sids = [sid for sid in dict.fromkeys(cd.source_ids) if sid in known]
        if not sids:
            notes.append(f"丢掉了没有来源的论断：{cd.statement}")
            continue
        ids["k"] = ids.get("k", 0) + 1
        ref_to_id[cd.ref] = f"k{ids['k']}"
        claims.append(Claim(id=ref_to_id[cd.ref], statement=cd.statement.strip(), kind=cd.kind, source_ids=sids,
                            outline_id=section_of[cd.ref], confidence=cd.confidence))
    by_id = {c.id: c for c in claims}
    sections, dropped = {}, []
    for node, sd in zip(nodes, drafts):
        sents = []
        for s in sd.sentences:
            if s.claim_ref and s.claim_ref not in ref_to_id:
                dropped.append(s.text.strip())
                continue
            cid = ref_to_id.get(s.claim_ref) if s.claim_ref else None
            sents.append(Sentence(text=_mark(s.text, by_id.get(cid)), claim_id=cid))
        sections[node.id] = SectionText(outline_id=node.id, sentences=sents)
    if dropped:
        notes.append("随这些论断一起去掉的句子：" + "；".join(dropped))
    return nodes, sections, claims, notes, ids


def _clean(items: list[str], keep: list[str] = ()) -> list[str]:
    return list(dict.fromkeys([*keep, *(a.strip() for a in items if a.strip())]))


# ---- 工具 ----


def make_report_tools(planner) -> list:
    @tool("write_report", description=prompts.render("tools.write_report.description"), args_schema=WriteArgs)
    def write_report(title: str, draft: str, change: str, runtime: ToolRuntime) -> Command:
        state = runtime.state
        d = Draft.of(state)
        res, known, notes = _consolidate(planner, state, draft, None)
        nodes, sections, claims, more, ids = _assemble(res, known, d.ids)
        if not nodes:
            raise ToolException(prompts.render("workbench.failed", error="整理结果里没有任何一节"))
        new = replace(d, title=title.strip(), outline=nodes, sections=sections, claims=claims, assumptions=_clean(res.assumptions),
                      time_issues={t.source_id: t.reason.strip() for t in res.time_issues if t.source_id in known}, ids=ids)
        return _done(runtime, "write_report", new, change, notes + more, f"整理成 {len(nodes)} 节、{len(claims)} 条论断")

    @tool("revise_section", description=prompts.render("tools.revise_section.description"), args_schema=ReviseArgs)
    def revise_section(section_id: str, title: str, draft: str, change: str, runtime: ToolRuntime) -> Command:
        state = runtime.state
        d = Draft.of(state)
        sid = section_id.strip()
        if not d.outline:
            raise ToolException("还没有报告，先用 write_report 写整份。")
        if sid != NEW_SECTION and sid not in {n.id for n in d.outline}:
            raise ToolException(f"没有这一节：{sid}（以最近一次返回的报告为准）。")
        if sid == NEW_SECTION:
            d.ids["c"] = d.ids.get("c", 0) + 1
            sid = f"c{d.ids['c']}"
        res, known, notes = _consolidate(planner, state, f"## {title.strip()}\n\n{draft}", title.strip())
        nodes, sections, claims, more, ids = _assemble(res, known, d.ids, sid)
        if not nodes or not sections[sid].sentences:
            raise ToolException(prompts.render("workbench.failed", error="整理结果里没有这一节的内容"))
        node = nodes[0].model_copy(update={"title": title.strip() or nodes[0].title})
        outline = [node if n.id == sid else n for n in d.outline] if any(n.id == sid for n in d.outline) else d.outline + [node]
        # 这一节原来的论断去掉；别的节也有句子引用的留着，归到第一个引用它的节，那些句子不能变成没有来源的话
        home = {}
        for n in d.outline:
            if n.id != sid:
                for s in d.sections.get(n.id, SectionText(outline_id=n.id, sentences=[])).sentences:
                    if s.claim_id:
                        home.setdefault(s.claim_id, n.id)
        kept = [c if c.outline_id != sid else c.model_copy(update={"outline_id": home[c.id]}) for c in d.claims if c.outline_id != sid or c.id in home]
        new = replace(d, outline=outline, sections={**d.sections, sid: sections[sid]},
                      claims=kept + claims, assumptions=_clean(res.assumptions, d.assumptions),
                      time_issues={**d.time_issues, **{t.source_id: t.reason.strip() for t in res.time_issues if t.source_id in known}}, ids=ids)
        return _done(runtime, "revise_section", new, change, notes + more, f"「{node.title}」整理成 {len(claims)} 条论断")

    @tool("edit_claim", description=prompts.render("tools.edit_claim.description"), args_schema=EditArgs)
    def edit_claim(claim_id: str, statement: str, change: str, runtime: ToolRuntime) -> Command:
        d = Draft.of(runtime.state)
        cid, text = claim_id.strip(), statement.strip()
        claim = d.visible_claims().get(cid)
        if claim is None:
            raise ToolException(f"没有这条论断：{cid}（以最近一次返回的报告为准）。")
        if not text:
            raise ToolException("新的说法是空的。")
        # 只有用户在页面上给了这句新说法、模型原样用了，才算“你改过”；模型自己改的说法仍是草稿
        msgs = runtime.state.get("messages", [])
        i = history.last_human(msgs)
        act = (history.said(msgs[i]).get("action") or {}) if i >= 0 else {}
        by_user = act.get("type") == "edit_claim" and act.get("claim_id") == cid and (act.get("statement") or "").strip() == text
        claim = claim.model_copy(update={"statement": text, "status": "user_edited" if by_user else "draft"})
        # 引用它的句子可能在好几节：换掉它所在节的第一句（没有就换全文第一句），其余的告诉模型
        hits = [(n.id, i) for n in d.outline for i, s in enumerate(d.sections.get(n.id, SectionText(outline_id=n.id, sentences=[])).sentences)
                if s.claim_id == cid]
        hits.sort(key=lambda h: h[0] != claim.outline_id)
        sections = dict(d.sections)
        if hits:
            key, i = hits[0]
            sents = list(sections[key].sentences)
            sents[i] = Sentence(text=_mark(text, claim), claim_id=cid)
            sections[key] = sections[key].model_copy(update={"sentences": sents})
        others = [d.sections[k].sentences[i].text for k, i in hits[1:]]
        notes = [f"这条论断还有 {len(others)} 句也引用了它，没有改：" + "；".join(others)] if others else []
        new = replace(d, claims=[claim if c.id == cid else c for c in d.claims], sections=sections)
        return _done(runtime, "edit_claim", new, change, notes, "改了一条论断的说法")

    @tool("exclude_source", description=prompts.render("tools.exclude_source.description"), args_schema=ExcludeArgs)
    def exclude_source(source_id: str, change: str, runtime: ToolRuntime) -> Command:
        state = runtime.state
        d = Draft.of(state)
        sid = source_id.strip()
        if sid not in {s.id for s in collect_sources(state.get("messages", []))}:
            raise ToolException(f"没有这个来源：{sid}。")
        if sid in d.excluded:
            raise ToolException(f"来源 {sid} 已经排除过了。")
        claims, orphans = [], set()
        for c in d.claims:
            if sid not in c.source_ids or c.orphaned:
                claims.append(c)
                continue
            rest = [x for x in c.source_ids if x != sid]
            claims.append(c.model_copy(update={"source_ids": rest, "orphaned": not rest}))
            if not rest:
                orphans.add(c.id)
        touched = any(sid in c.source_ids for c in d.claims if not c.orphaned)
        removed, sections = [], {}
        for key, sec in d.sections.items():
            keep = [s for s in sec.sentences if s.claim_id not in orphans]
            removed += [s.text for s in sec.sentences if s.claim_id in orphans]
            sections[key] = sec.model_copy(update={"sentences": keep})
        new = replace(d, claims=claims, sections=sections, excluded=d.excluded + [sid])
        if not touched:  # 报告没引用它：只记下排除，报告不变，不出新版本
            return _done(runtime, "exclude_source", new, None, ["报告没有引用这条来源，报告不变；之后不要再用它。"], "报告没有引用它，只标记排除")
        notes = ["失去来源、从报告里删掉的句子：" + "；".join(removed)] if removed else []
        thin = [n.title for n in d.outline if d.sections.get(n.id) and d.sections[n.id].sentences and not any(s.claim_id for s in sections[n.id].sentences)]
        if thin:
            notes.append("这些节已经没有带来源的句子：" + "、".join(f"「{t}」" for t in thin))
        return _done(runtime, "exclude_source", new, change, notes, f"删掉 {len(removed)} 句" if removed else "论断改挂其余来源")

    for t in (write_report, revise_section, edit_claim, exclude_source):
        t.handle_tool_error = True  # ToolException 变成 status=error 的工具结果，交给 agent 处理
    return [write_report, revise_section, edit_claim, exclude_source]


# ---- 一轮结束：落成一版 ----


def publish(state: ResearchState, thread_dir: Path, turn_id: str) -> dict:
    """这一轮改过工作稿：写 report_vN.md、跑校验，记一版。"""
    d = Draft.of(state)
    sources = sources_of(state)
    n = len(state.get("versions", [])) + 1
    text = report.to_markdown(report.report_blocks(d.title, d.outline, d.sections, d.claims, sources, d.assumptions,
                                                   history.user_answers(state.get("messages", []))))
    thread_dir.mkdir(parents=True, exist_ok=True)
    path = thread_dir / f"report_v{n}.md"
    path.write_text(text, encoding="utf-8")
    numbers = report.source_numbers(d.outline, d.sections, d.claims, sources)
    checks = report.run_checks(d.sections, d.claims, sources, d.time_issues, numbers) + [report.report_check(d.sections, d.claims)]
    version = ReportVersion(n=n, path=str(path), note="；".join(state.get("changes", [])), turn=turn_id)
    return {"versions": list(state.get("versions", [])) + [version], "check_results": checks, "changes": []}


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
