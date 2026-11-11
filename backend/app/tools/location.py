"""用户的当前位置（2026-11-11 Sean 定，见 docs/LangChain研究/04-MCP接入和agent-tools下沉.md 的“当前位置”一节）。

模型查天气、查地点、比路线时可以选“当前位置”（get_weather 的 location_source、place_search 的 anchor_source、
route_compare 的 origin_source / destination_source 是 current_location）。位置从用户的浏览器来：
- LocationMiddleware 在工具真去调之前停下来要位置：和问用户同一套（LangGraph 的 interrupt，状态 asking，见 app.tools.ask_user），
  只是 state.ask 的 kind 是 location，页面出的是位置卡（浏览器里已经允许过定位的不出卡，直接取了交回来）。
  不回应就一直等，不会过一阵自己往下做。Chat、Work 的轮都会要；不算进一轮最多问几次，“别再问了”也不管它。
- 坐标不进模型的参数，放在这次 MCP 调用的 _meta 里（agent-tools 的约定：agent-tools/geolocation，WGS84，见 app.tools.mcp.request_meta）；
  转成高德的坐标、查出是哪个城市都在服务里。
- 坐标只放在内存里（Places），这一轮完了就丢：不进检查点（交回 interrupt 的只有“给了 / 没给”）、不进日志、不给模型看。
  进程重启了坐标就没了：这一轮里再用到的重新要一次。
- 没给的（用户点了不用、浏览器取不到、用户直接说了个地方）工具不调，回一段话让模型别再要、改用用户说的地方（或记忆里的）或者问用户；
  这一轮里再选当前位置的不再停下来要（照 Fusion 的做法：拒绝过的这次运行里不再弹）。
只给主 agent：子 agent 没有这几个工具。命令行、评测没有页面可以要位置（ChatContext.places 是 None，见 research_runner.run_turn）：
不停下来，照常调，服务自己回 location_unavailable。
"""

from __future__ import annotations

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
    calls: set[str]  # 为这几次工具调用要来的：它们接着跑时还要把 interrupt 的值取走（见 LocationMiddleware）


class Places:
    """这一轮里用户给的位置，只在内存里：对话 id → 位置。这一轮完了（答完、停了、出错）就丢（见 research_runner）。"""

    def __init__(self) -> None:
        self._held: dict[str, _Held] = {}

    def put(self, thread_id: str, device: Device, calls: set[str]) -> None:
        held = self._held.get(thread_id)
        self._held[thread_id] = _Held(device, calls | (held.calls if held else set()))

    def get(self, thread_id: str) -> Device | None:
        held = self._held.get(thread_id)
        return held.device if held else None

    def asked_for(self, thread_id: str, call_id: str) -> bool:
        held = self._held.get(thread_id)
        return held is not None and call_id in held.calls

    def clear(self, thread_id: str) -> None:
        self._held.pop(thread_id, None)


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
    接着跑时这一步从头再执行一遍：为这次调用要来的位置已经在 Places 里，interrupt 这一次直接返回结果。"""

    async def awrap_tool_call(self, request, handler):
        call = request.tool_call
        purpose = wants(call["name"], call.get("args") or {})
        ctx = request.runtime.context if request.runtime else None
        places, thread_id = getattr(ctx, "places", None), getattr(ctx, "thread_id", None)
        if purpose is None or places is None or not thread_id:
            return await handler(request)
        answer = None
        device = places.get(thread_id)
        if device is None or places.asked_for(thread_id, call["id"]):
            if device is None and not_given_before(request.state["messages"]):
                return not_provided(call, None)
            answer = PlaceAnswer.model_validate(interrupt({"type": ASK, "kind": LOCATION, "call_id": call["id"], "purpose": purpose}))
            device = places.get(thread_id)
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
