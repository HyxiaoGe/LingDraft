"""调研场景的 state。

一个 agent 对整个问题自主调研，交文字候选答案；再由一次无工具的结构化整理得到章节、论断与来源关联。
已确认内容：来源（候选 / 系统选用 / 用户排除）、论断（草稿 / 已确认 / 用户改过，强制标事实或推断）。模型只能提候选。
"""

from __future__ import annotations

from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator

SourceStatus = Literal["candidate", "adopted", "excluded"]
ClaimKind = Literal["fact", "inference"]
ClaimStatus = Literal["draft", "confirmed", "user_edited"]
Confidence = Literal["high", "medium", "low"]

# 研究预算（每次调研 agent 运行）；收尾、整理各有自己的预算，互不占用
MAX_SEARCHES = 8
MAX_READS = 6
MAX_ASKS = 2  # 整个线程的提问上限
MAX_MODEL_CALLS = 30
INVALID_TOOL_CALL_RETRIES = 2  # 模型返回无法解析的工具调用时，让它重发的次数
CLOSE_RETRIES = 2  # 受控收尾（无工具，基于已有材料作答）的尝试次数
CONSOLIDATE_RETRIES = 2
MAX_GAP_ROUNDS = 1  # 整理时发现关键证据缺口，回研究环节补的轮数


class Scope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(description="研究问题的一句话表述")
    time_range: str | None = Field(default=None, description="时间边界，如“2026 年”")
    region: str | None = Field(default=None, description="地域边界，如“中国大陆为主，兼顾海外”")
    lang: str | None = Field(default=None, description="信源语言要求，如“中英文都要”")
    exclude: str | None = Field(default=None, description="不研究什么，如“不含纯营销稿”")
    deliverable: str | None = Field(default=None, description="交付形式，如“调研报告”")
    extra: str | None = Field(default=None, description="用户对澄清问题的回答，作为补充说明")


class AskRecord(BaseModel):
    """调研途中 ask_user 的一问一答。answer 为 None 表示用户跳过。"""

    question: str
    answer: str | None = None
    skipped: bool = False


class ClarifyQuestion(BaseModel):
    """intake 在需求真的模糊时提出的澄清问题；需求清楚就一个都不提。"""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(description="q1、q2…")
    title: str = Field(description="一句话问题")
    options: list[str] = Field(description="2～4 个可选答案，用户也可以自己写")
    why: str = Field(description="不问会怎样：说明这个问题会怎样影响调研方向")


class IntakeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Scope
    questions: list[ClarifyQuestion] = Field(description="最多 2 个；需求清楚时为空")


class OutlineNode(BaseModel):
    """报告的一节：由整理步骤按候选答案的内容划分，简单问题只有一节。"""

    model_config = ConfigDict(extra="forbid")
    id: str
    title: str
    question: str = Field(description="这一节回答的问题")


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    url: str
    title: str = ""
    retrieved_at: str = ""
    published_at: str | None = None
    excerpt: str = Field(default="", description="引用的原文片段；读不到正文时是搜索摘要")
    excerpt_kind: Literal["body", "summary"] = "summary"
    status: SourceStatus = "candidate"
    note: str = ""


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    statement: str
    kind: ClaimKind
    source_ids: list[str] = Field(default_factory=list)
    status: ClaimStatus = "draft"
    outline_id: str
    confidence: Confidence = "medium"
    orphaned: bool = Field(default=False, description="依赖的来源被排除后没有来源支撑")


class SectionText(BaseModel):
    """一节的正文：按句给出，每句挂论断，便于逐句回指。"""

    model_config = ConfigDict(extra="forbid")
    outline_id: str
    sentences: list[Sentence]


class Sentence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    claim_id: str | None = Field(description="这句话依据的论断 id；纯过渡句为 null")


SectionText.model_rebuild()


class ReportVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n: int
    path: str
    note: str


class CheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    passed: bool
    detail: str = ""


# ---- 中断的回传 schema ----
# 这里只管结构；“回答的是不是这张卡上的问题 / 来源 / 论断”由 answers.card_schema 按卡片内容补上。


class ClarifyForm(BaseModel):
    """澄清问题的回答：{问题 id: 答案}；null 或不传表示跳过，让模型自己判断。空白答案不算跳过。"""

    model_config = ConfigDict(extra="forbid")
    answers: dict[str, str | None]

    @model_validator(mode="after")
    def _no_blank(self):
        blank = sorted(k for k, v in self.answers.items() if v is not None and not v.strip())
        if blank:
            raise ValueError(f"回答是空白（要跳过请传 null）：{', '.join(blank)}")
        return self


# 终审每种操作带哪些字段：必填的，和允许出现的
_REVIEW_REQUIRED = {"approve": (), "exclude_source": ("target",), "edit_claim": ("target", "content"), "rewrite_section": ("target",)}
_REVIEW_ALLOWED = {"approve": (), "exclude_source": ("target",), "edit_claim": ("target", "content"),
                   "rewrite_section": ("target", "instruction")}


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["approve", "exclude_source", "edit_claim", "rewrite_section"]
    target: str | None = None
    content: str | None = None
    instruction: str | None = None

    @model_validator(mode="after")
    def _fields(self):
        extra = [f for f in ("target", "content", "instruction") if getattr(self, f) is not None and f not in _REVIEW_ALLOWED[self.action]]
        if extra:
            raise ValueError(f"{self.action} 不接受：{', '.join(extra)}")
        missing = [f for f in _REVIEW_REQUIRED[self.action] if not (getattr(self, f) or "").strip()]
        if missing:
            raise ValueError(f"{self.action} 缺少：{', '.join(missing)}")
        return self


class AskAnswer(BaseModel):
    """agent 动态提问的回答：给出回答，或者跳过（按模型最佳判断继续并记录假设），二者必居其一。"""

    model_config = ConfigDict(extra="forbid")
    answer: str | None = None
    skip: bool = False

    @model_validator(mode="after")
    def _one_of(self):
        if self.skip and self.answer is not None:
            raise ValueError("回答和跳过只能二选一")
        if not self.skip and not (self.answer or "").strip():
            raise ValueError("回答是空的；说不清楚请用跳过")
        return self


class ResearchState(TypedDict, total=False):
    request: str
    scope: Scope | None
    clarify: list[ClarifyQuestion]
    scope_confirmed: bool
    answer: str | None  # agent 的文字候选答案
    answer_how: str  # 怎么得到的：agent / close / none
    answered: bool  # 整理步骤判断候选答案是否回答了研究问题
    gap: str | None  # 整理时发现的关键证据缺口，回研究环节补
    gap_rounds: int
    outline: list[OutlineNode]
    sources: list[Source]
    claims: list[Claim]
    assumptions: list[str]
    asks: list[AskRecord]  # 途中提问的记录；回答是用户原话，不需要来源佐证，说不清楚的部分由整理步骤按假设处理
    asks_used: int
    sections: dict[str, SectionText]
    rewrite_targets: list[str]
    rewrite_instruction: str | None
    versions: list[ReportVersion]
    check_results: list[CheckResult]
    notes: list[str]
    exported: dict | None
    approved: bool
