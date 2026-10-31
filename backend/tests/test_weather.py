"""查天气：挑城市、网关的天气接口、整理预报和缓存、工具结果、过程记录和整轮对话。网络用 MockTransport 模拟。"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app.graph.research import history
from app.research_llm import FakeResearchPlanner
from app.tools.gateway import City, Day, GatewayAuthError, GatewayClient, GatewayError, Hour
from app.tools.research_tools import make_research_tools
from app.tools.weather import CACHE_SECONDS, Unresolved, WeatherService, city_label, select_city
from tests.test_research import Chat


def city(name: str, adm1: str = "", adm2: str = "", country: str = "中国", rank: int | None = 20, lid: str = "") -> City:
    return City(location_id=lid or f"{adm1}{adm2}{name}", name=name, adm1=adm1, adm2=adm2, country=country, rank=rank)


GZ = city("广州", "广东省", "广州", rank=11)
PANYU = city("番禺", "广东省", "广州")
XIHU_HZ = city("西湖", "浙江省", "杭州")
XIHU_NC = city("西湖", "江西省", "南昌")


def test_city_label_skips_repeats_and_home_country():
    assert city_label(GZ) == "广东省·广州"
    assert city_label(XIHU_HZ) == "浙江省·杭州·西湖"
    assert city_label(city("Paris", "Île-de-France", "Paris", country="France")) == "France·Île-de-France·Paris"


def test_select_city_only_takes_names_in_the_query():
    assert select_city("广州", [GZ, PANYU]) is GZ
    assert select_city("广州番禺", [GZ, PANYU]) is PANYU  # 写在后面的更具体
    with pytest.raises(Unresolved) as e:
        select_city("湖南", [city("长沙", "湖南省", "长沙")])  # 模糊查询给的城市名字不在地名里：不认
    assert e.value.kind == "location_not_found"


def test_select_city_uses_province_hints_then_asks():
    assert select_city("杭州西湖", [XIHU_HZ, XIHU_NC]) is XIHU_HZ
    assert select_city("江西省·南昌·西湖", [XIHU_HZ, XIHU_NC]) is XIHU_NC  # 模型照候选的写法再查
    with pytest.raises(Unresolved) as e:
        select_city("西湖", [XIHU_HZ, XIHU_NC])
    assert e.value.kind == "ambiguous_location" and e.value.candidates == ["浙江省·杭州·西湖", "江西省·南昌·西湖"]


def test_select_city_drops_minor_foreign_places_and_merges_same_label():
    minor = city("广州", "X", "Y", country="Nowhere", rank=85)
    assert select_city("广州", [minor, GZ]) is GZ
    twin = city("广州", "广东省", "广州", lid="other")
    assert select_city("广州", [GZ, twin]) is GZ  # 写法一模一样的重复条目：取第一个


# ---- 网关的天气接口 ----


def _client(handler) -> GatewayClient:
    return GatewayClient("http://gw", "k1", scene="research", transport=httpx.MockTransport(handler))


def test_weather_client_paths_and_parse():
    seen = []

    def handler(req: httpx.Request):
        seen.append((req.url.path, dict(req.url.params)))
        if req.url.path.endswith("/cities"):
            return httpx.Response(200, json={"cities": [{"location_id": "1", "name": "广州", "adm1": "广东省", "adm2": "广州", "country": "中国", "rank": 11}]})
        return httpx.Response(200, json={"daily": [{"date": "2026-10-31", "text_day": "晴", "text_night": "晴", "temp_max": "32", "temp_min": "22"}]})

    c = _client(handler)
    assert c.weather_cities("广州")[0].name == "广州"
    assert c.weather_daily("1")[0].temp_max == "32"
    assert seen == [("/weather/v1/cities", {"location": "广州", "number": "10", "lang": "zh"}), ("/weather/v1/forecast/daily", {"location_id": "1"})]


@pytest.mark.parametrize("status,body,kind,retryable", [
    (504, {"error": {"code": "upstream_timeout"}}, "upstream_timeout", True),
    (502, {"error": {"code": "upstream_auth_failed"}}, "upstream_auth_failed", False),  # 和风的凭证问题，不是我们的密钥
    (503, {"error": {"code": "weather_not_configured"}}, "weather_not_configured", False),
    (502, {}, "http_502", True),
    (200, {"cities": "x"}, "bad_response", False),
])
def test_weather_client_failures_are_typed(status, body, kind, retryable):
    with pytest.raises(GatewayError) as e:
        _client(lambda req: httpx.Response(status, json=body)).weather_cities("广州")
    assert not isinstance(e.value, GatewayAuthError) and (e.value.kind, e.value.retryable) == (kind, retryable)


def test_weather_not_allowed_is_an_auth_error():
    with pytest.raises(GatewayAuthError):
        _client(lambda req: httpx.Response(403, json={"error": "service_not_allowed"})).weather_cities("广州")


# ---- 整理预报、缓存 ----


class FakeWeather:
    """假网关客户端：记下调用；hourly 可以设成抛错。"""

    def __init__(self, cities=(GZ,), hourly_error: Exception | None = None) -> None:
        self.cities, self.hourly_error, self.calls = list(cities), hourly_error, []

    def weather_cities(self, location, lang="zh", number=10):
        self.calls.append(("cities", location, lang))
        return self.cities

    def weather_daily(self, location_id):
        self.calls.append(("daily", location_id))
        dates = ["2026-10-30", "2026-10-31", "2026-10-31", "坏日期", "2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"]
        return [Day(date=d, text_day="晴", text_night="多云", temp_max="?" if d == "2026-11-02" else "30", temp_min="20",
                    wind_dir_day="北风", wind_scale_day="1-3") for d in dates]

    def weather_hourly(self, location_id):
        self.calls.append(("hourly", location_id))
        if self.hourly_error:
            raise self.hourly_error
        return [Hour(time="2026-10-31T23:00+08:00", temp="21", text="晴", pop="0", precip="0.0"),
                Hour(time="2026-11-01T00:00+08:00", temp="20", text="小雨", pop="70", precip="1.2"),
                Hour(time="2026-10-31T23:00+08:00", temp="21", text="晴"),  # 时间往回走了：丢掉
                Hour(time="2026-11-01T01:00", temp="20", text="晴")]  # 没带时区：丢掉


def test_forecast_from_local_today_and_cached_for_half_an_hour():
    gw, t = FakeWeather(), [0.0]
    svc = WeatherService(gw, clock=lambda: t[0], now=lambda: datetime(2026, 10, 31, 15, tzinfo=UTC))
    fc = svc.forecast("广州")
    # 当地今天从逐小时的第一个小时来；昨天、重复、日期坏的、数字坏的都不要；最多 4 天
    assert fc.today == "2026-10-31" and [d.date for d in fc.days] == ["2026-10-31", "2026-11-01", "2026-11-03", "2026-11-04"]
    assert fc.location == "广东省·广州" and fc.days[0].wind == "北风 1-3级"
    assert [(h.time, h.pop) for h in fc.hourly] == [("2026-10-31T23:00+08:00", 0), ("2026-11-01T00:00+08:00", 70)]
    svc.forecast("广州")
    t[0] += CACHE_SECONDS + 1
    svc.forecast("广州")
    assert [c[0] for c in gw.calls] == ["cities", "daily", "hourly", "cities", "cities", "daily", "hourly"]


def test_hourly_failure_keeps_daily_and_uses_china_date():
    gw = FakeWeather(hourly_error=GatewayError("down", kind="upstream_unavailable"))
    fc = WeatherService(gw, now=lambda: datetime(2026, 10, 31, 17, tzinfo=UTC)).forecast("广州")  # 北京时间 11 月 1 日 1 点
    assert fc.hourly == [] and fc.today == "2026-11-01"


def test_english_names_are_looked_up_in_english():
    gw = FakeWeather(cities=[city("Paris", "Île-de-France", "Paris", country="France", rank=10)])
    WeatherService(gw).forecast("France·Île-de-France·Paris")
    assert gw.calls[0] == ("cities", "Paris", "en")


# ---- 工具 ----


def _weather_tool(client):
    return {t.name: t for t in make_research_tools(client)}["get_weather"]


def _call(tool, location: str) -> ToolMessage:
    return tool.invoke({"name": "get_weather", "args": {"location": location}, "id": "c1", "type": "tool_call"})


def test_tool_returns_forecast_or_tells_the_agent_why_not():
    tool = _weather_tool(FakeResearchPlanner()._gateway)
    ok = _call(tool, "广州")
    d = json.loads(ok.content)
    assert ok.status == "success" and d["ok"] and d["location"] == "广东省·广州" and len(d["days"]) == 4 and len(d["hourly"]) == 24
    amb = _call(tool, "朝阳")
    d = json.loads(amb.content)
    assert amb.status == "error" and d["kind"] == "ambiguous_location" and d["candidates"] == ["北京市·北京·朝阳", "辽宁省·朝阳"]
    assert json.loads(_call(tool, "火星").content)["kind"] == "location_not_found"
    assert json.loads(_call(tool, "  ").content)["kind"] == "bad_arguments"


def test_tool_hands_service_errors_to_agent_but_auth_errors_fail_the_run():
    down = _weather_tool(_client(lambda req: httpx.Response(504, json={"error": {"code": "upstream_timeout"}})))
    d = json.loads(_call(down, "广州").content)
    assert (d["kind"], d["retryable"]) == ("upstream_timeout", True)
    with pytest.raises(GatewayAuthError):
        _call(_weather_tool(_client(lambda req: httpx.Response(403, json={"error": "service_not_allowed"}))), "广州")


# ---- 过程记录、整轮 ----


def test_step_carries_the_forecast_or_the_reason():
    call = {"name": "get_weather", "args": {"location": "广州"}, "id": "c1"}
    assert history.step_of(call, None).model_dump(include={"kind", "text", "pending"}) == {"kind": "weather", "text": "广州", "pending": True}
    tool = _weather_tool(FakeResearchPlanner()._gateway)
    done = history.step_of(call, _call(tool, "广州"))
    assert done.ok and done.title == "广东省·广州" and len(done.weather.days) == 4
    bad = history.step_of({**call, "args": {"location": "朝阳"}}, _call(tool, "朝阳"))
    assert not bad.ok and bad.weather is None and "好几个地方" in bad.message
    broken = ToolMessage(content=json.dumps({"ok": True, "location": "x"}), name="get_weather", tool_call_id="c1")
    assert history.step_of(call, broken).message == "天气结果的格式不对"
    msgs = [AIMessage(content="", tool_calls=[{**call, "type": "tool_call"}]), _call(tool, "广州")]
    assert history.usage_of(msgs).weather == 1


def test_weather_turn_shows_a_weather_step_with_the_forecast():
    chat = Chat()
    v = chat.say("广州天气怎么样，能骑车吗")
    steps = [i for i in v["turns"][0]["items"] if i["type"] == "step"]
    assert [s["kind"] for s in steps] == ["weather", "search", "read"]
    assert steps[0]["title"] == "广东省·广州" and len(steps[0]["weather"]["hourly"]) == 24
    live = [e["data"]["step"] for e in chat.events if e["event"] == "step" and e["data"]["step"]["kind"] == "weather"]
    assert [s["pending"] for s in live] == [True, False] and live[1]["weather"] == steps[0]["weather"]  # 页面实时就能画卡片
    assert v["turns"][0]["record"]["usage"]["weather"] == 1
