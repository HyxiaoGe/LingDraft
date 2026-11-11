"""agent 自己的工具：取当前时间。工具描述在 app/graph/research/prompts/tools_web.toml 的 [tools.get_current_time]。
搜索、读网页、查天气、画图是 agent-tools 的 MCP 工具，见 app.tools.web_mcp、weather_mcp、image_mcp。

当前时间：系统提示词里只有日期，几点钟要模型自己来取。放进提示词的话每次请求都不一样，前缀缓存就断了；页面上不显示这一步。
"""

from __future__ import annotations

import json
from datetime import timedelta, timezone

from langchain_core.tools import tool

from app.clock import Clock
from app.graph.research import prompts

TIME_TOOL = "get_current_time"
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
CHINA_TZ = timezone(timedelta(hours=8))


def make_research_tools(clock: Clock | None = None) -> list:
    clock = clock or Clock()

    @tool(TIME_TOOL, description=prompts.render(f"tools.{TIME_TOOL}.description"))
    def get_current_time() -> str:
        now = clock.now(CHINA_TZ)
        return json.dumps({"now": now.strftime("%Y-%m-%d %H:%M"), "weekday": WEEKDAYS[now.weekday()], "timezone": "Asia/Shanghai"})

    return [get_current_time]
