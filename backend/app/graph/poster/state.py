"""海报任务的状态定义。

设计原则（见 docs/黑五海报PoC技术方案.md 第 2、4 节）：
- 关键事实是结构化字段（Fact），海报元素通过 bound_fact 绑定，渲染时取值，模型不可改写。
- 每个元素带 status，修改节点只允许动 draft；confirmed / user_edited 原样透传。
- 每轮渲染生成一个 Version 快照，可回退。
"""
from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from langgraph.graph.message import add_messages
from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---- 事实 -----------------------------------------------------------------

FactStatus = Literal["draft", "confirmed"]
FactSource = Literal["user", "inferred"]

# key, label, required
FACT_SPECS: list[tuple[str, str, bool]] = [
    ("product_name", "商品名称", True),
    ("offer", "优惠信息", True),
    ("date_range", "活动时间", True),
    ("cta", "行动号召", False),
    ("rules", "活动规则", False),
]
REQUIRED_FACT_KEYS = [k for k, _, req in FACT_SPECS if req]


class Fact(BaseModel):
    key: str
    label: str
    value: str | None = None
    source: FactSource = "inferred"
    status: FactStatus = "draft"
    required: bool = False


def empty_facts() -> dict[str, Fact]:
    return {k: Fact(key=k, label=label, required=req) for k, label, req in FACT_SPECS}


# ---- 风格 -----------------------------------------------------------------


class StyleOption(BaseModel):
    """palette 固定 4 个颜色：背景、主色、强调色、文字色。"""

    id: str
    name: str
    palette: list[str] = Field(min_length=4, max_length=4)
    mood: str
    description: str


# ---- 元素 -----------------------------------------------------------------

ElementKind = Literal[
    "headline", "subhead", "offer", "date", "cta", "product_image", "logo", "background", "decor"
]
ElementStatus = Literal["draft", "confirmed", "user_edited"]

# 文本类元素，模型可以写内容；其余由事实或素材决定
TEXT_KINDS = {"headline", "subhead", "cta"}
# 给用户和模型看的元素名
KIND_LABELS = {"headline": "主标题", "subhead": "副标题", "offer": "优惠", "date": "活动时间", "cta": "行动号召"}


class Element(BaseModel):
    id: str
    kind: ElementKind
    slot: str
    content: str | None = None
    bound_fact: str | None = None
    style: dict = Field(default_factory=dict)
    status: ElementStatus = "draft"
    version: int = 1

    @property
    def locked(self) -> bool:
        return self.status != "draft" or self.bound_fact is not None


class Version(BaseModel):
    n: int
    elements: list[Element]
    html_path: str
    png_path: str
    note: str
    layout: dict = Field(default_factory=dict)  # slot -> {bbox, overflow, text}


class CheckResult(BaseModel):
    name: str
    passed: bool
    detail: str = ""
    slots: list[str] = Field(default_factory=list)  # 出问题的槽位：只有模型能重写的槽位出问题时才值得重试


# ---- interrupt 的回传 schema（前端 / CLI 提交的内容） ------------------------


class FactsForm(BaseModel):
    """confirm_facts 的 resume 载荷。必填事实为空则校验失败，需重新提交。"""

    model_config = ConfigDict(extra="forbid")
    values: dict[str, str | None]

    @model_validator(mode="after")
    def _required_present(self) -> FactsForm:
        missing = [k for k in REQUIRED_FACT_KEYS if not (self.values.get(k) or "").strip()]
        if missing:
            raise ValueError(f"必填事实缺失: {missing}")
        return self


class StyleChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    style_id: str | None = None
    custom: str | None = None

    @model_validator(mode="after")
    def _one_of(self) -> StyleChoice:
        if not self.style_id and not (self.custom or "").strip():
            raise ValueError("需要选择一个风格或填写自定义描述")
        return self


# instruct：用户直接说的话，原样交给模型理解成下面几种操作
ReviewActionType = Literal["approve", "edit", "manual_edit", "change_fact", "change_style", "instruct"]


class ReviewAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: ReviewActionType
    target: str | None = None  # element id
    instruction: str | None = None
    content: str | None = None
    text: str | None = None

    @model_validator(mode="after")
    def _fields_for_action(self) -> ReviewAction:
        if self.action == "edit" and not (self.target and self.instruction):
            raise ValueError("edit 需要 target 与 instruction")
        if self.action == "manual_edit" and not (self.target and self.content is not None):
            raise ValueError("manual_edit 需要 target 与 content")
        if self.action == "instruct" and (not (self.text or "").strip() or self.target or self.instruction or self.content is not None):
            raise ValueError("instruct 只带 text，且不能为空")
        if self.action != "instruct" and self.text is not None:
            raise ValueError(f"{self.action} 不接受 text")
        return self


# ---- 图状态 ---------------------------------------------------------------


class PosterState(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    request: str
    product_image: str | None
    facts: dict[str, Fact]
    style_hint: str | None
    style_options: list[StyleOption]
    style: StyleOption | None
    style_changed: bool
    background_image: str | None
    template_id: str
    elements: list[Element]
    versions: list[Version]
    pending_action: dict | None
    pending_edits: list[dict]  # 模型从用户的话里理解出的元素修改（edit / manual_edit），一次做完
    review_text: str | None  # 终审时用户直接说的话，交给模型理解
    review_reply: str | None  # 模型没能完全对上用户的话时的回复，显示在下一张终审卡上
    fact_proposals: dict[str, str]  # 用户在终审时说要把事实改成什么：{事实 key: 新值}，只预填事实卡，仍由用户确认
    check_results: list[CheckResult]
    retry_count: int
    last_change: list[str]
    render_reason: str
    notes: list[str]
    exported: dict | None
