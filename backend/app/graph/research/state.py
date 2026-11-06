"""调研场景的 state：一条线程就是一个 agent 的对话。

消息列表就是对话记录：用户的话、模型的思考和正文、工具调用和结果都按发生的先后存在里面。
旁边只放线程标题和每一轮的记录。来源从搜索、读页的工具结果里取，不另外存。
生成的图存在 image-service 里、归灵稿（见 app.tools.image_mcp），消息里只记它的编号、大小这些。
"""

from __future__ import annotations

import re
from typing import Literal, NotRequired

from langchain.agents.middleware.types import AgentState
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.model_catalog import ModelUse

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
    excerpt: str = Field(default="", description="原文开头；没读正文、查文档查到过的是第一段文档；都没有的是搜索摘要")
    excerpt_kind: Literal["body", "summary", "docs"] = "summary"

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


class Place(BaseModel):
    """地点卡上的一个地方。由 maps-service 的 place_search 结果换来（见 history.places_of），没有坐标。"""

    model_config = ConfigDict(extra="forbid")
    name: str
    address: str = ""
    district: str = ""
    city: str = ""
    category: str = Field(default="", description="地图的分类，比如“餐饮服务;咖啡厅;咖啡厅”")
    distance_m: int | None = Field(default=None, description="离搜索的那个地方多远；在某地附近搜时才有")
    rating: float | None = None
    cost: float | None = Field(default=None, description="人均消费（元），高德标的，只有餐饮、酒店、景点、影院会给")
    open_hours: str = ""
    photo_url: str = ""
    map_url: str = Field(default="", description="在地图上看这个地方的链接；只有高德的有")
    provider: str = Field(description="amap / tmap")


class PlaceList(BaseModel):
    """一次查地点的结果，页面画成地点卡。"""

    model_config = ConfigDict(extra="forbid")
    query: str
    near: str = Field(default="", description="在哪附近搜的：服务定下的地名")
    city: str = ""
    places: list[Place]
    limitations: list[str] = Field(default_factory=list, description="服务标的限制，比如 first_term_only（腾讯只搜了第一个词）")
    fetched_at: str = ""


class RouteLeg(BaseModel):
    """公交方案里的一段：步行，或者坐一段地铁、公交。"""

    model_config = ConfigDict(extra="forbid")
    kind: str = Field(description="walking / subway / bus / other")
    line_name: str = ""
    departure_stop: str = ""
    arrival_stop: str = ""
    via_stop_count: int | None = None
    entrance: str = ""
    exit: str = ""
    distance_m: int | None = None
    duration_s: int | None = None


class TransitPlan(BaseModel):
    """公交的另一种走法。"""

    model_config = ConfigDict(extra="forbid")
    duration_s: int | None = None
    fare_yuan: float | None = None
    transit_type: str = ""
    transfers: int | None = None
    walking_distance_m: int | None = None
    legs: list[RouteLeg] = Field(default_factory=list)


class Route(BaseModel):
    """一种出行方式算出来的路线。公交没有总距离。"""

    model_config = ConfigDict(extra="forbid")
    mode: str = Field(description="driving / transit / walking / bicycling")
    distance_m: int | None = None
    duration_s: int | None = None
    summary: str = Field(default="", description="地图给的一句话，比如“速度最快”")
    toll_yuan: float | None = None
    fare_yuan: float | None = None
    transit_type: str = ""
    transfers: int | None = None
    walking_distance_m: int | None = None
    legs: list[RouteLeg] = Field(default_factory=list)
    alternatives: list[TransitPlan] = Field(default_factory=list)
    provider: str = ""


class RoutePlan(BaseModel):
    """一次比路线的结果，页面画成路线卡。由 maps-service 的 route_compare 结果换来（见 history.routes_of）。"""

    model_config = ConfigDict(extra="forbid")
    origin: str
    destination: str
    routes: list[Route]
    unavailable_modes: list[str] = Field(default_factory=list, description="没算出来的出行方式")
    departure_time: str = Field(default="", description="用户说的出发时间，只是原话：路线没按那个时刻算")
    fetched_at: str = ""


class TravelStop(BaseModel):
    """班次的出发或到达：城市、站（机场）、航站楼，时刻是当地时间 YYYY-MM-DDTHH:MM（国际航班不换算时区）。"""

    model_config = ConfigDict(extra="forbid")
    city: str
    station: str
    terminal: str = ""
    time: str


class TravelService(BaseModel):
    """一个直达班次。由 flyai 的 search_flights、search_trains 结果换来（见 history.travel_of），价格是查询时的参考价。"""

    model_config = ConfigDict(extra="forbid")
    option_id: str = Field(description="服务给的班次编号；行程卡靠它从这一轮的结果里找班次")
    number: str = Field(description="航班号 / 车次")
    carrier: str = Field(default="", description="航司简称，或者车型（高铁、普快）")
    departure: TravelStop
    arrival: TravelStop
    day_offset: int = Field(default=0, description="比出发那天晚几天到；1 是次日到达")
    duration_minutes: int
    travel_class: str = Field(default="", description="价格对应的舱位 / 座席")
    price_yuan: float | None = None
    booking_url: str = Field(default="", description="飞猪的预订页")


class TravelList(BaseModel):
    """一次查航班或火车的结果，页面画成航班卡、火车卡。"""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["flight", "train"]
    origin: str = Field(description="查的起点（用户说的）")
    destination: str
    date: str = Field(description="出发日期 YYYY-MM-DD")
    services: list[TravelService]
    limitations: list[str] = Field(default_factory=list, description="服务标的限制，比如 high_speed_not_covered（那一页没有高铁）")
    observed_at: str = Field(default="", description="服务查询的时刻（北京时间）")


class ItineraryLeg(TravelService):
    """行程里的一段：这一轮查到的某个班次。"""

    kind: Literal["flight", "train"]


class ItineraryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    reason: str
    legs: list[ItineraryLeg]


class Itinerary(BaseModel):
    """模型从这一轮查到的班次里排的行程方案（present_itinerary），页面画成行程卡。"""

    model_config = ConfigDict(extra="forbid")
    title: str
    plans: list[ItineraryPlan]


class GeneratedImage(BaseModel):
    """生成的一张图。存在 image-service 里、归灵稿，页面按编号经灵稿后端取（见 app.tools.image_mcp、api.main）。
    2026-11-04 以前的图是灵稿自己存的文件（有 file），那些文件不搬了（Sean 定），页面上说这张图已经不在了。"""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(description="图片编号（image-service 的）：模型改这张图时用它（source_image_id）")
    file: str | None = Field(default=None, description="以前灵稿自己存的图的文件名；现在的图没有")
    mime_type: str
    width: int
    height: int
    prompt: str = Field(description="交给生图模型的描述；改图时是修改要求")
    aspect_ratio: str
    model: str | None = Field(default=None, description="实际出图的模型")
    edited_from: str | None = Field(default=None, description="改的是哪张图（编号）")

    @property
    def stored(self) -> bool:
        """还在 image-service 里（不是以前灵稿自己存、已经不在了的那种）。"""
        return self.file is None


class ToolStep(BaseModel):
    """一次工具调用，给页面的过程记录用。"""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(description="工具调用 id；页面据此把实时进度和存下来的记录对上")
    kind: Literal["search", "read", "weather", "place", "route", "flight", "train", "itinerary", "image", "task", "library", "docs"]
    text: str = Field(description="搜索词 / 网址 / 地名 / 查地点的关键词 / 路线、航班、火车的起终点 / 行程方案的标题 / 画图的描述 / 子任务的标题"
                                  " / 找的库名 / 查文档的问题")
    limits: list[str] = Field(default_factory=list, description="搜索的时间、站点限制；查地点在哪查；比路线比哪几种方式；航班、火车的日期和条件；查的是哪个库")
    ok: bool = True
    hits: int = 0  # 搜索返回的条数；找到的候选库、查到的文档段数
    body: bool = False  # 读页读到了正文
    title: str = ""  # 读到的页面标题；查天气时是和风的地名
    source_ids: list[str] = Field(default_factory=list, description="这一步得到的来源")
    message: str = Field(default="", description="失败的原因")
    pending: bool = Field(default=False, description="已经发起、还没有结果")
    weather: Forecast | None = Field(default=None, description="查到的天气，页面画成天气卡片")
    places: PlaceList | None = Field(default=None, description="查到的地点，页面画成地点卡")
    routes: RoutePlan | None = Field(default=None, description="算出的路线，页面画成路线卡")
    travel: TravelList | None = Field(default=None, description="查到的航班、火车，页面画成航班卡、火车卡")
    itinerary: Itinerary | None = Field(default=None, description="排好的行程方案，页面画成行程卡")
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
    maps: int = 0  # 查地点、比路线的次数
    travel: int = 0  # 查航班、火车的次数
    docs: int = 0  # 找库、查文档的次数（Context7 按这两个工具的调用算额度）
    images: int = 0  # 画成的图（画新图和改图都算）
    subagents: int = 0  # 派出去的子 agent（真跑了的）
    # 这些模型调用加上做成了的工具调用（搜索、读页、画图……按次计价）一共多少元（钱数记在消息上，见 app.costs）。None 是有调用不知道钱数：
    # 没填价格的模型、记账之前（2026-11-06）的轮；点了停止的那一下本来就不算在 model_calls 里，也不在这里
    cost: float | None = None


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
    # 这一轮用的模型，开始时定下（见 agent.TurnMiddleware），出错重试接着用它。空的是 2026-11-05 之前的轮（都是 model_catalog.LEGACY_MODEL 答的），
    # 和排队时就停了、没开始的轮
    option: str = Field(default="", description="这一轮按哪个选项（models.toml 的 id）：一般就是对话定下的，那个选项停了改用 Auto 的是 auto")
    model: ModelUse | None = Field(default=None, description="主 agent 用的模型")
    subagent_model: ModelUse | None = Field(default=None, description="子 agent 用的模型")


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
    option: NotRequired[str]  # 对话定下的模型选项（models.toml 的 id）：新建对话时写进来，没选的第一轮开始时定成 auto；之后不换（见 agent.TurnMiddleware）
    turns: NotRequired[list[Turn]]
    compaction: NotRequired[Compaction]
