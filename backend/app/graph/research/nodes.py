"""调研图的节点。约定与海报图一致：interrupt 节点不调模型；调模型的节点只写 state；拼装 / 校验 / 导出是纯确定性节点。

research 把 agent 子图当函数调用，子图里的 ask_user 中断会冒到外层；恢复时本节点重跑，子图从自己的检查点接着跑。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.types import interrupt

from app.graph.research import prompts, report
from app.graph.research.agent import build_agent, close_out, collect_sources, count_asks, final_answer_text
from app.graph.research.state import (
    MAX_ASKS,
    MAX_GAP_ROUNDS,
    MAX_READS,
    MAX_SEARCHES,
    Claim,
    ClarifyForm,
    OutlineNode,
    ReportVersion,
    ResearchState,
    ReviewDecision,
    Scope,
    SectionText,
    Sentence,
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


def make_nodes(planner: ResearchPlanner, out_dir: Path) -> dict:
    out_dir = Path(out_dir)

    # ---- 范围 ----

    def intake(state: ResearchState) -> dict:
        _stage("正在理解研究问题")
        res = planner.intake(state["request"])
        return {
            "scope": res.scope, "clarify": res.questions[:2], "scope_confirmed": False,
            "answer": None, "answer_how": "none", "answered": False, "gap": None, "gap_rounds": 0, "outline": [],
            "sources": [], "claims": [], "assumptions": [], "asks_used": 0, "sections": {}, "rewrite_targets": [],
            "rewrite_instruction": None, "versions": [], "check_results": [], "notes": [], "exported": None, "approved": False,
        }

    def confirm_scope(state: ResearchState) -> dict:
        """只在 intake 提出了澄清问题时才打断；需求清楚就直接按原话定范围，不弹卡。"""
        scope = state.get("scope") or Scope(question=state["request"])
        questions = state.get("clarify") or []
        if not questions:
            return {"scope": scope, "scope_confirmed": True}
        payload = {
            "type": "research.scope",
            "title": "有几处要先问清",
            "scope": scope.model_dump(),
            "questions": [q.model_dump() for q in questions],
        }
        form = _validated(interrupt(payload, response_schema=ClarifyForm), ClarifyForm)
        unknown = set(form.answers) - {q.id for q in questions}
        if unknown:
            raise ValueError(f"回答的不是当前问题：{', '.join(sorted(unknown))}")
        answered = [f"{q.title}{(form.answers.get(q.id) or '').strip()}" for q in questions if (form.answers.get(q.id) or "").strip()]
        skipped = [q.title for q in questions if not (form.answers.get(q.id) or "").strip()]
        assumptions = list(state.get("assumptions", [])) + [f"「{t}」未回答，按模型默认理解处理" for t in skipped]
        return {
            "scope": scope.model_copy(update={"extra": "；".join(answered) or None}),
            "scope_confirmed": True,
            "assumptions": assumptions,
        }

    # ---- 调研 ----

    def research(state: ResearchState, config: RunnableConfig) -> dict:
        scope = state["scope"]
        gap = state.get("gap")
        _stage("补查证据：" + gap if gap else "正在调研")
        asks_used = state.get("asks_used", 0)
        ask_limit = max(MAX_ASKS - asks_used, 0)
        allow_ask = ask_limit > 0
        system = prompts.RESEARCH_SYSTEM.format(
            scope=scope.model_dump_json(indent=1, exclude_none=True),
            max_searches=MAX_SEARCHES, max_reads=MAX_READS,
            ask_note="" if allow_ask else "（提问额度已用完，不要调用 ask_user，自行判断并在答案里写明假设。）",
        )
        user = scope.question if not gap else scope.question + prompts.GAP_FILL_NOTE.format(gap=gap, answer=state.get("answer") or "")
        agent = build_agent(planner.agent_model(), planner.tools(), system, ask_limit=ask_limit)
        result = agent.invoke({"messages": [("user", user)]}, config)
        messages = result.get("messages", [])

        sources = list(state.get("sources", []))
        new_sources, _ = collect_sources(messages, sources, _next_index(sources, "s"))
        sources += new_sources

        notes = list(state.get("notes", []))
        answer = final_answer_text(messages)
        how = "agent"
        if answer is None:
            _stage("调研没有收尾，基于已有材料补一次")
            answer, why = close_out(planner.agent_model(), messages)
            how = "close" if answer else "none"
            notes.append(f"agent 没有给出文字答案（{why}）")
        return {
            "sources": sources, "answer": answer, "answer_how": how, "gap": None,
            "asks_used": asks_used + count_asks(messages), "notes": notes,
        }

    def consolidate(state: ResearchState) -> dict:
        """无工具的结构化整理：候选答案 + 已存来源 → 节、论断、来源关联。不新增事实。"""
        answer = state.get("answer")
        notes = list(state.get("notes", []))
        sources = list(state.get("sources", []))
        if not answer:
            notes.append("没有候选答案，无法整理；报告为空")
            return {"outline": [], "sections": {}, "claims": [], "answered": False, "notes": notes, "rewrite_targets": []}
        _stage("整理论断与来源")
        try:
            res = planner.consolidate(state["scope"], answer, sources)
        except Exception as e:  # noqa: BLE001
            notes.append(f"整理失败：{e}")
            return {"outline": [], "sections": {}, "claims": [], "answered": False, "notes": notes, "rewrite_targets": []}

        known = {s.id for s in sources if s.status != "excluded"}
        claims: list[Claim] = []
        ref_to_id: dict[str, str] = {}
        sec_of_ref: dict[str, str] = {}
        outline: list[OutlineNode] = []
        sections: dict[str, SectionText] = {}
        for i, sd in enumerate(res.sections, start=1):
            cid = f"c{i}"
            outline.append(OutlineNode(id=cid, title=sd.title, question=sd.question))
            for s in sd.sentences:
                if s.claim_ref:
                    sec_of_ref.setdefault(s.claim_ref, cid)
        if not outline:
            outline.append(OutlineNode(id="c1", title="结论", question=state["scope"].question))
        idx = _next_index(state.get("claims", []), "k")
        for cd in res.claims:
            sids = [sid for sid in cd.source_ids if sid in known]
            if not sids:
                notes.append(f"丢弃无来源论断：{cd.statement}")
                continue
            claim_id = f"k{idx}"
            idx += 1
            ref_to_id[cd.ref] = claim_id
            claims.append(Claim(id=claim_id, statement=cd.statement, kind=cd.kind, source_ids=sids,
                                outline_id=sec_of_ref.get(cd.ref, outline[0].id), confidence=cd.confidence))
        kind_of = {c.id: c.kind for c in claims}
        dropped_sents: list[str] = []
        for i, sd in enumerate(res.sections, start=1):
            cid = f"c{i}"
            sents = []
            for s in sd.sentences:
                if s.claim_ref and s.claim_ref not in ref_to_id:
                    dropped_sents.append(s.text.strip())  # 论断被丢弃，句子不能悄悄变成“不需要引用的过渡句”
                    continue
                claim_id = ref_to_id.get(s.claim_ref) if s.claim_ref else None
                text = s.text.strip()
                if claim_id and kind_of.get(claim_id) == "inference" and not text.startswith(report.INFERENCE_PREFIX):
                    text = report.INFERENCE_PREFIX + text  # 推断句的标记由代码保证，不靠模型
                sents.append(Sentence(text=text, claim_id=claim_id))
            sections[cid] = SectionText(outline_id=cid, sentences=sents)
        if dropped_sents:
            notes.append("随无来源论断一起从正文移除的句子：" + "；".join(dropped_sents))
        if "c1" not in sections:
            sections["c1"] = SectionText(outline_id="c1", sentences=[])
        used = {sid for c in claims for sid in c.source_ids}
        sources = [s.model_copy(update={"status": "adopted"}) if s.id in used and s.status != "excluded" else s for s in sources]
        assumptions = list(state.get("assumptions", [])) + [a.strip() for a in res.assumptions if a.strip() and a.strip() not in state.get("assumptions", [])]

        answered = bool(res.answered)
        gap = None if answered else ((res.gap or "").strip() or "候选答案没有回答研究问题")
        rounds = state.get("gap_rounds", 0)
        if gap and rounds < MAX_GAP_ROUNDS:
            notes.append(f"整理判断问题还没回答，回研究环节补一轮：{gap}")
            return {"outline": outline, "sections": sections, "claims": claims, "sources": sources, "assumptions": assumptions,
                    "notes": notes, "answered": False, "gap": gap, "gap_rounds": rounds + 1, "rewrite_targets": []}
        if gap:
            notes.append(f"补了一轮仍没回答研究问题：{gap}")
        return {"outline": outline, "sections": sections, "claims": claims, "sources": sources, "assumptions": assumptions,
                "notes": notes, "answered": answered, "gap": None, "rewrite_targets": []}

    # ---- 写作、校验、终审 ----

    def write(state: ResearchState, config: RunnableConfig) -> dict:
        """初稿直接拼装整理结果；终审后的改动只重写受影响的节。"""
        targets = state.get("rewrite_targets") or []
        instruction = state.get("rewrite_instruction")
        sections = dict(state.get("sections", {}))
        claims = state.get("claims", [])
        notes = list(state.get("notes", []))
        for node in state["outline"]:
            if node.id not in targets:
                continue
            _stage(f"重写：{node.title}")
            usable = [c for c in claims if c.outline_id == node.id and not c.orphaned]
            if not usable:
                sections[node.id] = SectionText(outline_id=node.id, sentences=[])
                notes.append(f"「{node.title}」没有可用论断，正文留空")
                continue
            sec = planner.write_section(state["scope"], node, usable, instruction)
            kind_of = {c.id: c.kind for c in usable}
            sents = []
            for s in sec.sentences:
                if s.claim_id and s.claim_id not in kind_of:
                    notes.append(f"重写「{node.title}」时丢弃挂在不可用论断上的句子：{s.text.strip()}")
                    continue
                claim_id = s.claim_id
                text = s.text.strip()
                if claim_id and kind_of[claim_id] == "inference" and not text.startswith(report.INFERENCE_PREFIX):
                    text = report.INFERENCE_PREFIX + text  # 推断标记由代码保证，重写也一样
                sents.append(Sentence(text=text, claim_id=claim_id))
            sections[node.id] = SectionText(outline_id=node.id, sentences=sents)
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
        results = report.run_checks(state["sections"], state["claims"], state["sources"], state["scope"])
        results = [report.answer_check(state.get("answer"), state.get("answer_how", "none"))] + results
        results += report.report_check(state["sections"], state["claims"], state.get("answered", False))
        return {"check_results": results}

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
            "claims": [c.model_dump() for c in claims],
            "sources": [s.model_dump() for s in sources],
            "checks": [c.model_dump() for c in state.get("check_results", [])],
            "assumptions": state.get("assumptions", []),
            "notes": state.get("notes", []),
            "answer_how": state.get("answer_how", "none"),
        }
        d = _validated(interrupt(payload, response_schema=ReviewDecision), ReviewDecision)
        if d.action == "approve":
            invalid = [c for c in state.get("check_results", []) if c.name == "report_valid" and not c.passed]
            if invalid:
                raise ValueError("报告里没有任何带来源的论断，不能当作成功结果导出；请换个问法重新发起")
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
        # rewrite_section
        if d.target not in {n.id for n in state["outline"]}:
            raise ValueError(f"章节不存在：{d.target}")
        return {"approved": False, "rewrite_targets": [d.target], "rewrite_instruction": d.instruction}

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
                   "status": "confirmed", "provenance": "research.consolidate"} for n in state["outline"]]
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
        "intake": intake, "confirm_scope": confirm_scope, "research": research, "consolidate": consolidate,
        "write": write, "check": check, "review": review, "export": export,
    }


# ---- 路由 ----


def route_after_consolidate(state: ResearchState) -> str:
    return "research" if state.get("gap") else "write"


def route_after_review(state: ResearchState) -> str:
    if state.get("rewrite_targets"):
        return "write"
    if state.get("approved"):
        return "export"
    return "review"  # 例如排除了一个没被引用的来源：没有要重写的节，回到终审
