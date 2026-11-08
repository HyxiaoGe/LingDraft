"""用户发的图怎么给模型，和看图工具 view_image（2026-11-08 Sean 定）。

- 看得见图的模型（models.toml 的 vision，见 Catalog.sees）：用户那句话后面接上图（base64，从 image-service 取来），最多带最近
  NATIVE_IMAGES 张，更早的、服务里没了的换成一句说明。Kimi 直连、TokenHub 不收公网链接，我们的图也不在公网上，所以一律 base64。
- 看不见的（DeepSeek V4 Pro、GLM-5.3/5.2、混元 Hy3）：接一段说明（图的编号、看不见、调 view_image 问），由 [roles] 的 vision
  （Flash，先走免费额度）看图、用文字回答。非必要不换模型，看不见的用工具补（Sean 定）。看不见的不一定报错：V4 Pro 把图丢了、
  编一个答案，所以只给写了看得见的发图。
- 改图、照着画照旧：说明里都给编号，当 source_image_id（见 image_mcp）。
存着的消息不变（图在 additional_kwargs 里，见 history.attached_images），每次调模型按这次用的模型组织：一轮里换了备用也跟着换。
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable

from langchain.tools import ToolRuntime
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.tools import ToolException, tool
from pydantic import BaseModel, Field

from app.graph.research import prompts
from app.graph.research.history import VIEW_TOOL, attached_images, owned_image_ids
from app.tools.gateway import GatewayClient

log = logging.getLogger(__name__)

NATIVE_IMAGES = 10  # 看得见图的模型最多带最近这么多张（每张几百到几千 token，前缀缓存命中时便宜）；更早的换成说明
CACHE_BYTES = 64 * 1024 * 1024  # 取来的图（base64）最多缓存这么多：同一张图一段对话里每次调模型都要


class ImageUris:
    """按编号取图、转成 data URI 给模型看。图不会变，按编号缓存，超过 CACHE_BYTES 先丢最久没用的。
    服务里没有（删了）是 None；网关、服务出问题照常抛出。"""

    def __init__(self, gateway: GatewayClient, limit: int = CACHE_BYTES) -> None:
        self.gateway, self.limit = gateway, limit
        self._cache: OrderedDict[str, str] = OrderedDict()
        self._size = 0

    async def get(self, image_id: str) -> str | None:
        if (uri := self._cache.get(image_id)) is not None:
            self._cache.move_to_end(image_id)
            return uri
        got = await self.gateway.image(image_id)
        if got is None:
            return None
        data, mime = got
        uri = f"data:{mime};base64,{base64.b64encode(data).decode()}"
        self._cache[image_id] = uri
        self._size += len(uri)
        while self._size > self.limit and len(self._cache) > 1:
            self._size -= len(self._cache.popitem(last=False)[1])
        return uri


async def with_images(messages: list[BaseMessage], sees: bool, image_uri: Callable[[str], Awaitable[str | None]]) -> list[BaseMessage]:
    """给模型的那份：带了图的用户消息，看得见图的接上图（最近 NATIVE_IMAGES 张）和编号；看不见的接一段说明（编号、调 view_image）。
    取不到的图（服务里没了、出问题）当给不了，说明里照样给编号（还能拿去改图）。没带图的消息原样返回。"""
    attached = {i: imgs for i, m in enumerate(messages) if (imgs := attached_images(m))}
    if not attached:
        return messages
    uris: dict[str, str] = {}
    if sees:
        recent = [a.id for imgs in attached.values() for a in imgs][-NATIVE_IMAGES:]
        got = await asyncio.gather(*(_uri(image_uri, iid) for iid in recent))
        uris = {iid: u for iid, u in zip(recent, got, strict=True) if u is not None}
    edit = prompts.render("tools.generate_image._edit")
    out = list(messages)
    for i, imgs in attached.items():
        m = messages[i]
        if not sees:  # 看不见的照旧给一段字（之前一直这么发，各家都收）：那句话、空一行、说明
            note = prompts.render("tools.generate_image.attached_unseen", ids=[a.id for a in imgs], edit=edit)
            content = f"{m.content}\n\n{note}" if isinstance(m.content, str) else [*m.content, {"type": "text", "text": note}]
            out[i] = m.model_copy(update={"content": content})
            continue
        blocks = [{"type": "text", "text": m.content}] if isinstance(m.content, str) else list(m.content)
        shown = [a.id for a in imgs if a.id in uris]
        gone = [a.id for a in imgs if a.id not in uris]
        blocks += [{"type": "image_url", "image_url": {"url": uris[iid]}} for iid in shown]
        notes = ([prompts.render("tools.generate_image.attached_seen", ids=shown, edit=edit)] if shown else []) + \
                ([prompts.render("tools.generate_image.attached_gone", ids=gone, edit=edit)] if gone else [])
        out[i] = m.model_copy(update={"content": [*blocks, *({"type": "text", "text": n} for n in notes)]})
    return out


async def _uri(image_uri: Callable[[str], Awaitable[str | None]], image_id: str) -> str | None:
    try:
        return await image_uri(image_id)
    except Exception as e:  # noqa: BLE001  取不到当给不了，这次照常调模型
        log.warning("取图给模型看没取到：%s: %s: %s", image_id, type(e).__name__, str(e)[:200])
        return None


class ViewArgs(BaseModel):
    image_id: str = Field(description=prompts.render("tools.view_image.image_id"))
    question: str = Field(description=prompts.render("tools.view_image.question"))


def make_view_image_tool(planner):
    """看图工具：只认这段对话里用户发的、画好的图（见 history.owned_image_ids）。由 planner.view_image 看（[roles] 的 vision）。
    没看成的（编号不对、图没了、看图的模型出问题）给模型一句说明、status=error，页面上写“没看成”，不收钱（见 ToolChargeMiddleware）。
    只给看不见图的模型（见 agent.ModelSelect）。"""

    @tool(VIEW_TOOL, description=prompts.render("tools.view_image.description"), args_schema=ViewArgs)
    async def view_image(runtime: ToolRuntime, image_id: str, question: str) -> str:
        iid = image_id.strip()
        if iid not in owned_image_ids(runtime.state.get("messages", [])):
            raise ToolException(prompts.render("tools.view_image.not_found", id=iid))
        try:
            uri = await planner.image_uri(iid)
        except Exception as e:  # noqa: BLE001
            log.warning("看图：取图没取到：%s: %s: %s", iid, type(e).__name__, str(e)[:200])
            raise ToolException(prompts.render("tools.view_image.failed")) from e
        if uri is None:
            raise ToolException(prompts.render("tools.view_image.unavailable", id=iid))
        try:
            return await planner.view_image(uri, question.strip())
        except Exception as e:  # noqa: BLE001  点了停止是 CancelledError，不在这里
            log.warning("看图：看图的模型没看成：%s: %s", type(e).__name__, str(e)[:200])
            raise ToolException(prompts.render("tools.view_image.failed")) from e

    view_image.handle_tool_error = True
    return view_image


def view_prompt(uri: str, question: str) -> list[BaseMessage]:
    """给看图的模型（[roles] 的 vision）的那句话：问题和图。"""
    return [HumanMessage(content=[{"type": "text", "text": prompts.render("tools.view_image.viewer", question=question)},
                                  {"type": "image_url", "image_url": {"url": uri}}])]
