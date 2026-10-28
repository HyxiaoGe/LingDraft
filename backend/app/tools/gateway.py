"""agent-tools 网关客户端：搜索与网页读取。

网关统一鉴权（Bearer 产品密钥）、记账、按前缀转发：
- POST /search/search  → search-service
- GET  /reader/read    → reader-service
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

# 正文最多给模型这么多字，避免一页就吃掉上下文
READ_MAX_CHARS = 6000


# ---- 网关成功响应的结构：在边界上校验，结构不对按失败处理，不让坏数据流到后面 ----


class _Hit(BaseModel):
    model_config = ConfigDict(extra="ignore")
    url: str | None = None  # 个别条目没有网址就跳过；有但不是字符串算格式错
    title: str | None = None
    description: str | None = None
    content: str | None = None
    published_at: str | None = None


class _SearchBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    results: list[_Hit]  # 没搜到是 []；缺了、不是列表、条目不对都算格式错


class _ReadBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    title: str | None = None
    content: str | None = None
    provider: str | None = None


def _brief(e: ValidationError) -> str:
    err = e.errors()[0]
    return ".".join(str(x) for x in err["loc"]) + "：" + err["msg"] if err["loc"] else err["msg"]


@dataclass
class SearchHit:
    title: str
    url: str
    description: str
    content: str
    published_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "description": self.description,
            "content": self.content,
            "published_at": self.published_at,
        }


@dataclass
class ReadResult:
    ok: bool
    url: str
    title: str = ""
    content: str = ""
    provider: str = ""
    kind: str = ""
    message: str = ""
    retryable: bool = False
    truncated: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        if self.ok:
            return {
                "ok": True,
                "url": self.url,
                "title": self.title,
                "content": self.content,
                "provider": self.provider,
                "truncated": self.truncated,
            }
        return {"ok": False, "url": self.url, "kind": self.kind, "message": self.message, "retryable": self.retryable}


class GatewayError(RuntimeError):
    """kind 是失败类型（timeout、http_502…），retryable 表示再试一次可能就好。"""

    def __init__(self, message: str, *, kind: str = "error", retryable: bool = False) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


class GatewayAuthError(GatewayError):
    """密钥不对或没有权限：配置问题，再试也不会好。"""


class GatewayClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        scene: str = "research",
        caller_user: str | None = None,
        timeout: float = 35.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise GatewayError("未设置 AGENT_TOOLS_API_KEY")
        headers = {"Authorization": f"Bearer {api_key}", "X-Caller-Scene": scene}
        if caller_user:
            headers["X-Caller-User"] = caller_user
        self._client = httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout, transport=transport)

    def close(self) -> None:
        self._client.close()

    def search(self, query: str, *, count: int = 5, kind: str = "web", freshness: str | None = None) -> list[SearchHit]:
        payload: dict[str, Any] = {"query": query, "type": kind, "count": max(1, min(count, 20))}
        if freshness:
            payload["freshness"] = freshness
        try:
            resp = self._client.post("/search/search", json=payload)
        except httpx.TimeoutException as e:
            raise GatewayError(f"搜索超时：{e}", kind="timeout", retryable=True) from e
        except httpx.HTTPError as e:
            raise GatewayError(f"搜索请求失败：{e}", kind="request_error", retryable=True) from e
        if resp.status_code in (401, 403):
            raise GatewayAuthError(f"搜索失败：HTTP {resp.status_code} {resp.text[:200]}", kind=f"http_{resp.status_code}")
        if resp.status_code != 200:
            raise GatewayError(f"搜索失败：HTTP {resp.status_code} {resp.text[:200]}", kind=f"http_{resp.status_code}",
                               retryable=resp.status_code in (429, 502, 503, 504))
        try:
            data = resp.json()
        except ValueError as e:  # 多半是中间代理的错误页，可能一会儿就好
            raise GatewayError("搜索服务返回的不是 JSON", kind="bad_response", retryable=True) from e
        try:
            body = _SearchBody.model_validate(data)
        except ValidationError as e:
            raise GatewayError(f"搜索服务返回的格式不对（{_brief(e)}）", kind="bad_response") from e
        return [
            SearchHit(title=r.title or "", url=r.url, description=r.description or "", content=r.content or r.description or "",
                      published_at=r.published_at)
            for r in body.results if r.url
        ]

    def read(self, url: str, *, full: bool = False, max_chars: int = READ_MAX_CHARS) -> ReadResult:
        params: dict[str, Any] = {"url": url}
        if full:
            params["full"] = "true"
        try:
            resp = self._client.get("/reader/read", params=params)
        except httpx.TimeoutException:
            return ReadResult(ok=False, url=url, kind="timeout", message="读取超时", retryable=True)
        except httpx.HTTPError as e:
            return ReadResult(ok=False, url=url, kind="request_error", message=str(e), retryable=True)
        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError:
                return ReadResult(ok=False, url=url, kind="bad_response", message="读取服务返回的不是 JSON", retryable=True)
            try:
                body = _ReadBody.model_validate(data)
            except ValidationError as e:
                return ReadResult(ok=False, url=url, kind="bad_response", message=f"读取服务返回的格式不对（{_brief(e)}）")
            content = body.content or ""
            return ReadResult(
                ok=True,
                url=url,
                title=body.title or "",
                content=content[:max_chars],
                provider=body.provider or "",
                truncated=len(content) > max_chars,
            )
        # reader-service 的失败是结构化的 detail；网关自己的失败是 {"error": ...}
        try:
            body = resp.json()
        except ValueError:
            body = {}
        detail = body.get("detail") if isinstance(body, dict) else None
        if isinstance(detail, dict):
            return ReadResult(
                ok=False,
                url=url,
                kind=detail.get("kind") or f"http_{resp.status_code}",
                message=detail.get("message") or "",
                retryable=bool(detail.get("retryable")),
            )
        return ReadResult(
            ok=False,
            url=url,
            kind=(body.get("error") if isinstance(body, dict) else None) or f"http_{resp.status_code}",
            message=(body.get("message") if isinstance(body, dict) else None) or resp.text[:200],
            retryable=resp.status_code in (502, 503, 504),
        )
