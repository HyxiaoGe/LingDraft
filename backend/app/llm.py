"""模型接入层。

- Planner 协议：图节点只依赖这个接口，测试用 FakePlanner，生产用 ArkPlanner（豆包 / 火山方舟）。
- 所有模型调用都是结构化输出；事实字段永远不交给模型生成。
"""
from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from app import config
from app.graph.poster import prompts
from app.graph.poster.state import Element, Fact, StyleOption

# ---- 结构化输出 schema（扁平、全部 required，符合方舟 strict 模式建议） ----


class IntakeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_name: str | None = Field(description="商品名称，用户未提及则为 null")
    offer: str | None = Field(description="优惠信息，如“5 折”“满 300 减 50”，用户未提及则为 null")
    date_range: str | None = Field(description="活动时间，保留用户原话，用户未提及则为 null")
    cta: str | None = Field(description="行动号召，如“立即抢购”，用户未提及则为 null")
    rules: str | None = Field(description="活动规则或限制条件，用户未提及则为 null")
    style_hint: str | None = Field(description="用户明确描述的视觉风格，未描述则为 null")


class StyleOptionsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    options: list[StyleOption] = Field(description="视觉方向列表")


class ElementUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(description="槽位 id，必须来自“需要撰写的槽位”")
    content: str = Field(description="该槽位的文案")


class ComposeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    updates: list[ElementUpdate]


# ---- 协议 ------------------------------------------------------------------


class Planner(Protocol):
    def intake(self, request: str) -> IntakeResult: ...

    def style_options(self, request: str, facts: dict[str, Fact], hint: str | None) -> list[StyleOption]: ...

    def compose(
        self,
        facts: dict[str, Fact],
        style: StyleOption,
        elements: list[Element],
        targets: list[str],
        instruction: str | None,
        constraints: list[str],
    ) -> list[ElementUpdate]: ...


def _facts_text(facts: dict[str, Fact]) -> str:
    lines = []
    for f in facts.values():
        if f.value:
            lines.append(f"- {f.label}（{f.key}）：{f.value}")
    return "\n".join(lines) or "（无）"


def _elements_text(elements: list[Element], targets: list[str]) -> tuple[str, str]:
    need, locked = [], []
    for e in elements:
        if e.id in targets:
            need.append(f"- {e.id}（{e.kind}）")
        elif e.content:
            locked.append(f"- {e.id}（{e.kind}）：{e.content}")
    return "\n".join(need) or "（无）", "\n".join(locked) or "（无）"


# ---- 生产实现：豆包 / 火山方舟 ----------------------------------------------


class ArkPlanner:
    """通过方舟 OpenAI 兼容接口调用豆包。结构化调用关闭深度思考。"""

    def __init__(self, model: str | None = None, lite_model: str | None = None):
        from langchain_openai import ChatOpenAI

        if not config.ARK_API_KEY:
            raise RuntimeError("未设置 ARK_API_KEY")
        common = {
            "base_url": config.ARK_BASE_URL,
            "api_key": config.ARK_API_KEY,
            "extra_body": {"thinking": {"type": "disabled"}},
            "timeout": 60,
            "max_retries": 2,
        }
        self._pro = ChatOpenAI(model=model or config.ARK_MODEL, **common)
        self._lite = ChatOpenAI(model=lite_model or config.ARK_MODEL_LITE, **common)

    def _structured(self, llm, schema):
        return llm.with_structured_output(schema, method="json_schema", strict=True)

    def intake(self, request: str) -> IntakeResult:
        chain = self._structured(self._lite, IntakeResult)
        return chain.invoke(
            [("system", prompts.INTAKE_SYSTEM), ("user", f"用户需求：{request}")]
        )

    def style_options(self, request: str, facts: dict[str, Fact], hint: str | None) -> list[StyleOption]:
        chain = self._structured(self._pro, StyleOptionsResult)
        if hint:
            ask = f"用户已指定风格：“{hint}”。请只给出 1 个贴合该描述的视觉方向。"
        else:
            ask = "请给出 3 个差异明显的视觉方向供用户选择。"
        res = chain.invoke(
            [
                ("system", prompts.STYLE_SYSTEM),
                ("user", f"活动需求：{request}\n已确认事实：\n{_facts_text(facts)}\n\n{ask}"),
            ]
        )
        return res.options

    def compose(self, facts, style, elements, targets, instruction, constraints) -> list[ElementUpdate]:
        chain = self._structured(self._pro, ComposeResult)
        need, locked = _elements_text(elements, targets)
        parts = [
            f"已确认事实：\n{_facts_text(facts)}",
            f"视觉方向：{style.name}，氛围：{style.mood}。{style.description}",
            f"需要撰写的槽位：\n{need}",
            f"锁定的元素（不要改动，只作参考）：\n{locked}",
        ]
        if instruction:
            parts.append(f"用户修改意见：{instruction}")
        if constraints:
            parts.append("上一轮校验未通过，必须满足：\n" + "\n".join(f"- {c}" for c in constraints))
        res = chain.invoke([("system", prompts.COMPOSE_SYSTEM), ("user", "\n\n".join(parts))])
        return [u for u in res.updates if u.id in targets]


# ---- 测试 / 演示实现：确定性输出，不联网 ------------------------------------


class FakePlanner:
    """按关键词给出固定结果，用于 pytest 与 --fake 演示。"""

    def __init__(self):
        self.calls: list[tuple] = []

    def intake(self, request: str) -> IntakeResult:
        self.calls.append(("intake", request))
        return IntakeResult(
            product_name="蓝牙耳机" if "耳机" in request else None,
            offer="5 折" if "5 折" in request or "5折" in request else None,
            date_range="11.20 至 11.27" if "11.20" in request else None,
            cta=None,
            rules=None,
            style_hint="极简白底" if "极简白底" in request else None,
        )

    def style_options(self, request, facts, hint) -> list[StyleOption]:
        self.calls.append(("style_options", hint))
        if hint:
            return [StyleOption(id="s_hint", name=hint, palette=["#ffffff", "#f2f2f2", "#111111", "#111111"], mood="极简", description="白底黑字")]
        return [
            StyleOption(id="s1", name="黑金炸裂", palette=["#0b0b0b", "#1f1a0e", "#f5c542", "#ffffff"], mood="热烈", description="黑底金字，强促销感"),
            StyleOption(id="s2", name="深蓝科技", palette=["#0a1a33", "#102a52", "#4fd1ff", "#ffffff"], mood="科技", description="深蓝渐变，电子产品常用"),
            StyleOption(id="s3", name="活力橙", palette=["#fff4e6", "#ffd7a8", "#ff6a00", "#1a1a1a"], mood="活泼", description="暖色明亮，年轻人群"),
        ]

    def compose(self, facts, style, elements, targets, instruction, constraints) -> list[ElementUpdate]:
        self.calls.append(("compose", tuple(targets), instruction, tuple(constraints)))
        product = facts["product_name"].value or "好物"
        defaults = {
            "headline": f"黑五预热 {product}狂欢",
            "subhead": "预热开抢，好礼限时放送",
            "cta": "立即抢购",
        }
        if instruction and "紧迫" in instruction:
            defaults["headline"] = "黑五开抢 不容错过"
        if instruction and "短" in instruction:
            defaults["headline"] = "黑五来了"
        if any("短" in c or "溢出" in c for c in constraints):
            defaults["headline"] = "黑五预热"
            defaults["subhead"] = "限时放送"
        return [ElementUpdate(id=t, content=defaults[t]) for t in targets if t in defaults]
