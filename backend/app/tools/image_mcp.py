"""生图：agent-tools 的 MCP 工具 generate_image（经网关 /image/v2/mcp）。

出图、存图、改图找原图、比例、缓存、给模型的说明都在 image-service 里，Fusion 和灵稿共用（见 agent-tools 的
services/image/README.md「产品用的生图工具」）。图存在服务里、归灵稿，结果里只有图片编号，没有地址；
页面要看时灵稿后端按编号经网关取（见 api.main），删对话、重新回答时按编号删（见 api.research_runner）。
工具说明后面接上灵稿的一段（prompts.toml 的 tools.generate_image.lingdraft）。这里只做灵稿自己的事（ImageMiddleware）：
- 每轮最多画 MAX_IMAGES_PER_TURN 张。照 Fusion：发出去就可能花了钱，没画成的也算；参数不对、找不到原图的服务没去画，不算。
- 改图只认这段对话里画的、用户发的、还在服务里的图：别的编号（编的、别的对话的、以前灵稿自己存的）不交给服务，照找不到原图告诉模型。
- 用户发的图（见 history.attached_images）：怎么给模型（看得见的直接看、看不见的调看图工具，说明里都有编号、要改就当 source_image_id）
  见 app.tools.vision，在 agent.ModelSelect 里按这次用的模型组织。
- 画成的：卡片要的东西（编号、大小、模型、描述）记在结果的 artifact 里，history 把它换成图片卡片；
  给模型的是服务的那段英文，后面接一句卡片上已经标了模型。
- 没画成：服务的那段英文照给模型，status=error，错误码在 artifact 里（页面上换成中文，见 history.IMAGE_FAILURES）。
  网关、服务连不上算 unavailable；网关今天的上限到了（429）算 quota_exhausted；
  密钥不对、网关没放行让这次运行失败，把配置问题亮出来（和搜索、天气一样）。
- 还没连上时用同名的占位工具（和搜索、读页一样）：调了说服务暂时连不上，运行器在后台接着连。
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable
from typing import Any

import httpx2
from fastmcp import Client
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.graph.research import prompts
from app.graph.research.history import IMAGE_TOOL, last_human, owned_image_ids
from app.graph.research.state import GeneratedImage
from app.tools import mcp
from app.tools.gateway import GatewayAuthError, GatewayLimitError
from app.tools.web_mcp import ToolOffline

logger = logging.getLogger(__name__)

MCP_PATH = "/image/v2/mcp"
# image-service 整条主备链限时 135 秒（含找参考图、各模型尝试），超时自己回 upstream_timeout；这里留出传输余量（照 Fusion）。网关转发限 180 秒
CALL_TIMEOUT = 150.0
MAX_IMAGES_PER_TURN = 3
UNAVAILABLE = "unavailable"
QUOTA = "quota_exhausted"
# 服务没去画（不会花钱）的错误码：不算这一轮画了几次
NOT_ATTEMPTED = {"invalid_params", "source_image_unavailable", "unauthorized"}


def gateway_target(base_url: str, api_key: str, *, scene: str = "research",
                   transport: httpx2.AsyncBaseTransport | None = None) -> Client:
    return mcp.gateway_target(base_url, api_key, MCP_PATH, scene=scene, transport=transport, timeout=CALL_TIMEOUT)


class _PromptArgs(BaseModel):
    prompt: str = Field(description="Detailed visual description of the image.")


def placeholder() -> BaseTool:
    """没连上时的同名工具：参数只留 prompt（别的照样能传，不用），调了抛 ToolOffline，中间件按 unavailable 回给模型。"""

    async def offline(**_: Any) -> str:
        raise ToolOffline(f"{IMAGE_TOOL} 还没连上")

    return StructuredTool.from_function(
        coroutine=offline, name=IMAGE_TOOL, args_schema=_PromptArgs,
        description=f"{prompts.render('tools.generate_image.brief')}\n\n{prompts.render('tools.generate_image.lingdraft')}",
    )


class ImageTool(mcp.McpTool):
    def __init__(self, target: Callable[[], Any]) -> None:
        super().__init__(IMAGE_TOOL, target)
        self._placeholder = placeholder()

    def current(self) -> BaseTool:
        return self.tool or self._placeholder


class _ServiceImage(BaseModel):
    """服务结果里的 image（见 image-service 的 StoredImage）；只取卡片要的。"""

    model_config = ConfigDict(extra="ignore")
    id: str = Field(min_length=1, max_length=64)
    mime_type: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    model: str | None = None
    edited_from: str | None = None


def attempts_before(messages: list[BaseMessage], call_id: str | None) -> int:
    """这一轮里排在这次调用前面、真去画了的生图调用有几次（没画成也算，照 Fusion：发出去就可能花了钱）。
    参数不对、找不到要改的图、超了次数的没去画，不算。同一条模型消息里并行的几次还没有结果，都算去画了：宁可少画一张，不多花钱。"""
    turn = messages[last_human(messages) + 1:]
    results = {m.tool_call_id: m for m in turn if isinstance(m, ToolMessage)}
    n = 0
    for m in turn:
        if not isinstance(m, AIMessage):
            continue
        for tc in m.tool_calls:
            if tc["name"] != IMAGE_TOOL:
                continue
            if tc.get("id") == call_id:
                return n
            r = results.get(tc.get("id"))
            if r is None or (isinstance(r.artifact, dict) and r.artifact.get("attempted")):
                n += 1
    return n


def ratio_of(width: int, height: int) -> str:
    """没要比例时卡片按图的实际形状占位。"""
    g = math.gcd(width, height)
    return f"{width // g}:{height // g}"


def failed(call_id: str, code: str, *, attempted: bool) -> ToolMessage:
    """中间件自己回的失败（服务没给结果，或者没交给服务）。"""
    text = prompts.render(f"tools.{IMAGE_TOOL}.{code}", max_per_turn=MAX_IMAGES_PER_TURN)
    return ToolMessage(content=text, artifact={"attempted": attempted, "error_code": code}, tool_call_id=call_id, name=IMAGE_TOOL,
                       status="error")


class ImageMiddleware(AgentMiddleware):
    """只管 generate_image，见模块说明。放在 ToolProgressMiddleware 里面：过程记录看到的是整理好的结果。"""

    async def awrap_tool_call(self, request, handler):
        call = request.tool_call
        if call["name"] != IMAGE_TOOL:
            return await handler(request)
        cid, args = call["id"], call.get("args") or {}
        msgs = request.state.get("messages", [])
        source = args.get("source_image_id")
        source = source.strip() if isinstance(source, str) else ""
        if source and source not in owned_image_ids(msgs):
            # 不交给服务：编号是编的、别的对话的、以前的图，都照找不到原图说，不算画了一次
            return failed(cid, "source_image_unavailable", attempted=False)
        if attempts_before(msgs, cid) >= MAX_IMAGES_PER_TURN:
            return failed(cid, "run_image_limit_reached", attempted=False)
        try:
            result = await handler(request)
        except Exception as e:
            if (task := asyncio.current_task()) is not None and task.cancelling():
                raise  # 点了停止：照常取消，在等的请求跟着断开，出到一半的图不要了
            if auth := mcp.find_error(e, GatewayAuthError):
                raise auth from e
            if mcp.find_error(e, GatewayLimitError):
                logger.warning("生图：网关今天的上限到了")
                return failed(cid, QUOTA, attempted=False)
            logger.warning("生图没调成：%s: %s", type(e).__name__, str(e)[:200])
            # 还没连上的没发出去；发出去了却没拿到结果的，服务那边可能已经画了
            return failed(cid, UNAVAILABLE, attempted=not mcp.find_error(e, ToolOffline))
        if not isinstance(result, ToolMessage):
            return result
        text = mcp.text_of(result.content)
        data = mcp.structured(result)
        if result.status == "error" or not isinstance(data, dict):
            # MCP 的 isError：参数没过服务的校验（比如比例不在可选值里），服务没去画；服务给的说明照给模型
            logger.warning("生图参数不对：%s", text[:200])
            return result.model_copy(update={"content": text, "status": "error",
                                             "artifact": {"attempted": False, "error_code": "invalid_arguments"}})
        if data.get("ok") is not True:
            err = data.get("error") if isinstance(data.get("error"), dict) else {}
            code = str(err.get("code") or "error")
            return result.model_copy(update={"content": text, "status": "error",
                                             "artifact": {"attempted": code not in NOT_ATTEMPTED, "error_code": code}})
        try:
            got = _ServiceImage.model_validate(data.get("image"))
        except ValidationError as e:
            logger.warning("生图结果的格式不对：%s", e.errors()[0]["msg"] if e.errors() else e)
            return failed(cid, "invalid_response", attempted=True)
        ratio = args.get("aspect_ratio")
        img = GeneratedImage(id=got.id, mime_type=got.mime_type, width=got.width, height=got.height,
                             prompt=str(args.get("prompt") or "").strip(),
                             aspect_ratio=ratio if isinstance(ratio, str) and ratio else ratio_of(got.width, got.height),
                             model=got.model, edited_from=got.edited_from)
        search = data.get("reference_search")
        status = search.get("status") if isinstance(search, dict) else None
        note = f"{text} {prompts.render(f'tools.{IMAGE_TOOL}.shown')}"
        return result.model_copy(update={"content": note, "status": "success",
                                         "artifact": {"attempted": True, "image": img.model_dump(), "reference_search_status": status}})
