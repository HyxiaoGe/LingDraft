"""三种确认卡的回答校验，接口层和 CLI 在提交 Command 之前共用。

澄清、终审两个节点把同一份 schema 交给 interrupt()：绕过入口的坏回答在节点里被拒，且不写进检查点，
线程停在原卡上，换正确的回答还能继续。agent 的提问由 HITL 中间件发起，挂不了 schema，只靠入口这一道。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ValidationError, model_validator

from app.graph.research.agent import ask_resume, is_ask_interrupt
from app.graph.research.state import AskAnswer, ClarifyForm, ReviewDecision


class InvalidAnswer(ValueError):
    """回答不符合当前这张卡；图没有看到它，线程停在原处。"""


def card_schema(card: dict) -> type[BaseModel]:
    """按卡片内容生成回答的 schema：澄清卡只认卡上的问题，终审只认卡上的来源、论断、章节。"""
    kind = card.get("type")
    if kind == "research.scope":
        question_ids = {q["id"] for q in card.get("questions", [])}

        class ScopeAnswer(ClarifyForm):
            @model_validator(mode="after")
            def _on_card(self):
                unknown = sorted(set(self.answers) - question_ids)
                if unknown:
                    raise ValueError(f"回答的不是当前问题：{', '.join(unknown)}")
                return self

        return ScopeAnswer
    if kind == "research.review":
        targets = {
            "exclude_source": ("来源", {s["id"] for s in card.get("sources", [])}),
            "edit_claim": ("论断", {c["id"] for c in card.get("claims", [])}),
            "rewrite_section": ("章节", {n["id"] for n in card.get("sections", [])}),
        }

        class ReviewAnswer(ReviewDecision):
            @model_validator(mode="after")
            def _on_card(self):
                if self.action in targets:
                    label, ids = targets[self.action]
                    if self.target not in ids:
                        raise ValueError(f"{label}不存在：{self.target}")
                if self.quote:  # 选中位置是页面算的，也要落在卡上的论断、章节上
                    for label, key, got in (("论断", "edit_claim", self.quote.claim_ids), ("章节", "rewrite_section", self.quote.section_ids)):
                        unknown = [x for x in got if x not in targets[key][1]]
                        if unknown:
                            raise ValueError(f"选中的{label}不存在：{', '.join(unknown)}")
                return self

        return ReviewAnswer
    raise InvalidAnswer(f"不认识的确认卡：{kind}")


def resume_value(card: dict, payload: dict) -> Any:
    """校验回答并转成图要的 resume 值；agent 的提问转成中间件的 decisions。不合法抛 InvalidAnswer。"""
    try:
        if is_ask_interrupt(card):
            a = AskAnswer.model_validate(payload)
            return ask_resume(a.answer, a.skip, card)
        card_schema(card).model_validate(payload)
        return payload
    except ValidationError as e:
        raise InvalidAnswer(_message(e)) from None


def _message(e: ValidationError) -> str:
    parts = []
    for err in e.errors():
        msg = err["msg"].removeprefix("Value error, ")
        loc = ".".join(str(x) for x in err["loc"])
        parts.append(f"{loc}：{msg}" if loc else msg)
    return "；".join(parts)
