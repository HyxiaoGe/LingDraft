"""调研场景的 state：一条线程就是一个 agent 的对话。

消息列表就是对话记录：用户的话、模型的思考和正文、工具调用和结果都按发生的先后存在里面。
旁边的字段放交付物（报告的节、论断、被排除的来源、各版）和每一轮的记录。来源从搜索、读页的工具结果里取，不另外存。
模型只能提候选：论断要挂在搜到、读到的来源上，挂不上的整理时丢掉。
"""

from __future__ import annotations

from typing import Literal, NotRequired

from langchain.agents.middleware.types import AgentState
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.tools.gateway import tidy_title

SourceStatus = Literal["candidate", "adopted", "excluded"]
ClaimKind = Literal["fact", "inference"]
ClaimStatus = Literal["draft", "user_edited"]
Confidence = Literal["high", "medium", "low"]

INVALID_TOOL_CALL_RETRIES = 2  # 模型返回无法解析的工具调用时，让它重发的次数
CONSOLIDATE_RETRIES = 2  # 整理（草稿 → 节和论断）出错时重试的次数


class Source(BaseModel):
    """一个网页。id 由网址算出来（见 sources.source_id），同一个网址在整条线程里都是同一个 id。"""

    model_config = ConfigDict(extra="forbid")
    id: str
    url: str
    title: str = ""
    retrieved_at: str = ""
    published_at: str | None = None
    excerpt: str = Field(default="", description="原文开头；读不到正文时是搜索摘要")
    excerpt_kind: Literal["body", "summary"] = "summary"
    status: SourceStatus = "candidate"

    @field_validator("title")
    @classmethod
    def _tidy(cls, v: str) -> str:
        return tidy_title(v)


class OutlineNode(BaseModel):
    """报告的一节。"""

    model_config = ConfigDict(extra="forbid")
    id: str
    title: str
    question: str = Field(description="这一节回答的问题")


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


class Sentence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    claim_id: str | None = Field(description="这句话依据的论断 id；纯过渡句为 null")


class SectionText(BaseModel):
    """一节的正文：按句给出，每句挂论断，便于逐句回指。"""

    model_config = ConfigDict(extra="forbid")
    outline_id: str
    sentences: list[Sentence]


class ReportVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n: int
    path: str
    note: str = Field(description="这一版改了什么：这一轮每次改报告时模型写的说明，按先后连起来")
    turn: str = Field(description="哪一轮出的（那一轮用户那句话的消息 id）")


class CheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    passed: bool
    detail: str = ""


class ToolStep(BaseModel):
    """一次工具调用，给页面的过程记录用。"""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(description="工具调用 id；页面据此把实时进度和存下来的记录对上")
    kind: Literal["search", "read", "skill", "report"]
    text: str = Field(description="搜索词 / 网址 / 方法名 / 改报告的说明")
    limits: list[str] = Field(default_factory=list, description="搜索的时间、站点限制")
    ok: bool = True
    hits: int = 0  # 搜索返回的条数
    body: bool = False  # 读页读到了正文
    title: str = ""  # 读到的页面标题
    source_ids: list[str] = Field(default_factory=list, description="这一步得到的来源")
    message: str = Field(default="", description="失败的原因；改报告时是结果")
    pending: bool = Field(default=False, description="已经发起、还没有结果")

    @field_validator("title")
    @classmethod
    def _tidy(cls, v: str) -> str:
        return tidy_title(v)


class Usage(BaseModel):
    """一轮的用量。token 只算调研 agent 的模型调用（大头），整理、起标题、推荐问题这些快模型调用不算。"""

    model_config = ConfigDict(extra="forbid")
    model_calls: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    searches: int = 0
    reads: int = 0


class Turn(BaseModel):
    """一轮：用户说一句话，到模型回复完。"""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(description="这一轮用户那句话的消息 id")
    started_at: str
    ended_at: str = ""
    version: int | None = Field(default=None, description="这一轮出的报告版本；没改报告为 None")
    suggestions: list[str] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    outcome: Literal["ok", "no_reply"] = Field(default="ok", description="no_reply：这一轮结束时模型没有给出正文")


# ---- 用户的话附带的东西：报告上选中的文字、按钮操作 ----


class Quote(BaseModel):
    """用户在报告里选中的文字：原文，以及它落在哪些论断、哪些节上（页面按报告块算出来）。"""

    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=1000)
    claim_ids: list[str] = Field(default_factory=list)
    section_ids: list[str] = Field(default_factory=list)


class Action(BaseModel):
    """报告上的按钮：排除来源、改一条论断的表述、重写一节。和用户的话一起交给模型，由模型去改。"""

    model_config = ConfigDict(extra="forbid")
    type: Literal["exclude_source", "edit_claim", "rewrite_section"]
    source_id: str | None = None
    claim_id: str | None = None
    section_id: str | None = None
    statement: str | None = None

    @model_validator(mode="after")
    def _fields(self):
        need = {"exclude_source": ("source_id",), "edit_claim": ("claim_id", "statement"), "rewrite_section": ("section_id",)}[self.type]
        missing = [f for f in need if not (getattr(self, f) or "").strip()]
        if missing:
            raise ValueError(f"{self.type} 缺少：{', '.join(missing)}")
        return self


class AskAnswer(BaseModel):
    """提问卡的回答：给出回答，或者跳过（按模型最佳判断继续并记录假设），二者必居其一。"""

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


class ResearchState(AgentState):
    title: NotRequired[str]  # 线程标题，第一轮开始前由模型起
    turns: NotRequired[list[Turn]]
    # 报告的工作稿：改报告的工具直接改它，一轮结束时有改动就落成一版
    report_title: NotRequired[str]
    outline: NotRequired[list[OutlineNode]]
    sections: NotRequired[dict[str, SectionText]]
    claims: NotRequired[list[Claim]]
    assumptions: NotRequired[list[str]]
    time_issues: NotRequired[dict[str, str]]  # 整理时判断发布时间不在用户要的时间范围里的来源：{来源 id: 理由}
    excluded: NotRequired[list[str]]  # 用户排除的来源 id
    changes: NotRequired[list[str]]  # 这一轮对工作稿的改动说明；一轮结束时写进版本、清空
    versions: NotRequired[list[ReportVersion]]
    check_results: NotRequired[list[CheckResult]]
    ids: NotRequired[dict[str, int]]  # 节（c）、论断（k）已经用到的编号：整份重写后也接着往下编，旧 id 不会指到新内容上
