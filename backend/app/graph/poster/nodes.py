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
from langgraph.types import interrupt

from app import config as cfg
from app.graph.poster.state import (
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
        "editable_by_model": not e.locked,
        "version": e.version,
    }


def make_nodes(planner: Planner, out_dir: Path):
    # ---- 生成前 ----

    def intake(state: PosterState) -> dict:
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
            "template_id": DEFAULT_TEMPLATE,
        }

    def confirm_facts(state: PosterState) -> dict:
        facts = state["facts"]
        payload = {
            "type": "confirm_facts",
            "message": "请确认海报上的关键事实。带 * 为必填；留空的事实不会出现在海报上。",
            "fields": [
                {"key": f.key, "label": f.label, "value": f.value, "required": f.required, "source": f.source}
                for f in facts.values()
            ],
        }
        form = _validated(interrupt(payload, response_schema=FactsForm), FactsForm)
        new_facts = {}
        for k, f in facts.items():
            v = (form.values.get(k) or "").strip() or None
            new_facts[k] = f.model_copy(update={"value": v, "status": "confirmed", "source": "user"})
        update = {"facts": new_facts, "pending_action": None}
        if state.get("elements"):
            update["render_reason"] = "事实更新"
            update["elements"] = _rebind_cta(state["elements"], state["facts"], new_facts)
        return update

    def gen_style_options(state: PosterState) -> dict:
        hint = state.get("style_hint")
        options = planner.style_options(state["request"], state["facts"], hint)
        if hint:
            return {"style_options": (state.get("style_options") or []) + options, "style": options[0]}
        return {"style_options": options}

    def ask_style(state: PosterState) -> dict:
        options = state["style_options"]
        payload = {
            "type": "choose_style",
            "message": "选择一个视觉方向，或用一句话描述你想要的风格。",
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

    # ---- 生成 ----

    def plan_layout(state: PosterState) -> dict:
        elements = state.get("elements") or _skeleton(state["facts"], state.get("product_image"))
        targets = [e.id for e in elements if e.kind in TEXT_KINDS and not e.locked]
        constraints = checks_mod.constraints_from(state.get("check_results", []))
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
        res = renderer.render(
            state["elements"], state["facts"], state["style"], state["template_id"], _thread_dir(out_dir, config), n
        )
        note = state.get("render_reason") or "重新渲染"
        versions.append(Version(n=n, elements=list(state["elements"]), html_path=res.html_path, png_path=res.png_path, note=note, layout=res.layout))
        return {"versions": versions}

    def check(state: PosterState) -> dict:
        layout = state["versions"][-1].layout
        results = checks_mod.run_checks(state["elements"], state["facts"], layout)
        failed = any(not r.passed for r in results)
        return {"check_results": results, "retry_count": (state.get("retry_count", 0) + 1) if failed else 0}

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
            "actions": ["approve", "edit", "manual_edit", "change_fact"],
        }
        action = _validated(interrupt(payload, response_schema=ReviewAction), ReviewAction)
        return {"pending_action": action.model_dump(), "notes": []}

    def revise(state: PosterState) -> dict:
        action = ReviewAction.model_validate(state["pending_action"])
        target = action.target
        elements = state["elements"]
        by_id = {e.id: e for e in elements}
        if target not in by_id:
            return {"notes": [f"没有名为 {target} 的元素"], "last_change": []}
        elements = _lock_others(elements, keep_draft={target})
        e = by_id[target]
        if e.bound_fact:
            return {
                "elements": elements,
                "last_change": [],
                "notes": [f"{target} 绑定了事实“{state['facts'][e.bound_fact].label}”，模型不能改写。要改它请使用 change_fact 走事实确认流程。"],
            }
        if e.status == "user_edited":
            return {"elements": elements, "last_change": [], "notes": [f"{target} 已被你手工编辑过，模型不再改动它。如需改动请继续手工编辑。"]}
        # 用户明确点名要改：已确认的元素重新打开为 draft，只开放这一个
        elements = [x.model_copy(update={"status": "draft"}) if x.id == target else x for x in elements]
        updates = planner.compose(state["facts"], state["style"], elements, [target], action.instruction, [])
        elements, changed = _apply_updates(elements, updates, {target})
        notes = [] if changed else [f"模型没有对 {target} 给出新内容"]
        return {
            "elements": elements,
            "last_change": changed,
            "notes": notes,
            "retry_count": 0,
            "render_reason": f"修改 {target}：{action.instruction}",
        }

    def manual_edit(state: PosterState) -> dict:
        action = ReviewAction.model_validate(state["pending_action"])
        elements = _lock_others(state["elements"], keep_draft=set())
        if action.target not in {e.id for e in elements}:
            return {"elements": elements, "last_change": [], "notes": [f"没有名为 {action.target} 的元素"]}
        out, changed = [], []
        for e in elements:
            if e.id == action.target:
                if e.bound_fact:
                    return {"elements": elements, "last_change": [], "notes": [f"{e.id} 绑定了事实，请使用 change_fact 修改。"]}
                e = e.model_copy(update={"content": action.content, "status": "user_edited", "version": e.version + 1})
                changed.append(e.id)
            out.append(e)
        return {"elements": out, "last_change": changed, "retry_count": 0, "render_reason": f"手工编辑 {action.target}"}

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
        "revise": revise,
        "manual_edit": manual_edit,
        "export": export,
    }


# ---- 路由 ------------------------------------------------------------------


def route_after_facts(state: PosterState) -> str:
    if state.get("elements"):
        return "render"
    if state.get("style"):
        return "plan_layout"
    return "gen_style_options"


def route_after_style_gen(state: PosterState) -> str:
    return "plan_layout" if state.get("style") else "ask_style"


def route_after_ask_style(state: PosterState) -> str:
    return "plan_layout" if state.get("style") else "gen_style_options"


def route_after_check(state: PosterState) -> str:
    failed = any(not r.passed for r in state.get("check_results", []))
    if failed and state.get("retry_count", 0) <= cfg.MAX_CHECK_RETRIES:
        return "plan_layout"
    return "review"


def route_after_review(state: PosterState) -> str:
    action = (state.get("pending_action") or {}).get("action")
    return {"approve": "export", "edit": "revise", "manual_edit": "manual_edit", "change_fact": "confirm_facts"}[action]


def route_after_revise(state: PosterState) -> str:
    return "render" if state.get("last_change") else "review"
