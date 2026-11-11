"""查地点、比路线：agent-tools 的 MCP 工具 place_search、route_compare（app.tools.maps_mcp）。取工具、占位、经网关调、
中间件整理结果、过程记录换成地点卡和路线卡、整轮对话。服务用进程内的假 maps-service（research_llm.fake_maps_server）；
经 HTTP 的用 ASGI 模拟网关。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx2
import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app import aio
from app.graph.research import history, prompts
from app.research_llm import FakeResearchPlanner, fake_maps_server
from app.tools.gateway import GatewayAuthError
from app.tools.maps_mcp import PLACE_TOOL, ROUTE_TOOL, MapsMiddleware, MapsTool, failed, gateway_target, placeholder
from tests.test_research import Chat


def _place(cid: str = "c1", **args) -> dict:
    return {"name": PLACE_TOOL, "args": {"query": "咖啡", "anchor_source": "named", "near": "外滩", "city": "上海"} | args, "id": cid,
            "type": "tool_call"}


def _route(cid: str = "c1", **args) -> dict:
    base = {"origin": "虹桥火车站", "destination": "外滩", "origin_source": "named", "destination_source": "named", "modes": ["driving", "transit"]}
    return {"name": ROUTE_TOOL, "args": base | args, "id": cid, "type": "tool_call"}


def _connected(name: str, target=fake_maps_server) -> MapsTool:
    t = MapsTool(name, target)
    aio.run(t.connect())
    return t


async def _through_middleware(tool, call: dict):
    return await MapsMiddleware().awrap_tool_call(SimpleNamespace(tool_call=call), lambda req: tool.ainvoke(call))


def _ask(tool, call: dict) -> ToolMessage:
    return aio.run(_through_middleware(tool, call))


# ---- 取工具、占位 ----


def test_connect_takes_the_service_tools_and_adds_lingdraft_note():
    for name, words in ((PLACE_TOOL, "Search for places"), (ROUTE_TOOL, "Compare transport modes")):
        t = MapsTool(name, fake_maps_server)
        assert t.current().name == name and t.current().description.startswith(prompts.render(f"tools.{name}.brief"))  # 还没连：占位
        aio.run(t.connect())
        assert t.current() is t.tool
        assert t.tool.description.startswith(words) and t.tool.description.endswith(prompts.render(f"tools.{name}.lingdraft"))
    # 当前位置从用户的浏览器来、可能不给（见 app.tools.location）：说明里说清，没给的这一轮别再选
    for name in (PLACE_TOOL, ROUTE_TOOL):
        note = prompts.render(f"tools.{name}.lingdraft")
        assert "from the user's browser" in note and "do not use current_location again in this turn" in note


def test_planner_has_map_tools_before_and_after_connecting():
    p = FakeResearchPlanner()
    names = [t.name for t in p.tools()]
    assert names.index(PLACE_TOOL) == names.index("generate_image") + 1 and names.index(ROUTE_TOOL) == names.index(PLACE_TOOL) + 1
    aio.run(p.connect_tools())
    after = [t.name for t in p.tools()]
    assert after[: len(names)] == names  # 连上以后换成真的，位置不变（前缀缓存）


def test_placeholder_says_the_map_service_is_offline():
    call = _place()
    out = aio.run(_through_middleware(placeholder(PLACE_TOOL), call))
    assert out.status == "error" and out.content == prompts.render("tools.place_search.unavailable")
    assert out.artifact["structured_content"]["error"] == {"code": "unavailable", "retryable": True}
    assert history.step_of(call, out).message == "地图服务暂时查不到"


# ---- 经网关调（HTTP） ----


class _Gateway:
    """模拟网关：转给假 maps-service 的 MCP，记下请求头；status 设了就直接回这个状态（429 到了每日上限、401 密钥不对……）。"""

    def __init__(self) -> None:
        self.mcp = fake_maps_server().http_app(path="/maps/mcp", stateless_http=True)
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
            t = MapsTool(ROUTE_TOOL, lambda: gateway_target("http://gw/", "k1", transport=httpx2.ASGITransport(app=gw)))
            await t.connect()
            ok = await _through_middleware(t.tool, _route())
            gw.status = 429
            limited = await _through_middleware(t.tool, _route())
            gw.status = 403
            try:
                await _through_middleware(t.tool, _route())
            except GatewayAuthError as e:
                return ok, limited, e
            return ok, limited, None

    ok, limited, auth = aio.run(go())
    assert ok.status == "success" and json.loads(ok.content)["routes"][0]["mode"] == "driving"
    assert gw.headers[0]["authorization"] == "Bearer k1" and gw.headers[0]["x-caller-scene"] == "research"
    # 网关今天的上限到了：和服务额度用完一样，告诉模型今天别再调地图
    assert limited.status == "error" and limited.content == prompts.render("tools.route_compare.quota_exhausted")
    assert limited.artifact["structured_content"]["error"]["code"] == "quota_exhausted"
    # 密钥不对、网关没放行：让这次运行失败，把配置问题亮出来
    assert auth is not None and auth.kind == "http_403"


# ---- 中间件 ----


def test_middleware_gives_the_model_the_service_text_and_keeps_the_card_data():
    tool = _connected(PLACE_TOOL).tool
    ok = _ask(tool, _place())
    seen = json.loads(ok.content)
    assert ok.status == "success" and isinstance(ok.content, str)
    # 模型看的是服务那段：没有链接、图片、服务商；整份在 artifact 里给卡片
    assert "map_url" not in seen["places"][0] and "provider" not in seen["places"][0]
    assert ok.artifact["structured_content"]["places"][0]["map_url"].startswith("https://uri.amap.com/")
    assert ok.artifact["retrieved_at"].endswith("+00:00")
    amb = _ask(tool, _place(near="鼓楼"))
    assert amb.status == "error" and "ambiguous_location" in amb.content
    assert amb.artifact["structured_content"]["error"]["candidate_argument"] == "near_candidate"
    # 参数没过服务的校验（MCP 的 isError）：服务的说明照给模型，记成 tool_error
    bad = _ask(tool, _place(anchor_source="somewhere"))
    assert bad.status == "error" and bad.artifact["structured_content"]["error"]["code"] == "tool_error"


def test_middleware_turns_connection_failures_into_unavailable():
    mw, call = MapsMiddleware(), _route()

    async def raising(e):
        async def handler(req):
            raise e
        return await mw.awrap_tool_call(SimpleNamespace(tool_call=call), handler)

    down = aio.run(raising(RuntimeError("Client failed to connect")))
    assert down.status == "error" and down.tool_call_id == "c1" and down.name == ROUTE_TOOL
    assert down.content == prompts.render("tools.route_compare.unavailable")
    wrapped = RuntimeError("Client failed to connect")
    wrapped.__cause__ = ExceptionGroup("tg", [GatewayAuthError("调 /maps/mcp 失败：HTTP 401", kind="http_401")])
    with pytest.raises(GatewayAuthError):
        aio.run(raising(wrapped))

    async def cancelled() -> bool:
        try:
            await raising(asyncio.CancelledError())
        except asyncio.CancelledError:
            return True
        return False

    assert aio.run(cancelled())  # 点了停止：照常取消，不算没查到

    other = {"name": "search_web", "args": {}, "id": "c2", "type": "tool_call"}
    sentinel = ToolMessage(content=[{"type": "text", "text": "x"}], tool_call_id="c2", name="search_web")
    out = aio.run(mw.awrap_tool_call(SimpleNamespace(tool_call=other), lambda req: asyncio.sleep(0, sentinel)))
    assert out is sentinel  # 别的工具不管


# ---- 过程记录、卡片 ----


def test_place_step_turns_the_result_into_a_place_card():
    tool = _connected(PLACE_TOOL).tool
    call = _place()
    pending = history.step_of(call, None)
    assert pending.model_dump(include={"kind", "text", "limits", "pending"}) == {"kind": "place", "text": "咖啡", "limits": ["外滩附近"], "pending": True}
    done = history.step_of(call, _ask(tool, call))
    card = done.places
    assert done.ok and card.query == "咖啡" and card.near == "外滩" and card.city == "上海" and len(card.places) == 2
    first = card.places[0]
    assert (first.name, first.district, first.distance_m, first.rating, first.cost, first.provider) == ("咖啡1号店", "黄浦区", 100, 4.4, 35, "amap")
    assert first.map_url.startswith("https://uri.amap.com/") and card.fetched_at and card.limitations == []
    # 在城市里按文字搜：标城市，没有距离
    city = history.step_of(_place(anchor_source="none", near=None), _ask(tool, _place(anchor_source="none", near=None)))
    assert city.limits == ["上海"] and city.places.near == "" and city.places.places[0].distance_m is None

    amb = history.step_of(call, _ask(tool, _place(near="鼓楼")))
    assert not amb.ok and amb.places is None and amb.message == "对得上好几个地方：北京市东城区、南京市鼓楼区"
    here = history.step_of(_place(anchor_source="current_location", near=None), _ask(tool, _place(anchor_source="current_location", near=None)))
    assert here.limits == ["当前位置附近"] and here.message == "拿不到当前位置"
    assert history.step_of(call, failed(PLACE_TOOL, "c1", "quota_exhausted")).message == "地图今天的额度用完了"
    broken = ToolMessage(content="{}", artifact={"structured_content": {"ok": True, "query": "x"}}, name=PLACE_TOOL, tool_call_id="c1")
    assert history.step_of(call, broken).message == "地图结果的格式不对"


def test_route_step_turns_the_result_into_a_route_card():
    tool = _connected(ROUTE_TOOL).tool
    call = _route(modes=["driving", "transit", "bicycling"], requested_departure_time="明早八点")
    pending = history.step_of(call, None)
    assert (pending.kind, pending.text, pending.limits) == ("route", "虹桥火车站 → 外滩", ["驾车", "公共交通", "骑行"])
    plan = history.step_of(call, _ask(tool, call)).routes
    assert (plan.origin, plan.destination, plan.departure_time, plan.unavailable_modes) == ("虹桥火车站", "外滩", "明早八点", ["bicycling"])
    driving, transit = plan.routes
    assert (driving.mode, driving.distance_m, driving.duration_s, driving.toll_yuan) == ("driving", 23007, 3540, 0)
    assert (transit.fare_yuan, transit.transfers, transit.walking_distance_m, transit.distance_m) == (5, 0, 1061, None)
    subway = transit.legs[1]
    assert (subway.kind, subway.line_name, subway.departure_stop, subway.arrival_stop, subway.via_stop_count) == (
        "subway", "地铁10号线", "虹桥火车站", "豫园", 13)
    assert (subway.entrance, subway.exit) == ("北2入口", "6口")

    amb = history.step_of(_route(origin="鼓楼"), _ask(tool, _route(origin="鼓楼")))
    assert not amb.ok and amb.routes is None and amb.message.startswith("对得上好几个地方：")
    here = _route(origin="current_location", origin_source="current_location")
    assert history.step_of(here, None).text == "当前位置 → 外滩"


def test_usage_counts_map_lookups():
    tool = _connected(PLACE_TOOL).tool
    msgs = [AIMessage(content="", tool_calls=[_place()]), _ask(tool, _place())]
    assert history.usage_of(msgs).maps == 1


# ---- 整轮 ----


def test_place_turn_shows_a_place_step_with_the_card():
    chat = Chat()
    v = chat.say("外滩附近有什么咖啡馆")
    steps = [i for i in v["turns"][0]["items"] if i["type"] == "step"]
    assert [s["kind"] for s in steps] == ["place"]
    assert steps[0]["places"]["near"] == "外滩" and len(steps[0]["places"]["places"]) == 2
    live = [e["data"]["step"] for e in chat.events if e["event"] == "step" and e["data"]["step"]["kind"] == "place"]
    assert [s["pending"] for s in live] == [True, False] and live[1]["places"] == steps[0]["places"]  # 页面实时就能画卡片
    assert v["turns"][0]["record"]["usage"]["maps"] == 1


def test_route_turn_shows_a_route_step_with_the_card():
    chat = Chat()
    v = chat.say("从虹桥火车站到外滩的路线")
    steps = [i for i in v["turns"][0]["items"] if i["type"] == "step"]
    assert [s["kind"] for s in steps] == ["route"]
    assert [r["mode"] for r in steps[0]["routes"]["routes"]] == ["driving", "transit"]
