"""查文档：Context7 的两个 MCP 工具，resolve-library-id（按库名找库）和 query-docs（查这个库的文档），经网关 /context7/mcp。

agent-tools 的 context7 服务是 Context7 官方 MCP 的代理：注入密钥、只放这两个工具、被限流时标 rate_limited
（见 agent-tools 的 services/context7/README.md）。工具的说明、参数都是 Context7 的，后面接上灵稿自己的一段
（prompts/tools_web.toml 的 tools.resolve-library-id.lingdraft 等）。连接、取工具这些和天气、搜索共用（见 app.tools.mcp）。
额度：Context7 免费版每月 1000 次（每月初重置，见 Context7 文档 docs/howto/usage 的 Free Plan 那句；和 Fusion 共用一个密钥，
只算这两个工具的调用），
网关给灵稿设了每天 30 次（gateway/.env 的 PRODUCTS）。这里只做灵稿自己的事：
- DocsMiddleware：query-docs 的结果是一段段 Markdown，每段有标题和 Source 那行出处。每段的 Source 下面加一行来源 id，
  模型在正文里用它引用；带来源的那几段另外记在结果的 artifact 里，页面上的来源、压缩后取回都从这里取，不再拆文字
  （见 graph.research.sources.doc_snippets）。找库的结果原样给模型，候选库的 id 记在 artifact 里给过程记录数个数。
- 没做成的：找不到库、找不到文档是 Context7 的原话（isError），照给模型；被 Context7 限流、网关今天的上限到了、
  连不上，给模型灵稿的一句（改用搜索、读官方文档）。密钥不对、网关没放行让这次运行失败（和天气、搜索一样）。
- 连不上时先不带这两个工具（和天气一样），运行器在后台接着试，连上了换上带它们的 agent（见 research_runner）。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx2
from fastmcp import Client
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from app.graph.research import prompts
from app.graph.research.sources import DOCS_TOOL, LIBRARY_TOOL, known_in, source_id, url_key
from app.tools import mcp
from app.tools.gateway import GatewayAuthError, GatewayLimitError

logger = logging.getLogger(__name__)

DOCS_TOOLS = (LIBRARY_TOOL, DOCS_TOOL)
MCP_PATH = "/context7/mcp"
UNAVAILABLE = "unavailable"  # 没调成（网关、服务连不上）时的错误码

# query-docs 结果的样子（2026-11-04 实际调的）：每段 “### 标题”、空行、“Source: 网址”、说明和代码，段与段之间一行 32 个减号
_SEPARATOR = re.compile(r"^(-{8,}[ \t]*)$", re.M)
_SOURCE_LINE = re.compile(r"^Source: (https?://\S+)[ \t]*$", re.M)
_TITLE_LINE = re.compile(r"^#{2,4} (.+)$", re.M)
# resolve-library-id 结果里每个候选库一行
_LIBRARY_LINE = re.compile(r"^- Context7-compatible library ID: (\S+)", re.M)


def gateway_target(base_url: str, api_key: str, *, scene: str = "research",
                   transport: httpx2.AsyncBaseTransport | None = None) -> Client:
    return mcp.gateway_target(base_url, api_key, MCP_PATH, scene=scene, transport=transport)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def failed(name: str, call_id: str, code: str) -> ToolMessage:
    """中间件自己回的失败：没调成、网关今天的上限到了、被 Context7 限流。"""
    return ToolMessage(content=prompts.render(f"tools.docs.{code}"), artifact={"error": {"code": code}},
                       tool_call_id=call_id, name=name, status="error")


def with_ids(text: str, known: dict[str, str]) -> tuple[str, list[dict]]:
    """每段文档的 Source 那行下面加一行来源 id；返回加好的文字和带来源的那几段（id、url、title、content）。
    同一个网址的几段是同一个 id；之前搜到、读过的网址沿用原来的 id。没有 Source 行、出处不是网址的段原样留着、不算来源。"""
    parts = _SEPARATOR.split(text)  # 段、分隔行、段……
    snippets = []
    for k in range(0, len(parts), 2):
        chunk = parts[k]
        if (src := _SOURCE_LINE.search(chunk)) is None:
            continue
        url = src.group(1)
        sid = known[url_key(url)] = source_id(url, known)
        title = t.group(1).strip() if (t := _TITLE_LINE.search(chunk[:src.start()])) else ""
        snippets.append({"id": sid, "url": url, "title": title, "content": chunk[src.end():].strip()})
        parts[k] = f"{chunk[:src.end()]}\nSource id: {sid}{chunk[src.end():]}"
    return "".join(parts), snippets


class DocsMiddleware(AgentMiddleware):
    """只管 resolve-library-id、query-docs，见模块说明。放在 ToolProgressMiddleware 里面：过程记录看到的是整理好的结果。"""

    async def awrap_tool_call(self, request, handler):
        call = request.tool_call
        name = call["name"]
        if name not in DOCS_TOOLS:
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
        if result.status == "error":
            data = mcp.structured(result)
            err = data.get("error") if isinstance(data, dict) and isinstance(data.get("error"), dict) else {}
            if err.get("code") == "rate_limited":  # context7 服务认出来的 Context7 限流（这个月的额度用完也是这样）
                logger.warning("%s：Context7 限流了：%s", name, text[:200])
                return failed(name, call["id"], "rate_limited")
            # 找不到库、找不到文档（Context7 的原话），或者别的错：照给模型
            return result.model_copy(update={"content": text, "status": "error", "artifact": {"error": {"code": "tool_error"}}})
        if name == LIBRARY_TOOL:
            return result.model_copy(update={"content": text, "status": "success",
                                             "artifact": {"retrieved_at": _now(), "libraries": _LIBRARY_LINE.findall(text)}})
        body, snippets = with_ids(text, known_in(request.state))
        return result.model_copy(update={"content": body, "status": "success", "artifact": {"retrieved_at": _now(), "snippets": snippets}})


def docs_tools(target: Callable[[], Any]) -> list[mcp.McpTool]:
    """两个工具在同一个服务里，共用一个地址。"""
    return [mcp.McpTool(LIBRARY_TOOL, target), mcp.McpTool(DOCS_TOOL, target)]
