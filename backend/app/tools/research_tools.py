"""agent 查资料的工具：搜索、读网页。工具描述在 app/graph/research/prompts.toml 的 [tools.*]。

搜索和读页的结果里每个网址带一个来源 id（见 app.graph.research.sources），模型在正文里用它引用。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from langchain.tools import ToolRuntime
from langchain_core.tools import ToolException, tool
from pydantic import BaseModel, Field

from app.graph.research import prompts
from app.graph.research.sources import known_ids, source_id, url_key
from app.tools.gateway import GatewayAuthError, GatewayClient, GatewayError

MAX_COUNT = 10
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


def make_research_tools(client: GatewayClient) -> list:
    @tool("search_web", description=prompts.render("tools.search_web.description"), args_schema=SearchArgs,
          response_format="content_and_artifact")
    def search_web(query: str, runtime: ToolRuntime, count: int = 5, recency_days: int | None = None,
                   domains: list[str] | None = None):
        sites = [d.strip() for d in domains or [] if d.strip()]
        if problem := _bad_args(count, recency_days, sites):
            raise ToolException(json.dumps({"ok": False, "kind": "bad_arguments", "message": problem, "retryable": False}, ensure_ascii=False))
        try:
            hits = client.search(query, count=count, freshness=freshness(recency_days), domains=sites)
        except GatewayAuthError:
            raise  # 密钥配置问题：让这次运行失败、把错误亮出来，改好配置再重试
        except GatewayError as e:
            # 服务偶发出错交给 agent 处理，不让整轮失败
            raise ToolException(json.dumps({"ok": False, "kind": e.kind, "message": str(e), "retryable": e.retryable},
                                           ensure_ascii=False)) from e
        known = known_ids(runtime.state.get("messages", []))
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
            res = {"id": source_id((res.get("url") or url).strip(), known_ids(runtime.state.get("messages", []))), **res}
        return json.dumps(res, ensure_ascii=False), {"retrieved_at": _now()}

    return [search_web, read_page]
