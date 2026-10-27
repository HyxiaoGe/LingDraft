"""调研 agent：create_agent + 中间件。外层图把它当一个节点调用，对整个问题跑一次。

- ask_user 由 HumanInTheLoopMiddleware 在执行前拦截（respond），用户回答直接成为工具结果。
- ToolBudgetMiddleware：额度用尽的工具从后续模型调用的工具列表里摘掉（模型看不到就不会反复调），
  执行层再硬拦一次；ModelCallLimitMiddleware 兜底防止死循环。
- InvalidToolCallRetryMiddleware：模型返回无法解析的工具调用（例如参数 JSON 里有裸引号）时，
  默认循环会直接结束；这里让它带着解析错误重发，最多几次。
- 收尾：agent 结束时没有文字答案，就用同一个模型不带工具再要一次（受控收尾，预算独立）。
- 候选来源从 agent 的工具消息里解析（消息在检查点里，跨进程恢复也不丢）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, NotRequired

from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware, ModelCallLimitMiddleware
from langchain.agents.middleware.types import AgentMiddleware, AgentState, ModelRequest, hook_config
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from app.graph.research import prompts
from app.graph.research.state import (
    CLOSE_RETRIES,
    INVALID_TOOL_CALL_RETRIES,
    MAX_ASKS,
    MAX_MODEL_CALLS,
    MAX_READS,
    MAX_SEARCHES,
    Source,
)

EXCERPT_CHARS = 400
LIMIT_EXCEEDED_PREFIX = "Model call limits exceeded"
EXHAUSTED_NOTE = "（系统）以下工具的额度已用完，不再提供：{names}。请基于已有材料继续或直接作答。"


class ToolBudgetMiddleware(AgentMiddleware):
    """按工具名限制一次运行里的调用次数：用完就从模型可见的工具里摘掉，并在执行层硬拦。"""

    def __init__(self, limits: dict[str, int]) -> None:
        super().__init__()
        self.limits = limits

    def _used(self, messages: list[BaseMessage]) -> dict[str, int]:
        used: dict[str, int] = {}
        for m in messages:
            if isinstance(m, ToolMessage) and m.name in self.limits:
                used[m.name] = used.get(m.name, 0) + 1
        return used

    def wrap_model_call(self, request: ModelRequest, handler):
        used = self._used(request.messages)
        exhausted = {n for n, lim in self.limits.items() if used.get(n, 0) >= lim}
        if not exhausted:
            return handler(request)
        tools = [t for t in request.tools if getattr(t, "name", None) not in exhausted]
        note = EXHAUSTED_NOTE.format(names="、".join(sorted(exhausted)))
        base = request.system_message.content if request.system_message else ""
        base = base if isinstance(base, str) else str(base)
        return handler(request.override(tools=tools, system_message=SystemMessage(f"{base}\n\n{note}")))

    def wrap_tool_call(self, request, handler):
        name = request.tool_call["name"]
        lim = self.limits.get(name)
        if lim is not None and self._used(request.state.get("messages", [])).get(name, 0) >= lim:
            return ToolMessage(content=f"工具 {name} 的额度已用完，不要再调用它。", name=name,
                               tool_call_id=request.tool_call["id"], status="error")
        return handler(request)


class _RetryState(AgentState):
    invalid_retries: NotRequired[int]


class InvalidToolCallRetryMiddleware(AgentMiddleware[_RetryState]):
    """最后一条模型消息只有无效工具调用、没有有效调用时，让模型带着解析错误重发。"""

    state_schema = _RetryState

    def __init__(self, max_retries: int) -> None:
        super().__init__()
        self.max_retries = max_retries

    @hook_config(can_jump_to=["model"])
    def after_model(self, state: _RetryState, runtime) -> dict[str, Any] | None:
        last = state["messages"][-1] if state.get("messages") else None
        if not isinstance(last, AIMessage) or last.tool_calls or not last.invalid_tool_calls:
            return None
        n = state.get("invalid_retries", 0)
        if n >= self.max_retries:
            return None
        return {"jump_to": "model", "invalid_retries": n + 1}


def build_agent(model: BaseChatModel, tools: list, system_prompt: str, *, allow_ask: bool = True):
    tools = [t for t in tools if allow_ask or t.name != "ask_user"]
    limits = {"search_web": MAX_SEARCHES, "read_page": MAX_READS}
    middleware: list = []
    if allow_ask:
        middleware.append(HumanInTheLoopMiddleware(interrupt_on={"ask_user": {"allowed_decisions": ["respond"]}}))
        limits["ask_user"] = MAX_ASKS
    middleware += [
        ToolBudgetMiddleware(limits),
        InvalidToolCallRetryMiddleware(INVALID_TOOL_CALL_RETRIES),
        ModelCallLimitMiddleware(run_limit=MAX_MODEL_CALLS, exit_behavior="end"),
    ]
    return create_agent(
        model=model,
        tools=tools,
        system_prompt=system_prompt,
        middleware=middleware,
        # 不传 checkpointer：作为子图时借用父图的检查点，命名空间带任务 id；中断后父节点重跑，子图从自己的检查点接着跑。
        name="research_agent",
    )


# ---- 候选答案与受控收尾 ----


def final_answer_text(messages: list[BaseMessage]) -> str | None:
    """agent 的文字答案：最后一条有正文、没有工具调用的模型消息；中间件注入的“超限”消息不算。"""
    for m in reversed(messages):
        if isinstance(m, AIMessage):
            text = m.content if isinstance(m.content, str) else "".join(
                b.get("text", "") for b in m.content if isinstance(b, dict)
            )
            if m.tool_calls or m.invalid_tool_calls or not text.strip() or text.startswith(LIMIT_EXCEEDED_PREFIX):
                continue
            return text.strip()
    return None


def close_out(model: BaseChatModel, messages: list[BaseMessage]) -> tuple[str | None, str]:
    """不带工具再要一次答案。返回 (文字或 None, 说明)。"""
    convo = [m for m in messages if not (isinstance(m, AIMessage) and str(m.content).startswith(LIMIT_EXCEEDED_PREFIX))]
    convo = convo + [HumanMessage(prompts.CLOSE_PROMPT)]
    last_err = ""
    for i in range(CLOSE_RETRIES):
        try:
            res = model.invoke(convo)
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            continue
        text = final_answer_text([res])
        if text:
            return text, f"收尾第 {i + 1} 次成功"
        last_err = "模型没有给出文字"
    return None, f"收尾 {CLOSE_RETRIES} 次都失败：{last_err}"


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


SKIP_MESSAGE = "（用户跳过了这个问题。按你的最佳判断继续，并在答案末尾写明你采用的假设。）"
UNSHOWN_MESSAGE = "（这个问题没有呈现给用户：一次只能问一个问题。按你的最佳判断处理，并在答案末尾写明假设。）"


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
