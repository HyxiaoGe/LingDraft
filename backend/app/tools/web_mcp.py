"""搜索、读网页：agent-tools 的 MCP 工具 search_web（经网关 /search/mcp）和 read_page（/reader/mcp）。

怎么搜、怎么读、结果的样子、失败时给模型的说明都在服务里，Fusion 和灵稿共用（见 agent-tools 的 services/search、
services/reader 的 README）。说明后面接上灵稿自己的一段（prompts/tools_web.toml 的 tools.search_web.lingdraft 等）。这里只做灵稿自己的事：
- WebToolsMiddleware：每条搜索结果、读到的那一页带上来源 id（见 graph.research.sources），模型在正文里用它引用；
  服务给的 meta（哪家搜到、读到的）不给模型，记在结果的 artifact 里。
- 没做成的：给模型的是服务的那句英文，status=error，错误码在 artifact 里（页面上换成中文，见 history.SEARCH_FAILURES）。
  网关、服务连不上算 unavailable；网关今天的上限到了（429）和服务的额度用完一样算 quota_exhausted；
  密钥不对、网关没放行让这次运行失败，把配置问题亮出来（和天气、生图一样）。
- 还没连上时用同名的占位工具：调了就说服务暂时连不上，模型照实告诉用户，不会当成没有搜索、凭记忆回答。
  运行器在后台接着连，连上了换上真的（见 research_runner）。
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
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from app.graph.research import prompts
from app.graph.research.sources import SEPARATORS, known_in, source_id, url_key
from app.tools import mcp
from app.tools.gateway import GatewayAuthError, GatewayLimitError

logger = logging.getLogger(__name__)

SEARCH_TOOL = "search_web"
READ_TOOL = "read_page"
WEB_TOOLS = (SEARCH_TOOL, READ_TOOL)
MCP_PATHS = {SEARCH_TOOL: "/search/mcp", READ_TOOL: "/reader/mcp"}
UNAVAILABLE = "unavailable"  # 没调成（网关、服务连不上，或者还没连上）时的错误码


def gateway_target(name: str, base_url: str, api_key: str, *, scene: str = "research",
                   transport: httpx2.AsyncBaseTransport | None = None) -> Client:
    return mcp.gateway_target(base_url, api_key, MCP_PATHS[name], scene=scene, transport=transport)


class ToolOffline(RuntimeError):
    """占位工具被调了：服务还没连上。"""


class _QueryArgs(BaseModel):
    query: str = Field(description="The search query.")


class _UrlArgs(BaseModel):
    url: str = Field(description="The complete http:// or https:// URL to read.")


def placeholder(name: str) -> BaseTool:
    """没连上时的同名工具：参数只留最要紧的一个（别的照样能传，不用），调了抛 ToolOffline，中间件按 unavailable 回给模型。"""

    async def offline(**_: Any) -> str:
        raise ToolOffline(f"{name} 还没连上")

    return StructuredTool.from_function(
        coroutine=offline, name=name, args_schema=_QueryArgs if name == SEARCH_TOOL else _UrlArgs,
        description=f"{prompts.render(f'tools.{name}.brief')}\n\n{prompts.render(f'tools.{name}.lingdraft')}",
    )


class WebTool(mcp.McpTool):
    def __init__(self, name: str, target: Callable[[], Any]) -> None:
        super().__init__(name, target)
        self._placeholder = placeholder(name)

    def current(self) -> BaseTool:
        return self.tool or self._placeholder


def failed(name: str, call_id: str, code: str) -> ToolMessage:
    """中间件自己回的失败（服务没给结果）：没调成，或者网关今天的上限到了。"""
    return ToolMessage(content=prompts.render(f"tools.{name}.{code}"), artifact={"error": {"code": code, "retryable": code == UNAVAILABLE}},
                       tool_call_id=call_id, name=name, status="error")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def with_ids(name: str, data: dict, state: dict, asked_url: str) -> dict:
    """服务的结果去掉 meta、带上来源 id。搜索结果里每条一个（同一批里重复的网址也是同一个 id），读页是那一页的。"""
    seen = {k: v for k, v in data.items() if k != "meta"}
    known = known_in(state)
    if name == READ_TOOL:
        return {"id": source_id(str(seen.get("url") or asked_url).strip(), known), **seen}
    hits = []
    for h in seen.get("results") or []:
        url = str(h.get("url") or "").strip() if isinstance(h, dict) else ""
        if url:
            sid = known[url_key(url)] = source_id(url, known)
            hits.append({"id": sid, **h})
    return {**seen, "results": hits}


class WebToolsMiddleware(AgentMiddleware):
    """只管 search_web、read_page，见模块说明。放在 ToolProgressMiddleware 里面：过程记录看到的是整理好的结果。"""

    async def awrap_tool_call(self, request, handler):
        call = request.tool_call
        name = call["name"]
        if name not in WEB_TOOLS:
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
            # 服务那边抛了没接住的错（MCP 的 isError，比如参数类型不对没过校验）：服务给的说明照给模型
            logger.warning("%s 出错：%s", name, text[:200])
            return result.model_copy(update={"content": text, "status": "error", "artifact": {"error": {"code": "tool_error", "retryable": False}}})
        if data.get("ok") is not True:
            err = data.get("error") if isinstance(data.get("error"), dict) else {}
            error = {k: err[k] for k in ("code", "retryable", "status") if k in err}
            return result.model_copy(update={"content": text, "status": "error", "artifact": {"error": {"code": "tool_error", **error}}})
        body = with_ids(name, data, request.state, str((call.get("args") or {}).get("url") or ""))
        return result.model_copy(update={"content": json.dumps(body, ensure_ascii=False, separators=SEPARATORS), "status": "success",
                                         "artifact": {"retrieved_at": _now(), "meta": data.get("meta")}})
