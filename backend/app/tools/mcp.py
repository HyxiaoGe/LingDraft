"""agent-tools 的 MCP 工具（天气、搜索、读页、生图）共用的部分：经网关连、取工具、认出网关的错误、结果变成文字。

工具本身（说明、参数、结果、错误码）都在 agent-tools 的服务里，Fusion 和灵稿共用；这里只接进来，
说明后面接上灵稿自己的一段（prompts/ 里的 tools.<工具>.lingdraft）。

每次调用单独连一次（无状态，三个请求）：一直连着并不省请求（11-04 试过），服务、网关重启了也不用管重连。
MCP 客户端出错时只给一个笼统的 MCPError，看不出 HTTP 状态，所以网关的 401/403、429 在 HTTP 客户端的钩子里认出来：
- 401/403：密钥不对、网关没放行，配置问题，让这次运行失败、把错误亮出来；
- 429：网关的每日上限（SERVICE_DAILY_LIMITS），今天再调也一样，工具按“额度用完”告诉模型。

随这一次调用带给服务、不经过模型的东西（MCP 请求的 _meta，比如用户的当前位置，见 app.tools.location）用 request_meta 包住这次调用：
LangChain 的适配器调工具时不带 _meta，所以带着 _meta 的这一次不走它，直接用 FastMCP 的客户端调，结果整理成和它一样的样子。
"""

from __future__ import annotations

import warnings
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import httpx2
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from langchain_core._api.beta_decorator import LangChainBetaWarning
from langchain_core.tools import BaseTool

from app.graph.research import prompts
from app.tools.gateway import GatewayAuthError, GatewayLimitError

with warnings.catch_warnings():  # langchain.mcp 还是 beta，导入时警告一次；知道了，不打进日志
    warnings.simplefilter("ignore", LangChainBetaWarning)
    from langchain.mcp import MCPAdapter

_META: ContextVar[dict[str, Any] | None] = ContextVar("mcp_request_meta", default=None)


@contextmanager
def request_meta(meta: dict[str, Any]) -> Iterator[None]:
    """这里面调的 MCP 工具，请求的 _meta 带上 meta（见模块说明）。不进工具的参数：模型看不到，过程记录、检查点里也没有。"""
    token = _META.set(meta)
    try:
        yield
    finally:
        _META.reset(token)


# 一次调用最多等这么久：服务自己限时（天气 20 秒、搜索 25 秒、读页 12 秒），网关转发限 30 秒。生图另外给（见 image_mcp）
CALL_TIMEOUT = 40.0


def gateway_target(base_url: str, api_key: str, path: str, *, scene: str = "research",
                   transport: httpx2.AsyncBaseTransport | None = None, timeout: float = CALL_TIMEOUT) -> Client:
    """经网关连一个服务的 MCP（path 如 /weather/mcp）。transport 给测试用（httpx2 是 MCP 库用的 httpx 分支）。"""

    async def check(response: httpx2.Response) -> None:
        if response.status_code in (401, 403):  # 网关认不出密钥、或者没给灵稿放行这个服务
            raise GatewayAuthError(f"调 {path} 失败：HTTP {response.status_code}", kind=f"http_{response.status_code}")
        if response.status_code == 429:  # 网关的每日上限；服务自己的失败是 200 里的 ok=false
            raise GatewayLimitError(f"调 {path} 失败：网关今天的上限到了")

    def factory(headers: dict[str, str] | None = None, timeout: httpx2.Timeout | None = None, auth: httpx2.Auth | None = None,
                follow_redirects: bool = True) -> httpx2.AsyncClient:
        # 没给 timeout 时 httpx 默认只等 5 秒；照 MCP 库自己的默认值（一次调用多久由 Client 的 timeout 管）
        return httpx2.AsyncClient(headers=headers, timeout=timeout or httpx2.Timeout(30.0, read=300.0), auth=auth,
                                  follow_redirects=follow_redirects, transport=transport, event_hooks={"response": [check]})

    url = base_url.rstrip("/") + path
    headers = {"Authorization": f"Bearer {api_key}", "X-Caller-Scene": scene}
    return Client(StreamableHttpTransport(url, headers=headers, httpx_client_factory=factory), timeout=timeout)


class McpTool:
    """一个 MCP 工具。connect 连上取到以后 tool 才有；target 是 MCP 的地址（网关的客户端，或者进程内的假服务）。"""

    def __init__(self, name: str, target: Callable[[], Any]) -> None:
        self.name = name
        self._target = target
        self.tool: BaseTool | None = None
        self.wrap: Callable[[BaseTool], BaseTool] | None = None  # 连上以后再包一层，评测录像用（见 app.evals.tape）

    async def connect(self) -> None:
        """取工具；连不上、没有这个工具都照常抛出，由调用方决定再试还是先不带它。"""
        if self.tool is not None:
            return
        tools = await MCPAdapter(self._target()).list_tools()
        found = next((t for t in tools if t.name == self.name), None)
        if found is None:
            raise RuntimeError(f"MCP 里没有 {self.name}（有 {[t.name for t in tools]}）")
        found.description = f"{found.description}\n\n{prompts.render(f'tools.{self.name}.lingdraft')}"
        found.coroutine = self._with_meta(found.coroutine)  # type: ignore[attr-defined]
        self.tool = self.wrap(found) if self.wrap else found

    def _with_meta(self, plain: Callable[..., Any]) -> Callable[..., Any]:
        """有 request_meta 的这一次自己调（带上 _meta），别的照常走适配器的。结果和适配器给的一样：内容块的文字、artifact 里整份结果；
        服务说出错了的（isError）只有文字、没有整份结果，各工具的中间件按没调成处理。"""

        async def call(**arguments: Any) -> Any:
            meta = _META.get()
            if not meta:
                return await plain(**arguments)
            target = self._target()
            client = target if isinstance(target, Client) else Client(target)
            async with client:
                result = await client.call_tool(self.name, arguments, raise_on_error=False, meta=meta)
            blocks = [{"type": "text", "text": b.text} for b in result.content if getattr(b, "type", "") == "text"]
            whole = result.structured_content
            return blocks, ({"structured_content": whole} if whole is not None and not result.is_error else None)

        return call


def find_error[E: BaseException](error: BaseException, kind: type[E]) -> E | None:
    """MCP 客户端把钩子里抛的错包了好几层（RuntimeError、ExceptionGroup……），顺着找出 kind 这一种。"""
    seen: set[int] = set()
    todo = [error]
    while todo:
        e = todo.pop()
        if id(e) in seen:
            continue
        seen.add(id(e))
        if isinstance(e, kind):
            return e
        todo.extend(x for x in (e.__cause__, e.__context__) if x is not None)
        if isinstance(e, BaseExceptionGroup):
            todo.extend(e.exceptions)
    return None


def text_of(content: Any) -> str:
    """MCP 的结果是一串内容块，变成一段文字（模型看的、存下来的都是它）。"""
    if isinstance(content, str):
        return content
    parts = [b.get("text", "") if isinstance(b, dict) else str(b) for b in content or []]
    return "\n".join(p for p in parts if p)


def structured(result: Any) -> Any:
    """工具结果的 artifact 里 MCP 的 structuredContent（整份结果）；没有是 None。"""
    art = getattr(result, "artifact", None)
    return art.get("structured_content") if isinstance(art, dict) else None
