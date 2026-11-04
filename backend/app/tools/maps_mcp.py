"""查地点、比路线：agent-tools 的 MCP 工具 place_search、route_compare（经网关 /maps/mcp）。

地名怎么定、重名怎么问、高德不行转腾讯、结果的样子、失败时给模型的说明都在 maps-service 里，Fusion 和灵稿共用
（见 agent-tools 的 services/maps/README.md）。说明后面接上灵稿自己的一段（prompts.toml 的 tools.place_search.lingdraft 等）。
这里只做灵稿自己的事：
- MapsMiddleware：给模型的是服务的那段文字（已经去掉图片、链接、服务商），没查成的 status=error；
  整份结果留在 artifact 的 structured_content 里，history 把它换成地点卡、路线卡，再记一下查的时间。
- 网关、服务连不上算 unavailable；网关今天的上限到了（429）算 quota_exhausted；
  密钥不对、网关没放行让这次运行失败，把配置问题亮出来（和搜索、天气、生图一样）。
- 还没连上时用同名的占位工具：调了就说地图服务暂时连不上，模型照实告诉用户，不会凭记忆编地点、路线。
  运行器在后台接着连，连上了换上真的（见 research_runner）。
- 灵稿现在拿不到用户的位置（没有浏览器定位），不往 MCP 的 _meta 里传；说明里让模型不用 current_location、问用户在哪。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx2
from fastmcp import Client
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from app.graph.research import prompts
from app.tools import mcp
from app.tools.gateway import GatewayAuthError, GatewayLimitError

logger = logging.getLogger(__name__)

PLACE_TOOL = "place_search"
ROUTE_TOOL = "route_compare"
MAPS_TOOLS = (PLACE_TOOL, ROUTE_TOOL)
MCP_PATH = "/maps/mcp"
UNAVAILABLE = "unavailable"  # 没调成（网关、服务连不上，或者还没连上）时的错误码


def gateway_target(base_url: str, api_key: str, *, scene: str = "research",
                   transport: httpx2.AsyncBaseTransport | None = None) -> Client:
    return mcp.gateway_target(base_url, api_key, MCP_PATH, scene=scene, transport=transport)


class ToolOffline(RuntimeError):
    """占位工具被调了：地图服务还没连上。"""


class _PlaceArgs(BaseModel):
    query: str = Field(description="What to search for.")


class _RouteArgs(BaseModel):
    origin: str = Field(description="Where the trip starts.")
    destination: str = Field(description="Where the trip ends.")


def placeholder(name: str) -> BaseTool:
    """没连上时的同名工具：参数只留最要紧的（别的照样能传，不用），调了抛 ToolOffline，中间件按 unavailable 回给模型。"""

    async def offline(**_: Any) -> str:
        raise ToolOffline(f"{name} 还没连上")

    return StructuredTool.from_function(
        coroutine=offline, name=name, args_schema=_PlaceArgs if name == PLACE_TOOL else _RouteArgs,
        description=f"{prompts.render(f'tools.{name}.brief')}\n\n{prompts.render(f'tools.{name}.lingdraft')}",
    )


class MapsTool(mcp.McpTool):
    def __init__(self, name: str, target: Callable[[], Any]) -> None:
        super().__init__(name, target)
        self._placeholder = placeholder(name)

    def current(self) -> BaseTool:
        return self.tool or self._placeholder


def failed(name: str, call_id: str, code: str) -> ToolMessage:
    """中间件自己回的失败（服务没给结果）：没调成，或者网关今天的上限到了。"""
    return ToolMessage(content=prompts.render(f"tools.{name}.{code}"),
                       artifact={"structured_content": {"ok": False, "error": {"code": code, "retryable": code == UNAVAILABLE}}},
                       tool_call_id=call_id, name=name, status="error")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class MapsMiddleware(AgentMiddleware):
    """只管 place_search、route_compare，见模块说明。放在 ToolProgressMiddleware 里面：过程记录看到的是整理好的结果。"""

    async def awrap_tool_call(self, request, handler):
        call = request.tool_call
        name = call["name"]
        if name not in MAPS_TOOLS:
            return await handler(request)
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
            # 服务那边抛了没接住的错（MCP 的 isError，比如参数没过校验）：服务给的说明照给模型
            logger.warning("%s 出错：%s", name, text[:200])
            error = {"ok": False, "error": {"code": "tool_error", "retryable": False}}
            return result.model_copy(update={"content": text, "status": "error", "artifact": {"structured_content": error}})
        ok = data.get("ok") is True
        return result.model_copy(update={"content": text, "status": "success" if ok else "error",
                                         "artifact": {"structured_content": data, "retrieved_at": _now()}})
