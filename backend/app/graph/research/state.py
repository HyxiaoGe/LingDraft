"""调研场景的 state。

已确认内容三种：来源（候选 / 已采纳 / 已排除，由用户定）、论断（草稿 / 已确认 / 用户改过，强制标事实或推断）、
大纲（确认后结构锁定）。模型只能提候选。
"""

from __future__ import annotations

from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field

SourceStatus = Literal["candidate", "adopted", "excluded"]
ClaimKind = Literal["fact", "inference"]
ClaimStatus = Literal["draft", "confirmed", "user_edited"]
Confidence = Literal["high", "medium", "low"]

ASK_LIMIT_PER_SECTION = 3  # 每轮（每章 agent 跑一次）上限，Sean 定
ASK_LIMIT_PER_THREAD = 6  # 整个线程的总上限，超了后续章节不再提问，只记假设
MAX_SEARCHES_PER_SECTION = 3
MAX_READS_PER_SECTION = 3
MAX_MODEL_CALLS_PER_SECTION = 20  # 真实跑过一次：14 次时有一章没来得及输出 Findings
MAX_OUTLINE_NODES = 4


class Scope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(description="研究问题的一句话表述")
    time_range: str | None = Field(default=None, description="时间边界，如“2026 年”")
    region: str | None = Field(default=None, description="地域边界，如“中国大陆为主，兼顾海外”")
    lang: str | None = Field(default=None, description="信源语言要求，如“中英文都要”")
    exclude: str | None = Field(default=None, description="不研究什么，如“不含纯营销稿”")
    deliverable: str | None = Field(default=None, description="交付形式，如“调研报告”")


SCOPE_FIELDS: list[tuple[str, str, bool, str]] = [
    ("question", "研究问题", True, "一句话说清要回答什么"),
    ("time_range", "时间范围", False, "只看哪段时间的信息"),
    ("region", "地域范围", False, "看哪些地区的产品或信源"),
    ("lang", "信源语言", False, "中文、英文还是都要"),
    ("exclude", "不研究什么", False, "明确排除的范围"),
    ("deliverable", "交付形式", False, "报告 / 对比表 / 要点清单"),
]


class OutlineNode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    title: str
    question: str = Field(description="这一章要回答的问题")
    researched: bool = False


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
    """一章的正文：按句给出，每句挂论断，便于逐句回指。"""

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


class ScopeForm(BaseModel):
    values: dict[str, str | None]

    def model_post_init(self, context, /) -> None:
        if not (self.values.get("question") or "").strip():
            raise ValueError("必填项缺失：研究问题")


class OutlineEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = Field(default=None, description="保留的章节给原 id；新增章节为 null")
    title: str
    question: str


class OutlineDecision(BaseModel):
    action: Literal["approve", "edit"]
    nodes: list[OutlineEdit] | None = None


class ReviewDecision(BaseModel):
    action: Literal["approve", "exclude_source", "edit_claim", "rewrite_section", "reopen_outline"]
    target: str | None = None
    content: str | None = None
    instruction: str | None = None


class AskAnswer(BaseModel):
    """agent 动态提问的回答；skip 为真时按模型最佳判断继续并记录假设。"""

    answer: str | None = None
    skip: bool = False


class ResearchState(TypedDict, total=False):
    request: str
    scope: Scope | None
    scope_confirmed: bool
    outline: list[OutlineNode]
    outline_confirmed: bool
    section_idx: int
    sources: list[Source]
    claims: list[Claim]
    assumptions: list[str]
    asks_used: int
    sections: dict[str, SectionText]
    rewrite_targets: list[str]
    rewrite_instruction: str | None
    versions: list[ReportVersion]
    check_results: list[CheckResult]
    notes: list[str]
    exported: dict | None
    approved: bool
