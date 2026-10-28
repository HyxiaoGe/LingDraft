"""调研 agent 的工具：搜索、读网页、问用户、读取调研方法。工具描述在 app/graph/research/prompts.toml 的 [tools.*]。

ask_user 的函数体不会被执行，由 HumanInTheLoopMiddleware 在执行前拦截，用户的回答直接作为工具结果。
"""

from __future__ import annotations

import json

from langchain_core.tools import ToolException, tool
from pydantic import BaseModel, Field

from app.graph.research import prompts, skills
from app.tools.gateway import GatewayAuthError, GatewayClient, GatewayError

MAX_COUNT = 10
MAX_DOMAINS = 5


def _arg(tool_name: str, name: str) -> str:
    return prompts.render(f"tools.{tool_name}.{name}")


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
    @tool("search_web", description=prompts.render("tools.search_web.description"), args_schema=SearchArgs)
    def search_web(query: str, count: int = 5, recency_days: int | None = None, domains: list[str] | None = None) -> str:
        # 超出范围的取值收进范围，不为此多耗一轮模型调用
        count = max(1, min(count, MAX_COUNT))
        days = max(1, min(recency_days, 365)) if recency_days is not None else None
        sites = [d.strip() for d in domains or [] if d.strip()][:MAX_DOMAINS]
        try:
            hits = client.search(query, count=count, freshness=freshness(days), domains=sites)
        except GatewayAuthError:
            raise  # 密钥配置问题：让这次运行失败、把错误亮出来，改好配置再重试
        except GatewayError as e:
            # 服务偶发出错交给 agent 处理，不让整次调研失败；出错的搜索也占额度，服务一直不好时不会无限重试
            raise ToolException(json.dumps({"ok": False, "kind": e.kind, "message": str(e), "retryable": e.retryable},
                                           ensure_ascii=False)) from e
        return json.dumps([h.to_dict() for h in hits], ensure_ascii=False)

    search_web.handle_tool_error = True  # ToolException 变成 status=error 的工具结果，其他异常照常抛出

    @tool("read_page", description=prompts.render("tools.read_page.description"), args_schema=ReadArgs)
    def read_page(url: str) -> str:
        return json.dumps(client.read(url).to_dict(), ensure_ascii=False)

    return [search_web, read_page, ask_user, load_skill]
