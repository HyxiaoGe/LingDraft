"""调研 agent 的工具：搜索、读网页、问用户。

ask_user 的函数体不会被执行，由 HumanInTheLoopMiddleware 在执行前拦截，用户的回答直接作为工具结果。
"""

from __future__ import annotations

import json

from langchain_core.tools import tool

from app.tools.gateway import GatewayClient


@tool
def ask_user(question: str, options: list[str], why: str) -> str:
    """向用户提一个问题，拿到答案后再继续。

    只在以下情况使用：术语或实体有歧义、两个可信来源互相矛盾且影响结论、发现范围需要收窄或扩大、
    连续几轮找不到可信来源。options 给 2～4 个可选答案；why 写清楚不问会怎样。
    不要和其他工具在同一步调用。
    """
    return "（由中间件拦截，不会执行）"


def make_research_tools(client: GatewayClient) -> list:
    @tool
    def search_web(query: str, count: int = 5) -> str:
        """搜索公开网页，返回候选来源列表（title、url、description、content 长摘要、published_at）。
        一次只搜一个明确的问题；count 1～10。"""
        hits = client.search(query, count=count)
        return json.dumps([h.to_dict() for h in hits], ensure_ascii=False)

    @tool
    def read_page(url: str) -> str:
        """读取网页正文（Markdown）。返回 ok=false 时说明该来源不可用，kind 给出原因，不要重试同一网址
        除非 retryable=true。正文过长会被截断（truncated=true）。"""
        return json.dumps(client.read(url).to_dict(), ensure_ascii=False)

    return [search_web, read_page, ask_user]
