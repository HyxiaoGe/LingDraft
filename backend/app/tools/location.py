"""用户的当前位置（2026-11-11 Sean 定，见 docs/LangChain研究/04-MCP接入和agent-tools下沉.md 的“当前位置”一节）。

模型查天气、查地点、比路线时可以选“当前位置”（get_weather 的 location_source、place_search 的 anchor_source、
route_compare 的 origin_source / destination_source 是 current_location）。位置从用户的浏览器来：
- LocationMiddleware 在工具真去调之前停下来要位置：和问用户同一套（LangGraph 的 interrupt，状态 asking，见 app.tools.ask_user），
  只是 state.ask 的 kind 是 location，页面出的是位置卡（浏览器里已经允许过定位的不出卡，直接取了交回来）。
  不回应就一直等，不会过一阵自己往下做。Chat、Work 的轮都会要；不算进一轮最多问几次，“别再问了”也不管它。
- 坐标不进模型的参数，放在这次 MCP 调用的 _meta 里（agent-tools 的约定：agent-tools/geolocation，WGS84，见 app.tools.mcp.request_meta）；
  转成高德的坐标、查出是哪个城市都在服务里。
- 坐标只放在内存里（Places）：不进检查点（交回 interrupt 的只有“给了 / 没给”）、不进日志、不给模型看。
- 要过一次就不再要（2026-11-11 Sean 定）：给过的位置按人留 KEEP_SECONDS，这段时间里这个人的哪段对话、哪一轮再用到都直接用，
  不停下来；过了再用到的重新要（页面不再出卡，悄悄取一次，见 timeline.jsx 的 LocateCard）。留得不长是因为人会走动。
  进程重启了坐标就没了：再用到的重新要一次。
- 没给的（用户点了不用、浏览器取不到、用户直接说了个地方）工具不调，回一段话让模型别再要、改用用户说的地方（或记忆里的）或者问用户；
  这一轮里再选当前位置的不再停下来要（照 Fusion 的做法：拒绝过的这次运行里不再弹）。
只给主 agent：子 agent 没有这几个工具。命令行、评测没有页面可以要位置（ChatContext.places 是 None，见 research_runner.run_turn）：
不停下来，照常调，服务自己回 location_unavailable。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import BaseMessage, ToolMessage
from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict, Field

from app.graph.research import prompts
from app.graph.research.history import (
    CURRENT_LOCATION,
    LOCATED,
    LOCATION,
    LOCATION_DECLINED,
    LOCATION_UNAVAILABLE,
    PLACE_TOOL,
    ROUTE_TOOL,
    last_human,
    located_of,
    where_of,
)
from app.tools import mcp
from app.tools.ask_user import ASK, MAX_ANSWER

WEATHER_TOOL = "get_weather"
GEO_META_KEY = "agent-tools/geolocation"  # agent-tools 的约定（services/maps/app/tools.py 的 GEOLOCATION_META_KEY）
# 要位置是为了什么：页面按它写位置卡上的那句话
WEATHER, NEARBY, ROUTE = "weather", "nearby", "route"
# 用户怎么回的：给了、点了不用、浏览器取不到、没给位置而是直接说了个地方
PROVIDED, DECLINED, UNAVAILABLE, SAID = "provided", "declined", "unavailable", "said"
# 给过的位置留多久（秒）。这段时间里再用到的直接用；过了重新跟页面要。人会走动，留久了“附近”“从我这儿出发”就不准了
KEEP_SECONDS = 600


class Device(BaseModel):
    """浏览器给的位置（navigator.geolocation，WGS84）。超出范围的进不来；精度太差（服务认 50 公里以内）由服务判断。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    longitude: float = Field(ge=-180, le=180, repr=False)
    latitude: float = Field(ge=-90, le=90, repr=False)
    accuracy_m: float | None = Field(default=None, ge=0, repr=False)

    def meta(self) -> dict:
        return {GEO_META_KEY: self.model_dump(exclude_none=True)}


class PlaceAnswer(BaseModel):
    """位置卡的结果：接着跑时交给 interrupt 的值（进检查点，所以里面没有坐标）。answer 只有 said 时有（用户说的地方）。
    asked_at、answered_at 是运行器填的，算这一轮用时的时候去掉等的这段（见 history.waited_ms）。"""

    model_config = ConfigDict(extra="forbid")
    status: Literal["provided", "declined", "unavailable", "said"]
    answer: str = Field(default="", max_length=MAX_ANSWER)
    asked_at: str = ""
    answered_at: str = ""


@dataclass
class _Held:
    device: Device = field(repr=False)
    until: float  # 留到什么时候（Places 的钟）


def holder(owner: str | None, thread_id: str) -> str:
    """位置记在谁名下：对话归的那个人（登录的人、游客的身份）；没归谁的（登录以前的老对话、测试）按对话记。"""
    return f"owner:{owner}" if owner else f"thread:{thread_id}"


class Places:
    """用户给的位置，只在内存里：谁（见 holder）→ 位置，留 keep 秒，过了就丢。
    另外记着每段对话这一轮里为哪几次工具调用停下来要过位置：它们接着跑时还要把 interrupt 的值取走（见 LocationMiddleware），
    这一轮完了（答完、停了、出错）就不记了（见 research_runner）。"""

    def __init__(self, keep: float = KEEP_SECONDS, clock: Callable[[], float] = time.monotonic) -> None:
        self._keep, self._clock = keep, clock
        self._held: dict[str, _Held] = {}
        self._asked: dict[str, set[str]] = {}  # 对话 id → 这一轮里停下来要过位置的工具调用

    def put(self, who: str, device: Device, thread_id: str, calls: set[str]) -> None:
        now = self._clock()
        self._held = {k: h for k, h in self._held.items() if h.until > now}  # 顺手把过了时候的都丢掉，不等那个人再来
        self._held[who] = _Held(device, now + self._keep)
        self._asked.setdefault(thread_id, set()).update(calls)

    def get(self, who: str) -> Device | None:
        held = self._held.get(who)
        if held is not None and held.until <= self._clock():
            del self._held[who]
            return None
        return held.device if held else None

    def asked_for(self, thread_id: str, call_id: str) -> bool:
        return call_id in self._asked.get(thread_id, ())

    def end_turn(self, thread_id: str) -> None:
        self._asked.pop(thread_id, None)


def wants(name: str, args: dict) -> str | None:
    """这次工具调用要不要用户的当前位置：要的话返回为了什么（WEATHER / NEARBY / ROUTE）。只认参数里明确选的 current_location。"""
    if name == WEATHER_TOOL:
        return WEATHER if args.get("location_source") == CURRENT_LOCATION else None
    if name == PLACE_TOOL:
        return NEARBY if args.get("anchor_source") == CURRENT_LOCATION else None
    if name == ROUTE_TOOL:
        return ROUTE if CURRENT_LOCATION in (args.get("origin_source"), args.get("destination_source")) else None
    return None


def not_given_before(messages: list[BaseMessage]) -> bool:
    """这一轮里已经要过位置、没给。messages 是到这一步为止的消息。"""
    return any(isinstance(m, ToolMessage) and (got := located_of(m)) is not None and got.get("status") != PROVIDED
               for m in messages[last_human(messages) + 1:])


def not_provided(call: dict, answer: PlaceAnswer | None) -> ToolMessage:
    """没拿到位置，工具没调：给模型的话按怎么没拿到的来（answer 是 None：这一轮前面已经要过、没给）。
    结果的样子和服务回的失败一样（structured_content 里 ok=false），过程里照常显示成没查成。"""
    status = answer.status if answer else "again"
    code = LOCATION_UNAVAILABLE if status in (UNAVAILABLE, PROVIDED) else LOCATION_DECLINED
    # provided 而坐标没了：交回位置以后进程重启过（坐标只在内存里），当取不到
    key = UNAVAILABLE if status == PROVIDED else status
    artifact: dict = {"structured_content": {"ok": False, "error": {"code": code, "retryable": False}}}
    if answer:
        artifact[LOCATED] = answer.model_dump()
    return ToolMessage(content=prompts.render(f"tools.location.{key}", answer=answer.answer if answer else ""), artifact=artifact,
                       tool_call_id=call["id"], name=call["name"], status="error")


class LocationMiddleware(AgentMiddleware):
    """见模块说明。放在 ToolProgressMiddleware 里面（过程里先有这一步，再停下来要位置）、WeatherMiddleware 和 MapsMiddleware 外面
    （它们把工具里抛的错都当成没调成，中断不能从它们里面抛出来）。
    这个人给过的位置还留着的直接用，不停下来。停下来要的，接着跑时这一步从头再执行一遍：
    为这次调用要来的位置已经在 Places 里，interrupt 这一次直接返回结果。"""

    async def awrap_tool_call(self, request, handler):
        call = request.tool_call
        purpose = wants(call["name"], call.get("args") or {})
        ctx = request.runtime.context if request.runtime else None
        places, thread_id = getattr(ctx, "places", None), getattr(ctx, "thread_id", None)
        if purpose is None or places is None or not thread_id:
            return await handler(request)
        answer = None
        who = holder(getattr(ctx, "owner", None), thread_id)
        device = places.get(who)
        if device is None or places.asked_for(thread_id, call["id"]):
            if device is None and not_given_before(request.state["messages"]):
                return not_provided(call, None)
            answer = PlaceAnswer.model_validate(interrupt({"type": ASK, "kind": LOCATION, "call_id": call["id"], "purpose": purpose}))
            device = places.get(who)
            if answer.status != PROVIDED or device is None:
                if (said := where_of(call, result := not_provided(call, answer))) is not None:
                    _emit({"type": "ask", "ask": said})
                return result
        with mcp.request_meta(device.meta()):
            result = await handler(request)
        if answer is not None and isinstance(result, ToolMessage):
            result = result.model_copy(update={"artifact": {**(result.artifact if isinstance(result.artifact, dict) else {}),
                                                            LOCATED: answer.model_dump()}})
        return result


def _emit(event: dict) -> None:
    from app.graph.research.agent import emit  # agent 导入本模块（build_agent 里），这里用到时再导入
    emit(event)
