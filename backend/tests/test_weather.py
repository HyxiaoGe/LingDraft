"""查天气：agent-tools 的 MCP 工具 get_weather（app.tools.weather_mcp）。取工具、经网关调、中间件整理结果、过程记录换成卡片、
整轮对话、起服务时连不上。服务用进程内的假 weather-service（research_llm.fake_weather_server）；经 HTTP 的用 ASGI 模拟网关。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx2
import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app import aio
from app.api import research_runner
from app.graph.research import history, prompts
from app.research_llm import FakeResearchPlanner, fake_weather_server
from app.tools.gateway import GatewayAuthError
from app.tools.weather_mcp import WEATHER_TOOL, WeatherMiddleware, WeatherTool, failed, gateway_target
from tests.test_research import Chat


def _ahead(days: int) -> str:
    return (datetime.now(timezone(timedelta(hours=8))) + timedelta(days=days)).date().isoformat()


def _tomorrow() -> str:
    return _ahead(1)


def _call(location: str, requested_date: str | None = None, cid: str = "c1") -> dict:
    args = {"location": location} | ({"requested_date": requested_date} if requested_date else {})
    return {"name": WEATHER_TOOL, "args": args, "id": cid, "type": "tool_call"}


def _connected(target=fake_weather_server) -> WeatherTool:
    w = WeatherTool(target)
    aio.run(w.connect())
    return w


async def _through_middleware(tool, call: dict):
    return await WeatherMiddleware().awrap_tool_call(SimpleNamespace(tool_call=call), lambda req: tool.ainvoke(call))


def _ask(tool, location: str, requested_date: str | None = None) -> ToolMessage:
    return aio.run(_through_middleware(tool, _call(location, requested_date)))


# ---- 取工具 ----


def test_connect_takes_the_service_tool_and_adds_lingdraft_note():
    tool = _connected().tool
    assert tool.name == WEATHER_TOOL
    # 说明是服务给的，后面接上灵稿自己的一段
    assert tool.description.startswith("Get the weather forecast") and tool.description.endswith(prompts.render("tools.get_weather.lingdraft"))
    assert set(tool.args_schema["properties"]) == {"location", "requested_date"} and tool.args_schema["required"] == ["location"]

    from fastmcp import FastMCP
    with pytest.raises(RuntimeError, match="没有 get_weather"):
        aio.run(WeatherTool(lambda: FastMCP("empty")).connect())


def test_planner_has_weather_only_after_connecting():
    p = FakeResearchPlanner()
    assert WEATHER_TOOL not in [t.name for t in p.tools()]
    aio.run(p.connect_tools())
    assert [t.name for t in p.tools()][-1] == WEATHER_TOOL


# ---- 经网关调（HTTP） ----


class _Gateway:
    """模拟网关：转给假 weather-service 的 MCP，记下请求头；status 设了就直接回这个状态（429 到了每日上限、401 密钥不对……）。"""

    def __init__(self) -> None:
        self.mcp = fake_weather_server().http_app(path="/weather/mcp", stateless_http=True)
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
            w = WeatherTool(lambda: gateway_target("http://gw/", "k1", transport=httpx2.ASGITransport(app=gw)))
            await w.connect()
            ok = await _through_middleware(w.tool, _call("广州"))
            gw.status = 429
            limited = await _through_middleware(w.tool, _call("广州"))
            gw.status = 401
            try:
                await _through_middleware(w.tool, _call("广州"))
            except GatewayAuthError as e:
                return ok, limited, e
            return ok, limited, None

    ok, limited, auth = aio.run(go())
    assert ok.status == "success" and json.loads(ok.content)["resolved_location"] == "广东省·广州"
    assert gw.headers[0]["authorization"] == "Bearer k1" and gw.headers[0]["x-caller-scene"] == "research"
    # 网关、服务出问题：算没查到，告诉模型没有可用结果，这一轮照常
    assert limited.status == "error" and limited.content == prompts.render("tools.get_weather.unavailable")
    assert limited.artifact["structured_content"]["error"]["code"] == "service_unavailable"
    # 密钥不对、网关没放行：让这次运行失败，把配置问题亮出来
    assert auth is not None and auth.kind == "http_401"


# ---- 中间件 ----


def test_middleware_gives_the_model_text_and_marks_failures():
    tool = _connected().tool
    ok = _ask(tool, "广州")
    seen = json.loads(ok.content)
    assert ok.status == "success" and isinstance(ok.content, str)
    # 模型看不到逐小时明细（只有摘要）；整份在 artifact 里给卡片
    assert "hourly" not in seen and seen["hourly_summary"]["precipitation_hours"] == 3
    assert len(ok.artifact["structured_content"]["hourly"]) == 24
    amb = _ask(tool, "朝阳")
    assert amb.status == "error" and "ambiguous_location" in amb.content
    assert amb.artifact["structured_content"]["error"]["candidates"] == ["北京市·北京·朝阳", "辽宁省·朝阳"]

    mw, call = WeatherMiddleware(), _call("广州")

    async def raising(e):
        async def handler(req):
            raise e
        return await mw.awrap_tool_call(SimpleNamespace(tool_call=call), handler)

    down = aio.run(raising(RuntimeError("Client failed to connect")))
    assert down.status == "error" and down.tool_call_id == "c1" and down.name == WEATHER_TOOL
    wrapped = RuntimeError("Client failed to connect")
    wrapped.__cause__ = ExceptionGroup("tg", [GatewayAuthError("查天气失败：HTTP 403", kind="http_403")])
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


def test_step_turns_the_result_into_a_card():
    tool = _connected().tool
    call = _call("广州")
    assert history.step_of(call, None).model_dump(include={"kind", "text", "pending"}) == {"kind": "weather", "text": "广州", "pending": True}
    done = history.step_of(call, _ask(tool, "广州"))
    fc = done.weather
    assert done.ok and done.title == "广东省·广州" and len(fc.days) == 4 and len(fc.hourly) == 24 and fc.requested_date is None
    assert (fc.days[0].day, fc.days[0].night, fc.days[0].high, fc.days[0].wind) == ("晴", "晴", 30, "北风 1-3级")
    assert fc.fetched_at.endswith("+08:00") and fc.hourly[0].pop is not None

    day = history.step_of(_call("广州", _tomorrow()), _ask(tool, "广州", _tomorrow())).weather
    assert day.requested_date == _tomorrow() and [d.date for d in day.days] == [_tomorrow()]
    assert len(day.hourly) == 24 and all(h.time.startswith(_tomorrow()) for h in day.hourly)  # 逐小时是那天一整天的
    far = history.step_of(_call("广州", _ahead(14)), _ask(tool, "广州", _ahead(14))).weather
    assert [d.date for d in far.days] == [_ahead(14)] and far.hourly == []  # 7 天以后只有逐日

    # 不在预报范围里：服务一天都不给，没有卡片，过程里说能查哪几天
    out = history.step_of(_call("广州", _ahead(20)), _ask(tool, "广州", _ahead(20)))
    md = lambda iso: f"{int(iso[5:7])}月{int(iso[8:10])}日"  # noqa: E731
    assert not out.ok and out.weather is None and out.title == "广东省·广州"
    assert out.message == f"{md(_ahead(20))}不在预报范围里，能查 {md(_ahead(0))}–{md(_ahead(14))}"

    def message(result: ToolMessage) -> str:
        step = history.step_of(call, result)
        assert not step.ok and step.weather is None
        return step.message

    assert message(_ask(tool, "朝阳")) == "对得上好几个地方：北京市·北京·朝阳、辽宁省·朝阳"
    assert message(_ask(tool, "火星")) == "和风天气里没找到这个地方"
    assert message(failed("c1")) == "天气服务暂时查不到"
    broken = ToolMessage(content="{}", artifact={"structured_content": {"ok": True, "resolved_location": "x"}}, name=WEATHER_TOOL,
                         tool_call_id="c1")
    assert message(broken) == "天气结果的格式不对"
    msgs = [AIMessage(content="", tool_calls=[call]), _ask(tool, "广州")]
    assert history.usage_of(msgs).weather == 1


def test_old_weather_results_still_show():
    """11-04 以前灵稿自己查和风，存下的结果是另一个样子：旧对话里的卡片照常显示。"""
    call = _call("广州")
    fc = {"location": "广东省·广州", "today": "2026-10-31", "fetched_at": "2026-10-31T07:00:00+00:00",
          "days": [{"date": "2026-10-31", "day": "晴", "night": "多云", "high": 30, "low": 20, "wind": "北风 1-3级"}],
          "hourly": [{"time": "2026-10-31T15:00+08:00", "temp": 28, "text": "晴", "pop": 0, "precip": 0.0}]}
    old = ToolMessage(content=json.dumps({"ok": True, "location": "广东省·广州"}), artifact={"retrieved_at": "x", "forecast": fc},
                      name=WEATHER_TOOL, tool_call_id="c1")
    step = history.step_of(call, old)
    assert step.ok and step.weather.location == "广东省·广州" and step.weather.requested_date is None
    gone = ToolMessage(content=json.dumps({"ok": False, "kind": "ambiguous_location", "message": "“朝阳”对得上好几个地方，没有查"}),
                       name=WEATHER_TOOL, tool_call_id="c1", status="error")
    assert history.step_of(call, gone).message == "“朝阳”对得上好几个地方，没有查"


# ---- 整轮、起服务 ----


def test_weather_turn_shows_a_weather_step_with_the_forecast():
    chat = Chat()
    v = chat.say("广州天气怎么样，能骑车吗")
    steps = [i for i in v["turns"][0]["items"] if i["type"] == "step"]
    assert [s["kind"] for s in steps] == ["weather", "search", "read"]
    assert steps[0]["title"] == "广东省·广州" and len(steps[0]["weather"]["hourly"]) == 24
    live = [e["data"]["step"] for e in chat.events if e["event"] == "step" and e["data"]["step"]["kind"] == "weather"]
    assert [s["pending"] for s in live] == [True, False] and live[1]["weather"] == steps[0]["weather"]  # 页面实时就能画卡片
    assert v["turns"][0]["record"]["usage"]["weather"] == 1


def test_runner_starts_without_weather_and_adds_it_once_connected(monkeypatch):
    monkeypatch.setattr(research_runner, "TOOLS_STARTUP_S", 0.2)
    monkeypatch.setattr(research_runner, "TOOLS_STARTUP_RETRY_S", 0.05)
    monkeypatch.setattr(research_runner, "TOOLS_RETRY_S", 0.05)

    class Down(FakeResearchPlanner):
        failures = 6  # 起服务时那几次、后台头一两次都连不上

        async def connect_tools(self) -> None:
            if self.failures:
                self.failures -= 1
                raise RuntimeError("Client failed to connect")
            await super().connect_tools()

    chat = Chat(planner=Down())
    first = chat.r.graph
    assert chat.r._tools_retry is not None and WEATHER_TOOL not in [t.name for t in chat.planner.tools()]
    chat.r._tools_retry.result(timeout=10)
    assert chat.r.graph is not first  # 换上了带天气的 agent
    v = chat.say("广州天气怎么样")
    assert [i["kind"] for i in v["turns"][0]["items"] if i["type"] == "step"][0] == "weather"
