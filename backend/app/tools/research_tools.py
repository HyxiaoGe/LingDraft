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

    只有三个条件同时满足才用：这个信息对话和工具都拿不到（只有用户知道，例如用途、内部限制、取舍偏好）；
    用户答 A 或 B 会明显改变推荐或方案；不问就完不成用户这次要的东西（用户要明确选择时，“分情况说明”不算完成）。
    来源矛盾、找不到材料不是提问的理由，应自己核对或在答案里说明证据不足。用户已经说过的不要再问。
    question 问用户自己知道的具体事实，一次只问一件事；options 给 2～4 个互斥的可选答案，不把几个条件捆在一起，
    可以含一个“尚不清楚，先按标注的假设给建议”；why 写清楚不问会怎样。不要和其他工具在同一步调用。
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
