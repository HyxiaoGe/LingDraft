"""从对话消息里读出来的东西：用户的原话、提问和回答、读过的调研方法、工具步骤、每轮的用量。

消息是唯一的记录，这些都现算，不另外存。
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from app.graph.research import prompts, skills
from app.graph.research.sources import parse
from app.graph.research.state import ToolStep, Usage

LINGDRAFT = "lingdraft"  # 用户消息的 additional_kwargs 里放页面显示用的原话、选中的文字、按钮操作；模型看到的是 content
STEP_TOOLS = {"search_web": "search", "read_page": "read", "load_skill": "skill", "write_report": "report",
              "revise_section": "report", "edit_claim": "report", "exclude_source": "report"}


def text_of(m: BaseMessage) -> str:
    if isinstance(m.content, str):
        return m.content
    return "".join(b.get("text", "") for b in m.content if isinstance(b, dict) and b.get("type") == "text")


def said(m: HumanMessage) -> dict:
    """用户这句话在页面上的样子：{text, quote?, action?}。"""
    return m.additional_kwargs.get(LINGDRAFT) or {"text": text_of(m)}


def last_human(messages: list[BaseMessage]) -> int:
    """最后一条用户消息的位置（这一轮从这里开始）；没有是 -1。"""
    return next((i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], HumanMessage)), -1)


def requests(messages: list[BaseMessage]) -> list[str]:
    """用户在对话里说过的话（原话），按先后。"""
    return [said(m)["text"] for m in messages if isinstance(m, HumanMessage)]


def final_text(messages: list[BaseMessage]) -> str:
    """这一轮的正文：最后一条模型消息的文字；它带着工具调用（这一轮还没结束）或者没有文字时是空串。"""
    for m in reversed(messages):
        if isinstance(m, AIMessage):
            return "" if m.tool_calls else text_of(m).strip()
    return ""


def dangling(messages: list[BaseMessage]) -> list[ToolMessage]:
    """没有结果的工具调用（例如参数一直解析不了、重试用完）补一条错误结果：下一轮发给模型的历史才合法。"""
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    out = []
    for m in messages:
        if isinstance(m, AIMessage):
            for tc in list(m.tool_calls) + list(m.invalid_tool_calls):
                if tc.get("id") and tc["id"] not in answered:
                    out.append(ToolMessage(content=prompts.render("agent.dangling_call"), name=tc.get("name") or "unknown",
                                           tool_call_id=tc["id"], status="error"))
                    answered.add(tc["id"])
    return out


# ---- 提问 ----


def reply(**fields) -> str:
    """ask_user 的工具结果由系统生成：用户的回答、跳过、没呈现分别是不同的键，读回来时看键，不看文字。"""
    return json.dumps(fields, ensure_ascii=False)


def ask_result(m: ToolMessage) -> dict:
    data = parse(m)
    return data if isinstance(data, dict) else {"answer": text_of(m).strip()}


def asks(messages: list[BaseMessage]) -> list[dict]:
    """呈现给用户的提问，按先后：{id, question, options, why, answer, skipped, pending}。没呈现的（同一批里多出来的）不算。"""
    results = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage) and m.name == "ask_user"}
    out = []
    for m in messages:
        if not isinstance(m, AIMessage):
            continue
        for tc in m.tool_calls:
            if tc["name"] != "ask_user":
                continue
            args, res = tc.get("args") or {}, results.get(tc["id"])
            data = ask_result(res) if res is not None else {}
            if data.get("unshown") or (res is not None and res.status == "error"):
                continue
            out.append({"id": tc["id"], "question": str(args.get("question") or ""), "options": list(args.get("options") or []),
                        "why": str(args.get("why") or ""), "answer": data.get("answer"), "skipped": bool(data.get("skipped")),
                        "pending": res is None})
    return out


def user_answers(messages: list[BaseMessage]) -> list[str]:
    """用户对提问的回答（原话）。不需要来源佐证；用户说不清楚的条件由整理步骤按假设处理。"""
    return [f"{a['question']} → {a['answer']}" for a in asks(messages) if a["answer"]]


# ---- 调研方法、工具步骤 ----


def collect_skills(messages: list[BaseMessage]) -> list[str]:
    """读取成功的调研方法，按读取顺序、去重。"""
    ok = {m.tool_call_id for m in messages if isinstance(m, ToolMessage) and m.name == "load_skill" and m.status != "error"}
    out: list[str] = []
    for m in messages:
        if isinstance(m, AIMessage):
            for tc in m.tool_calls:
                name = str((tc.get("args") or {}).get("name") or "").strip()
                if tc["name"] == "load_skill" and tc["id"] in ok and name in skills.catalog() and name not in out:
                    out.append(name)
    return out


def report_guide(messages: list[BaseMessage]) -> str | None:
    """读过的调研方法里的报告结构，给整理用；没读方法时为 None，按草稿的内容分节。"""
    return "\n\n".join(skills.catalog()[n].report.strip() for n in collect_skills(messages)) or None


def search_limits(args: dict) -> list[str]:
    return ([f"近 {args['recency_days']} 天"] if args.get("recency_days") else []) + [str(d) for d in args.get("domains") or []]


def step_of(call: dict, result: ToolMessage | None) -> ToolStep | None:
    """一次工具调用 → 过程记录；还没有结果的记成 pending。提问不在这里（页面上是单独的提问卡）。
    系统替模型纠错的结果（同批第二个改报告的调用、参数解析不了）标了 hidden，运行时没报进度，这里也不列。"""
    name, args, cid = call["name"], call.get("args") or {}, call.get("id") or ""
    kind = STEP_TOOLS.get(name)
    if kind is None or (result is not None and isinstance(result.artifact, dict) and result.artifact.get("hidden")):
        return None
    if kind == "search":
        text, extra = str(args.get("query", "")), {"limits": search_limits(args)}
    elif kind == "read":
        text, extra = str(args.get("url", "")), {}
    elif kind == "skill":
        skill = skills.catalog().get(str(args.get("name", "")).strip())
        if skill is None:
            return None
        text, extra = skill.title, {}
    else:
        text, extra = str(args.get("change") or ""), {}
    if result is None:
        return ToolStep(id=cid, kind=kind, text=text, pending=True, **extra)
    data = parse(result)
    failed = result.status == "error"
    if kind == "search":
        if isinstance(data, list) and not failed:
            return ToolStep(id=cid, kind=kind, text=text, hits=len(data), source_ids=[h["id"] for h in data if h.get("id")], **extra)
        msg = (data.get("message") or data.get("kind")) if isinstance(data, dict) else text_of(result)
        return ToolStep(id=cid, kind=kind, text=text, ok=False, message=str(msg or ""), **extra)
    if kind == "read":
        if isinstance(data, dict) and data.get("ok") and (data.get("content") or "").strip():
            return ToolStep(id=cid, kind=kind, text=text, body=True, title=data.get("title") or "", source_ids=[data["id"]] if data.get("id") else [])
        msg = (data.get("message") or data.get("kind")) if isinstance(data, dict) else text_of(result)
        return ToolStep(id=cid, kind=kind, text=text, ok=False, message=str(msg or "没有读到正文"))
    if kind == "skill":
        return ToolStep(id=cid, kind=kind, text=text, ok=not failed)
    art = result.artifact if isinstance(result.artifact, dict) else {}
    return ToolStep(id=cid, kind=kind, text=text, ok=not failed, message=str(art.get("summary") or "") if not failed else text_of(result)[:200])


def collect_steps(messages: list[BaseMessage]) -> list[ToolStep]:
    results = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)}
    steps = (step_of(tc, results.get(tc["id"])) for m in messages if isinstance(m, AIMessage) for tc in m.tool_calls)
    return [s for s in steps if s is not None]


# ---- 用量 ----


def usage_of(messages: list[BaseMessage]) -> Usage:
    """一轮里调研 agent 的模型调用和搜索、读页次数。token 来自模型返回的用量（流式要开 stream_usage）。"""
    u = Usage()
    for m in messages:
        if isinstance(m, AIMessage):
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
