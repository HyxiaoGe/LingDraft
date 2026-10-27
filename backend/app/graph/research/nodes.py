"""调研图的节点。约定与海报图一致：interrupt 节点不调模型；调模型的节点只写 state；拼装 / 校验 / 导出是纯确定性节点。

research_section 是例外：它把 agent 子图当函数调用，子图里的 ask_user 中断会冒到外层；恢复时本节点重跑，
子图从自己的检查点接着跑（create_agent(checkpointer=True)）。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.types import interrupt

from app.graph.research import prompts, report
from app.graph.research.agent import Findings, build_agent, collect_sources, count_asks
from app.graph.research.state import (
    ASK_LIMIT_PER_THREAD,
    MAX_OUTLINE_NODES,
    MAX_READS_PER_SECTION,
    MAX_SEARCHES_PER_SECTION,
    SCOPE_FIELDS,
    Claim,
    OutlineDecision,
    OutlineNode,
    ReportVersion,
    ResearchState,
    ReviewDecision,
    Scope,
    ScopeForm,
    SectionText,
    Source,
)
from app.research_llm import ResearchPlanner


def _stage(message: str) -> None:
    try:
        get_stream_writer()({"stage": message})
    except RuntimeError:
        pass


def _thread_dir(out_dir: Path, config: RunnableConfig) -> Path:
    return Path(out_dir) / str(config["configurable"]["thread_id"])


def _validated(value, model):
    return value if isinstance(value, model) else model.model_validate(value)


def _next_index(items, prefix: str) -> int:
    nums = [int(x.id[len(prefix):]) for x in items if x.id.startswith(prefix) and x.id[len(prefix):].isdigit()]
    return (max(nums) + 1) if nums else 1


def _source_view(s: Source) -> dict:
    return s.model_dump()


def _claim_view(c: Claim) -> dict:
    return c.model_dump()


def make_nodes(planner: ResearchPlanner, out_dir: Path) -> dict:
    out_dir = Path(out_dir)

    # ---- 范围 ----

    def intake(state: ResearchState) -> dict:
        _stage("正在理解研究问题")
        scope = planner.intake(state["request"])
        return {
            "scope": scope, "scope_confirmed": False, "outline": [], "outline_confirmed": False, "section_idx": 0,
            "sources": [], "claims": [], "assumptions": [], "asks_used": 0, "sections": {}, "rewrite_targets": [],
            "rewrite_instruction": None, "versions": [], "check_results": [], "notes": [], "exported": None, "approved": False,
        }

    def confirm_scope(state: ResearchState) -> dict:
        scope = state.get("scope") or Scope(question=state["request"])
        payload = {
            "type": "research.scope",
            "title": "先定研究范围",
            "fields": [
                {"key": k, "label": label, "value": getattr(scope, k), "required": req, "hint": hint,
                 "source": "inferred" if getattr(scope, k) else "missing",
                 # 只有推断出了值（要你确认）或必填的才值得问；其余默认"不限"，确认后可改
                 "ask": bool(getattr(scope, k)) or req}
                for k, label, req, hint in SCOPE_FIELDS
            ],
        }
        form = _validated(interrupt(payload, response_schema=ScopeForm), ScopeForm)
        values = {k: (v.strip() if isinstance(v, str) and v.strip() else None) for k, v in form.values.items()}
        merged = scope.model_dump()
        merged.update({k: v for k, v in values.items() if k in merged})
        return {"scope": Scope(**merged), "scope_confirmed": True}

    # ---- 大纲与调研 ----

    def plan(state: ResearchState) -> dict:
        _stage("拟候选大纲")
        nodes = planner.plan(state["scope"])[:MAX_OUTLINE_NODES]
        return {"outline": [n.model_copy(update={"researched": False}) for n in nodes], "outline_confirmed": False}

    def research_section(state: ResearchState, config: RunnableConfig) -> dict:
        outline = state["outline"]
        node = next(n for n in outline if not n.researched)
        _stage(f"调研：{node.title}")
        scope = state["scope"]
        asks_used = state.get("asks_used", 0)
        allow_ask = asks_used < ASK_LIMIT_PER_THREAD
        others = "；".join(f"{n.id} {n.title}" for n in outline if n.id != node.id) or "（无）"
        system = prompts.AGENT_SYSTEM.format(
            scope=scope.model_dump_json(indent=1),
            title=node.title,
            question=node.question,
            others=others,
            max_searches=MAX_SEARCHES_PER_SECTION,
            max_reads=MAX_READS_PER_SECTION,
            ask_note="" if allow_ask else "（本次调研的提问额度已用完，不要调用 ask_user，自行判断并写进 assumptions。）",
        )
        agent = build_agent(planner.agent_model(), planner.tools(), system, allow_ask=allow_ask)
        brief = f"[section:{node.id}] 请为章节“{node.title}”收集证据并输出 Findings。"
        result = agent.invoke({"messages": [("user", brief)]}, config)

        messages = result.get("messages", [])
        sources = list(state.get("sources", []))
        new_sources, url_to_id = collect_sources(messages, sources, _next_index(sources, "s"))
        sources += new_sources

        findings = result.get("structured_response")
        notes = list(state.get("notes", []))
        claims = list(state.get("claims", []))
        assumptions = list(state.get("assumptions", []))
        if isinstance(findings, Findings):
            idx = _next_index(claims, "k")
            for pc in findings.claims:
                sids = [url_to_id[u] for u in pc.source_urls if u in url_to_id]
                if not sids:
                    notes.append(f"丢弃无来源论断：{pc.statement}")
                    continue
                claims.append(Claim(id=f"k{idx}", statement=pc.statement, kind=pc.kind, source_ids=sids,
                                    outline_id=node.id, confidence=pc.confidence))
                idx += 1
            assumptions += [a for a in findings.assumptions if a not in assumptions]
        else:
            notes.append(f"章节“{node.title}”没有产出结构化结果（可能触发了调用上限）")

        new_outline = [n.model_copy(update={"researched": True}) if n.id == node.id else n for n in outline]
        return {
            "outline": new_outline, "sources": sources, "claims": claims, "assumptions": assumptions,
            "asks_used": asks_used + count_asks(messages), "notes": notes,
        }

    def confirm_outline(state: ResearchState) -> dict:
        outline = state["outline"]
        claims = state.get("claims", [])
        sources = {s.id: s for s in state.get("sources", [])}
        payload = {
            "type": "research.outline",
            "title": "大纲与找到的证据",
            "nodes": [
                {
                    "id": n.id, "title": n.title, "question": n.question,
                    "claims": [_claim_view(c) for c in claims if c.outline_id == n.id and not c.orphaned],
                }
                for n in outline
            ],
            "sources": [_source_view(s) for s in sources.values()],
            "assumptions": state.get("assumptions", []),
            "note": "确认大纲即采纳这些论断引用的来源；终审时仍可排除。",
        }
        decision = _validated(interrupt(payload, response_schema=OutlineDecision), OutlineDecision)
        if decision.action == "approve":
            used = {sid for c in claims if not c.orphaned for sid in c.source_ids}
            new_sources = [s.model_copy(update={"status": "adopted"}) if s.id in used and s.status != "excluded" else s
                           for s in sources.values()]
            return {"outline_confirmed": True, "sources": new_sources,
                    "rewrite_targets": [n.id for n in outline], "rewrite_instruction": None}
        # 编辑：保留带 id 的章节（含其调研结果），新章节待调研；删掉的章节其论断不再进报告
        by_id = {n.id: n for n in outline}
        new_outline: list[OutlineNode] = []
        next_n = _next_index(outline, "c")
        for e in decision.nodes or []:
            if e.id and e.id in by_id:
                new_outline.append(by_id[e.id].model_copy(update={"title": e.title, "question": e.question}))
            else:
                new_outline.append(OutlineNode(id=f"c{next_n}", title=e.title, question=e.question))
                next_n += 1
        keep = {n.id for n in new_outline}
        return {"outline": new_outline[:MAX_OUTLINE_NODES], "outline_confirmed": False,
                "claims": [c for c in claims if c.outline_id in keep]}

    # ---- 写作、校验、终审 ----

    def write(state: ResearchState, config: RunnableConfig) -> dict:
        targets = state.get("rewrite_targets") or [n.id for n in state["outline"]]
        instruction = state.get("rewrite_instruction")
        sections = dict(state.get("sections", {}))
        claims = state.get("claims", [])
        notes = list(state.get("notes", []))
        for node in state["outline"]:
            if node.id not in targets:
                continue
            _stage(f"撰写：{node.title}")
            usable = [c for c in claims if c.outline_id == node.id and not c.orphaned]
            if not usable:
                sections[node.id] = SectionText(outline_id=node.id, sentences=[])
                notes.append(f"章节“{node.title}”没有可用论断，正文留空")
                continue
            sec = planner.write_section(state["scope"], node, usable, instruction)
            ok_ids = {c.id for c in usable}
            sec = SectionText(outline_id=node.id, sentences=[
                s if (s.claim_id is None or s.claim_id in ok_ids) else s.model_copy(update={"claim_id": None})
                for s in sec.sentences
            ])
            sections[node.id] = sec
        _stage("拼装报告")
        text = report.assemble(state["scope"], state["outline"], sections, claims, state.get("sources", []),
                               state.get("assumptions", []))
        tdir = _thread_dir(out_dir, config)
        tdir.mkdir(parents=True, exist_ok=True)
        n = len(state.get("versions", [])) + 1
        path = tdir / f"report_v{n}.md"
        path.write_text(text, encoding="utf-8")
        note = "初稿" if n == 1 else (f"重写 {', '.join(targets)}" + (f"：{instruction}" if instruction else ""))
        return {
            "sections": sections,
            "versions": state.get("versions", []) + [ReportVersion(n=n, path=str(path), note=note)],
            "rewrite_targets": [], "rewrite_instruction": None, "notes": notes,
        }

    def check(state: ResearchState) -> dict:
        _stage("校验引用与标记")
        return {"check_results": report.run_checks(state["sections"], state["claims"], state["sources"], state["scope"])}

    def review(state: ResearchState) -> dict:
        v = state["versions"][-1]
        claims = state["claims"]
        sources = state["sources"]
        payload = {
            "type": "research.review",
            "title": f"第 {v.n} 版报告",
            "version": v.n,
            "report_path": v.path,
            "report": Path(v.path).read_text(encoding="utf-8"),
            "sections": [
                {"id": n.id, "title": n.title,
                 "sentences": [s.model_dump() for s in state["sections"].get(n.id, SectionText(outline_id=n.id, sentences=[])).sentences]}
                for n in state["outline"]
            ],
            "claims": [_claim_view(c) for c in claims],
            "sources": [_source_view(s) for s in sources],
            "checks": [c.model_dump() for c in state.get("check_results", [])],
            "assumptions": state.get("assumptions", []),
            "notes": state.get("notes", []),
        }
        d = _validated(interrupt(payload, response_schema=ReviewDecision), ReviewDecision)
        if d.action == "approve":
            return {"approved": True,
                    "claims": [c.model_copy(update={"status": "confirmed"}) if c.status == "draft" and not c.orphaned else c
                               for c in claims]}
        if d.action == "exclude_source":
            if not d.target or d.target not in {s.id for s in sources}:
                raise ValueError(f"来源不存在：{d.target}")
            new_sources = [s.model_copy(update={"status": "excluded"}) if s.id == d.target else s for s in sources]
            new_claims, affected, orphan_ids = [], [], []
            for c in claims:
                if d.target not in c.source_ids:
                    new_claims.append(c)
                    continue
                rest = [sid for sid in c.source_ids if sid != d.target]
                if rest:
                    new_claims.append(c.model_copy(update={"source_ids": rest}))
                else:
                    new_claims.append(c.model_copy(update={"source_ids": [], "orphaned": True, "status": "draft"}))
                    orphan_ids.append(c.id)
                affected.append(c.outline_id)
            notes = list(state.get("notes", []))
            if orphan_ids:
                notes.append(f"排除来源 {d.target} 后，论断 {', '.join(orphan_ids)} 失去来源支撑，已退回草稿并从报告移除")
            if not affected:
                notes.append(f"来源 {d.target} 没有被任何论断引用，已标记排除，报告不变")
            return {"approved": False, "sources": new_sources, "claims": new_claims, "rewrite_targets": sorted(set(affected)),
                    "rewrite_instruction": None, "notes": notes}
        if d.action == "edit_claim":
            target = next((c for c in claims if c.id == d.target), None)
            if target is None or not (d.content or "").strip():
                raise ValueError("edit_claim 需要有效的 target 与 content")
            new_claims = [c.model_copy(update={"statement": d.content.strip(), "status": "user_edited"}) if c.id == d.target else c
                          for c in claims]
            return {"approved": False, "claims": new_claims, "rewrite_targets": [target.outline_id], "rewrite_instruction": None}
        if d.action == "rewrite_section":
            if d.target not in {n.id for n in state["outline"]}:
                raise ValueError(f"章节不存在：{d.target}")
            return {"approved": False, "rewrite_targets": [d.target], "rewrite_instruction": d.instruction}
        # reopen_outline
        return {"approved": False, "outline_confirmed": False}

    def export(state: ResearchState, config: RunnableConfig) -> dict:
        _stage("导出确认物包")
        tdir = _thread_dir(out_dir, config)
        pkg = tdir / "package"
        pkg.mkdir(parents=True, exist_ok=True)
        v = state["versions"][-1]
        shutil.copyfile(v.path, pkg / "report.md")
        scope = state["scope"]
        items = [{"id": "scope", "kind": "scope", "content": scope.model_dump(), "status": "confirmed", "provenance": "research.scope"}]
        items += [{"id": n.id, "kind": "outline", "content": {"title": n.title, "question": n.question},
                   "status": "confirmed", "provenance": "research.outline"} for n in state["outline"]]
        items += [{"id": c.id, "kind": f"claim.{c.kind}", "content": c.statement, "status": c.status,
                   "source_ids": c.source_ids, "outline_id": c.outline_id, "confidence": c.confidence,
                   "provenance": "research.review"} for c in state["claims"] if not c.orphaned and c.status != "draft"]
        (pkg / "items.json").write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        (pkg / "sources.json").write_text(
            json.dumps([s.model_dump() for s in state["sources"] if s.status == "adopted"], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        manifest = {"scenario": "research", "thread_id": config["configurable"]["thread_id"], "version": v.n,
                    "question": scope.question, "files": ["report.md", "items.json", "sources.json"]}
        (pkg / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"exported": {"version": v.n, "dir": str(pkg), "report": str(pkg / "report.md"),
                             "items": str(pkg / "items.json"), "sources": str(pkg / "sources.json"),
                             "manifest": str(pkg / "manifest.json")}}

    return {
        "intake": intake, "confirm_scope": confirm_scope, "plan": plan, "research_section": research_section,
        "confirm_outline": confirm_outline, "write": write, "check": check, "review": review, "export": export,
    }


# ---- 路由 ----


def route_after_research(state: ResearchState) -> str:
    return "research_section" if any(not n.researched for n in state["outline"]) else "confirm_outline"


def route_after_outline(state: ResearchState) -> str:
    if state.get("outline_confirmed"):
        return "write"
    return "research_section" if any(not n.researched for n in state["outline"]) else "confirm_outline"


def route_after_review(state: ResearchState) -> str:
    if state.get("rewrite_targets"):
        return "write"
    if not state.get("outline_confirmed", True):
        return "confirm_outline"
    if state.get("approved"):
        return "export"
    return "review"  # 例如排除了一个没被引用的来源：没有要重写的章，回到终审
