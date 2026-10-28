"""调研 agent 的工具：搜索、读网页、问用户。工具描述在 app/graph/research/prompts.toml 的 [tools.*]。

ask_user 的函数体不会被执行，由 HumanInTheLoopMiddleware 在执行前拦截，用户的回答直接作为工具结果。
"""

from __future__ import annotations

import json

from langchain_core.tools import ToolException, tool

from app.graph.research import prompts
from app.tools.gateway import GatewayAuthError, GatewayClient, GatewayError


@tool("ask_user", description=prompts.render("tools.ask_user.description"))
def ask_user(question: str, options: list[str], why: str) -> str:
    return "（由中间件拦截，不会执行）"


def make_research_tools(client: GatewayClient) -> list:
    @tool("search_web", description=prompts.render("tools.search_web.description"))
    def search_web(query: str, count: int = 5) -> str:
        try:
            hits = client.search(query, count=count)
        except GatewayAuthError:
            raise  # 密钥配置问题：让这次运行失败、把错误亮出来，改好配置再重试
        except GatewayError as e:
            # 服务偶发出错交给 agent 处理，不让整次调研失败；出错的搜索也占额度，服务一直不好时不会无限重试
            raise ToolException(json.dumps({"ok": False, "kind": e.kind, "message": str(e), "retryable": e.retryable},
                                           ensure_ascii=False)) from e
        return json.dumps([h.to_dict() for h in hits], ensure_ascii=False)

    search_web.handle_tool_error = True  # ToolException 变成 status=error 的工具结果，其他异常照常抛出

    @tool("read_page", description=prompts.render("tools.read_page.description"))
    def read_page(url: str) -> str:
        return json.dumps(client.read(url).to_dict(), ensure_ascii=False)

    return [search_web, read_page, ask_user]
