"""调研 agent 查资料的工具：搜索、读网页、问用户、读取调研方法。工具描述在 app/graph/research/prompts.toml 的 [tools.*]。

搜索和读页的结果里每个网址带一个来源 id（见 app.graph.research.sources），模型在正文和报告草稿里用它引用。
ask_user 的函数体不会被执行，由 HumanInTheLoopMiddleware 在执行前拦截，用户的回答直接作为工具结果。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from langchain.tools import ToolRuntime
from langchain_core.tools import ToolException, tool
from pydantic import BaseModel, Field

from app.graph.research import prompts, skills
from app.graph.research.sources import known_ids, source_id, url_key
from app.tools.gateway import GatewayAuthError, GatewayClient, GatewayError

MAX_COUNT = 10
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


class SkillArgs(BaseModel):
    name: str = Field(description=_arg("load_skill", "name"))


def freshness(recency_days: int | None) -> str | None:
    """搜索服务只认 Brave 风格的四档（天、周、月、年），向上取整。"""
    if recency_days is None:
        return None
    for limit, value in ((1, "pd"), (7, "pw"), (31, "pm")):
        if recency_days <= limit:
            return value
    return "py"


@tool("ask_user", description=prompts.render("tools.ask_user.description"))
def ask_user(question: str, options: list[str], why: str) -> str:
    return "（由中间件拦截，不会执行）"


@tool("load_skill", description=prompts.render("tools.load_skill.description"), args_schema=SkillArgs)
def load_skill(name: str) -> str:
    skill = skills.catalog().get(name.strip())
    if skill is None:
        raise ToolException(prompts.render("tools.load_skill.unknown", name=name, names="、".join(skills.catalog())))
    return skill.content()


load_skill.handle_tool_error = True


def make_research_tools(client: GatewayClient) -> list:
    skills.catalog()  # 技能文件写坏了，在构建工具（服务启动）时就报错，不等 agent 读取时才发现

    @tool("search_web", description=prompts.render("tools.search_web.description"), args_schema=SearchArgs,
          response_format="content_and_artifact")
    def search_web(query: str, runtime: ToolRuntime, count: int = 5, recency_days: int | None = None,
                   domains: list[str] | None = None):
        # 超出范围的取值收进范围，不为此多耗一轮模型调用
        count = max(1, min(count, MAX_COUNT))
        days = max(1, min(recency_days, 365)) if recency_days is not None else None
        sites = [d.strip() for d in domains or [] if d.strip()][:MAX_DOMAINS]
        try:
            hits = client.search(query, count=count, freshness=freshness(days), domains=sites)
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

    return [search_web, read_page, ask_user, load_skill]
