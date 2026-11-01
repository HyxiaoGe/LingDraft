"""agent 的工具：搜索、读网页、查天气、画图、取当前时间。工具描述在 app/graph/research/prompts.toml 的 [tools.*]。

搜索和读页的结果里每个网址带一个来源 id（见 app.graph.research.sources），模型在正文里用它引用。
查天气的结果不是网页、不带来源 id：模型看逐日预报和逐小时的摘要；整份预报放在结果的 artifact 里（不发给模型），
页面画成天气卡片，卡片上写明来自和风天气（见 app.tools.weather）。
画图的结果模型只看到一句话（画成了、图片编号，或者没画成怎么跟用户说）；图的信息在 artifact 里，页面画成图片卡片（见 app.tools.images）。
当前时间：系统提示词里只有日期，几点钟要模型自己来取。放进提示词的话每次请求都不一样，前缀缓存就断了；页面上不显示这一步。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import ToolException, tool
from pydantic import BaseModel, Field

from app import config
from app.graph.research import prompts
from app.graph.research.history import IMAGE_TOOL
from app.graph.research.sources import known_in, source_id, url_key
from app.graph.research.state import GeneratedImage
from app.tools import images
from app.tools.gateway import GatewayAuthError, GatewayClient, GatewayError, ImageError
from app.tools.weather import CHINA_TZ, Unresolved, WeatherService, summarize_hourly

MAX_COUNT = 10
TIME_TOOL = "get_current_time"
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MAX_DAYS = 365
MAX_DOMAINS = 5


def _arg(tool_name: str, name: str) -> str:
    return prompts.render(f"tools.{tool_name}.{name}")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class SearchArgs(BaseModel):
    query: str = Field(description=_arg("search_web", "query"))
    count: int = Field(default=5, description=_arg("search_web", "count"))
    recency_days: int | None = Field(default=None, description=_arg("search_web", "recency_days"))
    domains: list[str] | None = Field(default=None, description=_arg("search_web", "domains"))


class ReadArgs(BaseModel):
    url: str = Field(description=_arg("read_page", "url"))


class WeatherArgs(BaseModel):
    location: str = Field(description=_arg("get_weather", "location"))


class ImageArgs(BaseModel):
    prompt: str = Field(min_length=1, max_length=images.MAX_PROMPT_CHARS, description=_arg(IMAGE_TOOL, "prompt"))
    aspect_ratio: images.AspectRatio | None = Field(default=None, description=_arg(IMAGE_TOOL, "aspect_ratio"))
    style: str | None = Field(default=None, max_length=images.MAX_STYLE_CHARS, description=_arg(IMAGE_TOOL, "style"))
    regenerate: bool = Field(default=False, description=_arg(IMAGE_TOOL, "regenerate"))
    reference_subject: str | None = Field(default=None, max_length=images.MAX_REFERENCE_SUBJECT_CHARS,
                                          description=_arg(IMAGE_TOOL, "reference_subject"))
    quality: images.Quality | None = Field(default=None, description=_arg(IMAGE_TOOL, "quality"))
    source_image_id: str | None = Field(default=None, max_length=64, description=_arg(IMAGE_TOOL, "source_image_id"))


def freshness(recency_days: int | None) -> str | None:
    """搜索服务只认 Brave 风格的四档（天、周、月、年），向上取整。"""
    if recency_days is None:
        return None
    for limit, value in ((1, "pd"), (7, "pw"), (31, "pm")):
        if recency_days <= limit:
            return value
    return "py"


def _bad_args(count: int, recency_days: int | None, sites: list[str]) -> str | None:
    """参数超出搜索服务支持的范围：不替模型改，告诉它哪里不对，让它自己重发。"""
    problems = []
    if not 1 <= count <= MAX_COUNT:
        problems.append(f"count 要在 1～{MAX_COUNT} 之间，传的是 {count}")
    if recency_days is not None and not 1 <= recency_days <= MAX_DAYS:
        problems.append(f"recency_days 要在 1～{MAX_DAYS} 之间（不限时间就不填），传的是 {recency_days}")
    if len(sites) > MAX_DOMAINS:
        problems.append(f"domains 最多 {MAX_DOMAINS} 个，传了 {len(sites)} 个")
    return "；".join(problems) or None


def _failed(kind: str, message: str, retryable: bool = False, **extra) -> ToolException:
    return ToolException(json.dumps({"ok": False, "kind": kind, "message": message, "retryable": retryable, **extra}, ensure_ascii=False))


def make_research_tools(client: GatewayClient) -> list:
    weather = WeatherService(client)

    @tool("search_web", description=prompts.render("tools.search_web.description"), args_schema=SearchArgs,
          response_format="content_and_artifact")
    def search_web(query: str, runtime: ToolRuntime, count: int = 5, recency_days: int | None = None,
                   domains: list[str] | None = None):
        sites = [d.strip() for d in domains or [] if d.strip()]
        if problem := _bad_args(count, recency_days, sites):
            raise _failed("bad_arguments", problem)
        try:
            hits = client.search(query, count=count, freshness=freshness(recency_days), domains=sites)
        except GatewayAuthError:
            raise  # 密钥配置问题：让这次运行失败、把错误亮出来，改好配置再重试
        except GatewayError as e:
            # 服务偶发出错交给 agent 处理，不让整轮失败
            raise _failed(e.kind, str(e), e.retryable) from e
        known = known_in(runtime.state)
        out = []
        for h in hits:
            d = h.to_dict()
            url = (d.get("url") or "").strip()
            if url:
                sid = known[url_key(url)] = source_id(url, known)  # 同一批结果里重复的网址也是同一个 id
                out.append({"id": sid, **d})
        return json.dumps(out, ensure_ascii=False), {"retrieved_at": _now()}

    search_web.handle_tool_error = True  # ToolException 变成 status=error 的工具结果，其他异常照常抛出

    @tool("read_page", description=prompts.render("tools.read_page.description"), args_schema=ReadArgs,
          response_format="content_and_artifact")
    def read_page(url: str, runtime: ToolRuntime):
        res = client.read(url).to_dict()
        if res.get("ok"):
            res = {"id": source_id((res.get("url") or url).strip(), known_in(runtime.state)), **res}
        return json.dumps(res, ensure_ascii=False), {"retrieved_at": _now()}

    @tool("get_weather", description=prompts.render("tools.get_weather.description"), args_schema=WeatherArgs,
          response_format="content_and_artifact")
    def get_weather(location: str):
        place = location.strip()
        if not place:
            raise _failed("bad_arguments", "location 是空的")
        try:
            fc = weather.forecast(place)
        except Unresolved as e:
            if e.kind == "ambiguous_location":
                raise _failed(e.kind, f"“{place}”对得上好几个地方，没有查", candidates=e.candidates) from e
            raise _failed(e.kind, f"和风天气里没找到“{place}”") from e
        except GatewayAuthError:
            raise  # 密钥或网关放行的问题：让这次运行失败、把错误亮出来
        except GatewayError as e:
            raise _failed(e.kind, str(e), e.retryable) from e
        seen = {"ok": True, **fc.model_dump(exclude={"hourly"})}
        if summary := summarize_hourly(fc.hourly):
            seen["hourly_summary"] = summary
        return json.dumps(seen, ensure_ascii=False), {"retrieved_at": _now(), "forecast": fc.model_dump()}

    get_weather.handle_tool_error = True

    @tool(TIME_TOOL, description=prompts.render(f"tools.{TIME_TOOL}.description"))
    def get_current_time() -> str:
        now = datetime.now(CHINA_TZ)
        return json.dumps({"now": now.strftime("%Y-%m-%d %H:%M"), "weekday": WEEKDAYS[now.weekday()], "timezone": "Asia/Shanghai"})

    @tool(IMAGE_TOOL, description=prompts.render(f"tools.{IMAGE_TOOL}.description"), args_schema=ImageArgs)
    def generate_image(prompt: str, runtime: ToolRuntime, aspect_ratio: str | None = None, style: str | None = None,
                       regenerate: bool = False, reference_subject: str | None = None, quality: str | None = None,
                       source_image_id: str | None = None):
        """照 Fusion 的 ImageGenerationToolHandler。没画成（包括没去画）的结果 status=error，artifact 里记着原因；
        真去画了的记 attempted（这一轮画了几次按它算，见 images.attempts_before）。"""
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
            out = images.wait_stoppable(lambda: client.generate_image(payload, scene="image_edit" if source else "chat_image"), ctx.check_stop)
            data, _ = images.wait_stoppable(lambda: client.image_file(out.path), ctx.check_stop)
            ext, mime, width, height = images.inspect_image(data)
            iid = images.new_id()
            img = GeneratedImage(id=iid, file=f"{iid}.{ext}", mime_type=mime, width=width, height=height, prompt=text,
                                 aspect_ratio=ratio, model=out.model, edited_from=source[0].id if source else None)
            ctx.images.save(thread_id, img.file, data)
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

    return [search_web, read_page, get_weather, generate_image, get_current_time]
