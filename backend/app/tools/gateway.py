"""agent-tools 网关客户端：生图，以及网关出错时的几种异常（天气、搜索、读页走 MCP，见 app.tools.mcp）。

网关统一鉴权（Bearer 产品密钥）、记账、按前缀转发：
- POST /image/v1/generate → image-service（还要在 image-service 里登记灵稿，见它的 PRODUCTS）；
  出的图在 GET /image/static/images/...，公开地址，不用密钥
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

# image-service 整条主备链限时 135 秒（含找参考图、各模型尝试），超时自己返回 upstream_timeout；这里留出传输余量（照 Fusion）
IMAGE_TIMEOUT = 150.0
DOWNLOAD_TIMEOUT = 30.0
MAX_IMAGE_BYTES = 10 * 1024 * 1024
IMAGE_MIME_TYPES = {"image/png", "image/jpeg", "image/webp"}
# image-service 静态目录下的图片；REST 返回的就是这个相对路径
_IMAGE_PATH = re.compile(r"^/static/images/[A-Za-z0-9_-]{1,128}\.(?:png|jpe?g|webp)$")
# image-service 找参考图（reference_subject）的结果；none_accepted、search_failed 时照常出图，但没有参考图
REFERENCE_SEARCH_STATUSES = {"found", "cached", "none_accepted", "search_failed", "skipped"}


# ---- 网关成功响应的结构：在边界上校验，结构不对按失败处理，不让坏数据流到后面 ----


class _ImageBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    image_url: str
    model: str | None = None
    reference_search: dict | None = None


def _brief(e: ValidationError) -> str:
    err = e.errors()[0]
    return ".".join(str(x) for x in err["loc"]) + "：" + err["msg"] if err["loc"] else err["msg"]


class GatewayError(RuntimeError):
    """kind 是失败类型（timeout、http_502…），retryable 表示再试一次可能就好。"""

    def __init__(self, message: str, *, kind: str = "error", retryable: bool = False) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


class GatewayAuthError(GatewayError):
    """密钥不对或没有权限：配置问题，再试也不会好。"""


class GatewayLimitError(GatewayError):
    """网关这个服务今天的调用上限到了（HTTP 429，北京时间 0 点重置）：今天再调也一样。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, kind="daily_limit_exceeded")


class ImageError(GatewayError):
    """生图没成。kind 是 image-service 的错误码（content_filtered、upstream_timeout……）或下载图片时的问题；
    fallback_from 是主模型失败的码：主模型没出成、换备用模型也失败时才有。"""

    def __init__(self, message: str, *, kind: str, retryable: bool = False, fallback_from: str | None = None) -> None:
        super().__init__(message, kind=kind, retryable=retryable)
        self.fallback_from = fallback_from


@dataclass
class ImageResult:
    path: str  # image-service 静态目录下的路径：/static/images/<id>.<扩展名>
    model: str | None  # 实际出图的模型（主模型没出成时是备用模型）
    reference_search_status: str | None


class GatewayClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        scene: str = "research",
        caller_user: str | None = None,
        timeout: float = 35.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise GatewayError("未设置 AGENT_TOOLS_API_KEY")
        headers = {"Authorization": f"Bearer {api_key}", "X-Caller-Scene": scene}
        if caller_user:
            headers["X-Caller-User"] = caller_user
        # 异步的：在对话的事件循环里用（见 app.aio），用户点了停止，在等的请求马上断开
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout, transport=transport)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def generate_image(self, payload: dict[str, Any], *, scene: str) -> ImageResult:
        """出一张图（payload 是 image-service 的 /v1/generate 参数）。scene 是 chat_image / image_edit，网关和 image-service 按它统计。"""
        try:
            resp = await self._client.post("/image/v1/generate", json=payload, headers={"X-Caller-Scene": scene}, timeout=IMAGE_TIMEOUT)
        except httpx.TimeoutException as e:
            raise ImageError("生图超时", kind="upstream_timeout", retryable=True) from e
        except httpx.HTTPError as e:
            raise ImageError(f"生图请求失败：{e}", kind="request_error", retryable=True) from e
        if resp.status_code in (401, 403):  # 网关没放行 image，或者 image-service 里没登记灵稿
            raise GatewayAuthError(f"生图失败：HTTP {resp.status_code} {resp.text[:200]}", kind=f"http_{resp.status_code}")
        try:
            data = resp.json()
        except ValueError as e:
            raise ImageError("生图服务返回的不是 JSON", kind="invalid_response", retryable=True) from e  # 多半是中间代理的错误页
        if resp.status_code != 200:
            # image-service 的失败是 {"error": 码, "retryable": …, "fallback_from": {"code": …}}；网关自己的失败是 {"error": "…"}
            body = data if isinstance(data, dict) else {}
            code = body.get("error") if isinstance(body.get("error"), str) else None
            retryable = body.get("retryable")
            fallback = body.get("fallback_from")
            raise ImageError(f"生图失败：{code or resp.status_code}", kind=code or f"http_{resp.status_code}",
                             retryable=retryable if isinstance(retryable, bool) else resp.status_code in (429, 502, 503, 504),
                             fallback_from=fallback.get("code") if isinstance(fallback, dict) and isinstance(fallback.get("code"), str) else None)
        try:
            body = _ImageBody.model_validate(data)
        except ValidationError as e:
            raise ImageError(f"生图服务返回的格式不对（{_brief(e)}）", kind="invalid_response") from e
        path = urlsplit(body.image_url).path
        if not _IMAGE_PATH.fullmatch(path):
            raise ImageError("生图服务给的图片地址不对", kind="invalid_response")
        status = (body.reference_search or {}).get("status")
        return ImageResult(path=path, model=body.model, reference_search_status=status if status in REFERENCE_SEARCH_STATUSES else None)

    async def image_file(self, path: str) -> tuple[bytes, str]:
        """取回 image-service 出的图：(内容, MIME 类型)。只认它静态目录下的 PNG / JPEG / WebP，最大 10 MB。"""
        if not _IMAGE_PATH.fullmatch(path):
            raise ImageError("图片地址不对", kind="invalid_response")
        try:
            async with self._client.stream("GET", "/image" + path, timeout=DOWNLOAD_TIMEOUT) as resp:
                mime = (resp.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
                if resp.status_code != 200 or mime not in IMAGE_MIME_TYPES:
                    raise ImageError(f"取图失败：HTTP {resp.status_code} {mime}", kind="image_download_failed", retryable=True)
                chunks, size = [], 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_IMAGE_BYTES:
                        raise ImageError("图片太大", kind="image_too_large")
                    chunks.append(chunk)
        except httpx.HTTPError as e:
            raise ImageError(f"取图失败：{e}", kind="image_download_failed", retryable=True) from e
        content = b"".join(chunks)
        if not content:
            raise ImageError("取回的图片是空的", kind="image_download_failed", retryable=True)
        return content, mime
