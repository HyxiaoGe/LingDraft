"""查航班、火车，排行程：agent-tools flyai 的 MCP 工具 search_flights、search_trains 和灵稿自己的 present_itinerary（app.tools.flyai_mcp）。
取工具、占位、经网关调、中间件（每轮次数、比路线时核对车站城市、核对行程）、过程记录换成航班卡、火车卡、行程卡、整轮对话。
服务用进程内的假 flyai（research_llm.fake_flyai_server）；经 HTTP 的用 ASGI 模拟网关。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx2
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app import aio
from app.graph.research import history, prompts
from app.research_llm import FakeResearchPlanner, fake_flyai_server
from app.tools.flyai_mcp import (
    FLIGHT_TOOL,
    ITINERARY_TOOL,
    MAX_TRAVEL_PER_TURN,
    ROUTE_TOOL,
    TRAIN_TOOL,
    TravelMiddleware,
    TravelTool,
    failed,
    gateway_target,
    itinerary_tool,
    placeholder,
)
from app.tools.gateway import GatewayAuthError
from tests.test_research import Chat

DAY, BACK = "2026-11-20", "2026-11-22"


def _call(tool: str, cid: str = "c1", **args) -> dict:
    base = {"origin": "北京", "destination": "上海", "departure_date": DAY}
    if tool == TRAIN_TOOL:
        base["train_category"] = "high_speed"
    return {"name": tool, "args": base | args, "id": cid, "type": "tool_call"}


def _route(cid: str = "r1", **args) -> dict:
    base = {"origin": "上海南站", "destination": "外滩", "origin_source": "named", "destination_source": "named", "modes": ["transit"]}
    return {"name": ROUTE_TOOL, "args": base | args, "id": cid, "type": "tool_call"}


def _plan(*ids: str, title: str = "去飞回铁") -> dict:
    return {"title": title, "reason": "去程坐飞机，回程坐高铁。", "option_ids": list(ids)}


def _present(*plans: dict, cid: str = "p1") -> dict:
    return {"name": ITINERARY_TOOL, "args": {"title": "北京上海往返", "plans": list(plans)}, "id": cid, "type": "tool_call"}


def _connected(name: str, target=fake_flyai_server) -> TravelTool:
    t = TravelTool(name, target)
    aio.run(t.connect())
    return t


async def _through(tool, call: dict, messages: list | None = None):
    async def handler(req):
        return await tool.ainvoke(call)

    request = SimpleNamespace(tool_call=call, state={"messages": messages or []})
    return await TravelMiddleware().awrap_tool_call(request, handler)


def _ask(tool, call: dict, messages: list | None = None) -> ToolMessage:
    return aio.run(_through(tool, call, messages))


def _searched(*pairs: tuple[str, dict]) -> list:
    """一段对话：用户问了一句，模型查了这几次，每次都有结果。"""
    msgs: list = [HumanMessage("查一下")]
    for tool_name, call in pairs:
        msgs += [AIMessage(content="", tool_calls=[call]), _ask(_connected(tool_name).tool, call)]
    return msgs


def _ids(msg: ToolMessage) -> list[str]:
    return [s["option_id"] for s in msg.artifact["structured_content"]["services"]]


# ---- 取工具、占位 ----


def test_connect_takes_the_service_tools_and_adds_lingdraft_note():
    for name in (FLIGHT_TOOL, TRAIN_TOOL):
        t = TravelTool(name, fake_flyai_server)
        assert t.current().name == name and t.current().description.startswith(prompts.render(f"tools.{name}.brief"))  # 还没连：占位
        aio.run(t.connect())
        assert t.current() is t.tool
        assert t.tool.description.startswith("Search one-way direct") and t.tool.description.endswith(prompts.render(f"tools.{name}.lingdraft"))
    # 灵稿的话：卡片上都列了、别贴预订链接；接站换乘用查到的站名和城市
    note = prompts.render("tools.search_trains.lingdraft")
    assert "do not output booking links" in note and "present_itinerary" in note and "city field" in note


def test_planner_has_travel_tools_after_maps_and_keeps_their_place():
    p = FakeResearchPlanner()
    names = [t.name for t in p.tools()]
    assert names.index(FLIGHT_TOOL) == names.index("route_compare") + 1 and names.index(TRAIN_TOOL) == names.index(FLIGHT_TOOL) + 1
    aio.run(p.connect_tools())
    assert [t.name for t in p.tools()][: len(names)] == names  # 连上以后换成真的，位置不变（前缀缓存）


def test_placeholder_says_the_travel_service_is_offline():
    call = _call(FLIGHT_TOOL)
    out = aio.run(_through(placeholder(FLIGHT_TOOL), call))
    assert out.status == "error" and out.content == prompts.render("tools.travel.unavailable", noun="flight")
    assert out.artifact["structured_content"]["error"] == {"code": "unavailable", "retryable": True}
    assert history.step_of(call, out).message == "航班火车暂时查不到"


# ---- 经网关调（HTTP） ----


class _Gateway:
    """模拟网关：转给假 flyai 的 MCP，记下请求头；status 设了就直接回这个状态。"""

    def __init__(self) -> None:
        self.mcp = fake_flyai_server().http_app(path="/flyai/mcp", stateless_http=True)
        self.status: int | None = None
        self.headers: list[dict] = []

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            self.headers.append({k.decode(): v.decode() for k, v in scope["headers"]})
            if self.status:
                await send({"type": "http.response.start", "status": self.status, "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"error":"daily_limit_exceeded"}'})
                return
        await self.mcp(scope, receive, send)


def test_calls_go_through_the_gateway_and_auth_errors_fail_the_run():
    gw = _Gateway()

    async def go():
        async with gw.mcp.lifespan(gw.mcp):
            t = TravelTool(TRAIN_TOOL, lambda: gateway_target("http://gw/", "k1", transport=httpx2.ASGITransport(app=gw)))
            await t.connect()
            ok = await _through(t.tool, _call(TRAIN_TOOL))
            gw.status = 429
            limited = await _through(t.tool, _call(TRAIN_TOOL))
            gw.status = 401
            try:
                await _through(t.tool, _call(TRAIN_TOOL))
            except GatewayAuthError as e:
                return ok, limited, e
            return ok, limited, None

    ok, limited, auth = aio.run(go())
    assert ok.status == "success" and json.loads(ok.content)["services"][0]["number"] == "G1"
    assert gw.headers[0]["authorization"] == "Bearer k1" and gw.headers[0]["x-caller-scene"] == "research"
    assert limited.status == "error" and limited.content == prompts.render("tools.travel.quota_exhausted", noun="train")
    assert limited.artifact["structured_content"]["error"]["code"] == "quota_exhausted"
    assert auth is not None and auth.kind == "http_401"


# ---- 中间件：结果 ----


def test_middleware_gives_the_model_the_service_text_and_keeps_the_card_data():
    tool = _connected(FLIGHT_TOOL).tool
    ok = _ask(tool, _call(FLIGHT_TOOL))
    seen = json.loads(ok.content)
    assert ok.status == "success" and "ok" not in seen and "booking_url" not in seen["services"][0]  # 模型看不到预订链接
    assert ok.artifact["structured_content"]["services"][0]["booking_url"].startswith("https://router.feizhu.com/")
    assert ok.artifact["retrieved_at"].endswith("+00:00")
    unknown = _ask(tool, _call(FLIGHT_TOOL, origin="火星"))
    assert unknown.status == "error" and unknown.artifact["structured_content"]["error"]["code"] == "origin_not_recognized"
    # 参数没过服务的校验（MCP 的 isError）：服务的说明照给模型，记成 tool_error
    bad = _ask(_connected(TRAIN_TOOL).tool, _call(TRAIN_TOOL, train_category="maglev"))
    assert bad.status == "error" and bad.artifact["structured_content"]["error"]["code"] == "tool_error"


def test_middleware_turns_connection_failures_into_unavailable():
    mw, call = TravelMiddleware(), _call(TRAIN_TOOL)

    async def raising(e):
        async def handler(req):
            raise e
        return await mw.awrap_tool_call(SimpleNamespace(tool_call=call, state={"messages": []}), handler)

    down = aio.run(raising(RuntimeError("Client failed to connect")))
    assert down.status == "error" and down.name == TRAIN_TOOL and down.content == prompts.render("tools.travel.unavailable", noun="train")
    wrapped = RuntimeError("Client failed to connect")
    wrapped.__cause__ = ExceptionGroup("tg", [GatewayAuthError("调 /flyai/mcp 失败：HTTP 403", kind="http_403")])
    with pytest.raises(GatewayAuthError):
        aio.run(raising(wrapped))

    async def cancelled() -> bool:
        try:
            await raising(asyncio.CancelledError())
        except asyncio.CancelledError:
            return True
        return False

    assert aio.run(cancelled())


# ---- 中间件：每轮次数 ----


def test_each_turn_runs_at_most_four_searches_and_refused_ones_do_not_count():
    tool = _connected(FLIGHT_TOOL).tool
    calls = [_call(FLIGHT_TOOL, f"c{k}", destination=f"城市{k}") for k in range(MAX_TRAVEL_PER_TURN)]
    msgs: list = [HumanMessage("上一轮"), AIMessage(content="", tool_calls=[_call(FLIGHT_TOOL, "old")]), _ask(tool, _call(FLIGHT_TOOL, "old")),
                  AIMessage(content="好"), HumanMessage("查几趟")]
    # 没去查飞猪的（排队没等到）不算
    busy = ToolMessage(content="busy", tool_call_id="b1", name=FLIGHT_TOOL, status="error",
                       artifact={"structured_content": {"ok": False, "error": {"code": "busy"}}})
    msgs += [AIMessage(content="", tool_calls=[_call(FLIGHT_TOOL, "b1")]), busy]
    for c in calls[:-1]:
        msgs += [AIMessage(content="", tool_calls=[c]), _ask(tool, c)]
    # 第 4 次和一个并行的第 5 次在同一条模型消息里：第 4 次照查，第 5 次超了
    extra = _call(FLIGHT_TOOL, "c9", destination="城市9")
    msgs.append(AIMessage(content="", tool_calls=[calls[-1], extra]))
    assert _ask(tool, calls[-1], msgs).status == "success"
    over = _ask(tool, extra, msgs)
    assert over.status == "error" and over.content == prompts.render("tools.travel.turn_limit", noun="flight", max_per_turn=MAX_TRAVEL_PER_TURN)
    assert history.step_of(extra, over).message == "这一轮查的次数到上限了"
    # 超了次数的没去查，不算用量；排队没等到的到了服务那边，照常算一次
    assert history.usage_of([busy, over, _ask(tool, calls[0])]).travel == 2


# ---- 中间件：比路线时核对车站城市 ----


def test_route_from_a_returned_station_needs_that_stations_city():
    msgs = _searched((TRAIN_TOOL, _call(TRAIN_TOOL)))  # 北京南站 → 上海南站

    async def ask(call):
        reached = []

        async def handler(req):
            reached.append(call["id"])
            return ToolMessage(content="routes", tool_call_id=call["id"], name=ROUTE_TOOL)

        out = await TravelMiddleware().awrap_tool_call(SimpleNamespace(tool_call=call, state={"messages": msgs}), handler)
        return out, bool(reached)

    missing, reached = aio.run(ask(_route()))
    assert not reached and missing.status == "error" and missing.artifact["structured_content"]["error"]["code"] == "station_city"
    assert '"上海南站"' in missing.content and '"上海"' in missing.content and "origin_city was not given" in missing.content
    assert history.step_of(_route(), missing).message == "车站所在的城市没填对，没去查"
    wrong, reached = aio.run(ask(_route(origin_city="杭州")))
    assert not reached and 'origin_city was "杭州"' in wrong.content
    # 终点是查到的车站也一样
    to_station = _route(origin="外滩", destination="北京南站", destination_city="上海")
    assert "destination_city" in aio.run(ask(to_station))[0].content
    # 城市对上了（带不带“市”都行）、不是查到的站、用户的位置：照常交给地图
    assert aio.run(ask(_route(origin_city="上海市")))[1]
    assert aio.run(ask(_route(origin="虹桥火车站")))[1]
    # 被拦下的不算地图用量
    assert history.usage_of([missing]).maps == 0


# ---- 排行程 ----


def test_present_itinerary_turns_returned_services_into_an_itinerary_card():
    msgs = _searched((FLIGHT_TOOL, _call(FLIGHT_TOOL, "f1")), (TRAIN_TOOL, _call(TRAIN_TOOL, "t1", origin="上海", destination="北京", departure_date=BACK)))
    flight_ids, train_ids = _ids(msgs[2]), _ids(msgs[4])
    call = _present(_plan(flight_ids[0], train_ids[0]), _plan(train_ids[1], title="只有回程"))
    out = aio.run(_through(None, call, msgs))
    assert out.status == "success" and out.content == prompts.render("tools.present_itinerary.presented", plan_count=2,
                                                                     plan_titles='"去飞回铁"; "只有回程"')
    step = history.step_of(call, out)
    assert step.kind == "itinerary" and step.text == "北京上海往返" and step.ok
    first = step.itinerary.plans[0]
    assert [(leg.kind, leg.number) for leg in first.legs] == [("flight", "MU5101"), ("train", "G1")]
    flight = first.legs[0]
    assert (flight.departure.station, flight.departure.terminal, flight.arrival.city, flight.price_yuan) == ("北京机场", "T2", "上海", 1060)
    assert flight.booking_url.startswith("https://router.feizhu.com/") and flight.carrier == "东航"
    # 同一个回答里只出一张
    msgs += [AIMessage(content="", tool_calls=[call]), out]
    again = aio.run(_through(None, _present(_plan(flight_ids[1]), cid="p2"), msgs))
    assert again.status == "error" and again.content == prompts.render("tools.present_itinerary.already_presented")
    assert history.step_of(_present(_plan(flight_ids[1]), cid="p2"), again).message == "行程方案没排成"
    # 下一轮“按刚才查的再排一个”：认这段对话里查到的
    msgs += [AIMessage(content="排好了"), HumanMessage("换成都坐飞机的")]
    assert aio.run(_through(None, _present(_plan(flight_ids[1]), cid="p3"), msgs)).status == "success"


def test_present_itinerary_rejects_ids_not_returned_and_bad_order():
    msgs = _searched((FLIGHT_TOOL, _call(FLIGHT_TOOL, "f1")), (TRAIN_TOOL, _call(TRAIN_TOOL, "t1", origin="上海", destination="北京", departure_date=BACK)))
    flight_ids, train_ids = _ids(msgs[2]), _ids(msgs[4])

    def present(*ids):
        return aio.run(_through(None, _present(_plan(*ids)), msgs))

    made_up = present(flight_ids[0], "opt_made_up")
    assert made_up.status == "error" and '"opt_made_up"' in made_up.content and "not returned" in made_up.content
    backwards = present(train_ids[0], flight_ids[0])
    assert backwards.status == "error" and "departs before the previous one arrives" in backwards.content
    # 同一天两趟航班：第二趟在第一趟到之前起飞
    assert present(flight_ids[0], flight_ids[1]).status == "error"
    assert "more than once" in present(flight_ids[0], flight_ids[0]).content
    assert "No flight or train search" in aio.run(_through(None, _present(_plan(flight_ids[0])), [HumanMessage("排一下")])).content
    # 参数不对（没给方案）：说明哪里不对，不出卡片
    bad = aio.run(_through(None, {"name": ITINERARY_TOOL, "args": {"title": "x", "plans": []}, "id": "p9", "type": "tool_call"}, msgs))
    assert bad.status == "error" and "plans" in bad.content
    assert itinerary_tool().name == ITINERARY_TOOL and "at most 3" in itinerary_tool().description


# ---- 过程记录、卡片 ----


def test_travel_step_turns_the_result_into_a_card():
    tool = _connected(TRAIN_TOOL).tool
    call = _call(TRAIN_TOOL, seat_class="first_class", departure_after_hour=8, arrival_before_hour=14, max_price_yuan=900)
    pending = history.step_of(call, None)
    assert (pending.kind, pending.text, pending.limits) == ("train", "北京 → 上海", ["11 月 20 日", "只要高铁动车", "一等座", "8 点后出发",
                                                                                    "14 点前到", "900 元以内"])
    card = history.step_of(call, _ask(tool, call)).travel
    assert (card.kind, card.origin, card.destination, card.date, card.observed_at) == ("train", "北京", "上海", DAY, "2026-11-04T10:00:00+08:00")
    g1 = card.services[0]
    assert (g1.number, g1.carrier, g1.departure.station, g1.arrival.time, g1.duration_minutes, g1.travel_class, g1.price_yuan, g1.day_offset) == (
        "G1", "高铁", "北京南站", f"{DAY}T11:29", 269, "二等座", 662, 0)
    none = _call(TRAIN_TOOL, destination="无班次")
    assert history.step_of(none, _ask(tool, none)).message == "没查到符合条件的直达班次"
    assert history.step_of(call, failed(TRAIN_TOOL, "c1", "quota_exhausted")).message == "航班火车今天的额度用完了"
    broken = ToolMessage(content="{}", artifact={"structured_content": {"ok": True, "services": [{"number": "G1"}]}}, name=TRAIN_TOOL, tool_call_id="c1")
    assert history.step_of(call, broken).message == "查到的结果格式不对"
    assert history.step_of(_call(FLIGHT_TOOL, departure_after_hour=8, departure_before_hour=12), None).limits == ["11 月 20 日", "8 点到 12 点出发"]


# ---- 整轮 ----


def test_flight_turn_shows_a_flight_step_with_the_card():
    chat = Chat()
    v = chat.say("明天北京到上海的航班")
    steps = [i for i in v["turns"][0]["items"] if i["type"] == "step"]
    assert [s["kind"] for s in steps] == ["flight"]
    assert [s["number"] for s in steps[0]["travel"]["services"]] == ["MU5101", "CA1501"]
    live = [e["data"]["step"] for e in chat.events if e["event"] == "step" and e["data"]["step"]["kind"] == "flight"]
    assert [s["pending"] for s in live] == [True, False] and live[1]["travel"] == steps[0]["travel"]  # 页面实时就能画卡片
    assert v["turns"][0]["record"]["usage"]["travel"] == 1


def test_itinerary_turn_shows_both_searches_and_the_itinerary_card():
    chat = Chat()
    v = chat.say("帮我排个北京上海往返的行程")
    steps = [i for i in v["turns"][0]["items"] if i["type"] == "step"]
    assert [s["kind"] for s in steps] == ["flight", "train", "itinerary"] and all(s["ok"] for s in steps)
    plan = steps[2]["itinerary"]["plans"][0]
    assert [leg["number"] for leg in plan["legs"]] == ["MU5101", "G1"] and steps[2]["text"] == "北京上海往返"


def test_transfer_turn_routes_from_the_returned_station_with_its_city():
    chat = Chat()
    v = chat.say("明天北京到上海的高铁，到了以后接站去外滩")
    steps = [i for i in v["turns"][0]["items"] if i["type"] == "step"]
    assert [s["kind"] for s in steps] == ["train", "route"] and all(s["ok"] for s in steps)
    assert steps[1]["text"] == "上海南站 → 外滩"
