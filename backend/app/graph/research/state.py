"""调研场景的 state：一条线程就是一个 agent 的对话。

消息列表就是对话记录：用户的话、模型的思考和正文、工具调用和结果都按发生的先后存在里面。
旁边只放线程标题和每一轮的记录。来源从搜索、读页的工具结果里取，不另外存。
"""

from __future__ import annotations

from typing import Literal, NotRequired

from langchain.agents.middleware.types import AgentState
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.tools.gateway import tidy_title

INVALID_TOOL_CALL_RETRIES = 2  # 模型返回无法解析的工具调用时，让它重发的次数


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

    @field_validator("title")
    @classmethod
    def _tidy(cls, v: str) -> str:
        return tidy_title(v)


class ToolStep(BaseModel):
    """一次工具调用，给页面的过程记录用。"""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(description="工具调用 id；页面据此把实时进度和存下来的记录对上")
    kind: Literal["search", "read"]
    text: str = Field(description="搜索词 / 网址")
    limits: list[str] = Field(default_factory=list, description="搜索的时间、站点限制")
    ok: bool = True
    hits: int = 0  # 搜索返回的条数
    body: bool = False  # 读页读到了正文
    title: str = ""  # 读到的页面标题
    source_ids: list[str] = Field(default_factory=list, description="这一步得到的来源")
    message: str = Field(default="", description="失败的原因")
    pending: bool = Field(default=False, description="已经发起、还没有结果")

    @field_validator("title")
    @classmethod
    def _tidy(cls, v: str) -> str:
        return tidy_title(v)


class Usage(BaseModel):
    """一轮的用量。token 只算 agent 的模型调用（大头），起标题、推荐问题这些快模型调用不算。"""

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
    suggestions: list[str] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    outcome: Literal["ok", "no_reply", "blocked", "stopped"] = Field(
        default="ok", description="no_reply：这一轮结束时模型没有给出正文；blocked：被模型服务的内容审核拦下，这一轮停了；"
                                  "stopped：用户点了停止，已经写出来的正文留着")


class ResearchState(AgentState):
    title: NotRequired[str]  # 线程标题，第一轮开始前由模型起
    turns: NotRequired[list[Turn]]
