"""agent 自己的工具：画图、取当前时间。工具描述在 app/graph/research/prompts.toml 的 [tools.*]。
搜索、读网页、查天气是 agent-tools 的 MCP 工具，见 app.tools.web_mcp、app.tools.weather_mcp。

画图的结果模型只看到一句话（画成了、图片编号，或者没画成怎么跟用户说）；图的信息在 artifact 里，页面画成图片卡片（见 app.tools.images）。
当前时间：系统提示词里只有日期，几点钟要模型自己来取。放进提示词的话每次请求都不一样，前缀缓存就断了；页面上不显示这一步。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app import config
from app.graph.research import prompts
from app.graph.research.history import IMAGE_TOOL
from app.graph.research.state import GeneratedImage
from app.tools import images
from app.tools.gateway import GatewayAuthError, GatewayClient, ImageError

TIME_TOOL = "get_current_time"
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
CHINA_TZ = timezone(timedelta(hours=8))


def _arg(tool_name: str, name: str) -> str:
    return prompts.render(f"tools.{tool_name}.{name}")


class ImageArgs(BaseModel):
    prompt: str = Field(min_length=1, max_length=images.MAX_PROMPT_CHARS, description=_arg(IMAGE_TOOL, "prompt"))
    aspect_ratio: images.AspectRatio | None = Field(default=None, description=_arg(IMAGE_TOOL, "aspect_ratio"))
    style: str | None = Field(default=None, max_length=images.MAX_STYLE_CHARS, description=_arg(IMAGE_TOOL, "style"))
    regenerate: bool = Field(default=False, description=_arg(IMAGE_TOOL, "regenerate"))
    reference_subject: str | None = Field(default=None, max_length=images.MAX_REFERENCE_SUBJECT_CHARS,
                                          description=_arg(IMAGE_TOOL, "reference_subject"))
    quality: images.Quality | None = Field(default=None, description=_arg(IMAGE_TOOL, "quality"))
    source_image_id: str | None = Field(default=None, max_length=64, description=_arg(IMAGE_TOOL, "source_image_id"))


def make_research_tools(client: GatewayClient) -> list:
    @tool(TIME_TOOL, description=prompts.render(f"tools.{TIME_TOOL}.description"))
    def get_current_time() -> str:
        now = datetime.now(CHINA_TZ)
        return json.dumps({"now": now.strftime("%Y-%m-%d %H:%M"), "weekday": WEEKDAYS[now.weekday()], "timezone": "Asia/Shanghai"})

    @tool(IMAGE_TOOL, description=prompts.render(f"tools.{IMAGE_TOOL}.description"), args_schema=ImageArgs)
    async def generate_image(prompt: str, runtime: ToolRuntime, aspect_ratio: str | None = None, style: str | None = None,
                             regenerate: bool = False, reference_subject: str | None = None, quality: str | None = None,
                             source_image_id: str | None = None):
        """照 Fusion 的 ImageGenerationToolHandler。没画成（包括没去画）的结果 status=error，artifact 里记着原因；
        真去画了的记 attempted（这一轮画了几次按它算，见 images.attempts_before）。
        出图要等很久：用户点了停止，这次运行被取消，在等的请求跟着断开，出到一半的图不要了。"""
        msgs = runtime.state.get("messages", [])

        def result(content: str, artifact: dict, ok: bool) -> ToolMessage:
            return ToolMessage(content=content, artifact=artifact, tool_call_id=runtime.tool_call_id, name=IMAGE_TOOL,
                               status="success" if ok else "error")

        def failed(code: str, attempted: bool, retryable: bool = False, fallback_from: str | None = None) -> ToolMessage:
            text = prompts.render(f"tools.{IMAGE_TOOL}.failed", error_code=code, retryable=retryable, fallback_from=fallback_from)
            return result(text, {"attempted": attempted, "error_code": code}, ok=False)

        text = prompt.strip()
        if not text:
            return failed("invalid_arguments", attempted=False)
        source = None
        if source_image_id is not None:
            # 找不到原图不算画了一次。模型自己编的编号也按找不到处理：让它如实告诉用户这张图改不了，而不是当成参数错去重画
            sid = source_image_id.strip()
            source = images.find_source(msgs, sid) if images.valid_id(sid) else None
            if source is None:
                return failed("source_image_unavailable", attempted=False)
        if images.attempts_before(msgs, runtime.tool_call_id) >= images.MAX_IMAGES_PER_TURN:
            return failed("run_image_limit_reached", attempted=False)
        ratio = aspect_ratio or (images.closest_aspect_ratio(source[0].width, source[0].height) if source else "1:1")
        # 主模型、备用模型由 image-service 的 DEFAULT_MODEL / DEFAULT_FALLBACK_MODEL 决定；grounding 只有 Gemini 备用模型用得上
        payload: dict = {"prompt": text, "aspect_ratio": ratio, "grounding": True}
        if regenerate:
            payload["no_cache"] = True
        if quality:
            payload["quality"] = quality
        if style and style.strip():
            payload["style"] = style.strip()
        subject = (reference_subject or "").strip() or None
        if subject:
            payload["reference_subject"] = subject
        if source:
            payload["reference_images"] = [source[1]]
        ctx = runtime.context
        thread_id = runtime.config["configurable"]["thread_id"]
        try:
            out = await client.generate_image(payload, scene="image_edit" if source else "chat_image")
            data, _ = await client.image_file(out.path)
            ext, mime, width, height = images.inspect_image(data)
            iid = images.new_id()
            img = GeneratedImage(id=iid, file=f"{iid}.{ext}", mime_type=mime, width=width, height=height, prompt=text,
                                 aspect_ratio=ratio, model=out.model, edited_from=source[0].id if source else None)
            await asyncio.to_thread(ctx.images.save, thread_id, img.file, data)
        except GatewayAuthError:
            raise  # 网关没放行或 image-service 没登记灵稿：配置问题，让这次运行失败、把错误亮出来
        except ImageError as e:
            return failed(e.kind, attempted=True, retryable=e.retryable, fallback_from=e.fallback_from)
        except ValueError:
            return failed("invalid_image", attempted=True)
        except OSError:
            return failed("image_store_failed", attempted=True)
        note = prompts.render(f"tools.{IMAGE_TOOL}.success", image_id=img.id, reference_subject=subject,
                              reference_search_status=out.reference_search_status, edited=source is not None)
        return result(note, {"attempted": True, "image": img.model_dump(), "source_url": config.IMAGE_SERVICE_URL.rstrip("/") + out.path,
                             "reference_search_status": out.reference_search_status}, ok=True)

    return [generate_image, get_current_time]
