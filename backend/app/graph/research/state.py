"""调研场景的 state：一条线程就是一个 agent 的对话。

消息列表就是对话记录：用户的话、模型的思考和正文、工具调用和结果都按发生的先后存在里面。
旁边只放线程标题和每一轮的记录。来源从搜索、读页的工具结果里取，不另外存。
生成的图是文件，存在数据目录里（见 app.tools.images），消息里只记它的编号和文件名。
"""

from __future__ import annotations

import re
from typing import Literal, NotRequired

from langchain.agents.middleware.types import AgentState
from pydantic import BaseModel, ConfigDict, Field, field_validator

INVALID_TOOL_CALL_RETRIES = 2  # 模型返回无法解析的工具调用时，让它重发的次数

_SEP = r"(?:\s+[-–—]\s+|\s*[|｜_]\s*)"  # 横线两边要有空格，不然“10-10-2024”也会被当成重复
_REPEAT = re.compile(rf"^(?P<t>.{{2,}}?){_SEP}(?P=t)(?={_SEP}|$)")


def tidy_title(title: str) -> str:
    """网页标题常把正文标题重复一遍（“A - A”“A | A - 站名”），只留一份。只认一字不差的重复，不猜意思。
    搜索服务给的标题已经整理过（agent-tools search_web）；这里管 11-05 以前存下的来源和读页的标题。"""
    return _REPEAT.sub(r"\g<t>", title.strip(), count=1)


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


class WeatherDay(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: str = Field(description="当地日期 YYYY-MM-DD")
    day: str = Field(description="白天的天气")
    night: str = Field(description="夜间的天气")
    high: float
    low: float
    wind: str = Field(default="", description="白天的风向风力，比如“北风 1-3级”")


class WeatherHour(BaseModel):
    model_config = ConfigDict(extra="forbid")
    time: str = Field(description="当地时间，带时区，比如 2026-10-31T15:00+08:00")
    temp: float
    text: str
    pop: int | None = Field(default=None, description="降水概率 %")
    precip: float | None = Field(default=None, description="降水量 mm")


class Forecast(BaseModel):
    """一次查天气的结果（和风天气），页面画成天气卡片。由 weather-service 的结果换来（见 history.forecast_of）。"""

    model_config = ConfigDict(extra="forbid")
    location: str = Field(description="和风的地名，比如“广东省·广州”")
    today: str = Field(description="查的时候当地是哪天")
    fetched_at: str
    requested_date: str | None = Field(default=None, description="问的是哪一天；在预报范围内时 days 只有这一天")
    days: list[WeatherDay] = Field(description="从今天起几天的逐日预报，或者问的那一天")
    hourly: list[WeatherHour] = Field(default_factory=list, description="接下来 24 小时（问了 7 天内的某天时是那天的）；取不到、问得更远时是空的")


class GeneratedImage(BaseModel):
    """生成的一张图（image-service）。文件在这个对话的图片目录里（见 app.tools.images.ImageStore），页面按文件名取。"""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(description="图片编号：模型改这张图时用它（source_image_id）")
    file: str = Field(description="文件名，<编号>.<扩展名>")
    mime_type: str
    width: int
    height: int
    prompt: str = Field(description="交给生图模型的描述；改图时是修改要求")
    aspect_ratio: str
    model: str | None = Field(default=None, description="实际出图的模型")
    edited_from: str | None = Field(default=None, description="改的是哪张图（编号）")


class ToolStep(BaseModel):
    """一次工具调用，给页面的过程记录用。"""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(description="工具调用 id；页面据此把实时进度和存下来的记录对上")
    kind: Literal["search", "read", "weather", "image", "task"]
    text: str = Field(description="搜索词 / 网址 / 地名 / 画图的描述 / 子任务的标题")
    limits: list[str] = Field(default_factory=list, description="搜索的时间、站点限制")
    ok: bool = True
    hits: int = 0  # 搜索返回的条数
    body: bool = False  # 读页读到了正文
    title: str = ""  # 读到的页面标题；查天气时是和风的地名
    source_ids: list[str] = Field(default_factory=list, description="这一步得到的来源")
    message: str = Field(default="", description="失败的原因")
    pending: bool = Field(default=False, description="已经发起、还没有结果")
    weather: Forecast | None = Field(default=None, description="查到的天气，页面画成天气卡片")
    image: GeneratedImage | None = Field(default=None, description="画好的图，页面画成图片卡片")
    aspect_ratio: str = Field(default="", description="画图时要的比例：还没出图时页面按它占位；改图没给比例时是空的")
    edit: bool = Field(default=False, description="画图是在改之前的一张图")
    prompt: str = Field(default="", description="子任务：主 agent 交给子 agent 的完整说明")

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
    weather: int = 0  # 查天气的次数
    images: int = 0  # 画成的图（画新图和改图都算）
    subagents: int = 0  # 派出去的子 agent（真跑了的）


class Turn(BaseModel):
    """一轮：用户说一句话，到模型回复完。"""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(description="这一轮用户那句话的消息 id")
    started_at: str
    ended_at: str = ""
    suggestions: list[str] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage, description="主 agent 的")
    subagent_usage: Usage = Field(default_factory=Usage, description="这一轮所有子 agent 加起来的；模型不一样，价钱不一样，和主 agent 分开记")
    outcome: Literal["ok", "no_reply", "blocked", "stopped"] = Field(
        default="ok", description="no_reply：这一轮结束时模型没有给出正文；blocked：被模型服务的内容审核拦下，这一轮停了；"
                                  "stopped：用户点了停止，已经写出来的正文留着")


class Compaction(BaseModel):
    """对话太长时，发给模型的那份怎么压（见 compaction）。只记压到哪一轮和写好的短版，消息本身不改，页面照常从消息里取。
    边界是轮的 id（这一轮用户那句话的消息 id），只往后挪；到了边界的轮（含边界那一轮）按同样的方式压，每次调模型拼出来的一样，前缀缓存接得上。"""

    model_config = ConfigDict(extra="forbid")
    cleared_through: str = Field(default="", description="到这一轮为止：搜索、读页、取回的结果换成一行说明，思考不带")
    summarized_through: str = Field(default="", description="到这一轮为止：模型那边整个换成短版（summaries），用户的话照原样")
    summaries: dict[str, str] = Field(default_factory=dict, description="轮 id → 这一轮模型那边的短版，写一次就不再改")
    shortened: dict[str, str] = Field(default_factory=dict, description="用户消息 id → 特别长的用户消息的短版（只在换成短版的轮里用）")


class ResearchState(AgentState):
    title: NotRequired[str]  # 线程标题，第一轮开始前由模型起
    turns: NotRequired[list[Turn]]
    compaction: NotRequired[Compaction]
