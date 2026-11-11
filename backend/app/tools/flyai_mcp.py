"""查航班、火车，排行程：agent-tools flyai 的 MCP 工具 search_flights、search_trains（经网关 /flyai/mcp），和灵稿自己的 present_itinerary。

日期定不了先问、参数怎么规整、飞猪认不出的地名、只要高铁、结果的样子、失败时给模型的说明都在 flyai 服务里，Fusion 和灵稿共用
（见 agent-tools 的 services/flyai/README.md）。说明后面接上灵稿自己的一段（prompts/tools_travel.toml 的 tools.search_flights.lingdraft 等）。
这里只做灵稿自己的事（TravelMiddleware）：
- 查航班、火车：给模型的是服务的那段文字（已经去掉预订链接），没查成的 status=error；整份结果留在 artifact 的
  structured_content 里，history 把它换成航班卡、火车卡。每轮最多查 MAX_TRAVEL_PER_TURN 次（照 Fusion fb07f632），
  参数不对、排队没等到这些没去查飞猪的不算。
- 网关、服务连不上算 unavailable；网关今天的上限到了（429）算 quota_exhausted；密钥不对、网关没放行让这次运行失败（和地图一样）。
  还没连上时用同名的占位工具。
- 比路线时核对车站的城市（照 Fusion 9685489a“班次站点缺少城市时，要求模型使用当前 run 的结构化城市做一次受控修参”）：
  route_compare 的起点或终点正好是这段对话查到的某个站名时，要带上这个站的城市；没带、带错都不调地图，告诉模型该填哪个城市。
  地图服务按城市找地名，城市不对就可能定到别处，所以不交给它。
- 排行程（present_itinerary）：模型从这段对话查到的班次里挑，按 option_id 列出来，这里核对、换成行程卡；
  不在查到的班次里的编号、顺序不对的都退回给模型改，每轮最多出一张。Fusion 只认这一轮查到的，灵稿是对话，
  “那就按刚才查的排一下”很常见，所以认这段对话里还在的结果（被压缩掉的就找不到，模型会重查）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx2
from fastmcp import Client
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field, ValidationError

from app.graph.research import prompts
from app.graph.research.history import FLIGHT_TOOL, ITINERARY_TOOL, ROUTE_TOOL, TRAIN_TOOL, TRAVEL_KINDS, last_human, service_of
from app.graph.research.state import Itinerary, ItineraryLeg, ItineraryPlan
from app.tools import mcp
from app.tools.gateway import GatewayAuthError, GatewayLimitError

logger = logging.getLogger(__name__)

TRAVEL_TOOLS = (FLIGHT_TOOL, TRAIN_TOOL)
KINDS = TRAVEL_KINDS
MCP_PATH = "/flyai/mcp"
UNAVAILABLE = "unavailable"  # 没调成（网关、服务连不上，或者还没连上）时的错误码
MAX_TRAVEL_PER_TURN = 4
# 这些没去查飞猪，不占每轮的次数：参数不对、服务排队没等到、超了次数、网关上限到了、MCP 的参数校验没过
NOT_COUNTED = {"invalid_arguments", "busy", "turn_limit", "quota_exhausted", "tool_error"}
MAX_PLANS = 3
MAX_LEGS = 6


def gateway_target(base_url: str, api_key: str, *, scene: str = "research",
                   transport: httpx2.AsyncBaseTransport | None = None) -> Client:
    return mcp.gateway_target(base_url, api_key, MCP_PATH, scene=scene, transport=transport)


class ToolOffline(RuntimeError):
    """占位工具被调了：航班火车服务还没连上。"""


class _TravelArgs(BaseModel):
    origin: str = Field(description="Where the trip starts.")
    destination: str = Field(description="Where the trip ends.")
    departure_date: str = Field(description="Departure date as YYYY-MM-DD.")


def placeholder(name: str) -> BaseTool:
    """没连上时的同名工具：参数只留最要紧的，调了抛 ToolOffline，中间件按 unavailable 回给模型。"""

    async def offline(**_: Any) -> str:
        raise ToolOffline(f"{name} 还没连上")

    return StructuredTool.from_function(
        coroutine=offline, name=name, args_schema=_TravelArgs,
        description=f"{prompts.render(f'tools.{name}.brief')}\n\n{prompts.render(f'tools.{name}.lingdraft')}",
    )


class TravelTool(mcp.McpTool):
    def __init__(self, name: str, target: Callable[[], Any]) -> None:
        super().__init__(name, target)
        self._placeholder = placeholder(name)

    def current(self) -> BaseTool:
        return self.tool or self._placeholder


# ---- 排行程的工具：参数在这里，核对、出卡片在 TravelMiddleware ----


class PlanArgs(BaseModel):
    title: str = Field(min_length=1, max_length=40, description=prompts.render("tools.present_itinerary.plan_title"))
    reason: str = Field(min_length=1, max_length=300, description=prompts.render("tools.present_itinerary.plan_reason"))
    option_ids: list[str] = Field(min_length=1, max_length=MAX_LEGS, description=prompts.render("tools.present_itinerary.option_ids"))


class ItineraryArgs(BaseModel):
    title: str = Field(min_length=1, max_length=60, description=prompts.render("tools.present_itinerary.title"))
    plans: list[PlanArgs] = Field(min_length=1, max_length=MAX_PLANS, description=prompts.render("tools.present_itinerary.plans"))


def itinerary_tool() -> BaseTool:
    async def present_itinerary(**_: Any) -> str:
        # TravelMiddleware 接管了这个工具，不会走到这里；走到了说明中间件没装上
        raise RuntimeError(f"{ITINERARY_TOOL} 要经 TravelMiddleware")

    return StructuredTool.from_function(coroutine=present_itinerary, name=ITINERARY_TOOL, args_schema=ItineraryArgs,
                                        description=prompts.render("tools.present_itinerary.description", max_plans=MAX_PLANS))


# ---- 这段对话查到的班次 ----


def _results(messages: list[BaseMessage]) -> list[tuple[str, dict]]:
    """查成了的航班、火车结果（种类, structuredContent），按先后。"""
    found = []
    for m in messages:
        if isinstance(m, ToolMessage) and m.name in TRAVEL_TOOLS and m.status != "error":
            data = mcp.structured(m)
            if isinstance(data, dict) and data.get("ok") is True and isinstance(data.get("services"), list):
                found.append((KINDS[m.name], data))
    return found


def services_in(messages: list[BaseMessage]) -> dict[str, tuple[str, dict]]:
    """option_id → (种类, 班次)；同一个编号查到几次用最后一次。"""
    found: dict[str, tuple[str, dict]] = {}
    for kind, data in _results(messages):
        for s in data["services"]:
            if isinstance(s, dict) and isinstance(s.get("option_id"), str):
                found[s["option_id"]] = (kind, s)
    return found


def station_cities(messages: list[BaseMessage]) -> dict[str, str]:
    """站名（机场名）→ 城市，来自这段对话查到的班次；同一个站查到几次用最后一次。"""
    cities: dict[str, str] = {}
    for _kind, s in services_in(messages).values():
        for end in ("departure", "arrival"):
            stop = s.get(end)
            if isinstance(stop, dict) and isinstance(stop.get("station"), str) and isinstance(stop.get("city"), str):
                cities[stop["station"].strip()] = stop["city"].strip()
    return cities


def travel_before(messages: list[BaseMessage], call_id: str | None) -> int:
    """这一轮里排在这次调用前面、去查了飞猪的航班火车查询有几次。并行的几次还没有结果，都算查了。"""
    turn = messages[last_human(messages) + 1:]
    results = {m.tool_call_id: m for m in turn if isinstance(m, ToolMessage)}
    n = 0
    for m in turn:
        if not isinstance(m, AIMessage):
            continue
        for tc in m.tool_calls:
            if tc["name"] not in TRAVEL_TOOLS:
                continue
            if tc.get("id") == call_id:
                return n
            r = results.get(tc.get("id"))
            if r is None or _error_code(r) not in NOT_COUNTED:
                n += 1
    return n


def _error_code(m: ToolMessage) -> str | None:
    data = mcp.structured(m)
    err = data.get("error") if isinstance(data, dict) else None
    return str(err.get("code")) if isinstance(err, dict) and err.get("code") else None


def _same_city(a: str, b: str) -> bool:
    return a.strip().removesuffix("市") == b.strip().removesuffix("市")


def _quoted(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


# ---- 中间件 ----


def failed(name: str, call_id: str, code: str) -> ToolMessage:
    """中间件自己回的查询失败（服务没给结果）：没调成、网关今天的上限到了、超了每轮次数。"""
    text = prompts.render(f"tools.travel.{code}", noun=KINDS[name], max_per_turn=MAX_TRAVEL_PER_TURN)
    return ToolMessage(content=text, artifact={"structured_content": {"ok": False, "error": {"code": code, "retryable": code == UNAVAILABLE}}},
                       tool_call_id=call_id, name=name, status="error")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def check_route_stations(call: dict, messages: list[BaseMessage]) -> ToolMessage | None:
    """route_compare 的起终点是查到的车站时，城市要和查到的一样；不一样返回给模型的失败，一样（或者不是车站）返回 None。"""
    args = call.get("args") or {}
    cities = station_cities(messages)
    problems = []
    for end in ("origin", "destination"):
        if args.get(f"{end}_source") != "named":
            continue
        station = str(args.get(end) or "").strip()
        expected = cities.get(station)
        given = str(args.get(f"{end}_city") or "").strip()
        if expected and not (given and _same_city(given, expected)):
            problems.append({"endpoint": end, "station": _quoted(station), "city": _quoted(expected), "given": _quoted(given) if given else ""})
    if not problems:
        return None
    logger.info("route_compare 的车站城市没对上：%s", [p["endpoint"] for p in problems])
    return ToolMessage(content=prompts.render("tools.route_compare.station_city", problems=problems),
                       artifact={"structured_content": {"ok": False, "error": {"code": "station_city", "retryable": True}}},
                       tool_call_id=call["id"], name=ROUTE_TOOL, status="error")


def _itinerary_failed(call_id: str, code: str, detail: str = "") -> ToolMessage:
    return ToolMessage(content=prompts.render(f"tools.present_itinerary.{code}", detail=detail),
                       artifact={"error_code": code}, tool_call_id=call_id, name=ITINERARY_TOOL, status="error")


def present(call: dict, messages: list[BaseMessage]) -> ToolMessage:
    """核对模型排的行程，换成行程卡；不对的退回给模型改（见模块说明）。"""
    cid = call["id"]
    turn = messages[last_human(messages) + 1:]
    if any(isinstance(m, ToolMessage) and m.name == ITINERARY_TOOL and m.status != "error" for m in turn):
        return _itinerary_failed(cid, "already_presented")
    try:
        parsed = ItineraryArgs.model_validate(call.get("args") or {})
    except ValidationError as e:
        first = e.errors()[0] if e.errors() else {}
        where = ".".join(str(x) for x in first.get("loc", ()))
        return _itinerary_failed(cid, "failed", f"The arguments are invalid ({where}: {first.get('msg', 'invalid')}).")
    found = services_in(messages)
    if not found:
        return _itinerary_failed(cid, "failed", "No flight or train search in this conversation has returned services yet.")
    plans = []
    for plan in parsed.plans:
        ids = [i.strip() for i in plan.option_ids]
        unknown = [i for i in ids if i not in found]
        if unknown:
            return _itinerary_failed(cid, "failed", f"These option_id values were not returned by any search in this conversation: "
                                                    f"{', '.join(_quoted(i) for i in unknown)}.")
        if len(set(ids)) != len(ids):
            return _itinerary_failed(cid, "failed", f"The plan {_quoted(plan.title)} lists the same service more than once.")
        legs = []
        for i in ids:
            kind, s = found[i]
            try:
                legs.append(ItineraryLeg(kind=kind, **service_of(s).model_dump()))
            except (ValidationError, KeyError, TypeError, AttributeError):
                return _itinerary_failed(cid, "failed", f"The service {_quoted(i)} could not be read; search again.")
        # 时刻都是当地时间（国际航班不换算时区），按字面比；后一段要在前一段到了以后出发
        if any(a.arrival.time > b.departure.time for a, b in zip(legs, legs[1:], strict=False)):
            return _itinerary_failed(cid, "failed", f"In the plan {_quoted(plan.title)}, a service departs before the previous one arrives, "
                                                    f"or the services are not in travel order.")
        plans.append(ItineraryPlan(title=plan.title.strip(), reason=plan.reason.strip(), legs=legs))
    itinerary = Itinerary(title=parsed.title.strip(), plans=plans)
    titles = "; ".join(_quoted(p.title) for p in plans)
    text = prompts.render("tools.present_itinerary.presented", plan_count=len(plans), plan_titles=titles)
    return ToolMessage(content=text, artifact={"itinerary": itinerary.model_dump()}, tool_call_id=cid, name=ITINERARY_TOOL, status="success")


class TravelMiddleware(AgentMiddleware):
    """管 search_flights、search_trains、present_itinerary，以及 route_compare 的车站城市核对，见模块说明。
    放在 ToolProgressMiddleware 里面、MapsMiddleware 外面：车站城市不对的不交给地图。"""

    async def awrap_tool_call(self, request, handler):
        call = request.tool_call
        name = call["name"]
        if name not in (*TRAVEL_TOOLS, ITINERARY_TOOL, ROUTE_TOOL):
            return await handler(request)
        state = getattr(request, "state", None) or {}
        messages = state.get("messages", []) if isinstance(state, dict) else []
        if name == ROUTE_TOOL:
            return check_route_stations(call, messages) or await handler(request)
        if name == ITINERARY_TOOL:
            return present(call, messages)
        if travel_before(messages, call["id"]) >= MAX_TRAVEL_PER_TURN:
            return failed(name, call["id"], "turn_limit")
        try:
            result = await handler(request)
        except Exception as e:
            if (task := asyncio.current_task()) is not None and task.cancelling():
                raise  # 点了停止：照常取消
            if auth := mcp.find_error(e, GatewayAuthError):
                raise auth from e
            if mcp.find_error(e, GatewayLimitError):
                logger.warning("%s：网关今天的上限到了", name)
                return failed(name, call["id"], "quota_exhausted")
            logger.warning("%s 没调成：%s: %s", name, type(e).__name__, str(e)[:200])
            return failed(name, call["id"], UNAVAILABLE)
        if not isinstance(result, ToolMessage):
            return result
        text = mcp.text_of(result.content)
        data = mcp.structured(result)
        if result.status == "error" or not isinstance(data, dict):
            # MCP 的 isError（参数没过服务的校验）：服务没去查，给的说明照给模型
            logger.warning("%s 参数不对：%s", name, text[:200])
            error = {"ok": False, "error": {"code": "tool_error", "retryable": False}}
            return result.model_copy(update={"content": text, "status": "error", "artifact": {"structured_content": error}})
        ok = data.get("ok") is True
        return result.model_copy(update={"content": text, "status": "success" if ok else "error",
                                         "artifact": {"structured_content": data, "retrieved_at": _now()}})
