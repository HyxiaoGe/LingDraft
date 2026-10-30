"""调研场景的 state。

一个 agent 对整个问题自主调研，交文字候选答案；再由一次无工具的结构化整理得到章节、论断与来源关联。
已确认内容：来源（候选 / 系统选用 / 用户排除）、论断（草稿 / 已确认 / 用户改过，强制标事实或推断）。模型只能提候选。
"""

from __future__ import annotations

from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.tools.gateway import tidy_title

SourceStatus = Literal["candidate", "adopted", "excluded"]
ClaimKind = Literal["fact", "inference"]
ClaimStatus = Literal["draft", "confirmed", "user_edited"]
Confidence = Literal["high", "medium", "low"]

# 研究预算（每次调研 agent 运行）；收尾、整理各有自己的预算，互不占用
MAX_SEARCHES = 8
MAX_READS = 6
MAX_ASKS = 2  # 整个线程的提问上限
MAX_SKILL_LOADS = 2  # 一次运行里读取调研方法的次数
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

    @field_validator("title")
    @classmethod
    def _tidy(cls, v: str) -> str:
        return tidy_title(v)  # 新标题在网关那里就理过了；这里管以前存下的（打开旧线程时）


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
    asked: str = Field(default="", description="这一版是按用户的哪个操作或哪句话改的；初稿和旧版本为空")
    understood: str = Field(default="", description="用户直接说的话被理解成了哪些操作；按钮操作为空")


class ToolStep(BaseModel):
    """调研 agent 的一次工具调用（额度用完被拦下的不算），给页面的过程时间线用。"""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(description="工具调用 id；页面据此把实时进度和存下来的记录对上")
    kind: Literal["search", "read", "skill", "ask"]
    text: str = Field(description="搜索词 / 网址 / 方法名 / 问题")
    limits: list[str] = Field(default_factory=list, description="搜索的时间、站点限制")
    ok: bool = True
    hits: int = 0  # 搜索返回的条数
    body: bool = False  # 读页读到了正文
    title: str = ""  # 读到的页面标题
    source_ids: list[str] = Field(default_factory=list, description="这一步得到的来源")
    message: str = Field(default="", description="失败的原因；提问时是用户的回答")

    @field_validator("title")
    @classmethod
    def _tidy(cls, v: str) -> str:
        return tidy_title(v)


class ResearchRound(BaseModel):
    """一轮调研：第一轮，或整理发现缺口后的补查。"""

    model_config = ConfigDict(extra="forbid")
    gap: str | None = None  # 补查那一轮要补的缺口
    steps: list[ToolStep] = Field(default_factory=list)
    answer_how: str = "agent"  # agent / close / none / kept
    new_sources: int = 0
    ended_at: str = ""


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


# 终审每种操作带哪些字段：必填的，和允许出现的。instruct 是用户直接说的话，由模型理解成上面几种操作
_REVIEW_REQUIRED = {"approve": (), "exclude_source": ("target",), "edit_claim": ("target", "content"), "rewrite_section": ("target",),
                    "instruct": ("text",)}
_REVIEW_ALLOWED = {"approve": (), "exclude_source": ("target",), "edit_claim": ("target", "content"),
                   "rewrite_section": ("target", "instruction"), "instruct": ("text", "quote")}


class Quote(BaseModel):
    """用户在报告里选中的文字：原文，以及它落在哪些论断、哪些节上（页面按报告块算出来）。"""

    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=1000)
    claim_ids: list[str] = Field(default_factory=list)
    section_ids: list[str] = Field(default_factory=list)


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["approve", "exclude_source", "edit_claim", "rewrite_section", "instruct"]
    target: str | None = None
    content: str | None = None
    instruction: str | None = None
    text: str | None = None
    quote: Quote | None = None  # 只跟 instruct：用户先选中一段再说的话

    @model_validator(mode="after")
    def _fields(self):
        extra = [f for f in ("target", "content", "instruction", "text", "quote") if getattr(self, f) is not None and f not in _REVIEW_ALLOWED[self.action]]
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
    clarify_answers: dict[str, str | None]  # 澄清卡的回答：{问题 id: 答案}，跳过的是 None
    answer: str | None  # agent 的文字候选答案
    answer_how: str  # 怎么得到的：agent / close / none
    answered: bool  # 整理步骤判断候选答案是否回答了研究问题
    gap: str | None  # 整理时发现的关键证据缺口，回研究环节补
    open_gap: str | None  # 补过之后仍缺的证据：报告照常给出，并写明还没答完的地方
    gap_rounds: int
    rounds: list[ResearchRound]  # 每轮调研做了哪些搜索、读页、提问
    outline: list[OutlineNode]
    sources: list[Source]
    time_issues: dict[str, str]  # 整理步骤判断发布时间不在用户要的时间范围里的来源：{来源 id: 理由}
    claims: list[Claim]
    assumptions: list[str]
    asks: list[AskRecord]  # 途中提问的记录；回答是用户原话，不需要来源佐证，说不清楚的部分由整理步骤按假设处理
    asks_used: int
    skills: list[str]  # 这次调研选用的调研方法（技能名），整理和重写按它的报告结构
    sections: dict[str, SectionText]
    rewrite_targets: list[str]
    rewrite_instructions: dict[str, str]  # 重写某节时用户的要求：{节 id: 要求}
    review_text: str | None  # 终审时用户直接说的话，交给模型理解
    review_quote: dict | None  # 用户说那句话前在报告里选中的文字（Quote）
    change: dict | None  # 这次修改是按什么来的：{"asked": 用户的操作或原话, "understood": 理解成的操作}，写进下一版
    review_reply: str | None  # 模型没能把用户的话对上具体操作时的回复，显示在下一张终审卡上
    versions: list[ReportVersion]
    check_results: list[CheckResult]
    notes: list[str]
    approved: bool
