"""用户输入的校验和转换：提问卡的回答、用户的一句话（附带选中的文字、按钮操作）。接口层和 CLI 在交给 agent 之前共用。

agent 的提问由 HITL 中间件发起，挂不了 schema，回答只靠入口这一道校验。选中的文字和按钮操作要对着当前的报告：
对象不存在就拒绝，不交给模型猜。
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage
from pydantic import ValidationError

from app.graph.research import history, prompts
from app.graph.research.sources import collect_sources
from app.graph.research.state import Action, AskAnswer, Quote, ResearchState
from app.graph.research.workbench import Draft


class InvalidAnswer(ValueError):
    """回答或操作不合法；agent 没有看到它，线程停在原处。"""


# ---- 提问卡 ----


def is_ask_interrupt(value: Any) -> bool:
    return isinstance(value, dict) and bool(value.get("action_requests"))


def ask_view(value: dict) -> dict:
    args = value["action_requests"][0].get("args") or {}
    return {"type": "research.ask", "title": args.get("question", ""), "options": list(args.get("options") or []), "why": args.get("why", "")}


def ask_resume(answer: str | None, skip: bool = False, pending: dict | None = None) -> dict:
    """用户的回答 → 中间件的 decisions。模型若违反“一次只问一个”在同一步发了多个 ask_user，
    只有第一个呈现给了用户，其余按未呈现处理（decisions 数量必须与 action_requests 一致）。"""
    if skip or not (answer or "").strip():
        message = history.reply(skipped=True, note=prompts.render("agent.ask_skipped"))
    else:
        message = history.reply(answer=answer.strip())
    n = len(pending.get("action_requests") or []) if pending else 1
    decisions = [{"type": "respond", "message": message}]
    decisions += [{"type": "respond", "message": history.reply(unshown=True, note=prompts.render("agent.ask_unshown"))}
                  for _ in range(max(n, 1) - 1)]
    return {"decisions": decisions}


def resume_value(card: dict, payload: dict) -> dict:
    """校验提问卡的回答并转成中间件的 decisions。不合法抛 InvalidAnswer。"""
    if not is_ask_interrupt(card):
        raise InvalidAnswer("不认识的提问卡")
    try:
        a = AskAnswer.model_validate(payload)
    except ValidationError as e:
        raise InvalidAnswer(_message(e)) from None
    return ask_resume(a.answer, a.skip, card)


# ---- 用户的一句话 ----


def user_message(state: ResearchState, text: str, quote: dict | None = None, action: dict | None = None) -> HumanMessage:
    """用户的话 → 交给 agent 的消息。选中的文字和按钮操作写成附注接在后面，原样的话、选中、操作另存给页面显示。"""
    try:
        q = Quote.model_validate(quote) if quote else None
        a = Action.model_validate(action) if action else None
    except ValidationError as e:
        raise InvalidAnswer(_message(e)) from None
    text = text.strip()
    if not text and a is None:
        raise InvalidAnswer("说点什么再发送")
    d = Draft.of(state)
    claims, outline = d.visible_claims(), {n.id: n for n in d.outline}
    notes = []
    if q is not None:
        bad = [x for x in q.claim_ids if x not in claims] + [x for x in q.section_ids if x not in outline]
        if bad:
            raise InvalidAnswer(f"选中的内容不在当前报告里：{', '.join(bad)}")
        notes.append(prompts.render("user.quote", text=q.text, claims="、".join(q.claim_ids), sections="、".join(q.section_ids)))
    if a is not None:
        notes.append(_action_note(state, d, a))
    shown = {"text": text, **({"quote": q.model_dump()} if q else {}), **({"action": a.model_dump(exclude_none=True)} if a else {})}
    return HumanMessage(content="\n\n".join([text, *notes]).strip(), additional_kwargs={history.LINGDRAFT: shown})


def _action_note(state: ResearchState, d: Draft, a: Action) -> str:
    if a.type == "exclude_source":
        src = next((s for s in collect_sources(state.get("messages", [])) if s.id == a.source_id), None)
        if src is None:
            raise InvalidAnswer(f"来源不存在：{a.source_id}")
        if a.source_id in d.excluded:
            raise InvalidAnswer("这条来源已经排除过了")
        return prompts.render("user.exclude_source", source_id=src.id, name=src.title or src.url)
    if a.type == "edit_claim":
        claim = d.visible_claims().get(a.claim_id or "")
        if claim is None:
            raise InvalidAnswer(f"论断不存在：{a.claim_id}")
        return prompts.render("user.edit_claim", claim_id=claim.id, before=claim.statement, statement=(a.statement or "").strip())
    node = next((n for n in d.outline if n.id == a.section_id), None)
    if node is None:
        raise InvalidAnswer(f"章节不存在：{a.section_id}")
    return prompts.render("user.rewrite_section", section_id=node.id, title=node.title)


def _message(e: ValidationError) -> str:
    parts = []
    for err in e.errors():
        msg = err["msg"].removeprefix("Value error, ")
        loc = ".".join(str(x) for x in err["loc"])
        parts.append(f"{loc}：{msg}" if loc else msg)
    return "；".join(parts)
