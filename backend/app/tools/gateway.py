"""agent-tools 网关客户端：搜索、网页读取、天气。

网关统一鉴权（Bearer 产品密钥）、记账、按前缀转发：
- POST /search/search  → search-service
- GET  /reader/read    → reader-service
- GET  /weather/v1/... → weather-service（和风天气的只读代理；付费服务，网关里要给灵稿放行 weather）
"""

from __future__ import annotations

import re
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
    content: str  # 读页成功一定有正文：reader-service 读不到正文时自己按 empty_content 失败返回
    provider: str | None = None


class City(BaseModel):
    """和风的一个城市或区县。查城市是模糊匹配，随便写什么都会返回几个，挑哪个见 app.tools.weather.select_city。"""

    model_config = ConfigDict(extra="ignore")
    location_id: str
    name: str
    adm1: str = ""  # 省
    adm2: str = ""  # 市
    country: str = ""
    rank: int | None = None  # 越小越重要


class Day(BaseModel):
    """一天的预报，日期是当地日期；数字和风给的是字符串。"""

    model_config = ConfigDict(extra="ignore")
    date: str
    text_day: str
    text_night: str
    temp_max: str
    temp_min: str
    wind_dir_day: str | None = None
    wind_scale_day: str | None = None


class Hour(BaseModel):
    """一个小时的预报，时间带当地时区。"""

    model_config = ConfigDict(extra="ignore")
    time: str
    temp: str
    text: str
    pop: str | None = None  # 降水概率 %
    precip: str | None = None  # 降水量 mm


class _Cities(BaseModel):
    model_config = ConfigDict(extra="ignore")
    cities: list[City]  # 查不到是 []


class _Daily(BaseModel):
    model_config = ConfigDict(extra="ignore")
    daily: list[Day]


class _Hourly(BaseModel):
    model_config = ConfigDict(extra="ignore")
    hourly: list[Hour]


# weather-service 的失败码里再试一次可能就好的（见 agent-tools/services/weather/README.md）
_WEATHER_RETRYABLE = {"upstream_timeout", "upstream_rate_limited", "upstream_unavailable"}


def _brief(e: ValidationError) -> str:
    err = e.errors()[0]
    return ".".join(str(x) for x in err["loc"]) + "：" + err["msg"] if err["loc"] else err["msg"]


_SEP = r"(?:\s+[-–—]\s+|\s*[|｜_]\s*)"  # 横线两边要有空格，不然“10-10-2024”也会被当成重复
_REPEAT = re.compile(rf"^(?P<t>.{{2,}}?){_SEP}(?P=t)(?={_SEP}|$)")


def tidy_title(title: str) -> str:
    """网页标题常把正文标题重复一遍（“A - A”“A | A - 站名”），只留一份。只认一字不差的重复，不猜意思。"""
    return _REPEAT.sub(r"\g<t>", title.strip(), count=1)


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

    def search(self, query: str, *, count: int = 5, kind: str = "web", freshness: str | None = None,
               domains: list[str] | None = None) -> list[SearchHit]:
        """freshness 是 pd / pw / pm / py（近一天、周、月、年）；domains 限定站点。"""
        payload: dict[str, Any] = {"query": query, "type": kind, "count": max(1, min(count, 20))}
        if freshness:
            payload["freshness"] = freshness
        if domains:
            payload["domain_filters"] = list(domains)
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
            SearchHit(title=tidy_title(r.title or ""), url=r.url, description=r.description or "", content=r.content or r.description or "",
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
            content = body.content
            if not content.strip():
                return ReadResult(ok=False, url=url, kind="bad_response", message="读取服务返回的正文是空的")
            return ReadResult(
                ok=True,
                url=url,
                title=tidy_title(body.title or ""),
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

    def weather_cities(self, location: str, *, lang: str = "zh", number: int = 10) -> list[City]:
        """lang=en 时按英文名查（中文名查 “Paris” 查不到巴黎）。"""
        return self._weather("/v1/cities", {"location": location, "number": number, "lang": lang}, _Cities).cities

    def weather_daily(self, location_id: str) -> list[Day]:
        return self._weather("/v1/forecast/daily", {"location_id": location_id}, _Daily).daily

    def weather_hourly(self, location_id: str) -> list[Hour]:
        return self._weather("/v1/forecast/hourly", {"location_id": location_id}, _Hourly).hourly

    def _weather(self, path: str, params: dict[str, Any], schema: type[BaseModel]) -> Any:
        try:
            resp = self._client.get("/weather" + path, params=params)
        except httpx.TimeoutException as e:
            raise GatewayError("查天气超时", kind="timeout", retryable=True) from e
        except httpx.HTTPError as e:
            raise GatewayError(f"查天气请求失败：{e}", kind="request_error", retryable=True) from e
        if resp.status_code in (401, 403):  # 网关认不出密钥、或者没给灵稿放行天气
            raise GatewayAuthError(f"查天气失败：HTTP {resp.status_code} {resp.text[:200]}", kind=f"http_{resp.status_code}")
        try:
            data = resp.json()
        except ValueError as e:
            raise GatewayError("天气服务返回的不是 JSON", kind="bad_response", retryable=True) from e
        if resp.status_code != 200:
            # weather-service 的失败是 {"error": {"code": ...}}；网关自己的失败是 {"error": "..."}
            err = data.get("error") if isinstance(data, dict) else None
            code = err.get("code") if isinstance(err, dict) else err if isinstance(err, str) else None
            kind = code or f"http_{resp.status_code}"
            raise GatewayError(f"查天气失败：{kind}", kind=kind,
                               retryable=kind in _WEATHER_RETRYABLE or (not code and resp.status_code in (429, 502, 503, 504)))
        try:
            return schema.model_validate(data)
        except ValidationError as e:
            raise GatewayError(f"天气服务返回的格式不对（{_brief(e)}）", kind="bad_response") from e
