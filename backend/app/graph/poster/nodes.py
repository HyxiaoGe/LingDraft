"""海报图的节点。约定：

- 一个节点最多一个 interrupt，且 interrupt 节点不调用模型（resume 时节点会从头重跑）。
- 调模型的节点只产出结构化结果并写回 state，不做副作用。
- render / check / export 是纯确定性节点。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.types import interrupt
from pydantic import ValidationError

from app import config as cfg
from app.graph.poster.state import (
    KIND_LABELS,
    TEXT_KINDS,
    Element,
    Fact,
    FactsForm,
    PosterState,
    ReviewAction,
    StyleChoice,
    Version,
    empty_facts,
)
from app.llm import Planner
from app.render import checks as checks_mod
from app.render import renderer

DEFAULT_TEMPLATE = "bold_sale"


def _stage(message: str) -> None:
    """向前端推送"正在做什么"。节点在图外被直接调用（测试）时静默。"""
    try:
        get_stream_writer()({"stage": message})
    except RuntimeError:
        pass


def _thread_dir(out_dir: Path, config: RunnableConfig) -> Path:
    return Path(out_dir) / str(config["configurable"]["thread_id"])


def _validated(value, model):
    return value if isinstance(value, model) else model.model_validate(value)


def _skeleton(facts: dict[str, Fact], product_image: str | None) -> list[Element]:
    """初始元素清单。事实槽位绑定事实；文本槽位留给模型；素材槽位锁定。"""
    els = [
        Element(id="background", kind="background", slot="background"),
        Element(id="product_image", kind="product_image", slot="product_image", content=product_image, status="confirmed"),
        Element(id="headline", kind="headline", slot="headline"),
        Element(id="subhead", kind="subhead", slot="subhead"),
        Element(id="offer", kind="offer", slot="offer", bound_fact="offer"),
        Element(id="date", kind="date", slot="date", bound_fact="date_range"),
    ]
    if facts["cta"].value:
        els.append(Element(id="cta", kind="cta", slot="cta", bound_fact="cta"))
    else:
        els.append(Element(id="cta", kind="cta", slot="cta"))
    return els


def _rebind_cta(elements: list[Element], old: dict[str, Fact], new: dict[str, Fact]) -> list[Element]:
    """cta 是唯一“可选事实”：用户填了就绑定事实，清空了就解绑并保留原文字交由用户/模型处理。"""
    out = []
    for e in elements:
        if e.id == "cta":
            if new["cta"].value and e.bound_fact is None:
                e = e.model_copy(update={"bound_fact": "cta", "content": None})
            elif not new["cta"].value and e.bound_fact == "cta":
                e = e.model_copy(update={"bound_fact": None, "content": old["cta"].value, "status": "draft"})
        out.append(e)
    return out


def _apply_updates(elements: list[Element], updates, allowed: set[str]) -> tuple[list[Element], list[str]]:
    """只把更新应用到 allowed 且未锁定的元素；返回新列表与实际改动的 id。"""
    by_id = {u.id: u.content for u in updates}
    out, changed = [], []
    for e in elements:
        if e.id in allowed and e.id in by_id and not e.locked:
            new_content = by_id[e.id]
            if new_content != e.content:
                e = e.model_copy(update={"content": new_content, "version": e.version + (1 if e.content else 0)})
                changed.append(e.id)
        out.append(e)
    return out, changed


def _lock_others(elements: list[Element], keep_draft: set[str]) -> list[Element]:
    """用户针对某元素提意见时，其余 draft 元素视为已确认。"""
    return [
        e if (e.id in keep_draft or e.status != "draft") else e.model_copy(update={"status": "confirmed"})
        for e in elements
    ]


def _element_view(e: Element, facts: dict[str, Fact]) -> dict:
    return {
        "id": e.id,
        "kind": e.kind,
        "status": e.status,
        "bound_fact": e.bound_fact,
        "text": renderer.resolve_text(e, facts) if e.kind != "product_image" else None,
        # 已确认的元素被点名时会重新打开，所以只有事实绑定和手工编辑过的才不可由模型改
        "editable_by_model": e.bound_fact is None and e.status != "user_edited",
        "version": e.version,
    }


def _describe(a: ReviewAction, facts: dict[str, Fact]) -> str:
    name = KIND_LABELS.get(a.target, a.target)  # 元素 id 和类型同名
    if a.action == "edit":
        return f"修改{name}：{a.instruction}"
    if a.action == "manual_edit":
        return f"{name}改成“{a.content}”" if a.content else f"去掉{name}"
    if a.action == "change_fact":
        return f"{facts[a.target].label}改成“{a.content}”" if a.target in facts and a.content else "修改事实"
    if a.action == "change_style":
        return f"换风格：{a.instruction}" if a.instruction else "换风格"
    return "就这版，导出"


def _worth_retry(elements: list[Element], results) -> bool:
    """只有模型能重写的槽位出问题才值得重试：事实槽位、用户改过或已确认的文案，重写也改不到它们。"""
    rewritable = {e.slot for e in elements if not e.locked}
    return any(not r.passed and rewritable & set(r.slots) for r in results)


def _style_change(instruction: str | None) -> dict:
    """全局换风格：元素内容一个不动，只换配色/背景后重渲染。"""
    return {"style": None, "style_changed": True, "style_hint": instruction or None}


def make_nodes(planner: Planner, out_dir: Path):
    # ---- 生成前 ----

    def intake(state: PosterState) -> dict:
        _stage("正在理解需求，提取关键事实…")
        res = planner.intake(state["request"])
        facts = empty_facts()
        for k, f in facts.items():
            v = getattr(res, k, None)
            if v:
                facts[k] = f.model_copy(update={"value": v, "source": "inferred"})
        return {
            "facts": facts,
            "style_hint": res.style_hint,
            "style": None,
            "style_options": [],
            "elements": [],
            "versions": [],
            "check_results": [],
            "retry_count": 0,
            "notes": [],
            "last_change": [],
            "exported": None,
            "background_image": None,
            "style_changed": False,
            "template_id": DEFAULT_TEMPLATE,
        }

    def confirm_facts(state: PosterState) -> dict:
        facts = state["facts"]
        proposals = state.get("fact_proposals") or {}

        def field(f: Fact) -> dict:
            if f.key in proposals:
                # 终审时用户说的新值只预填，仍由用户在这张卡上确认
                return {"key": f.key, "label": f.label, "value": proposals[f.key], "required": f.required, "source": "proposed", "previous": f.value}
            return {"key": f.key, "label": f.label, "value": f.value, "required": f.required, "source": f.source}

        payload = {
            "type": "confirm_facts",
            "message": "请确认海报上的关键事实。带 * 为必填；留空的事实不会出现在海报上。",
            "fields": [field(f) for f in facts.values()],
        }
        form = _validated(interrupt(payload, response_schema=FactsForm), FactsForm)
        new_facts = {}
        for k, f in facts.items():
            v = (form.values.get(k) or "").strip() or None
            new_facts[k] = f.model_copy(update={"value": v, "status": "confirmed", "source": "user"})
        update = {"facts": new_facts, "pending_action": None, "fact_proposals": {}}
        if state.get("elements"):
            update["render_reason"] = "事实更新"
            update["elements"] = _rebind_cta(state["elements"], state["facts"], new_facts)
        return update

    def gen_style_options(state: PosterState) -> dict:
        hint = state.get("style_hint")
        _stage("正在构思视觉方向…")
        options = planner.style_options(state["request"], state["facts"], hint)
        if hint:
            return {"style_options": (state.get("style_options") or []) + options, "style": options[0]}
        return {"style_options": options}

    def ask_style(state: PosterState) -> dict:
        options = state["style_options"]
        payload = {
            "type": "choose_style",
            "message": "选择一个视觉方向，或用一句话描述你想要的风格。"
            + ("（换风格不会改动任何已有文案）" if state.get("style_changed") else ""),
            "options": [o.model_dump() for o in options],
            "allow_custom": True,
        }
        choice = _validated(interrupt(payload, response_schema=StyleChoice), StyleChoice)
        if choice.style_id:
            match = [o for o in options if o.id == choice.style_id]
            if not match:
                raise ValueError(f"未知风格 id: {choice.style_id}")
            return {"style": match[0]}
        # 自定义描述：不在 interrupt 节点里调模型，交给 gen_style_options 按 hint 生成
        return {"style_hint": choice.custom, "style": None}

    def gen_background(state: PosterState, config: RunnableConfig) -> dict:
        """按当前风格生成一张背景图；关闭开关时清空，模板退回渐变。"""
        update = {"background_image": None, "style_changed": False}
        if state.get("elements"):
            update["render_reason"] = f"更换风格：{state['style'].name}"
        if not cfg.BACKGROUND_ENABLED:
            return update
        _stage("正在生成背景图（约 15 秒）…")
        tdir = _thread_dir(out_dir, config)
        n = len(state.get("versions") or []) + 1
        path = planner.background(state["style"], state["facts"], tdir / f"bg_v{n}.png")
        update["background_image"] = str(path)
        return update

    # ---- 生成 ----

    def plan_layout(state: PosterState) -> dict:
        elements = state.get("elements") or _skeleton(state["facts"], state.get("product_image"))
        targets = [e.id for e in elements if e.kind in TEXT_KINDS and not e.locked]
        constraints = checks_mod.constraints_from(state.get("check_results", []))
        _stage("正在撰写文案…" if not constraints else "校验未通过，正在按约束重写文案…")
        updates = planner.compose(state["facts"], state["style"], elements, targets, None, constraints)
        elements, changed = _apply_updates(elements, updates, set(targets))
        reason = "校验未通过后重新生成" if constraints else "初稿"
        return {
            "elements": elements,
            "last_change": changed,
            "render_reason": reason,
            "template_id": state.get("template_id") or DEFAULT_TEMPLATE,
        }

    def render(state: PosterState, config: RunnableConfig) -> dict:
        versions = list(state.get("versions", []))
        n = len(versions) + 1
        _stage(f"正在排版渲染第 {n} 版…")
        res = renderer.render(
            state["elements"],
            state["facts"],
            state["style"],
            state["template_id"],
            _thread_dir(out_dir, config),
            n,
            background_image=state.get("background_image"),
        )
        note = state.get("render_reason") or "重新渲染"
        versions.append(Version(n=n, elements=list(state["elements"]), html_path=res.html_path, png_path=res.png_path, note=note, layout=res.layout))
        return {"versions": versions}

    def check(state: PosterState) -> dict:
        layout = state["versions"][-1].layout
        _stage("正在校验事实、文案合规与排版…")
        try:
            issues = planner.check_copy(state["facts"], checks_mod.free_copy(state["elements"], layout))
        except Exception:  # 合规检查失败不挡交付：如实标为没检查，留给用户看
            issues = None
        results = checks_mod.run_checks(state["elements"], state["facts"], layout, issues)
        retry = _worth_retry(state["elements"], results)
        return {"check_results": results, "retry_count": (state.get("retry_count", 0) + 1) if retry else 0}

    # ---- 生成后 ----

    def review(state: PosterState) -> dict:
        v = state["versions"][-1]
        payload = {
            "type": "review",
            "message": "查看预览。可以确认、针对某个元素提修改意见、直接改文字，或修改事实。",
            "version": v.n,
            "png_path": v.png_path,
            "html_path": v.html_path,
            "elements": [_element_view(e, state["facts"]) for e in state["elements"]],
            "checks": [c.model_dump() for c in state.get("check_results", [])],
            "notes": state.get("notes", []),
            "reply": state.get("review_reply"),
            "actions": ["approve", "edit", "manual_edit", "change_fact", "change_style", "instruct"],
            "style": state["style"].model_dump(),
            "style_options": [o.model_dump() for o in state.get("style_options", [])],
        }
        action = _validated(interrupt(payload, response_schema=ReviewAction), ReviewAction)
        # 每次用户操作后重新计重试次数（改事实、换风格之后的校验也能重试）
        update = {"pending_action": action.model_dump(), "pending_edits": [], "notes": [], "review_reply": None, "retry_count": 0,
                  "review_text": action.text if action.action == "instruct" else None}
        if action.action == "change_style":
            update.update(_style_change(action.instruction))
        return update

    def interpret(state: PosterState) -> dict:
        """终审时用户直接说的话：交给模型理解成操作，逐条过和按钮回传一样的校验后执行。

        一次只做一类修改：有文案修改就先做文案修改（几处一起改）；否则改事实（几项一起预填）或换风格；
        都没有才导出。没做的部分和对不上的地方写进 reply，显示在下一张终审卡上。
        """
        text = state["review_text"]
        facts = state["facts"]
        views = {e.id: _element_view(e, facts) for e in state["elements"]}
        _stage("正在理解你的意见…")
        try:
            plan = planner.interpret_review(facts, state["style"], list(views.values()), text)
        except Exception:
            return {"pending_action": None, "review_text": None, "review_reply": "这句话没能处理，请换个说法，或者用卡片上的按钮。"}
        edits, fact_ops, style_ops, approve, bad = [], [], [], False, []
        for op in plan.ops:
            try:
                a = ReviewAction.model_validate({k: v for k, v in op.model_dump().items() if v is not None})
            except ValidationError:
                bad.append(f"有一处没理解完整（{op.action}）")
                continue
            v = views.get(a.target) if a.target else None
            name = KIND_LABELS.get(a.target, a.target)
            if a.action in ("edit", "manual_edit"):
                if not v or v["kind"] not in TEXT_KINDS | {"offer", "date"}:
                    bad.append(f"海报上没有“{name}”这处文案")
                elif v["bound_fact"]:
                    bad.append(f"{name}显示的是事实“{facts[v['bound_fact']].label}”，要改请说改成什么，会打开事实确认")
                elif a.action == "edit" and not v["editable_by_model"]:
                    bad.append(f"{name}你手工改过，模型不再改写；请直接给出新文字")
                else:
                    edits.append(a)
            elif a.action == "change_fact":
                if a.target is not None and a.target not in facts:
                    bad.append(f"没有 {a.target} 这项事实")
                else:
                    fact_ops.append(a)
            elif a.action == "change_style":
                style_ops.append(a)
            else:
                approve = True
        replies = [plan.reply] if plan.reply else []
        replies += bad
        if edits:
            chosen = edits
            left = "、".join(x for x, ops in (("改事实", fact_ops), ("换风格", style_ops)) if ops)
            if left:
                replies.append(f"{left}这次没做：先改了文案，看完这版再说一次。")
            update = {"pending_action": {"action": "edits"}, "pending_edits": [a.model_dump() for a in edits]}
        elif fact_ops:
            chosen = fact_ops
            if style_ops:
                replies.append("换风格这次没做：先确认事实，看完新版再说一次。")
            proposals = {a.target: a.content for a in fact_ops if a.target and a.content}
            update = {"pending_action": {"action": "change_fact"}, "fact_proposals": proposals}
        elif style_ops:
            chosen = style_ops[:1]
            update = {"pending_action": style_ops[0].model_dump(), **_style_change(style_ops[0].instruction)}
        elif approve:
            chosen = []
            update = {"pending_action": {"action": "approve"}}
        else:
            return {"pending_action": None, "review_text": None,
                    "review_reply": "；".join(replies) or "没对上要改的地方。可以点海报上的元素，或者说具体改哪一处、怎么改。"}
        update["review_text"] = None
        update["review_reply"] = "；".join(replies) or None
        if chosen:
            update["notes"] = [f"按你的话（“{text}”）理解为：" + "；".join(_describe(a, facts) for a in chosen)]
        return update

    def _revise_one(state: PosterState, elements: list[Element], action: ReviewAction) -> tuple[list[Element], list[str], str | None]:
        """按一条 edit 改一个元素：返回新元素列表、改动的 id、没改成时的说明。"""
        target = action.target
        e = next((x for x in elements if x.id == target), None)
        if e is None:
            return elements, [], f"没有名为 {target} 的元素"
        if e.bound_fact:
            return elements, [], f"{target} 绑定了事实“{state['facts'][e.bound_fact].label}”，模型不能改写。要改它请使用 change_fact 走事实确认流程。"
        if e.status == "user_edited":
            return elements, [], f"{target} 已被你手工编辑过，模型不再改动它。如需改动请继续手工编辑。"
        # 用户明确点名要改：已确认的元素重新打开为 draft，只开放这一个
        elements = [x.model_copy(update={"status": "draft"}) if x.id == target else x for x in elements]
        _stage(f"正在按你的意见修改 {target}…")
        updates = planner.compose(state["facts"], state["style"], elements, [target], action.instruction, [])
        elements, changed = _apply_updates(elements, updates, {target})
        return elements, changed, None if changed else f"模型没有对 {target} 给出新内容"

    def revise(state: PosterState) -> dict:
        """按钮来的一条 edit，或模型从用户的话里理解出的几条 edit / manual_edit，按顺序做完再渲染一次。"""
        pending = state.get("pending_edits") or [state["pending_action"]]
        actions = [ReviewAction.model_validate(a) for a in pending]
        elements = _lock_others(state["elements"], keep_draft={a.target for a in actions if a.action == "edit"})
        # 从用户的话理解来的修改，保留“理解为”那条说明；按钮来的不需要
        notes = list(state.get("notes") or []) if state.get("pending_edits") else []
        changed, done = [], []
        for a in actions:
            if a.action == "manual_edit":
                elements, ch, note = _manual_one(elements, a)
            else:
                elements, ch, note = _revise_one(state, elements, a)
            changed += [c for c in ch if c not in changed]
            if note:
                notes.append(note)
            if ch:
                done.append(_describe(a, state["facts"]))
        return {
            "elements": elements,
            "last_change": changed,
            "notes": notes,
            "pending_edits": [],
            "retry_count": 0,
            "render_reason": "；".join(done) or "重新渲染",
        }

    def _manual_one(elements: list[Element], action: ReviewAction) -> tuple[list[Element], list[str], str | None]:
        e = next((x for x in elements if x.id == action.target), None)
        if e is None:
            return elements, [], f"没有名为 {action.target} 的元素"
        if e.bound_fact:
            return elements, [], f"{e.id} 绑定了事实，请使用 change_fact 修改。"
        e = e.model_copy(update={"content": action.content, "status": "user_edited", "version": e.version + 1})
        return [e if x.id == e.id else x for x in elements], [e.id], None

    def manual_edit(state: PosterState) -> dict:
        action = ReviewAction.model_validate(state["pending_action"])
        elements = _lock_others(state["elements"], keep_draft=set())
        elements, changed, note = _manual_one(elements, action)
        if note:
            return {"elements": elements, "last_change": [], "notes": [note]}
        return {"elements": elements, "last_change": changed, "retry_count": 0, "render_reason": f"手工编辑 {action.target}"}

    def export(state: PosterState, config: RunnableConfig) -> dict:
        elements = [e.model_copy(update={"status": "confirmed"}) if e.status == "draft" else e for e in state["elements"]]
        tdir = _thread_dir(out_dir, config)
        v = state["versions"][-1]
        final_png = tdir / "final.png"
        final_html = tdir / "final.html"
        shutil.copyfile(v.png_path, final_png)
        shutil.copyfile(v.html_path, final_html)
        brief = {
            "request": state["request"],
            "facts": {k: f.model_dump() for k, f in state["facts"].items()},
            "style": state["style"].model_dump(),
            "template_id": state["template_id"],
            "background_image": state.get("background_image"),
            "elements": [e.model_dump() for e in elements],
            "versions": [{"n": x.n, "note": x.note, "png_path": x.png_path} for x in state["versions"]],
        }
        brief_path = tdir / "brief.json"
        brief_path.write_text(json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8")
        exported = {"png": str(final_png), "html": str(final_html), "brief": str(brief_path), "version": v.n}
        return {"elements": elements, "exported": exported}

    return {
        "intake": intake,
        "confirm_facts": confirm_facts,
        "gen_style_options": gen_style_options,
        "ask_style": ask_style,
        "plan_layout": plan_layout,
        "render": render,
        "check": check,
        "review": review,
        "interpret": interpret,
        "revise": revise,
        "manual_edit": manual_edit,
        "gen_background": gen_background,
        "export": export,
    }


# ---- 路由 ------------------------------------------------------------------


def route_after_facts(state: PosterState) -> str:
    if state.get("elements"):
        return "render"
    if state.get("style"):
        return "gen_background"
    return "gen_style_options"


def route_after_style_gen(state: PosterState) -> str:
    return "gen_background" if state.get("style") else "ask_style"


def route_after_ask_style(state: PosterState) -> str:
    return "gen_background" if state.get("style") else "gen_style_options"


def route_after_background(state: PosterState) -> str:
    # 已有元素（换风格路径）直接重渲染，不重新写文案
    return "render" if state.get("elements") else "plan_layout"


def route_after_check(state: PosterState) -> str:
    retry = _worth_retry(state.get("elements", []), state.get("check_results", []))
    if retry and state.get("retry_count", 0) <= cfg.MAX_CHECK_RETRIES:
        return "plan_layout"
    return "review"


def _route_action(state: PosterState) -> str:
    action = (state.get("pending_action") or {}).get("action")
    return {
        None: "review",
        "approve": "export",
        "edit": "revise",
        "edits": "revise",
        "manual_edit": "manual_edit",
        "change_fact": "confirm_facts",
        "change_style": "gen_style_options" if state.get("style_hint") else "ask_style",
        "instruct": "interpret",
    }[action]


def route_after_review(state: PosterState) -> str:
    return _route_action(state)


def route_after_interpret(state: PosterState) -> str:
    return _route_action(state)


def route_after_revise(state: PosterState) -> str:
    return "render" if state.get("last_change") else "review"
