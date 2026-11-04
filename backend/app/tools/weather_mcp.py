"""查天气：agent-tools 的 MCP 工具 get_weather（经网关 /weather/mcp），用 LangChain 的 MCP 适配器（langchain.mcp）接进来。

挑城市、按当地日期整理预报、逐小时摘要、缓存、给模型的说明都在 weather-service 里，Fusion 和灵稿共用
（见 agent-tools 的 services/weather/README.md）。连接、取工具这些和搜索、读页共用（见 app.tools.mcp）。这里只做灵稿自己的事：
- 工具说明后面接上灵稿的一段（天气卡片、查不到时用哪个工具，见 prompts.toml 的 [tools.get_weather]）。
  连不上时先不带天气，运行器在后台接着试，连上了换上带天气的 agent（见 research_runner）。
- WeatherMiddleware：结果变成一段文字给模型，没查成的 status=error；网关、服务出问题算“没查到”，
  密钥不对或网关没放行让这次运行失败，把配置问题亮出来（和搜索、生图一样）。
- 页面要的整份预报在结果的 artifact 里（structured_content），history 把它换成天气卡片。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import httpx2
from fastmcp import Client
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from app.graph.research import prompts
from app.tools import mcp
from app.tools.gateway import GatewayAuthError

logger = logging.getLogger(__name__)

WEATHER_TOOL = "get_weather"
MCP_PATH = "/weather/mcp"
# 没调成（网关、服务出问题）时结果里的错误码；页面上显示成“天气服务暂时查不到”
UNAVAILABLE_CODE = "service_unavailable"


def gateway_target(base_url: str, api_key: str, *, scene: str = "research",
                   transport: httpx2.AsyncBaseTransport | None = None) -> Client:
    return mcp.gateway_target(base_url, api_key, MCP_PATH, scene=scene, transport=transport)


class WeatherTool(mcp.McpTool):
    def __init__(self, target: Callable[[], Any]) -> None:
        super().__init__(WEATHER_TOOL, target)


def failed(call_id: str, code: str = UNAVAILABLE_CODE) -> ToolMessage:
    return ToolMessage(
        content=prompts.render("tools.get_weather.unavailable"),
        artifact={"structured_content": {"ok": False, "error": {"code": code, "retryable": False}}},
        tool_call_id=call_id, name=WEATHER_TOOL, status="error",
    )


class WeatherMiddleware(AgentMiddleware):
    """只管 get_weather 这一个工具，见模块说明。放在 ToolProgressMiddleware 里面：过程记录看到的是整理好的结果。"""

    async def awrap_tool_call(self, request, handler):
        call = request.tool_call
        if call["name"] != WEATHER_TOOL:
            return await handler(request)
        try:
            result = await handler(request)
        except Exception as e:
            if (task := asyncio.current_task()) is not None and task.cancelling():
                raise  # 点了停止：照常取消
            if auth := mcp.find_error(e, GatewayAuthError):
                raise auth from e
            logger.warning("查天气没调成：%s: %s", type(e).__name__, str(e)[:200])
            return failed(call["id"])
        if not isinstance(result, ToolMessage):
            return result
        data = mcp.structured(result)
        ok = result.status != "error" and isinstance(data, dict) and data.get("ok") is True
        return result.model_copy(update={"content": mcp.text_of(result.content), "status": "success" if ok else "error"})
