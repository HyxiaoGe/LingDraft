"""从对话消息里读出来的东西：用户的原话、工具步骤、每轮的用量。

消息是唯一的记录，这些都现算，不另外存。
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from app.graph.research import prompts
from app.graph.research.sources import parse
from app.graph.research.state import ToolStep, Usage

BLOCKED = "lingdraft_blocked"  # 一轮因内容审核停下时，系统补的那条模型消息 response_metadata 里的键，值是原因（见 agent.ContentGuard）
STOPPED = "lingdraft_stopped"  # 用户点了停止时，已经流出来的那部分正文存成的模型消息 response_metadata 里的键
STEP_TOOLS = {"search_web": "search", "read_page": "read"}


def text_of(m: BaseMessage) -> str:
    if isinstance(m.content, str):
        return m.content
    return "".join(b.get("text", "") for b in m.content if isinstance(b, dict) and b.get("type") == "text")


def last_human(messages: list[BaseMessage]) -> int:
    """最后一条用户消息的位置（这一轮从这里开始）；没有是 -1。"""
    return next((i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], HumanMessage)), -1)


def requests(messages: list[BaseMessage]) -> list[str]:
    """用户在对话里说过的话，按先后。"""
    return [text_of(m) for m in messages if isinstance(m, HumanMessage)]


def final_text(messages: list[BaseMessage]) -> str:
    """这一轮的正文：最后一条模型消息的文字；它带着工具调用（这一轮还没结束）或者没有文字时是空串。"""
    for m in reversed(messages):
        if isinstance(m, AIMessage):
            return "" if m.tool_calls else text_of(m).strip()
    return ""


def blocked_reason(m: BaseMessage) -> str | None:
    return m.response_metadata.get(BLOCKED) if isinstance(m, AIMessage) else None


def without_blocked_turns(messages: list[BaseMessage]) -> list[BaseMessage]:
    """去掉因内容审核停下的那几轮（从用户的话到这一轮结束）：带着它们，之后的每次请求都会被拦。"""
    turns: list[list[BaseMessage]] = []
    for m in messages:
        if isinstance(m, HumanMessage) or not turns:
            turns.append([])
        turns[-1].append(m)
    return [m for t in turns if not any(blocked_reason(x) for x in t) for m in t]


def dangling(messages: list[BaseMessage], stopped: bool = False) -> list[ToolMessage]:
    """没有结果的工具调用补一条错误结果，下一轮发给模型的历史才合法：参数一直解析不了、重试用完，
    或者用户点了停止（stopped，结果标上 stopped，页面上这一步显示成停止了）。"""
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    note = prompts.render("agent.stopped_call" if stopped else "agent.dangling_call")
    out = []
    for m in messages:
        if isinstance(m, AIMessage):
            for tc in list(m.tool_calls) + list(m.invalid_tool_calls):
                if tc.get("id") and tc["id"] not in answered:
                    out.append(ToolMessage(content=note, name=tc.get("name") or "unknown", tool_call_id=tc["id"], status="error",
                                           artifact={"stopped": True} if stopped else None))
                    answered.add(tc["id"])
    return out


# ---- 工具步骤 ----


def search_limits(args: dict) -> list[str]:
    return ([f"近 {args['recency_days']} 天"] if args.get("recency_days") else []) + [str(d) for d in args.get("domains") or []]


def step_of(call: dict, result: ToolMessage | None) -> ToolStep | None:
    """一次工具调用 → 过程记录；还没有结果的记成 pending。
    系统替模型纠错的结果（参数解析不了）标了 hidden，运行时没报进度，这里也不列。"""
    name, args, cid = call["name"], call.get("args") or {}, call.get("id") or ""
    kind = STEP_TOOLS.get(name)
    if kind is None or (result is not None and isinstance(result.artifact, dict) and result.artifact.get("hidden")):
        return None
    if kind == "search":
        text, extra = str(args.get("query", "")), {"limits": search_limits(args)}
    else:
        text, extra = str(args.get("url", "")), {}
    if result is None:
        return ToolStep(id=cid, kind=kind, text=text, pending=True, **extra)
    if isinstance(result.artifact, dict) and result.artifact.get("stopped"):
        return ToolStep(id=cid, kind=kind, text=text, ok=False, message="停止了，没有做完", **extra)
    data = parse(result)
    if kind == "search":
        if isinstance(data, list) and result.status != "error":
            dropped = result.artifact.get("blocked", 0) if isinstance(result.artifact, dict) else 0
            return ToolStep(id=cid, kind=kind, text=text, hits=len(data), source_ids=[h["id"] for h in data if h.get("id")],
                            message=prompts.render("agent.hits_blocked", n=dropped) if dropped else "", **extra)
        msg = (data.get("message") or data.get("kind")) if isinstance(data, dict) else text_of(result)
        return ToolStep(id=cid, kind=kind, text=text, ok=False, message=str(msg or ""), **extra)
    if isinstance(data, dict) and data.get("ok") and (data.get("content") or "").strip():
        return ToolStep(id=cid, kind=kind, text=text, body=True, title=data.get("title") or "", source_ids=[data["id"]] if data.get("id") else [])
    msg = (data.get("message") or data.get("kind")) if isinstance(data, dict) else text_of(result)
    return ToolStep(id=cid, kind=kind, text=text, ok=False, message=str(msg or "没有读到正文"))


def collect_steps(messages: list[BaseMessage]) -> list[ToolStep]:
    results = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)}
    steps = (step_of(tc, results.get(tc["id"])) for m in messages if isinstance(m, AIMessage) for tc in m.tool_calls)
    return [s for s in steps if s is not None]


# ---- 用量 ----


def usage_of(messages: list[BaseMessage]) -> Usage:
    """一轮里 agent 的模型调用和搜索、读页次数。token 来自模型返回的用量（流式要开 stream_usage）。"""
    u = Usage()
    for m in messages:
        if isinstance(m, AIMessage) and not blocked_reason(m) and not m.response_metadata.get(STOPPED):  # 系统补的消息不是一次完整的模型调用
            meta: dict[str, Any] = m.usage_metadata or {}  # type: ignore[assignment]
            u.model_calls += 1
            u.input_tokens += meta.get("input_tokens") or 0
            u.output_tokens += meta.get("output_tokens") or 0
            u.cached_tokens += (meta.get("input_token_details") or {}).get("cache_read") or 0
            u.reasoning_tokens += (meta.get("output_token_details") or {}).get("reasoning") or 0
        elif isinstance(m, ToolMessage) and m.name == "search_web":
            u.searches += 1
        elif isinstance(m, ToolMessage) and m.name == "read_page":
            u.reads += 1
    return u
