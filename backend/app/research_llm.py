"""对话场景的模型接入层：ResearchPlanner 协议，LiveResearchPlanner（DeepSeek / 豆包，按配置）与 FakeResearchPlanner（离线）。

agent 本身由 app.graph.research.agent 构建，这里提供它要的聊天模型和查资料的工具，以及几个不带工具的快模型步骤：
起标题、推荐问题，内容审核拦下请求时把资料一条条单独送审，还有对话太长时给之前几轮写短版（见 graph.research.compaction）。
这几步和 agent 一样在对话的事件循环里异步地跑（见 app.aio）；整理记忆（dream）在后台线程里，是同步的。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
import re
import threading
import time
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langgraph.constants import TAG_NOSTREAM
from PIL import Image, ImageDraw
from pydantic import BaseModel, ConfigDict, Field

from app import aio, config
from app.graph.research import history, prompts
from app.graph.research.compaction import RECALL_TOOL as RECALL
from app.graph.research.sources import DOCS_TOOL, doc_snippets, search_hits
from app.model_guard import ContentBlocked, GuardedChatModel, Interrupted, ModelUnavailable, guard_key
from app.tools import docs_mcp, flyai_mcp, image_mcp, maps_mcp, web_mcp, weather_mcp
from app.tools.docs_mcp import LIBRARY_TOOL, docs_tools
from app.tools.flyai_mcp import FLIGHT_TOOL, ITINERARY_TOOL, TRAIN_TOOL, TravelTool
from app.tools.gateway import GatewayClient
from app.tools.image_mcp import ImageTool
from app.tools.maps_mcp import PLACE_TOOL, ROUTE_TOOL, MapsTool
from app.tools.research_tools import TIME_TOOL, make_research_tools
from app.tools.weather_mcp import WEATHER_TOOL, WeatherTool
from app.tools.web_mcp import READ_TOOL, SEARCH_TOOL, WebTool

PROBE_WORKERS = 4  # 单条送审同时发几个
TITLE_TIMEOUT = 8  # 照 Fusion：起标题的超时（秒）
TITLE_MAX_TOKENS = 64  # 起标题最多生成这么多：标题要的是 15 个字以内，模型不照做、去回答问题时到这里就截住
SUMMARY_MAX_TOKENS = 2000  # 写短版最多生成这么多（要的是 300 词、600 字以内）

# ---- 结构化输出 schema（扁平、全部 required，符合 strict 模式） ----


class Suggestions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    questions: list[str] = Field(description="Three follow-up questions the user may want to ask")


# ---- 协议 ----


class ResearchPlanner(Protocol):
    gateway: GatewayClient  # 按编号取图、删图（画的图存在 image-service 里，见 app.tools.gateway）

    def agent_model(self) -> BaseChatModel: ...

    def subagent_model(self) -> BaseChatModel: ...

    def tools(self) -> list: ...

    async def connect_tools(self) -> None: ...  # 连上还没连上的 MCP 工具（见 McpTools）；有没连上的照常抛出（见 research_runner）

    async def title(self, text: str) -> str: ...

    async def suggest(self, question: str, reply: str) -> list[str]: ...

    async def blocked(self, texts: Sequence[str]) -> list[bool]: ...

    async def summarize(self, prompt: str) -> str: ...

    def dream(self, prompt: str) -> str: ...  # 整理记忆：提示词进，json 文字出（见 graph.research.memory）。后台线程里调，同步的


# ---- 思考计时 ----

THINKING_MS = "thinking_ms"  # 思考用时记在消息的 response_metadata 里的键


class ThinkingClock:
    """一次模型调用思考了多久：从第一个思考片段到第一个正文或工具调用片段，还没等到就算到现在。"""

    def __init__(self) -> None:
        self._start: float | None = None
        self._end: float | None = None

    def see(self, msg: AIMessageChunk) -> bool:
        """记下一个片段。返回 True：思考到这个片段为止（它是思考之后第一个正文或工具调用片段）。"""
        t = time.monotonic()
        if msg.additional_kwargs.get("reasoning_content"):
            self._start = self._start or t
        elif self._start is not None and self._end is None and (msg.content or msg.tool_call_chunks):
            self._end = t
            return True
        return False

    def ms(self) -> int | None:
        """没有思考片段就是 None。"""
        return None if self._start is None else int(((self._end or time.monotonic()) - self._start) * 1000)


class ThinkingTimer(BaseCallbackHandler):
    """记下每次模型调用思考了多久（见 ThinkingClock），写进这条消息的 response_metadata[THINKING_MS]，随消息存进检查点。
    只有流式调用才有片段，才计时。用户点了停止、调用没跑完的，由运行器存那半截消息时一起记（见 api.research_runner.StreamBuffer）。
    run_inline：异步调用时直接在事件循环里调（只记个时间），不然每个片段都要转到线程池里调一次。"""

    run_inline = True

    def __init__(self) -> None:
        self._mu = threading.Lock()
        self._runs: dict[UUID, ThinkingClock] = {}

    def on_chat_model_start(self, serialized, messages, *, run_id: UUID, **kwargs: Any) -> None:
        pass  # 不实现的话回调管理器会退回 on_llm_start，把消息全转成字符串

    def on_llm_new_token(self, token, *, chunk=None, run_id: UUID, **kwargs: Any) -> None:
        msg = getattr(chunk, "message", None)
        if not isinstance(msg, AIMessageChunk):
            return
        with self._mu:
            self._runs.setdefault(run_id, ThinkingClock()).see(msg)

    def on_llm_end(self, response, *, run_id: UUID, **kwargs: Any) -> None:
        with self._mu:
            clock = self._runs.pop(run_id, None)
        ms = clock.ms() if clock else None
        if ms is None or not response.generations or not response.generations[0]:
            return
        msg = getattr(response.generations[0][0], "message", None)
        if msg is not None:  # 就是最后存进 state 的那个消息对象
            msg.response_metadata[THINKING_MS] = ms

    def on_llm_error(self, error, *, run_id: UUID, **kwargs: Any) -> None:
        with self._mu:
            self._runs.pop(run_id, None)


# ---- 聊天模型工厂 ----


def make_chat_model(provider: str, model: str = "", *, thinking: bool, effort: str = "high", timeout: int = 180,
                    callbacks: list | None = None, max_tokens: int | None = None, guard: str | None = None) -> BaseChatModel:
    """按提供方构造聊天模型。每次调用都过熔断，服务商的错误换成 ModelUnavailable / ContentBlocked（见 app.model_guard）。
    熔断默认按模型名记，同一个模型的各种用途一起断开；guard 给了就这个用途单独记（见 LiveResearchPlanner 的起标题）。

    DeepSeek 思考模式下：带工具的请求要把 reasoning_content 原样传回（langchain-openai / langchain-deepseek 都不做，这里补）。
    同一轮里的不传接口报 400；之前几轮的照它现在的文档也传，会拼进上下文，只传这一轮的话，下一轮上一轮的消息变了，前缀缓存从那里断
    （2026-11-01 Sean 定）。不接受强制 tool_choice（with_structured_output 会设），这里去掉；temperature 无效。
    """
    if provider == "ark":
        from langchain_openai import ChatOpenAI

        class ChatArk(GuardedChatModel, ChatOpenAI):
            def _guard_key(self) -> str:
                return guard_key(GuardedChatModel._guard_key(self), guard)

        if not config.ARK_API_KEY:
            raise RuntimeError("未设置 ARK_API_KEY")
        return ChatArk(
            model=model or config.ARK_MODEL, base_url=config.ARK_BASE_URL, api_key=config.ARK_API_KEY,
            extra_body={"thinking": {"type": "enabled" if thinking else "disabled"}}, timeout=timeout, max_retries=2, temperature=0,
            stream_usage=True, callbacks=callbacks, max_tokens=max_tokens,
        )
    if provider != "deepseek":
        raise ValueError(f"未知的模型提供方：{provider}")
    from langchain_deepseek import ChatDeepSeek

    if not config.DEEPSEEK_API_KEY:
        raise RuntimeError("未设置 DEEPSEEK_API_KEY")

    class ChatDeepSeekRT(GuardedChatModel, ChatDeepSeek):
        def _guard_key(self) -> str:
            return guard_key(GuardedChatModel._guard_key(self), guard)

        def _get_request_payload(self, input_, *, stop=None, **kwargs):
            payload = super()._get_request_payload(input_, stop=stop, **kwargs)
            if thinking and payload.get("tool_choice") not in (None, "auto", "none"):
                payload.pop("tool_choice")
            msgs = self._convert_input(input_).to_messages()
            if len(msgs) == len(payload.get("messages", [])):
                for m, d in zip(msgs, payload["messages"]):
                    if isinstance(m, AIMessage) and m.additional_kwargs.get("reasoning_content") is not None:
                        d["reasoning_content"] = m.additional_kwargs["reasoning_content"]
            return payload

    kw: dict[str, Any] = {"reasoning_effort": effort} if thinking else {"temperature": 0}
    return ChatDeepSeekRT(
        model=model or config.DEEPSEEK_MODEL, api_key=config.DEEPSEEK_API_KEY, api_base=config.DEEPSEEK_BASE_URL,
        extra_body={"thinking": {"type": "enabled" if thinking else "disabled"}}, timeout=timeout, max_retries=2,
        stream_usage=True, callbacks=callbacks, max_tokens=max_tokens, **kw,
    )


# ---- 真实模型 ----


def _structured(llm, schema):
    """DeepSeek 的 json_schema 实际走工具调用；模型偶尔直接回一段文字、不调工具，langchain 这时给 None 而不报错。
    转成异常，交给调用方的重试或失败处理，不让 None 流进后面的步骤。"""

    def parsed(out: dict):
        if out["parsed"] is None:
            raise RuntimeError(f"模型没有按结构返回：{out['parsing_error'] or '没有调用结构化输出'}")
        return out["parsed"]

    return llm.with_structured_output(schema, method="json_schema", strict=True, include_raw=True) | parsed


class McpTools:
    """agent-tools 的 MCP 工具：搜索、读页、画图、地图（查地点、比路线）、航班火车、天气、查文档（Context7）。target 是各自的 MCP 地址
    （网关的客户端，或者进程内的假服务）；地图的两个工具在同一个服务里，共用一个地址；航班、火车也是；找库、查文档也是。
    没连上的：搜索、读页、画图、地图、航班火车先用同名的占位工具（调了说服务暂时连不上，见 app.tools.web_mcp、image_mcp、maps_mcp、flyai_mcp），
    天气、查文档先不带。"""

    def __init__(self, search: Any, read: Any, weather: Any, image: Any, maps: Any, flyai: Any, docs: Any) -> None:
        self.web = [WebTool(SEARCH_TOOL, search), WebTool(READ_TOOL, read)]
        self.image = ImageTool(image)
        self.maps = [MapsTool(PLACE_TOOL, maps), MapsTool(ROUTE_TOOL, maps)]
        self.travel = [TravelTool(FLIGHT_TOOL, flyai), TravelTool(TRAIN_TOOL, flyai)]
        self.weather = WeatherTool(weather)
        self.docs = docs_tools(docs)

    def tools(self, own: list) -> list:
        """顺序固定（搜索、读页、画图、地图、航班火车、灵稿自己的、天气、找库、查文档）：工具列表在请求的前缀里，顺序变了前缀缓存就断了。"""
        return [*(t.current() for t in self.web), self.image.current(), *(t.current() for t in self.maps),
                *(t.current() for t in self.travel), *own, *([self.weather.tool] if self.weather.tool else []),
                *(t.tool for t in self.docs if t.tool)]

    async def connect(self) -> None:
        """同时连还没连上的，连上一个算一个；有没连上的抛 RuntimeError（第一个的原因在 __cause__ 里）。"""
        todo = [t for t in (*self.web, self.image, *self.maps, *self.travel, self.weather, *self.docs) if t.tool is None]
        errors = [(t.name, e) for t, e in zip(todo, await asyncio.gather(*(t.connect() for t in todo), return_exceptions=True), strict=True)
                  if isinstance(e, BaseException)]
        if errors:
            raise RuntimeError(f"没连上 {'、'.join(n for n, _ in errors)}：{type(errors[0][1]).__name__}: {errors[0][1]}") from errors[0][1]


class LiveResearchPlanner:
    def __init__(self, gateway: GatewayClient | None = None, scene: str = "research") -> None:
        """scene 是调 agent-tools 时记账的场景（网关按它分开记）：评测用 research_eval。"""
        provider, model = config.RESEARCH_PROVIDER, config.RESEARCH_MODEL
        self._agent = make_chat_model(provider, model, thinking=config.RESEARCH_THINKING, effort=config.RESEARCH_EFFORT,
                                      callbacks=[ThinkingTimer()])
        light = config.DEEPSEEK_FLASH_MODEL if provider == "deepseek" else config.ARK_MODEL_LITE
        # 子 agent 用快一些、便宜一些的模型（见 config.SUBAGENT_MODEL）
        sub = config.SUBAGENT_MODEL or light
        self._sub = make_chat_model(provider, sub, thinking=config.SUBAGENT_THINKING, effort=config.SUBAGENT_EFFORT,
                                    callbacks=[ThinkingTimer()])
        # 快模型步骤（推荐问题、单条送审）不开思考：都是小活，开思考慢得多
        self._fast = make_chat_model(provider, model, thinking=False, timeout=120)
        # 起标题照 Fusion 用轻量模型、不开思考、超时短：标题是锦上添花，慢了、坏了就用原话（见 graph.research.agent._TitleJob）。
        # 熔断单独记：超时短只是不想等，不说明服务坏了，不能把同一个模型上的子 agent、写短版一起断开；
        # 连续超时就先不起标题（断开期间直接用原话，回答也不用再等它）
        self._titler = make_chat_model(provider, light, thinking=False, timeout=TITLE_TIMEOUT, max_tokens=TITLE_MAX_TOKENS, guard="起标题")
        # 对话太长时给之前几轮写短版：轻量模型、不开思考（见 graph.research.compaction）
        self._summarizer = make_chat_model(provider, light, thinking=False, timeout=120, max_tokens=SUMMARY_MAX_TOKENS)
        # 每天整理一次记忆：默认和主 agent 同一个模型、开思考，要判断一件事是长期的还是一次性的（见 config.DREAM_MODEL）
        self._dreamer = make_chat_model(provider, config.DREAM_MODEL or model, thinking=config.DREAM_THINKING, effort=config.DREAM_EFFORT,
                                        timeout=300)
        url, key = config.AGENT_TOOLS_URL, config.AGENT_TOOLS_API_KEY
        self.gateway = gateway or GatewayClient(url, key, scene=scene)  # 按编号取图、删图（图存在 image-service 里）
        self._tools = make_research_tools()
        self._mcp = McpTools(lambda: web_mcp.gateway_target(SEARCH_TOOL, url, key, scene=scene),
                             lambda: web_mcp.gateway_target(READ_TOOL, url, key, scene=scene),
                             lambda: weather_mcp.gateway_target(url, key, scene=scene),
                             lambda: image_mcp.gateway_target(url, key, scene=scene),
                             lambda: maps_mcp.gateway_target(url, key, scene=scene),
                             lambda: flyai_mcp.gateway_target(url, key, scene=scene),
                             lambda: docs_mcp.gateway_target(url, key, scene=scene))

    def agent_model(self) -> BaseChatModel:
        return self._agent

    def subagent_model(self) -> BaseChatModel:
        return self._sub

    def tools(self) -> list:
        return self._mcp.tools(self._tools)

    async def connect_tools(self) -> None:
        await self._mcp.connect()

    async def title(self, text: str) -> str:
        """调用方不带这次运行的上下文来调（见 agent._TitleJob），字不进页面。"""
        out = await self._titler.ainvoke([("user", prompts.render("title.prompt", content=text))])
        return str(out.content).strip()

    async def suggest(self, question: str, reply: str) -> list[str]:
        res = await _structured(self._fast, Suggestions).ainvoke([("user", prompts.render("suggest.prompt", question=question, reply=reply))])
        return [q.strip() for q in res.questions if q.strip()]

    async def blocked(self, texts: Sequence[str]) -> list[bool]:
        """每段文字单独发一次（只有这段、不思考、只要 1 个 token），看内容审核拦不拦，同时最多 PROBE_WORKERS 个。服务不可用照常抛出。
        在 agent 调模型的那一步里调：片段标 nostream，不进这次运行的消息流（不然页面上会当成回答）。"""
        probe = self._fast.bind(max_tokens=1)
        slots = asyncio.Semaphore(PROBE_WORKERS)

        async def one(text: str) -> bool:
            async with slots:
                try:
                    await probe.ainvoke([("user", text)], config={"tags": [TAG_NOSTREAM]})
                except ContentBlocked:
                    return True
                return False

        return await aio.gather(*(one(t) for t in texts))

    async def summarize(self, prompt: str) -> str:
        """在 agent 调模型之前那一步里调（点了停止跟着这次运行一起取消），片段标 nostream，不进这次运行的消息流。"""
        out = await self._summarizer.ainvoke([("user", prompt)], config={"tags": [TAG_NOSTREAM]})
        return str(out.content).strip()

    def dream(self, prompt: str) -> str:
        """按 JSON 输出（response_format json_object）：开着思考也能用，不像结构化输出要强制调工具。"""
        out = self._dreamer.bind(response_format={"type": "json_object"}).invoke([("user", prompt)])
        return str(out.content)


# ---- 离线假实现 ----


FAKE_PLACES = {"广州": "广东省·广州", "北京市·北京·朝阳": "北京市·北京·朝阳", "辽宁省·朝阳": "辽宁省·朝阳"}
FAKE_SKY = ["晴", "多云", "小雨", "阴"]
FAKE_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _fake_forecast(location: str, requested_date: str | None) -> dict:
    """假的 weather-service（结果的样子照真的，见 agent-tools 的 services/weather/README.md）：地方只有广州和两个朝阳；
    逐日从北京时间今天起 15 天（没问哪天给 4 天），逐小时从这个小时起 7 天（没问哪天给 24 个）、每天 14～16 点有雨。"""
    if location.strip() == "朝阳":
        return {"ok": False, "error": {"code": "ambiguous_location", "message": "The place name matches several areas.",
                                       "retryable": False, "candidates": ["北京市·北京·朝阳", "辽宁省·朝阳"]}}
    place = next((label for name, label in FAKE_PLACES.items() if name in location), None)
    if place is None:
        return {"ok": False, "error": {"code": "location_not_found", "message": "The weather service could not find this place.",
                                       "retryable": False}}
    now = datetime.now(timezone(timedelta(hours=8))).replace(minute=0, second=0, microsecond=0)
    today = now.date()
    days = [{"date": (d := today + timedelta(days=k)).isoformat(), "weekday": FAKE_WEEKDAYS[d.weekday()],
             "day_weather": FAKE_SKY[k % len(FAKE_SKY)], "night_weather": "晴", "high_c": 30 - k, "low_c": 21 - k,
             "day_wind_direction": "北风", "day_wind_power": "1-3级"}
            for k in range(15)]
    hourly = []
    for k in range(168):
        at = now + timedelta(hours=k)
        wet = 14 <= at.hour <= 16
        hourly.append({"time": at.isoformat(), "temp_c": 22 + round(8 * max(0.0, 1 - abs(at.hour - 14) / 9)),
                       "weather": "小雨" if wet else "晴", "pop": 70 if wet else 0, "precip_mm": 1.2 if wet else 0.0})
    out: dict = {"ok": True, "resolved_location": place, "local_today": today.isoformat()}
    limitations = []
    if requested_date:
        try:
            ahead = (date.fromisoformat(requested_date) - today).days
        except ValueError:
            return {"ok": False, "error": {"code": "invalid_arguments", "message": "requested_date must be YYYY-MM-DD", "retryable": False}}
        out["requested_date"] = requested_date
        if not 0 <= ahead < 7:  # 逐小时只在没指定日期、问今天起 7 天内的某天时取
            hourly = []
        if picked := [d for d in days if d["date"] == requested_date]:
            days, hourly = picked, [h for h in hourly if h["time"][:10] == requested_date]
        else:  # 不在范围里：一天都不给，只给能查的头尾两天
            out["forecast_range"] = {"first_date": days[0]["date"], "last_date": days[-1]["date"]}
            days, hourly = [], []
            limitations.append("requested_date_not_covered")
    else:
        days, hourly = days[:4], hourly[:24]
    out |= {"day_count": len(days), "forecast_days": days}
    if hourly:
        wet = [h for h in hourly if h["precip_mm"] > 0]
        peak = max(hourly, key=lambda h: h["pop"])
        summary = {"window": f"{hourly[0]['time']} ~ {hourly[-1]['time']}", "temp_min_c": min(h["temp_c"] for h in hourly),
                   "temp_max_c": max(h["temp_c"] for h in hourly), "max_pop_percent": peak["pop"], "max_pop_time": peak["time"],
                   "precipitation_hours": len(wet)}
        if wet:
            summary |= {"first_precipitation_time": wet[0]["time"], "last_precipitation_time": wet[-1]["time"],
                        "total_precip_mm": round(sum(h["precip_mm"] for h in wet), 1)}
        out |= {"hourly": hourly, "hourly_summary": summary}
    return out | {"fetched_at": now.isoformat(timespec="seconds"), "limitations": limitations}


def fake_weather_server() -> FastMCP:
    """进程内的假 weather-service（MCP），离线模式、测试、评测用。"""
    server = FastMCP("weather")

    @server.tool(name=WEATHER_TOOL, description="Get the weather forecast for a named place (offline fake of the QWeather service).",
                 output_schema=None)
    async def get_weather(location: str, requested_date: str | None = None) -> ToolResult:
        res = _fake_forecast(location, requested_date)
        if not res["ok"]:
            text = f"No forecast was fetched: {res['error']['code']}. Do not fabricate weather facts."
        else:
            text = json.dumps({k: v for k, v in res.items() if k != "hourly"}, ensure_ascii=False)
        return ToolResult(content=text, structured_content=res)

    return server


FAKE_AMBIGUOUS_PLACE = "鼓楼"  # 假 maps-service 里重名的地名
_FAKE_MAPS_HIDDEN = {"ok", "photo_url", "map_url", "provider", "providers"}  # 照 maps-service：模型看不到的字段


def _fake_for_model(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _fake_for_model(v) for k, v in value.items() if k not in _FAKE_MAPS_HIDDEN}
    if isinstance(value, list):
        return [_fake_for_model(v) for v in value]
    return value


def _fake_maps_failure(code: str, place: str, argument: str) -> ToolResult:
    error: dict[str, Any] = {"code": code, "message": code, "retryable": code == "ambiguous_location", "place_name": place}
    if code == "ambiguous_location":
        error |= {"candidate_argument": argument, "candidates": [
            {"candidate": 1, "city": "北京市", "district": "东城区", "category": "风景名胜", "location": "116.39,39.94"},
            {"candidate": 2, "city": "南京市", "district": "鼓楼区", "category": "区县级地名", "location": "118.77,32.06"}]}
    return ToolResult(content=f"No result was fetched: {code} for {place}. Do not fabricate place or route facts.",
                      structured_content={"ok": False, "error": error})


def fake_maps_server() -> FastMCP:
    """进程内的假 maps-service（MCP）：查地点给两个固定的地方，比路线按要的方式给固定的路线；地名是 FAKE_AMBIGUOUS_PLACE 时重名。
    结果的样子照 agent-tools 的 services/maps/README.md。"""
    server = FastMCP("maps")

    @server.tool(name=PLACE_TOOL, description="Search for places in mainland China (offline fake of the map service).", output_schema=None)
    async def place_search(query: str, anchor_source: Literal["named", "current_location", "none"], city: str | None = None,
                           near: str | None = None, near_candidate: int | None = None, radius_m: int = 3000, limit: int = 5) -> ToolResult:
        if anchor_source == "current_location":
            return _fake_maps_failure("location_unavailable", "current_location", "")
        if anchor_source == "named" and (near or "").strip() == FAKE_AMBIGUOUS_PLACE and near_candidate is None:
            return _fake_maps_failure("ambiguous_location", FAKE_AMBIGUOUS_PLACE, "near_candidate")
        places = [{"place_id": f"amap_FAKE{k}", "name": f"{query.split('|')[0]}{k}号店", "address": f"中山东一路{k}号", "district": "黄浦区",
                   "city": "上海市", "category": "餐饮服务;咖啡厅;咖啡厅", "rating": 4.5 - k / 10, "reference_cost_yuan": 30 + k * 5,
                   "map_url": f"https://uri.amap.com/marker?poiid=FAKE{k}&src=lingdraft", "provider": "amap",
                   **({"distance_m": 100 * k} if anchor_source == "named" else {})} for k in (1, 2)][:limit]
        res: dict[str, Any] = {"ok": True, "status": "success", "query": query, "result_count": len(places), "places": places,
                               "limitations": [], "providers": ["amap"]}
        if city:
            res["city"] = city
        if anchor_source == "named" and near:
            res |= {"near": near, "anchor": {"source": "named", "label": near.strip(), "city": "上海市"}}
        return ToolResult(content=json.dumps(_fake_for_model(res), ensure_ascii=False), structured_content=res)

    @server.tool(name=ROUTE_TOOL, description="Compare transport modes between two places (offline fake of the map service).", output_schema=None)
    async def route_compare(origin: str, destination: str, origin_source: Literal["named", "current_location"],
                            destination_source: Literal["named", "current_location"], modes: list[str], origin_city: str | None = None,
                            destination_city: str | None = None, origin_candidate: int | None = None, destination_candidate: int | None = None,
                            origin_place_id: str | None = None, destination_place_id: str | None = None,
                            requested_departure_time: str | None = None) -> ToolResult:
        if "current_location" in (origin_source, destination_source):
            return _fake_maps_failure("location_unavailable", "current_location", "")
        if origin.strip() == FAKE_AMBIGUOUS_PLACE and origin_candidate is None:
            return _fake_maps_failure("ambiguous_location", FAKE_AMBIGUOUS_PLACE, "origin_candidate")
        made = {
            "driving": {"mode": "driving", "distance_m": 23007, "duration_s": 3540, "summary": "速度最快", "toll_yuan": 0, "provider": "amap"},
            "transit": {"mode": "transit", "duration_s": 3000, "fare_yuan": 5, "transit_type": "subway", "transfers": 0,
                        "walking_distance_m": 1061, "provider": "amap",
                        "legs": [{"kind": "walking", "distance_m": 43, "duration_s": 60},
                                 {"kind": "subway", "line_name": "地铁10号线", "departure_stop": origin.strip(), "arrival_stop": "豫园",
                                  "via_stop_count": 13, "distance_m": 18137, "duration_s": 2040, "entrance": "北2入口", "exit": "6口"},
                                 {"kind": "walking", "distance_m": 1018, "duration_s": 900}]},
            "walking": {"mode": "walking", "distance_m": 19000, "duration_s": 15000, "provider": "amap"},
        }
        routes = [made[m] for m in modes if m in made]
        res: dict[str, Any] = {"ok": True, "status": "success" if len(routes) == len(modes) else "degraded",
                               "origin": {"source": "named", "label": origin.strip(), "city": "上海市"},
                               "destination": {"source": "named", "label": destination.strip(), "city": "上海市"},
                               "routes": routes, "unavailable_modes": [m for m in modes if m not in made],
                               "limitations": ["departure_time_not_applied"] if requested_departure_time else [], "providers": ["amap"]}
        if requested_departure_time:
            res["requested_departure_time"] = requested_departure_time
        return ToolResult(content=json.dumps(_fake_for_model(res), ensure_ascii=False), structured_content=res)

    return server


FAKE_UNKNOWN_PLACE = "火星"  # 假 flyai 认不出的地名
FAKE_NO_SERVICE = "无班次"  # 假 flyai 查不到班次的目的地


def _fake_option_id(kind: str, number: str, day: str) -> str:
    return "opt_" + hashlib.sha256(f"{kind}|{number}|{day}".encode()).hexdigest()[:20]


def _fake_service(kind: str, origin: str, destination: str, day: str, number: str, carrier: str, dep: str, arr: str, minutes: int,
                  travel_class: str, price: float) -> dict:
    station = (lambda city: f"{city}机场") if kind == "flight" else (lambda city: f"{city}南站")
    return {"option_id": _fake_option_id(kind, number, day), "number": number, ("airline" if kind == "flight" else "train_type"): carrier,
            "departure": {"city": origin, "station": station(origin), **({"terminal": "T2"} if kind == "flight" else {}), "time": f"{day}T{dep}"},
            "arrival": {"city": destination, "station": station(destination), "time": f"{day}T{arr}", "day_offset": 0},
            "duration_minutes": minutes, "travel_class": travel_class, "price_yuan": price,
            "booking_url": f"https://router.feizhu.com/fake/{number}"}


def _fake_travel(kind: str, origin: str, destination: str, departure_date: str, query: dict) -> ToolResult:
    o, d = origin.strip(), destination.strip()
    if FAKE_UNKNOWN_PLACE in (o, d):
        which = "origin" if o == FAKE_UNKNOWN_PLACE else "destination"
        error = {"code": f"{which}_not_recognized", "message": f"{which} not recognized", "retryable": False}
        return ToolResult(content=f"No result was fetched: the travel service did not recognize the {which}.",
                          structured_content={"ok": False, "error": error})
    if d == FAKE_NO_SERVICE:
        return ToolResult(content="This query returned no direct services.",
                          structured_content={"ok": False, "error": {"code": "no_services", "message": "no services", "retryable": False}})
    if kind == "flight":
        services = [_fake_service(kind, o, d, departure_date, "MU5101", "东航", "08:00", "10:15", 135, "经济舱", 1060),
                    _fake_service(kind, o, d, departure_date, "CA1501", "国航", "09:30", "11:40", 130, "经济舱", 990)]
    else:
        services = [_fake_service(kind, o, d, departure_date, "G1", "高铁", "07:00", "11:29", 269, "二等座", 662),
                    _fake_service(kind, o, d, departure_date, "G3", "高铁", "08:00", "12:32", 272, "二等座", 662)]
    res = {"ok": True, "observed_at": "2026-11-04T10:00:00+08:00", "query": {"origin": o, "destination": d, "departure_date": departure_date, **query},
           "services": services, "limitations": []}
    hidden = {"ok", "booking_url"}  # 照 flyai：模型看不到的字段
    shown = {k: v for k, v in res.items() if k not in hidden} | {"services": [{k: v for k, v in s.items() if k not in hidden} for s in services]}
    return ToolResult(content=json.dumps(shown, ensure_ascii=False), structured_content=res)


def fake_flyai_server() -> FastMCP:
    """进程内的假 flyai（MCP）：航班、火车各给两个固定的班次，站名是“城市机场”“城市南站”；FAKE_UNKNOWN_PLACE 认不出，
    目的地是 FAKE_NO_SERVICE 时没有班次。结果的样子照 agent-tools 的 services/flyai/README.md。"""
    server = FastMCP("flyai")

    @server.tool(name=FLIGHT_TOOL, description="Search one-way direct flights (offline fake of the travel service).", output_schema=None)
    async def search_flights(origin: str, destination: str, departure_date: str, cabin_class: str | None = None,
                             departure_after_hour: int | None = None, departure_before_hour: int | None = None,
                             arrival_before_hour: int | None = None, max_price_yuan: float | None = None, sort_by: str | None = None,
                             limit: int | None = None) -> ToolResult:
        return _fake_travel("flight", origin, destination, departure_date, {"cabin_class": cabin_class} if cabin_class else {})

    @server.tool(name=TRAIN_TOOL, description="Search one-way direct trains (offline fake of the travel service).", output_schema=None)
    async def search_trains(origin: str, destination: str, departure_date: str, train_category: Literal["high_speed", "all"],
                            seat_class: str | None = None, departure_after_hour: int | None = None, departure_before_hour: int | None = None,
                            arrival_before_hour: int | None = None, max_price_yuan: float | None = None, sort_by: str | None = None,
                            limit: int | None = None) -> ToolResult:
        return _fake_travel("train", origin, destination, departure_date, {"train_category": train_category})

    return server


FAKE_READ_MAX_CHARS = 8000  # 照 reader-service：正文最多给这么多字


FAKE_LIBRARY_ID = "/websites/langchain_oss_python"
# 照 2026-11-04 真实调的样子（两段同一页、一段另一页）
FAKE_DOCS = """### Trim messages using @before_model middleware

Source: https://docs.langchain.com/oss/python/langchain/short-term-memory

Keep only the last few messages to fit the context window.

```python
@before_model
def trim_messages(state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
    return None
```

--------------------------------

### Create decorator-based middleware with @before_model and @wrap_model_call

Source: https://docs.langchain.com/oss/python/langchain/middleware/custom

Decorators like @before_model inspect or update state before model calls.

--------------------------------

### Create middleware > Decorator-based middleware

Source: https://docs.langchain.com/oss/python/langchain/middleware/custom

Quick and simple for single-hook middleware."""


def fake_docs_server() -> FastMCP:
    """进程内的假 Context7（agent-tools 的 context7 服务转的官方 MCP）：工具名、参数、结果的样子照真的，库只有 LangChain；
    找不到库、找不到文档照真的回 isError 和一句话。"""
    server = FastMCP("context7")

    @server.tool(name=LIBRARY_TOOL, description="Resolves a package/product name to a Context7-compatible library ID and returns matching libraries.")
    def resolve(query: str, libraryName: str) -> str:  # noqa: N803 参数名照 Context7
        if "langchain" not in libraryName.lower():
            raise ToolError(f'No libraries found for "{libraryName}". Try a different search term.')
        return ("Available Libraries:\n\n- Title: LangChain OSS Python\n"
                f"- Context7-compatible library ID: {FAKE_LIBRARY_ID}\n- Code Snippets: 12246\n- Source Reputation: High\n----------\n"
                "- Title: LangChain v1\n- Context7-compatible library ID: /websites/langchain_oss_python_releases_langchain-v1\n"
                "- Code Snippets: 28\n- Source Reputation: High")

    @server.tool(name=DOCS_TOOL, description="Retrieves and queries up-to-date documentation and code examples from Context7 for any programming library or framework.")
    def query(libraryId: str, query: str, language: str | None = None) -> str:  # noqa: N803
        if libraryId != FAKE_LIBRARY_ID:
            raise ToolError(f'Library "{libraryId}" not found. Please check the library ID or your access permissions.')
        return FAKE_DOCS

    return server


def fake_search_server(hits: list[dict] | None = None) -> FastMCP:
    """进程内的假 search-service（MCP）：每次搜索给 3 条稳定的候选（测试给了 hits 就每次都给这几条）。结果的样子照 agent-tools 的 search_web。"""
    server = FastMCP("search")

    @server.tool(name=SEARCH_TOOL, description="Search the web and get candidate sources (offline fake of the search service).",
                 output_schema=None)
    async def search_web(query: str, count: int = 10, recency_days: int | None = None, domains: list[str] | None = None) -> ToolResult:
        q = query.strip()
        slug = re.sub(r"\W+", "-", q)[:24] or "q"
        found = hits if hits is not None else [
            {"title": f"关于“{q}”的评测 {i}", "url": f"https://example.test/{slug}/{i}", "snippet": f"摘要 {i}：围绕“{q}”的说明。",
             "published_at": f"2026-0{i}-01"} for i in (1, 2, 3)]
        res: dict[str, Any] = {"ok": True, "query": q, "results": found[:count]}
        text = json.dumps(res, ensure_ascii=False, separators=(",", ":"))
        return ToolResult(content=text, structured_content={**res, "meta": {"provider": "fake", "cached": False}})

    return server


def fake_read_server(page_repeat: int = 5) -> FastMCP:
    """进程内的假 reader-service（MCP）：正文是一句话重复 page_repeat 遍；网址以 /3 结尾的读不到（404）。"""
    server = FastMCP("reader")

    @server.tool(name=READ_TOOL, description="Read one web page and get its text (offline fake of the reader service).", output_schema=None)
    async def read_page(url: str, full_page: bool = False) -> ToolResult:
        if url.endswith("/3"):
            error = {"code": "page_not_found", "message": "The page does not exist.", "retryable": False, "status": 404}
            return ToolResult(content="The page does not exist (the site returned HTTP 404). Use another source.",
                              structured_content={"ok": False, "error": error})
        body = f"正文：{url} 的内容。" * page_repeat
        res: dict[str, Any] = {"ok": True, "url": url, "title": "正文标题", "content": body[:FAKE_READ_MAX_CHARS]}
        if len(body) > FAKE_READ_MAX_CHARS:
            res["truncated"] = True
        res["content_chars"] = len(body)
        return ToolResult(content=json.dumps(res, ensure_ascii=False), structured_content={**res, "meta": {"provider": "fake"}})

    return server


FAKE_IMAGE_FILTERED = "【违规图】"  # 画图的描述里有它，假 image-service 按内容审核没过拒绝
FAKE_CHUNK_GAP_S = 0.0  # 假模型异步流式时片段之间隔多久；测试停止时调大，让已经吐出的片段先到页面（真模型片段之间有网络的空档）
FAKE_IMAGE_SLOW = "慢慢画"  # 描述里有它，假 image-service 过几秒才出图（看占位、试停止：点了停止马上断开）
FAKE_IMAGE_SLOW_S = 6.0


def _fake_png(prompt: str, ratio: str, edit: bool) -> bytes:
    """按比例画一张纯色底、带个方块的 PNG，颜色随描述变；改出来的图方块是白的。"""
    w, h = (int(x) for x in ratio.split(":"))
    size = (512, round(512 * h / w)) if w >= h else (round(512 * w / h), 512)
    d = hashlib.md5(prompt.encode()).digest()
    img = Image.new("RGB", size, (60 + d[0] % 150, 60 + d[1] % 150, 60 + d[2] % 150))
    ImageDraw.Draw(img).rectangle((size[0] // 3, size[1] // 3, size[0] * 2 // 3, size[1] * 2 // 3),
                                  fill=(255, 255, 255) if edit else (20, 24, 31))
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


FAKE_RATIOS = ("1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9", "1:4", "4:1", "1:8", "8:1")
FakeRatio = Literal[FAKE_RATIOS]  # 照 image-service：比例不在可选值里，参数校验不过（MCP 的 isError）


def _closest(width: int, height: int) -> str:
    target = math.log(width / height)
    return min(FAKE_RATIOS, key=lambda r: abs(math.log(int(r.split(":")[0]) / int(r.split(":")[1])) - target))


class FakeImageService:
    """进程内的假 image-service：MCP 的 generate_image（照 /v2/mcp：图存在服务里，结果里只有编号）出图、存在这里，
    取图、删图走假网关（GET / DELETE /image/v1/images/{id}，见 gateway_client）。离线模式、测试、评测用。
    calls 记下每次画图的参数；描述里有 FAKE_IMAGE_FILTERED 按内容审核没过拒绝，有 FAKE_IMAGE_SLOW 过几秒才出图。
    测试可以设 fail（下一次起都按这个错误回）、block（出图前等它）、reference_status（找参考图的结果）。"""

    def __init__(self) -> None:
        self.made: dict[str, tuple[bytes, int, int]] = {}
        self.calls: list[dict] = []
        self.deleted: list[str] = []
        self.fail: dict | None = None
        self.block: threading.Event | None = None
        self.reference_status = "found"

    def server(self) -> FastMCP:
        server = FastMCP("image-service")

        @server.tool(name="generate_image", output_schema=None,
                     description="Generate a new image, or edit an image generated earlier (offline fake of image-service).")
        async def generate_image(prompt: str, aspect_ratio: FakeRatio | None = None, style: str | None = None, regenerate: bool = False,
                                 reference_subject: str | None = None, quality: Literal["standard", "high"] | None = None,
                                 source_image_id: str | None = None) -> ToolResult:
            args = {"prompt": prompt, "aspect_ratio": aspect_ratio, "style": style, "regenerate": regenerate,
                    "reference_subject": reference_subject, "quality": quality, "source_image_id": source_image_id}
            self.calls.append({k: v for k, v in args.items() if v not in (None, False)})
            return await self._generate(prompt, aspect_ratio, quality, reference_subject, (source_image_id or "").strip() or None)

        return server

    async def _generate(self, prompt: str, ratio: str | None, quality: str | None, subject: str | None, source: str | None) -> ToolResult:
        def failure(code: str, text: str) -> ToolResult:
            return ToolResult(content=text, structured_content={"ok": False, "error": {"code": code, "message": code, "retryable": False}})

        if source is not None and source not in self.made:
            return failure("source_image_unavailable", "The image to edit could not be found among the editable images of this conversation.")
        if self.fail is not None:
            return ToolResult(content=f"Image generation failed (error code: {self.fail['code']}).", structured_content={"ok": False, "error": self.fail})
        if FAKE_IMAGE_FILTERED in prompt:
            return failure("content_filtered", "The image model refused this request under its content safety filter. Do not retry on your own.")
        if self.block is not None:
            await asyncio.to_thread(self.block.wait, 10)  # 在线程里等，不卡对话的事件循环（点了停止，这里跟着取消）
        if FAKE_IMAGE_SLOW in prompt:
            await asyncio.sleep(FAKE_IMAGE_SLOW_S)
        if ratio is None:
            ratio = _closest(*self.made[source][1:]) if source else "1:1"
        data = _fake_png(prompt, ratio, source is not None)
        with Image.open(io.BytesIO(data)) as im:
            width, height = im.size
        iid = str(uuid4())
        self.made[iid] = (data, width, height)
        model = "doubao-seedream-5-0-pro" if quality == "high" else "doubao-seedream-5-0-flash"
        image = {"id": iid, "mime_type": "image/png", "width": width, "height": height, "bytes": len(data), "model": model,
                 "edited_from": source, "created_at": "2026-11-04T00:00:00+00:00"}
        text = (f"The image was {'edited' if source else 'generated'}. Image id: {iid}. Use it only as source_image_id if the user later asks "
                "to change this image; never show it to the user. Do not output an image link, a markdown image, or the image id.")
        return ToolResult(content=text, structured_content={
            "ok": True, "image": image, "requested_model": model, "fallback_used": False, "grounding_used": False, "cached": False,
            "reference_search": {"status": self.reference_status} if subject else None})

    async def handle(self, req: httpx.Request) -> httpx.Response:
        """假网关：取图、删图。"""
        iid = req.url.path.removeprefix("/image/v1/images/")
        if iid == req.url.path:
            return httpx.Response(404, json={"error": "not_found"})
        if req.method == "DELETE":
            self.deleted.append(iid)
            self.made.pop(iid, None)
            return httpx.Response(204)
        got = self.made.get(iid)
        return httpx.Response(200, content=got[0], headers={"content-type": "image/png"}) if got else httpx.Response(404)

    def gateway_client(self) -> GatewayClient:
        return GatewayClient("http://fake-gw", "fake-key", scene="research", transport=httpx.MockTransport(self.handle))


_FAKE_IMAGE_ID = re.compile(r"Image id: ([0-9a-f-]{36})")  # 生图结果里给模型的编号（image-service 的结果里写的）
FAKE_USAGE = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
              "input_token_details": {"cache_read": 60}, "output_token_details": {"reasoning": 5}}
_BAD_ARGS = '{"query": "a" x}'  # 解析不了的参数


def _budget_note(m: BaseMessage) -> bool:
    return isinstance(m, HumanMessage) and str(m.content).startswith("<budget>")


def _turn_start(messages: list[BaseMessage]) -> int:
    """这一轮用户的话的位置：最后一条用户消息，子 agent 请求末尾的预算提醒（<budget>）、用户插进这一轮的话不算。"""
    return next((i for i in range(len(messages) - 1, -1, -1) if history.is_request(messages[i]) and not _budget_note(messages[i])), -1)


class FakeToolModel(BaseChatModel):
    """按这一轮的进度发固定的工具调用：搜 → 读 → 文字回复。每次回复都带一段假思考和用量。

    用户的话里的关键字控制特殊路径（只给测试用）：
    - “直接答”：不调工具直接回复；“不回复”：最后不给文字；
    - “坏JSON”：第一步先发一个参数无法解析的工具调用；“一直坏”：每次都发。
    - “几点”：先取当前时间，再照常往下走。
    - “天气”：先查天气，地名是“天气”前面那段（没有就是广州），再照常搜、读。
    - “附近”：查地点，在“附近”前面那个地名附近查（没有就是外滩），查完直接回复，不搜；
      “路线”：比虹桥火车站到外滩的驾车和公交，起点改成“从……到”里的地名，算完直接回复，不搜。
    - “航班”：查北京到上海明天的航班；“高铁”：查北京到上海明天的高铁；“行程”：去程查航班、回程查高铁，查完用 present_itinerary
      排成一个方案；“接站”：查完高铁，从查到的第一趟的到达站比路线到外滩。地名在“从……到……”里给就用那两个。都不搜。
    - “画”：画一张图（描述就是用户的话，“横”的是 16:9），画完直接回复，不搜；“改图”：改这段对话里最近画的那张；
      “再画”“再生成”：用上一次的参数再画一张（regenerate）。
    - “子任务”：派一个子 agent（task），拿到报告后回复，引用报告里的来源；“分头”：一次派两个，并行。“派N个”：一次派 N 个。
    - “一直搜”：每次并行搜两个，不管预算，直到请求末尾的预算提醒说搜索用完了（子 agent 才有这条提醒）。
    - “取回”：用 recall 取回这段对话第一次搜索的第一个来源；“取回第N轮”：取回第 N 轮。取到后回复取到的开头。
    用户插进这一轮的话不改这些路径，给出文字回复时开头说一声收到了哪些（测试据此看模型有没有看到）。
    内容审核（模拟服务商）：请求里任何地方有 FAKE_BLOCKED 就整个拒绝；用户的话里有“查完就拦”时，有工具结果就拒绝；
    有“输出拦截”时，吐完思考后输出被截断。
    模型服务生成到一半断了（Interrupted，模拟 model_guard 换好的错误）：用户的话里有“中途断”时，写文字回复时吐完思考和前半段正文就断，
    这一轮里只断一次（主 agent、子 agent 各一次：子任务的说明里带着用户的话）；“一直断”：每次都断。
    子 agent 出错：用户的话里有“出错”时，派出去的第 1 个子 agent 搜完以后模型服务不可用（ModelUnavailable），别的子 agent 照常。
    """

    bound: list[str] = Field(default_factory=list)
    prefix: str = "fake"  # 消息 id 的前缀：子 agent 的假模型用另一个，和主 agent 的消息分得开
    tool_choice: str | None = None  # "none"：不许调工具，直接回复
    gates: list = Field(default_factory=list)  # 测试用：放进去的 threading.Event，每次调用先等它（模拟慢模型）；bind_tools 复制后还是同一个 list
    # 异步调用时等闸门、定回复（_ready）放到线程里做：测试里改写 _message 的子类可以在里面同步地等，不卡事件循环

    @property
    def _llm_type(self) -> str:
        return "fake-tool-model"

    def bind_tools(self, tools: Any, **kwargs: Any) -> FakeToolModel:
        return self.model_copy(update={"bound": [getattr(t, "name", getattr(t, "__name__", "")) for t in tools],
                                       "tool_choice": kwargs.get("tool_choice")})

    def _decide(self, messages: list[BaseMessage]) -> tuple[str, list[dict]]:
        # 用户的话：最后一条用户消息，子 agent 请求末尾的预算提醒（<budget>，见 subagent.SubagentPrompt）、插进这一轮的话不算
        i = _turn_start(messages)
        text, turn = str(messages[i].content), messages[i + 1:]
        names = [tc["name"] for m in turn if isinstance(m, AIMessage) for tc in m.tool_calls]
        bad = [tc for m in turn if isinstance(m, AIMessage) for tc in m.invalid_tool_calls]
        hits = _hits(messages)
        # 工具调用 id 里带上一段随机的：只按消息条数，去掉内容审核停下的那一轮以后条数会和之前重复，结果就对错了调用
        n = f"{len(messages)}{uuid4().hex[:6]}"

        def call(tool: str, **args) -> tuple[str, list[dict]]:
            return "", [{"name": tool, "args": args, "id": f"call_{tool}_{n}"}]

        if "出错" in text and "第1方面" in text and hits:  # 只有子 agent 的任务说明里有“第N方面”
            raise ModelUnavailable("模型服务暂时不可用（连接失败），稍后再试")
        if self.tool_choice == "none":
            return (f"不能再查了，按查到的说 [{hits[0]['id']}]。" if hits else "不能再查了，没查到。"), []
        if "坏JSON" in text and (not bad or "一直坏" in text) and not names:
            return "", [{"name": "search_web", "args": _BAD_ARGS, "id": f"bad_{n}", "invalid": True}]
        if "几点" in text and TIME_TOOL in self.bound and TIME_TOOL not in names:
            return call(TIME_TOOL)
        if "直接答" in text:
            return "直接回答：这个问题不用查。", []
        if "天气" in text and WEATHER_TOOL in self.bound and WEATHER_TOOL not in names:
            return call(WEATHER_TOOL, location=text.split("天气")[0].strip()[:8] or "广州")
        if "文档" in text and LIBRARY_TOOL in self.bound:
            return self._docs(text, turn, call)
        if ("附近" in text or "路线" in text) and PLACE_TOOL in self.bound:
            return self._maps(text, turn, call)
        if any(k in text for k in ("航班", "高铁", "行程", "接站")) and FLIGHT_TOOL in self.bound:
            return self._travel(text, turn, call)
        if any(k in text for k in ("画", "改图", "再生成")) and "generate_image" in self.bound:
            return self._draw(text, messages, turn, call)
        if any(k in text for k in ("子任务", "分头", "派")) and "task" in self.bound:
            return self._delegate(text, turn, n)
        if "一直搜" in text and "search_web" in self.bound:  # 每次并行搜两个，直到预算提醒说搜索用完了
            note = messages[-1] if _budget_note(messages[-1]) else None
            if note is not None and re.search(r"search_web Budget Remaining: 0\b", str(note.content)):
                return "预算用完了，按查到的说。", []
            return "", [{"name": "search_web", "args": {"query": f"{text[:10]}{n}{k}", "count": 3}, "id": f"call_search_web_{n}{k}"} for k in "ab"]
        if "取回" in text and RECALL in self.bound:
            return self._recall(text, messages, turn, call)
        if "search_web" not in names:
            return call("search_web", query=text.splitlines()[0][:20], count=3)
        if "read_page" not in names and hits:
            return call("read_page", url=hits[0]["url"])
        if "不回复" in text:
            return "", []
        return f"结论：可以用但不稳定 [{hits[0]['id']}]，详情见第二条 [{hits[1]['id']}]。" if hits else "没查到。", []

    @staticmethod
    def _docs(text: str, turn: list[BaseMessage], call) -> tuple[str, list[dict]]:
        """“X 的文档”：先找库（库名是话里第一个英文词），再查文档，引用查到的第一段；没做成就照说。"""
        done = {m.name: m for m in turn if isinstance(m, ToolMessage) and m.name in (LIBRARY_TOOL, DOCS_TOOL)}
        lib, docs = done.get(LIBRARY_TOOL), done.get(DOCS_TOOL)
        if lib is None:
            name = m.group(0) if (m := re.search(r"[A-Za-z][\w.\-]*", text)) else text[:10]
            return call(LIBRARY_TOOL, libraryName=name, query=text[:40])
        if lib.status == "error":
            return "没找到这个库。", []
        if docs is None:
            return call(DOCS_TOOL, libraryId=(lib.artifact or {}).get("libraries", [FAKE_LIBRARY_ID])[0], query=text[:40])
        if docs.status == "error" or not (snippets := doc_snippets(docs)):
            return "文档没查成。", []
        return f"用 @before_model 在调模型前改消息 [{snippets[0]['id']}]。", []

    @staticmethod
    def _maps(text: str, turn: list[BaseMessage], call) -> tuple[str, list[dict]]:
        if done := next((m for m in reversed(turn) if isinstance(m, ToolMessage) and m.name in (PLACE_TOOL, ROUTE_TOOL)), None):
            return ("地图没查成。" if done.status == "error" else "查好了，看卡片。"), []
        if "路线" in text:
            origin = m.group(1) if (m := re.search(r"从(\S+?)到", text)) else "虹桥火车站"
            return call(ROUTE_TOOL, origin=origin, destination="外滩", origin_source="named", destination_source="named",
                        modes=["driving", "transit"])
        near = text.split("附近")[0].strip()[-6:] or "外滩"
        return call(PLACE_TOOL, query="咖啡", anchor_source="named", near=near, city="上海", limit=2)

    @staticmethod
    def _travel(text: str, turn: list[BaseMessage], call) -> tuple[str, list[dict]]:
        done = {m.name: m for m in turn if isinstance(m, ToolMessage)}
        found = [s for m in done.values() if m.name in (FLIGHT_TOOL, TRAIN_TOOL) and m.status != "error"
                 for s in ((m.artifact or {}).get("structured_content") or {}).get("services", [])]
        m = re.search(r"从(\S+?)到(\S+?)(?:的|$|，|。|\s)", text)
        origin, destination = (m.group(1), m.group(2)) if m else ("北京", "上海")
        day = (date.today() + timedelta(days=1)).isoformat()
        back = (date.today() + timedelta(days=3)).isoformat()
        if "行程" in text:
            if FLIGHT_TOOL not in done:
                return call(FLIGHT_TOOL, origin=origin, destination=destination, departure_date=day)
            if TRAIN_TOOL not in done:
                return call(TRAIN_TOOL, origin=destination, destination=origin, departure_date=back, train_category="high_speed")
            if ITINERARY_TOOL not in done and len(found) >= 3:
                out = next(s for s in found if s["departure"]["city"] == origin)
                ret = next(s for s in found if s["departure"]["city"] == destination)
                return call(ITINERARY_TOOL, title=f"{origin}{destination}往返", plans=[
                    {"title": "去飞回铁", "reason": "去程赶时间坐飞机，回程坐高铁。", "option_ids": [out["option_id"], ret["option_id"]]}])
            return ("排好了，看行程卡。" if ITINERARY_TOOL in done and done[ITINERARY_TOOL].status != "error" else "没排成。"), []
        tool = FLIGHT_TOOL if "航班" in text else TRAIN_TOOL
        if tool not in done:
            extra = {} if tool == FLIGHT_TOOL else {"train_category": "high_speed"}
            return call(tool, origin=origin, destination=destination, departure_date=day, **extra)
        if "接站" in text and found and ROUTE_TOOL not in done:
            arrival = found[0]["arrival"]
            return call(ROUTE_TOOL, origin=arrival["station"], origin_city=arrival["city"], destination="外滩", origin_source="named",
                        destination_source="named", modes=["transit"])
        return ("没查成。" if done[tool].status == "error" else "查好了，看卡片。"), []

    @staticmethod
    def _draw(text: str, messages: list[BaseMessage], turn: list[BaseMessage], call) -> tuple[str, list[dict]]:
        if done := next((m for m in reversed(turn) if isinstance(m, ToolMessage) and m.name == "generate_image"), None):
            return ("没画成。" if done.status == "error" else "画好了，看看要不要调整。"), []
        prompt = text.splitlines()[0][:200]
        ids = _FAKE_IMAGE_ID.findall("\n".join(str(m.content) for m in messages if isinstance(m, ToolMessage) and m.name == "generate_image"))
        if "改图" in text and ids:
            return call("generate_image", prompt=prompt, source_image_id=ids[-1])
        before = [tc["args"] for m in messages if isinstance(m, AIMessage) for tc in m.tool_calls if tc["name"] == "generate_image"]
        if ("再画" in text or "再生成" in text) and before:
            return call("generate_image", **{**before[-1], "regenerate": True})
        return call("generate_image", prompt=prompt, **({"aspect_ratio": "16:9"} if "横" in text else {}))

    @staticmethod
    def _recall(text: str, messages: list[BaseMessage], turn: list[BaseMessage], call) -> tuple[str, list[dict]]:
        """“取回第N轮”取回那一轮；“取回”取回这段对话里第一次搜索的第一个来源。取到了就回复取到的开头。"""
        if done := next((m for m in reversed(turn) if isinstance(m, ToolMessage) and m.name == RECALL), None):
            return f"取回了：{str(done.content)[:40]}", []
        if m := re.search(r"取回第(\d+)轮", text):
            return call(RECALL, turn=int(m.group(1)))
        first = next((m for m in messages if isinstance(m, ToolMessage) and m.name == "search_web" and m.status != "error"), None)
        sid = re.search(r'"id":\s*"(s[0-9a-f]{6,10})"', str(first.content)) if first else None
        return call(RECALL, source_id=sid.group(1)) if sid else ("没有能取回的。", [])

    @staticmethod
    def _delegate(text: str, turn: list[BaseMessage], n: int) -> tuple[str, list[dict]]:
        reports = [m for m in turn if isinstance(m, ToolMessage) and m.name == "task"]
        if reports:
            ids = list(dict.fromkeys(re.findall(r"\[(s[0-9a-f]{6,10})\]", "\n".join(str(m.content) for m in reports))))
            return "汇总：" + ("".join(f"[{i}]" for i in ids[:2]) if ids else "子任务没查到。"), []
        k = int(m.group(1)) if (m := re.search(r"派(\d+)个", text)) else 2 if "分头" in text else 1
        q = text.splitlines()[0][:12]
        return "", [{"name": "task", "args": {"title": f"查{q}{j + 1}", "prompt": f"查一查 {q} 第{j + 1}方面"}, "id": f"call_task_{n}_{j}"}
                    for j in range(k)]

    def _message(self, messages: list[BaseMessage]) -> AIMessage:
        _fake_review(messages)
        content, calls = self._decide(messages)
        if content and not calls and (said := [history.text_of(m) for m in messages[_turn_start(messages):] if history.is_guide(m)]):
            content = f"收到：{'；'.join(said)}。{content}"
        valid = [{**c, "type": "tool_call"} for c in calls if not c.get("invalid")]
        invalid = [{"name": c["name"], "args": c["args"], "id": c["id"], "error": "bad json", "type": "invalid_tool_call"}
                   for c in calls if c.get("invalid")]
        return AIMessage(content=content, tool_calls=valid, invalid_tool_calls=invalid, id=f"{self.prefix}-{len(messages)}-{uuid4().hex[:8]}",
                         additional_kwargs={"reasoning_content": "（假思考）先想一想。"}, usage_metadata=FAKE_USAGE)

    def _ready(self, messages: list[BaseMessage]) -> AIMessage:
        for g in self.gates:
            g.wait(10)  # 10 秒兜底，测试不会永远卡住
        return self._message(messages)

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._ready(messages))])

    async def _agenerate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=await asyncio.to_thread(self._ready, messages))])

    def _chunks(self, msg: AIMessage) -> list[AIMessageChunk]:
        """流式的片段：先吐思考，再分两段吐正文，最后吐工具调用（参数整段）和用量。"""
        half = len(msg.content) // 2
        chunks = [AIMessageChunk(content="", id=msg.id, additional_kwargs={"reasoning_content": msg.additional_kwargs["reasoning_content"]})]
        chunks += [AIMessageChunk(content=part, id=msg.id) for part in (msg.content[:half], msg.content[half:]) if part]
        tcc = [{"name": tc["name"], "args": json.dumps(tc["args"], ensure_ascii=False), "id": tc["id"], "index": k, "type": "tool_call_chunk"}
               for k, tc in enumerate(msg.tool_calls)]
        tcc += [{"name": tc["name"], "args": tc["args"], "id": tc["id"], "index": len(tcc) + k, "type": "tool_call_chunk"}
                for k, tc in enumerate(msg.invalid_tool_calls)]
        chunks.append(AIMessageChunk(content="", id=msg.id, tool_call_chunks=tcc, usage_metadata=FAKE_USAGE, chunk_position="last"))
        return chunks

    def _pieces(self, msg: AIMessage, messages: list[BaseMessage]) -> Iterator[ChatGenerationChunk]:
        text = _turn_text(messages)
        cut = bool(msg.content) and not msg.tool_calls and ("一直断" in text or ("中途断" in text and self._first_cut(messages)))
        for k, c in enumerate(self._chunks(msg)):  # 回调（on_llm_new_token）由 BaseChatModel 发
            if k == 1 and "输出拦截" in text:
                raise ContentBlocked(output=True)
            if k == 2 and cut:
                raise Interrupted("模型服务暂时不可用（连接失败），稍后再试", streamed=True)
            yield ChatGenerationChunk(message=c)

    def _first_cut(self, messages: list[BaseMessage]) -> bool:
        """“中途断”：这一轮（按开始这一轮的那条用户消息）这个模型还没断过。"""
        i = _turn_start(messages)
        key = f"{self.prefix}:{messages[i].id if i >= 0 else ''}"
        if key in _CUT:
            return False
        _CUT.add(key)
        return True

    def _stream(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any) -> Iterator[ChatGenerationChunk]:
        yield from self._pieces(self._ready(messages), messages)

    async def _astream(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any) -> AsyncIterator[ChatGenerationChunk]:
        for c in self._pieces(await asyncio.to_thread(self._ready, messages), messages):
            yield c
            await asyncio.sleep(FAKE_CHUNK_GAP_S)  # 片段之间让出事件循环，像真模型在等网络：点了停止在这里取消得到


FAKE_BLOCKED = "【审核不通过】"
_CUT: set[str] = set()  # 假模型“中途断”已经断过的（模型的前缀:这一轮那条用户消息的 id）
FAKE_REMEMBER = "记住："  # 假模型整理记忆时，用户说的这几个字后面那句记成一条


def _turn_text(messages: list[BaseMessage]) -> str:
    i = _turn_start(messages)
    return str(messages[i].content) if i >= 0 else ""


def _fake_review(messages: list[BaseMessage]) -> None:
    if any(FAKE_BLOCKED in str(m.content) for m in messages):
        raise ContentBlocked(output=False)
    if "查完就拦" in _turn_text(messages) and any(isinstance(m, ToolMessage) and m.name in ("search_web", "read_page") for m in messages):
        raise ContentBlocked(output=False)


def _hits(messages: list[BaseMessage]) -> list[dict]:
    """最近一次成功搜索的结果（压缩换成说明的不算）。"""
    for m in reversed(messages):
        if isinstance(m, ToolMessage) and m.name == "search_web" and m.status != "error":
            try:
                data = json.loads(m.content)
            except (TypeError, ValueError):
                continue
            if (hits := search_hits(data)) is not None:
                return hits
    return []


class FakeResearchPlanner:
    def __init__(self, gateway: GatewayClient | None = None, *, page_repeat: int = 5, search: Any = None, read: Any = None) -> None:
        """search、read 是搜索、读页的 MCP 地址，默认是进程内的假服务（测试换成出错的）；读到的正文是一句话重复 page_repeat 遍。"""
        self.calls: list[tuple[str, Any]] = []
        self.fail_title = False
        self.dream_replies: list[str] = []  # 整理记忆时按顺序给这些回答；用完了照 dream 的说明
        self.images = FakeImageService()
        self.gateway = gateway or self.images.gateway_client()
        self._tools = make_research_tools()
        self._mcp = McpTools(search or fake_search_server, read or (lambda: fake_read_server(page_repeat)), fake_weather_server,
                             self.images.server, fake_maps_server, fake_flyai_server, fake_docs_server)
        self._model = FakeToolModel()
        self._sub_model = FakeToolModel(prefix="fake-sub")

    def agent_model(self) -> BaseChatModel:
        return self._model

    def subagent_model(self) -> BaseChatModel:
        return self._sub_model

    def tools(self) -> list:
        return self._mcp.tools(self._tools)

    async def connect_tools(self) -> None:
        await self._mcp.connect()

    async def title(self, text: str) -> str:
        self.calls.append(("title", text))
        if self.fail_title:
            raise RuntimeError("假模型：起标题失败")
        return f"标题：{text[:8]}"

    async def suggest(self, question: str, reply: str) -> list[str]:
        self.calls.append(("suggest", question))
        return [f"追问{k}：{question[:6]}" for k in (1, 2, 3)]

    async def blocked(self, texts: Sequence[str]) -> list[bool]:
        self.calls.append(("blocked", list(texts)))
        return [FAKE_BLOCKED in t for t in texts]

    async def summarize(self, prompt: str) -> str:
        """短版：交来的那一轮里引用过的来源 id（最多 3 个）；压用户消息时是它的开头。"""
        self.calls.append(("summarize", prompt))
        if "<message>" in prompt:
            return "（短的用户消息）" + prompt.split("<message>")[-1].strip()[:20]
        ids = list(dict.fromkeys(re.findall(r"\[(s[0-9a-f]{6,10})\]", prompt.split("<turn>")[-1])))[:3]
        return "（短版）这一轮查过了。" + "".join(f"[{i}]" for i in ids)

    def dream(self, prompt: str) -> str:
        """新的几轮里用户说的 FAKE_REMEMBER 后面那句记成一条（之前整理过的几轮不看）。"""
        self.calls.append(("dream", prompt))
        if self.dream_replies:
            return self.dream_replies.pop(0)
        new = prompt.split("</title>")[-1].split("</earlier_turns>")[-1]  # 标题是假模型照用户的话起的，也不看
        notes = re.findall(FAKE_REMEMBER + r"([^<\n]+)", new)
        return json.dumps({"operations": [{"action": "add", "text": n.strip()} for n in notes]}, ensure_ascii=False)
