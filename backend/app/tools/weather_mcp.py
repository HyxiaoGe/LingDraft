"""查天气：agent-tools 的 MCP 工具 get_weather（经网关 /weather/mcp），用 LangChain 的 MCP 适配器（langchain.mcp）接进来。

挑城市、按当地日期整理预报、逐小时摘要、缓存、给模型的说明都在 weather-service 里，Fusion 和灵稿共用
（见 agent-tools 的 services/weather/README.md）。这里只做灵稿自己的事：
- 取工具（WeatherTool.connect）：工具说明后面接上灵稿的一段（天气卡片、查不到时用哪个工具，见 prompts.toml 的
  [tools.get_weather]）。连不上时先不带天气，运行器在后台接着试，连上了换上带天气的 agent（见 research_runner）。
- WeatherMiddleware：结果变成一段文字给模型，没查成的 status=error；网关、服务出问题算“没查到”，
  密钥不对或网关没放行让这次运行失败，把配置问题亮出来（和搜索、生图一样）。
- 页面要的整份预报在结果的 artifact 里（structured_content），history 把它换成天气卡片。

每次调用单独连一次（无状态，三个请求）：一直连着并不省请求（11-04 试过），服务、网关重启了也不用管重连。
MCP 客户端出错时只给一个笼统的 MCPError，看不出 HTTP 状态，所以 401/403 在 HTTP 客户端的钩子里认出来（见 gateway_target）。
"""

from __future__ import annotations

import asyncio
import logging
import warnings
from collections.abc import Callable
from typing import Any

import httpx2
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from langchain.agents.middleware import AgentMiddleware
from langchain_core._api.beta_decorator import LangChainBetaWarning
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool

from app.graph.research import prompts
from app.tools.gateway import GatewayAuthError

with warnings.catch_warnings():  # langchain.mcp 还是 beta，导入时警告一次；知道了，不打进日志
    warnings.simplefilter("ignore", LangChainBetaWarning)
    from langchain.mcp import MCPAdapter

logger = logging.getLogger(__name__)

WEATHER_TOOL = "get_weather"
MCP_PATH = "/weather/mcp"
# 一次调用最多等这么久：weather-service 自己限 20 秒，网关转发限 30 秒
CALL_TIMEOUT = 40.0
# 没调成（网关、服务出问题）时结果里的错误码；页面上显示成“天气服务暂时查不到”
UNAVAILABLE_CODE = "service_unavailable"


def gateway_target(base_url: str, api_key: str, *, scene: str = "research",
                   transport: httpx2.AsyncBaseTransport | None = None) -> Client:
    """经网关连 weather-service 的 MCP。transport 给测试用（httpx2 是 MCP 库用的 httpx 分支）。"""

    async def check(response: httpx2.Response) -> None:
        if response.status_code in (401, 403):  # 网关认不出密钥、或者没给灵稿放行天气
            raise GatewayAuthError(f"查天气失败：HTTP {response.status_code}", kind=f"http_{response.status_code}")

    def factory(headers: dict[str, str] | None = None, timeout: httpx2.Timeout | None = None, auth: httpx2.Auth | None = None,
                follow_redirects: bool = True) -> httpx2.AsyncClient:
        # 没给 timeout 时 httpx 默认只等 5 秒；照 MCP 库自己的默认值（一次调用多久由 Client 的 timeout 管）
        return httpx2.AsyncClient(headers=headers, timeout=timeout or httpx2.Timeout(30.0, read=300.0), auth=auth,
                                  follow_redirects=follow_redirects, transport=transport, event_hooks={"response": [check]})

    url = base_url.rstrip("/") + MCP_PATH
    headers = {"Authorization": f"Bearer {api_key}", "X-Caller-Scene": scene}
    return Client(StreamableHttpTransport(url, headers=headers, httpx_client_factory=factory), timeout=CALL_TIMEOUT)


class WeatherTool:
    """get_weather 这个工具。connect 连上 MCP 取到以后 tool 才有；target 是 MCP 的地址（网关的客户端，或者进程内的假服务）。"""

    def __init__(self, target: Callable[[], Any]) -> None:
        self._target = target
        self.tool: BaseTool | None = None

    async def connect(self) -> None:
        """取工具；连不上、没有这个工具都照常抛出，由调用方决定再试还是先不带天气。"""
        if self.tool is not None:
            return
        tools = await MCPAdapter(self._target()).list_tools()
        found = next((t for t in tools if t.name == WEATHER_TOOL), None)
        if found is None:
            raise RuntimeError(f"天气的 MCP 里没有 {WEATHER_TOOL}（有 {[t.name for t in tools]}）")
        found.description = f"{found.description}\n\n{prompts.render('tools.get_weather.lingdraft')}"
        self.tool = found


def _auth_error(error: BaseException) -> GatewayAuthError | None:
    """MCP 客户端把钩子里抛的错包了好几层（RuntimeError、ExceptionGroup……），顺着找出来。"""
    seen: set[int] = set()
    todo = [error]
    while todo:
        e = todo.pop()
        if id(e) in seen:
            continue
        seen.add(id(e))
        if isinstance(e, GatewayAuthError):
            return e
        todo.extend(x for x in (e.__cause__, e.__context__) if x is not None)
        if isinstance(e, BaseExceptionGroup):
            todo.extend(e.exceptions)
    return None


def _text(content: Any) -> str:
    """MCP 的结果是一串内容块，变成一段文字（模型看的、存下来的都是它）。"""
    if isinstance(content, str):
        return content
    parts = [b.get("text", "") if isinstance(b, dict) else str(b) for b in content or []]
    return "\n".join(p for p in parts if p)


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
            if auth := _auth_error(e):
                raise auth from e
            logger.warning("查天气没调成：%s: %s", type(e).__name__, str(e)[:200])
            return failed(call["id"])
        if not isinstance(result, ToolMessage):
            return result
        art = result.artifact if isinstance(result.artifact, dict) else {}
        data = art.get("structured_content")
        ok = result.status != "error" and isinstance(data, dict) and data.get("ok") is True
        return result.model_copy(update={"content": _text(result.content), "status": "success" if ok else "error"})
