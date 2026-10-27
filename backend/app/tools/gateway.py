"""agent-tools 网关客户端：搜索与网页读取。

网关统一鉴权（Bearer 产品密钥）、记账、按前缀转发：
- POST /search/search  → search-service
- GET  /reader/read    → reader-service
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

# 正文最多给模型这么多字，避免一页就吃掉上下文
READ_MAX_CHARS = 6000


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
    pass


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
        except httpx.HTTPError as e:
            raise GatewayError(f"搜索请求失败：{e}") from e
        if resp.status_code != 200:
            raise GatewayError(f"搜索失败：HTTP {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        hits = []
        for r in data.get("results") or []:
            hits.append(
                SearchHit(
                    title=r.get("title") or "",
                    url=r.get("url") or "",
                    description=r.get("description") or "",
                    content=r.get("content") or r.get("description") or "",
                    published_at=r.get("published_at"),
                )
            )
        return hits

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
            data = resp.json()
            content = data.get("content") or ""
            truncated = len(content) > max_chars
            return ReadResult(
                ok=True,
                url=url,
                title=data.get("title") or "",
                content=content[:max_chars],
                provider=data.get("provider") or "",
                truncated=truncated,
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
