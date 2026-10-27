"""调研 agent：create_agent + 中间件。外层图把它当一个节点调用。

- ask_user 由 HumanInTheLoopMiddleware 在执行前拦截（respond），用户回答直接成为工具结果。
- ToolCallLimitMiddleware 限制每章的搜索 / 读取 / 提问次数；ModelCallLimitMiddleware 兜底防止死循环。
- 候选来源不靠闭包收集，而是事后从 agent 的工具消息里解析（消息在检查点里，跨进程恢复也不丢）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal

from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware, ModelCallLimitMiddleware, ToolCallLimitMiddleware
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, ToolMessage
from pydantic import BaseModel, ConfigDict, Field

from app.graph.research.state import (
    ASK_LIMIT_PER_SECTION,
    MAX_MODEL_CALLS_PER_SECTION,
    MAX_READS_PER_SECTION,
    MAX_SEARCHES_PER_SECTION,
    Source,
)

EXCERPT_CHARS = 400


class ProposedClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    statement: str = Field(description="一句话论断")
    kind: Literal["fact", "inference"] = Field(description="fact=来源里明确写了；inference=从来源推出来的")
    source_urls: list[str] = Field(description="支撑这条论断的网址，必须是搜到或读过的")
    confidence: Literal["high", "medium", "low"]


class Findings(BaseModel):
    """一章的调研结果。"""

    model_config = ConfigDict(extra="forbid")
    claims: list[ProposedClaim]
    assumptions: list[str] = Field(description="没问用户、自行决定的取舍，或没有来源支撑的判断")
    summary: str = Field(description="两句话概括这一章找到了什么")


def build_agent(model: BaseChatModel, tools: list, system_prompt: str, *, allow_ask: bool = True):
    tools = [t for t in tools if allow_ask or t.name != "ask_user"]
    middleware = [
        ToolCallLimitMiddleware(tool_name="search_web", run_limit=MAX_SEARCHES_PER_SECTION, exit_behavior="continue"),
        ToolCallLimitMiddleware(tool_name="read_page", run_limit=MAX_READS_PER_SECTION, exit_behavior="continue"),
        ModelCallLimitMiddleware(run_limit=MAX_MODEL_CALLS_PER_SECTION, exit_behavior="end"),
    ]
    if allow_ask:
        middleware.insert(0, HumanInTheLoopMiddleware(interrupt_on={"ask_user": {"allowed_decisions": ["respond"]}}))
        middleware.append(ToolCallLimitMiddleware(tool_name="ask_user", run_limit=ASK_LIMIT_PER_SECTION, exit_behavior="continue"))
    return create_agent(
        model=model,
        tools=tools,
        system_prompt=system_prompt,
        middleware=middleware,
        response_format=ToolStrategy(Findings),
        # 不传 checkpointer：作为子图时借用父图的检查点，命名空间带任务 id，每章一份独立历史；
        # 中断后父节点重跑时任务 id 不变，子图从自己的检查点接着跑。
        # （checkpointer=True 会按线程只留一份历史，各章消息会串在一起。）
        name="research_agent",
    )


# ---- 中断的转换：中间件的载荷 ↔ 统一的问题卡 ----


def is_ask_interrupt(value: Any) -> bool:
    return isinstance(value, dict) and bool(value.get("action_requests"))


def ask_view(value: dict) -> dict:
    req = value["action_requests"][0]
    args = req.get("args") or {}
    return {
        "type": "research.ask",
        "title": args.get("question", ""),
        "options": list(args.get("options") or []),
        "why": args.get("why", ""),
    }


SKIP_MESSAGE = "（用户跳过了这个问题。按你的最佳判断继续，并把你采用的假设写进 assumptions。）"
UNSHOWN_MESSAGE = "（这个问题没有呈现给用户：一次只能问一个问题。按你的最佳判断处理，并把假设写进 assumptions。）"


def ask_resume(answer: str | None, skip: bool = False, pending: dict | None = None) -> dict:
    """用户的回答 → 中间件的 decisions。模型若违反“一次只问一个”在同一步发了多个 ask_user，
    只有第一个呈现给了用户，其余按未呈现处理（decisions 数量必须与 action_requests 一致）。"""
    message = SKIP_MESSAGE if skip or not (answer or "").strip() else answer.strip()
    n = len(pending.get("action_requests") or []) if pending else 1
    decisions = [{"type": "respond", "message": message}]
    decisions += [{"type": "respond", "message": UNSHOWN_MESSAGE} for _ in range(max(n, 1) - 1)]
    return {"decisions": decisions}


# ---- 从工具消息里解析候选来源 ----


def _parse(msg: ToolMessage) -> Any:
    try:
        return json.loads(msg.content if isinstance(msg.content, str) else "")
    except (TypeError, ValueError):
        return None


def collect_sources(messages: list[BaseMessage], existing: list[Source], next_index: int) -> tuple[list[Source], dict[str, str]]:
    """返回新增的来源和 url→id 映射（含已有的）。读到正文的来源用正文片段替换搜索摘要。"""
    by_url = {s.url: s for s in existing}
    new: list[Source] = []
    now = datetime.now(UTC).isoformat(timespec="seconds")
    for m in messages:
        if not isinstance(m, ToolMessage):
            continue
        data = _parse(m)
        if m.name == "search_web" and isinstance(data, list):
            for hit in data:
                url = (hit.get("url") or "").strip()
                if not url or url in by_url:
                    continue
                src = Source(
                    id=f"s{next_index}",
                    url=url,
                    title=hit.get("title") or "",
                    retrieved_at=now,
                    published_at=hit.get("published_at"),
                    excerpt=(hit.get("content") or hit.get("description") or "")[:EXCERPT_CHARS],
                    excerpt_kind="summary",
                )
                next_index += 1
                by_url[url] = src
                new.append(src)
        elif m.name == "read_page" and isinstance(data, dict) and data.get("ok"):
            url = data.get("url") or ""
            src = by_url.get(url)
            if src is None:
                src = Source(id=f"s{next_index}", url=url, title=data.get("title") or "", retrieved_at=now)
                next_index += 1
                by_url[url] = src
                new.append(src)
            src.excerpt = (data.get("content") or "")[:EXCERPT_CHARS]
            src.excerpt_kind = "body"
            if data.get("title") and not src.title:
                src.title = data["title"]
    return new, {u: s.id for u, s in by_url.items()}


def count_asks(messages: list[BaseMessage]) -> int:
    return sum(1 for m in messages if isinstance(m, ToolMessage) and m.name == "ask_user")
