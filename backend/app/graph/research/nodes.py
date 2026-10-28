"""调研图的节点。约定与海报图一致：interrupt 节点不调模型；调模型的节点只写 state；拼装 / 校验是纯确定性节点。

research 把 agent 子图当函数调用，子图里的 ask_user 中断会冒到外层；恢复时本节点重跑，子图从自己的检查点接着跑。
终审时用户直接说的话由 interpret 交给模型理解成具体操作，代码只校验操作的结构和对象是否存在。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.graph import END
from langgraph.types import interrupt

from app.graph.research import prompts, report, skills
from app.graph.research.agent import (
    build_agent,
    close_out,
    collect_asks,
    collect_skills,
    collect_sources,
    final_answer_text,
    search_failures,
)
from app.graph.research.answers import InvalidAnswer, card_schema, resume_value
from app.graph.research.state import (
    MAX_ASKS,
    MAX_GAP_ROUNDS,
    MAX_READS,
    MAX_SEARCHES,
    AskRecord,
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


def _user_answers(state: ResearchState) -> list[str]:
    """用户在调研途中的回答（原话）。不是模型假设，也不需要来源佐证；用户说不清楚的条件由整理步骤按假设处理。"""
    asks = [_validated(a, AskRecord) for a in state.get("asks", [])]  # 中断恢复后字段可能是 dict
    return [f"{a.question} → {a.answer}" for a in asks if not a.skipped and a.answer]


def _answered(state: ResearchState) -> bool:
    """第 3 批之前的线程没有这个字段，当时交付的报告也没标“没答完”，按已回答处理；新线程从 intake 起就有。"""
    return state.get("answered", True)


def blocks_of(state: ResearchState) -> list[dict]:
    """当前这版报告的结构化内容（页面按它渲染，终审卡和理解用户的话用的 Markdown 也由它生成）。"""
    if not state.get("versions"):
        return []
    return report.report_blocks(state["scope"], state.get("outline", []), state.get("sections", {}), state.get("claims", []),
                                state.get("sources", []), state.get("assumptions", []), _user_answers(state),
                                _answered(state), state.get("open_gap"))


def _skills(state: ResearchState) -> list[str]:
    """这次调研选用的调研方法；后来从目录里删掉的不算。"""
    return [n for n in state.get("skills", []) if n in skills.catalog()]


def _report(state: ResearchState) -> str | None:
    """所选调研方法的报告结构，给整理和重写用；没选方法时为 None，按候选答案的内容分节。"""
    reports = [skills.catalog()[n].report.strip() for n in _skills(state)]
    return "\n\n".join(reports) or None


def research_system(scope: Scope, *, allow_ask: bool, loaded: list[str] = ()) -> str:
    """还没选方法时列出技能目录，由模型决定读不读；补查那一轮直接给出已经选定的方法。"""
    if loaded:
        method = prompts.render("agent.skill_loaded", content="\n\n".join(skills.catalog()[n].content() for n in loaded))
    else:
        method = prompts.render("agent.skill_catalog", catalog=skills.catalog_text()) if skills.catalog() else ""
    return prompts.render("agent.system", today=date.today().isoformat(), scope=scope.model_dump_json(indent=1, exclude_none=True),
                          max_searches=MAX_SEARCHES, max_reads=MAX_READS, method=method,
                          ask_note="" if allow_ask else prompts.render("agent.ask_exhausted"))


def research_user(scope: Scope, gap: str | None, answer: str | None) -> str:
    """补查那一轮带上缺口和上一轮的答案。"""
    return prompts.render("agent.gap_user", question=scope.question, gap=gap, answer=answer or "") if gap else scope.question


def _mark(text: str, claim: Claim | None) -> str:
    """推断句的标记由代码按论断类型加上；提示词要求模型不要自己写。"""
    text = text.strip()
    return report.INFERENCE_PREFIX + text if claim is not None and claim.kind == "inference" else text


def make_nodes(planner: ResearchPlanner, out_dir: Path) -> dict:
    out_dir = Path(out_dir)

    # ---- 范围 ----

    def intake(state: ResearchState) -> dict:
        _stage("正在理解研究问题")
        res = planner.intake(state["request"])
        return {
            "scope": res.scope, "clarify": res.questions[:2], "scope_confirmed": False,
            "answer": None, "answer_how": "none", "answered": False, "gap": None, "open_gap": None, "gap_rounds": 0, "outline": [],
            "sources": [], "time_issues": {}, "claims": [], "assumptions": [], "asks": [], "asks_used": 0, "skills": [], "sections": {},
            "rewrite_targets": [], "rewrite_instructions": {}, "review_text": None, "review_reply": None,
            "versions": [], "check_results": [], "notes": [], "approved": False,
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
        # 回答不合法时 interrupt() 自己抛错且什么都不存，线程停在这张卡上；不要在 interrupt() 之后再 raise，那样坏回答会留在检查点里反复重放
        form = _validated(interrupt(payload, response_schema=card_schema(payload)), ClarifyForm)
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
        loaded = _skills(state)
        system = research_system(scope, allow_ask=ask_limit > 0, loaded=loaded)
        user = research_user(scope, gap, state.get("answer"))
        tools = [t for t in planner.tools() if not (loaded and t.name == "load_skill")]  # 方法已经在系统提示词里
        agent = build_agent(planner.agent_model(), tools, system, ask_limit=ask_limit)
        result = agent.invoke({"messages": [("user", user)]}, config)
        messages = result.get("messages", [])

        sources = list(state.get("sources", []))
        new_sources, _ = collect_sources(messages, sources, _next_index(sources, "s"))
        sources += new_sources

        notes = list(state.get("notes", []))
        picked = loaded + [n for n in collect_skills(messages) if n not in loaded]
        notes += [f"调研方法：{skills.catalog()[n].title}" for n in picked if n not in loaded]
        failed = search_failures(messages)
        if failed:
            notes.append(f"搜索服务出错 {len(failed)} 次（{failed[0]}），agent 换了搜索或基于已有材料继续")
        answer = final_answer_text(messages)
        how = "agent"
        if answer is None:
            _stage("调研没有收尾，基于已有材料补一次")
            answer, why = close_out(planner.agent_model(), messages)
            how = "close" if answer else "none"
            notes.append(f"agent 没有给出文字答案（{why}）")
        new_asks = collect_asks(messages)
        asks = list(state.get("asks", [])) + new_asks
        assumptions = list(state.get("assumptions", [])) + [f"「{a.question}」用户跳过了，按模型假设处理" for a in new_asks if a.skipped]
        out = {"sources": sources, "asks": asks, "assumptions": assumptions, "skills": picked,
               "asks_used": asks_used + len(new_asks), "notes": notes}  # 只算呈现给用户的提问（含跳过的）
        if answer is None and gap and state.get("answer"):
            # 补查没拿到新答案：上一轮的答案和整理结果都留着，不拿空答案冲掉，也不把同一份答案再整理一遍（结论可能碰巧翻过来）
            notes.append("补查没有给出新答案，沿用上一轮的答案和整理结果")
            return {**out, "gap": None, "open_gap": gap, "answered": False}
        # gap 留着给整理用：回补后整理不出来时，要说明还缺什么
        return {**out, "answer": answer, "answer_how": how}

    def consolidate(state: ResearchState) -> dict:
        """无工具的结构化整理：候选答案 + 已存来源 → 节、论断、来源关联。不新增事实。

        回补那一轮整理不出来时，保留上一轮的整理结果（第一轮整理不出来就是空的），标明仍没答完。
        """
        answer = state.get("answer")
        notes = list(state.get("notes", []))
        sources = list(state.get("sources", []))
        kept = {"outline": state.get("outline", []), "sections": state.get("sections", {}), "claims": state.get("claims", []),
                "time_issues": state.get("time_issues") or {}, "answered": False, "gap": None, "open_gap": state.get("gap"),
                "rewrite_targets": []}
        if not answer:
            notes.append("没有候选答案，无法整理；报告为空")
            return {**kept, "notes": notes}
        _stage("整理论断与来源")
        try:
            res = planner.consolidate(state["scope"], answer, sources, _user_answers(state), _report(state))
        except Exception as e:  # noqa: BLE001
            notes.append(f"整理失败：{e}" + ("；保留上一轮的整理结果" if kept["claims"] else ""))
            return {**kept, "notes": notes}

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
        by_id = {c.id: c for c in claims}
        dropped_sents: list[str] = []
        for i, sd in enumerate(res.sections, start=1):
            cid = f"c{i}"
            sents = []
            for s in sd.sentences:
                if s.claim_ref and s.claim_ref not in ref_to_id:
                    dropped_sents.append(s.text.strip())  # 论断被丢弃，句子不能悄悄变成“不需要引用的过渡句”
                    continue
                claim_id = ref_to_id.get(s.claim_ref) if s.claim_ref else None
                sents.append(Sentence(text=_mark(s.text, by_id.get(claim_id)), claim_id=claim_id))
            sections[cid] = SectionText(outline_id=cid, sentences=sents)
        if dropped_sents:
            notes.append("随无来源论断一起从正文移除的句子：" + "；".join(dropped_sents))
        if "c1" not in sections:
            sections["c1"] = SectionText(outline_id="c1", sentences=[])
        assumptions = list(state.get("assumptions", [])) + [a.strip() for a in res.assumptions if a.strip() and a.strip() not in state.get("assumptions", [])]
        time_issues = {t.source_id: t.reason.strip() for t in res.time_issues if t.source_id in known}

        answered = bool(res.answered)
        gap = None if answered else ((res.gap or "").strip() or "候选答案没有回答研究问题")
        rounds = state.get("gap_rounds", 0)
        if gap and rounds < MAX_GAP_ROUNDS:
            notes.append(f"整理判断问题还没回答，回研究环节补一轮：{gap}")
            return {"outline": outline, "sections": sections, "claims": claims, "assumptions": assumptions, "time_issues": time_issues,
                    "notes": notes, "answered": False, "gap": gap, "open_gap": None, "gap_rounds": rounds + 1, "rewrite_targets": []}
        if gap:
            notes.append(f"补了一轮仍没回答研究问题：{gap}")
        return {"outline": outline, "sections": sections, "claims": claims, "assumptions": assumptions, "time_issues": time_issues,
                "notes": notes, "answered": answered, "gap": None, "open_gap": gap, "rewrite_targets": []}

    # ---- 写作、校验、终审 ----

    def write(state: ResearchState, config: RunnableConfig) -> dict:
        """初稿直接拼装整理结果；终审后的改动只重写受影响的节。"""
        targets = state.get("rewrite_targets") or []
        instructions = state.get("rewrite_instructions") or {}
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
            sec = planner.write_section(state["scope"], node, usable, instructions.get(node.id), _user_answers(state), _report(state))
            by_id = {c.id: c for c in usable}
            sents = []
            for s in sec.sentences:
                if s.claim_id and s.claim_id not in by_id:
                    notes.append(f"重写「{node.title}」时丢弃挂在不可用论断上的句子：{s.text.strip()}")
                    continue
                sents.append(Sentence(text=_mark(s.text, by_id.get(s.claim_id)), claim_id=s.claim_id))
            sections[node.id] = SectionText(outline_id=node.id, sentences=sents)
        _stage("拼装报告")
        sources = report.source_status(state.get("sources", []), sections, claims)
        answered, open_gap = _answered(state), state.get("open_gap")
        text = report.assemble(state["scope"], state["outline"], sections, claims, sources,
                               state.get("assumptions", []), _user_answers(state), answered, open_gap)
        tdir = _thread_dir(out_dir, config)
        tdir.mkdir(parents=True, exist_ok=True)
        n = len(state.get("versions", [])) + 1
        path = tdir / f"report_v{n}.md"
        path.write_text(text, encoding="utf-8")
        title = {o.id: o.title for o in state["outline"]}
        note = "初稿" if n == 1 else "重写" + "，".join(f"「{title.get(t, t)}」" + (f"：{instructions[t]}" if instructions.get(t) else "")
                                                     for t in targets)
        return {
            "sections": sections, "sources": sources,
            "versions": state.get("versions", []) + [ReportVersion(n=n, path=str(path), note=note)],
            "rewrite_targets": [], "rewrite_instructions": {}, "notes": notes,
        }

    def check(state: ResearchState) -> dict:
        _stage("校验引用与标记")
        results = report.run_checks(state["sections"], state["claims"], state["sources"], state.get("time_issues") or {})
        results = [report.answer_check(state.get("answer"), state.get("answer_how", "none"))] + results
        results += report.report_check(state["sections"], state["claims"], _answered(state), state.get("open_gap"))
        return {"check_results": results}

    def _review_payload(state: ResearchState) -> dict:
        """报告从检查点里的数据生成，不读磁盘上的 report_vN.md：换了存储或文件丢了，线程照样能接着改。"""
        v = state["versions"][-1]
        blocks = blocks_of(state)
        return {
            "type": "research.review",
            "title": f"第 {v.n} 版报告",
            "version": v.n,
            "report_path": v.path,
            "report": report.to_markdown(blocks),
            "blocks": blocks,
            "sections": [
                {"id": n.id, "title": n.title,
                 "sentences": [s.model_dump() for s in state["sections"].get(n.id, SectionText(outline_id=n.id, sentences=[])).sentences]}
                for n in state["outline"]
            ],
            "claims": [c.model_dump() for c in state["claims"]],
            "sources": [s.model_dump() for s in state["sources"]],
            "checks": [c.model_dump() for c in state.get("check_results", [])],
            "assumptions": state.get("assumptions", []),
            "notes": state.get("notes", []),
            "answer_how": state.get("answer_how", "none"),
            "reply": state.get("review_reply"),
        }

    def review(state: ResearchState) -> dict:
        payload = _review_payload(state)
        d = _validated(interrupt(payload, response_schema=card_schema(payload)), ReviewDecision)  # 目标存在、字段齐全由 schema 保证
        if d.action == "instruct":
            return {"approved": False, "review_text": d.text.strip(), "review_reply": None}
        return {**_apply(state, [d]), "review_reply": None}

    def interpret(state: ResearchState) -> dict:
        """把用户在终审时直接说的话交给模型，理解成和按钮一样的操作；对不上就带着模型的回复回到终审。"""
        text = state.get("review_text") or ""
        _stage("理解你的修改要求")
        payload = _review_payload(state)
        try:
            plan = planner.interpret_review(state["scope"], payload["report"], state["outline"], state["claims"], state["sources"], text)
        except Exception as e:  # noqa: BLE001
            return {"review_text": None, "review_reply": f"这句话没能处理（{type(e).__name__}），可以再说一次，或者用下面的选项"}
        ops: list[ReviewDecision] = []
        bad: list[str] = []
        for op in plan.ops:
            fields = {k: v for k, v in op.model_dump().items() if v not in (None, "")}
            try:  # 和按钮回传走同一道校验：对象要存在、字段要齐
                ops.append(ReviewDecision.model_validate(resume_value(payload, fields)))
            except InvalidAnswer as e:
                bad.append(str(e))
        parts = [(plan.reply or "").strip()] + (["有一部分没对上：" + "；".join(bad)] if bad else [])
        reply = "；".join(x for x in parts if x) or None
        changes = [d for d in ops if d.action != "approve"]
        if not ops or (not changes and any(d.action != "approve" for d in plan.ops)):
            return {"review_text": None, "review_reply": reply or "没对上要改的地方，可以换个说法，或者用下面的选项"}
        if not changes:
            return {"approved": True, "review_text": None, "review_reply": None,
                    "notes": list(state.get("notes", [])) + [f"按你的话（{text}）理解为：不用改了"]}
        out = _apply(state, changes)
        out["notes"] = list(out.get("notes", state.get("notes", []))) + [
            f"按你的话（{text}）理解为：" + "；".join(_describe(state, d) for d in changes)]
        return {**out, "review_text": None, "review_reply": reply}

    return {
        "intake": intake, "confirm_scope": confirm_scope, "research": research, "consolidate": consolidate,
        "write": write, "check": check, "review": review, "interpret": interpret,
    }


def _apply(state: ResearchState, decisions: list[ReviewDecision]) -> dict:
    """按顺序应用终审操作（按钮来的和模型理解出来的走同一条路）。approve 只在单独出现时结束线程。"""
    if len(decisions) == 1 and decisions[0].action == "approve":
        # “不用改了”：结束线程。每一版写出来就是交付物，这里不再动论断状态——系统交付不等于人工确认
        return {"approved": True}
    sources, claims = list(state["sources"]), list(state["claims"])
    notes = list(state.get("notes", []))
    targets: list[str] = []
    instructions: dict[str, str] = {}
    for d in decisions:
        if d.action == "exclude_source":
            sources = [s.model_copy(update={"status": "excluded"}) if s.id == d.target else s for s in sources]
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
            claims = new_claims
            if orphan_ids:
                notes.append(f"排除来源 {d.target} 后，论断 {', '.join(orphan_ids)} 失去来源支撑，已退回草稿并从报告移除")
            if not affected:
                notes.append(f"来源 {d.target} 没有被任何论断引用，已标记排除，报告不变")
            targets += affected
        elif d.action == "edit_claim":
            target = next(c for c in claims if c.id == d.target)
            claims = [c.model_copy(update={"statement": d.content.strip(), "status": "user_edited"}) if c.id == d.target else c
                      for c in claims]
            targets.append(target.outline_id)
        elif d.action == "rewrite_section":
            targets.append(d.target)
            if d.instruction and d.instruction.strip():
                instructions[d.target] = d.instruction.strip()
    return {"approved": False, "sources": sources, "claims": claims, "rewrite_targets": sorted(set(targets)),
            "rewrite_instructions": instructions, "notes": notes}


def _describe(state: ResearchState, d: ReviewDecision) -> str:
    if d.action == "exclude_source":
        src = next((s for s in state["sources"] if s.id == d.target), None)
        return f"排除来源 {d.target}" + (f"（{src.title}）" if src and src.title else "")
    if d.action == "edit_claim":
        return f"论断 {d.target} 改成：{d.content.strip()}"
    if d.action == "rewrite_section":
        node = next((n for n in state["outline"] if n.id == d.target), None)
        return f"重写「{node.title if node else d.target}」" + (f"：{d.instruction.strip()}" if d.instruction else "")
    return "不用改了"


# ---- 路由 ----


def route_after_research(state: ResearchState) -> str:
    """回补那一轮没拿到新答案（research 记下了 open_gap）就直接用上一轮的整理结果出报告。"""
    return "write" if state.get("open_gap") else "consolidate"


def route_after_consolidate(state: ResearchState) -> str:
    return "research" if state.get("gap") else "write"


def route_after_review(state: ResearchState) -> str:
    if state.get("review_text"):
        return "interpret"
    return route_after_interpret(state)


def route_after_interpret(state: ResearchState) -> str:
    if state.get("rewrite_targets"):
        return "write"
    if state.get("approved"):
        return END
    return "review"  # 例如排除了一个没被引用的来源、或者没对上用户的话：没有要重写的节，回到终审
