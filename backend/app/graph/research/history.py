"""从对话消息里读出来的东西：用户的原话、工具步骤、画好的图、每轮的用量。

消息是唯一的记录，这些都现算，不另外存。子 agent 的消息在 task 工具结果的 artifact 里（见 sources.subagent_messages）。
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from pydantic import ValidationError

from app.graph.research import prompts
from app.graph.research.sources import SUBAGENT_TOOL, collect_sources, parse, subagent_messages
from app.graph.research.state import Forecast, GeneratedImage, ToolStep, Usage

BLOCKED = "lingdraft_blocked"  # 一轮因内容审核停下时，系统补的那条模型消息 response_metadata 里的键，值是原因（见 agent.ContentGuard）
STOPPED = "lingdraft_stopped"  # 用户点了停止时，已经流出来的那部分正文存成的模型消息 response_metadata 里的键
GUIDE = "lingdraft_guide"  # 用户插进正在跑的这一轮的话：这条用户消息 additional_kwargs 里的键（见 agent.TurnMiddleware.before_model）
IMAGE_TOOL = "generate_image"
STEP_TOOLS = {"search_web": "search", "read_page": "read", "get_weather": "weather", IMAGE_TOOL: "image", SUBAGENT_TOOL: "task"}
BLOCKED_NOTE = "没通过模型服务的内容审核，内容已去掉"  # 内容审核去掉的搜索、读页结果在页面上的说明
# 没画成的原因在页面上的说法（照 Fusion 的图片卡片），没列的都说“图片生成失败”
IMAGE_FAILURES = {
    "content_filtered": "图片内容未通过模型审核",
    "upstream_timeout": "生成超时",
    "generation_in_progress": "相同的请求正在生成",
    "source_image_unavailable": "这张图片无法直接编辑",
    "run_image_limit_reached": "这一轮画的图已经到上限",
}
IMAGE_FAILED = "图片生成失败"
# 子任务没做成的原因在页面上的说法（task 工具结果 artifact 的 failure，见 app.graph.research.subagent）
TASK_FAILURES = {
    "limit": "这一轮派的子任务已经到上限，没有派",
    "no_report": "子任务没有交回结果",
    "blocked": "子任务查到的内容没通过模型服务的内容审核，停下了",
}
TASK_FAILED = "子任务没做成"


def text_of(m: BaseMessage) -> str:
    if isinstance(m.content, str):
        return m.content
    return "".join(b.get("text", "") for b in m.content if isinstance(b, dict) and b.get("type") == "text")


def is_guide(m: BaseMessage) -> bool:
    """用户插进正在跑的这一轮的话：算这一轮里的一条，不开新的一轮。"""
    return isinstance(m, HumanMessage) and bool(m.additional_kwargs.get(GUIDE))


def is_request(m: BaseMessage) -> bool:
    """开始一轮的用户消息（插进来的话不算）。"""
    return isinstance(m, HumanMessage) and not is_guide(m)


def last_human(messages: list[BaseMessage]) -> int:
    """最后一轮开始的那条用户消息的位置（这一轮从这里开始，插进来的话不算）；没有是 -1。"""
    return next((i for i in range(len(messages) - 1, -1, -1) if is_request(messages[i])), -1)


def requests(messages: list[BaseMessage]) -> list[str]:
    """用户在对话里开始每一轮的那句话，按先后（插进来的话不算）。"""
    return [text_of(m) for m in messages if is_request(m)]


def final_text(messages: list[BaseMessage]) -> str:
    """这一轮的正文：最后一条模型消息的文字；它带着工具调用（这一轮还没结束）或者没有文字时是空串。"""
    for m in reversed(messages):
        if isinstance(m, AIMessage):
            return "" if m.tool_calls else text_of(m).strip()
    return ""


def blocked_reason(m: BaseMessage) -> str | None:
    return m.response_metadata.get(BLOCKED) if isinstance(m, AIMessage) else None


def streamed_something(m: AIMessage) -> bool:
    """点停止时流出来的半截消息里有东西：有正文，或者只想了还没写。主 agent、子 agent 的都按这个决定存不存。"""
    return bool(text_of(m).strip() or m.additional_kwargs.get("reasoning_content"))


def is_stopped_thought(m: BaseMessage) -> bool:
    """点停止时只想了、正文一个字没写的那半截：存着给页面看（思考了什么、想了多久），不发给模型。
    发过去就是一条没有正文的回复，接口不一定收；想了一半的东西对下一轮也没用。不发和这一条没存时发出去的一样。"""
    return isinstance(m, AIMessage) and bool(m.response_metadata.get(STOPPED)) and not m.tool_calls and not text_of(m).strip()


def without_stopped_thoughts(messages: list[BaseMessage]) -> list[BaseMessage]:
    return [m for m in messages if not is_stopped_thought(m)]


def without_blocked_turns(messages: list[BaseMessage]) -> list[BaseMessage]:
    """去掉因内容审核停下的那几轮（从用户的话到这一轮结束）：带着它们，之后的每次请求都会被拦。"""
    turns: list[list[BaseMessage]] = []
    for m in messages:
        if is_request(m) or not turns:
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


def not_run(m: ToolMessage) -> bool:
    """系统替模型补的结果，工具没真执行：参数解析不了的纠错（hidden）、子 agent 超了预算的（over_budget，见 subagent.ToolBudget）。"""
    return isinstance(m.artifact, dict) and bool(m.artifact.get("hidden") or m.artifact.get("over_budget"))


def step_of(call: dict, result: ToolMessage | None) -> ToolStep | None:
    """一次工具调用 → 过程记录；还没有结果的记成 pending。
    工具没真执行的（not_run），运行时没报进度，这里也不列。"""
    name, args, cid = call["name"], call.get("args") or {}, call.get("id") or ""
    kind = STEP_TOOLS.get(name)
    if kind is None or (result is not None and not_run(result)):
        return None
    if kind == "search":
        text, extra = str(args.get("query", "")), {"limits": search_limits(args)}
    elif kind == "image":
        text = str(args.get("prompt", ""))
        extra = {"aspect_ratio": str(args.get("aspect_ratio") or ""), "edit": bool(args.get("source_image_id"))}
    elif kind == "task":
        text, extra = str(args.get("title", "")), {"prompt": str(args.get("prompt", ""))}
    else:
        text, extra = str(args.get("url" if kind == "read" else "location", "")), {}
    if result is None:
        return ToolStep(id=cid, kind=kind, text=text, pending=True, **extra)
    if isinstance(result.artifact, dict) and result.artifact.get("stopped"):
        return ToolStep(id=cid, kind=kind, text=text, ok=False, message="已停止生成" if kind == "image" else "停止了，没有做完", **extra)
    if kind == "image":
        return _image_step(cid, text, extra, result)
    if kind == "task":
        return _task_step(cid, text, extra, result)
    data = parse(result)
    if kind == "weather":
        return _weather_step(cid, text, data, result)
    if kind == "search":
        if isinstance(data, list) and result.status != "error":
            dropped = result.artifact.get("blocked", 0) if isinstance(result.artifact, dict) else 0
            return ToolStep(id=cid, kind=kind, text=text, hits=len(data), source_ids=[h["id"] for h in data if h.get("id")],
                            message=f"{dropped} 条没通过内容审核，已去掉" if dropped else "", **extra)
        return ToolStep(id=cid, kind=kind, text=text, ok=False, message=_failure(data, result), **extra)
    if isinstance(data, dict) and data.get("ok") and (data.get("content") or "").strip():
        return ToolStep(id=cid, kind=kind, text=text, body=True, title=data.get("title") or "", source_ids=[data["id"]] if data.get("id") else [])
    return ToolStep(id=cid, kind=kind, text=text, ok=False, message=_failure(data, result) or "没有读到正文")


def _failure(data: Any, result: ToolMessage) -> str:
    """没做成的原因（页面上显示）。内容审核去掉的结果，模型看的说明是英文的，页面上换成中文的。"""
    if not isinstance(data, dict):
        return text_of(result)
    if data.get("kind") == "content_blocked":
        return BLOCKED_NOTE
    return str(data.get("message") or data.get("kind") or "")


def _weather_step(cid: str, text: str, data: Any, result: ToolMessage) -> ToolStep:
    """卡片要的整份预报在 artifact 里；模型看的只有逐日和逐小时的摘要（见 research_tools.get_weather）。"""
    if isinstance(data, dict) and data.get("ok") and result.status != "error":
        try:
            fc = Forecast.model_validate(result.artifact.get("forecast") if isinstance(result.artifact, dict) else None)
            return ToolStep(id=cid, kind="weather", text=text, title=fc.location, weather=fc)
        except ValidationError:
            return ToolStep(id=cid, kind="weather", text=text, ok=False, message="天气结果的格式不对")
    return ToolStep(id=cid, kind="weather", text=text, ok=False, message=_failure(data, result) or "没查到")


def image_of(m: BaseMessage) -> GeneratedImage | None:
    """一条生图结果里画好的图；没画成是 None。模型看的只是一句话，图的信息在 artifact 里（见 research_tools.generate_image）。"""
    art = m.artifact if isinstance(m, ToolMessage) and m.name == IMAGE_TOOL else None
    if not isinstance(art, dict) or not art.get("image"):
        return None
    try:
        return GeneratedImage.model_validate(art["image"])
    except ValidationError:
        return None


def _image_step(cid: str, text: str, extra: dict, result: ToolMessage) -> ToolStep:
    if img := image_of(result):
        return ToolStep(id=cid, kind="image", text=text, image=img, **{**extra, "aspect_ratio": img.aspect_ratio})
    code = result.artifact.get("error_code") if isinstance(result.artifact, dict) else None  # 参数没通过校验的没有 artifact
    return ToolStep(id=cid, kind="image", text=text, ok=False, message=IMAGE_FAILURES.get(code, IMAGE_FAILED), **extra)


def _task_step(cid: str, text: str, extra: dict, result: ToolMessage) -> ToolStep:
    """子任务：来源是它的子 agent 查到的。子 agent 每一步做了什么由运行器从同一条结果里展开（见 api.research_runner.conversation）。"""
    sids = [s.id for s in collect_sources(subagent_messages(result))]
    if result.status != "error":
        return ToolStep(id=cid, kind="task", text=text, source_ids=sids, **extra)
    code = result.artifact.get("failure") if isinstance(result.artifact, dict) else None
    return ToolStep(id=cid, kind="task", text=text, ok=False, source_ids=sids, message=TASK_FAILURES.get(code, TASK_FAILED), **extra)


def collect_steps(messages: list[BaseMessage]) -> list[ToolStep]:
    results = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)}
    steps = (step_of(tc, results.get(tc["id"])) for m in messages if isinstance(m, AIMessage) for tc in m.tool_calls)
    return [s for s in steps if s is not None]


# ---- 用量 ----


def usage_of(messages: list[BaseMessage]) -> Usage:
    """一轮里 agent 的模型调用和搜索、读页、查天气次数，画成了几张图，派了几个子 agent。token 来自模型返回的用量（流式要开 stream_usage）。
    只算这些消息自己的：子 agent 的另算（见 subagent_usage）。"""
    u = Usage()
    for m in messages:
        if isinstance(m, AIMessage) and not blocked_reason(m) and not m.response_metadata.get(STOPPED):  # 系统补的消息不是一次完整的模型调用
            meta: dict[str, Any] = m.usage_metadata or {}  # type: ignore[assignment]
            u.model_calls += 1
            u.input_tokens += meta.get("input_tokens") or 0
            u.output_tokens += meta.get("output_tokens") or 0
            u.cached_tokens += (meta.get("input_token_details") or {}).get("cache_read") or 0
            u.reasoning_tokens += (meta.get("output_token_details") or {}).get("reasoning") or 0
        elif isinstance(m, ToolMessage) and not_run(m):  # 没真执行的不算
            continue
        elif isinstance(m, ToolMessage) and m.name == "search_web":
            u.searches += 1
        elif isinstance(m, ToolMessage) and m.name == "read_page":
            u.reads += 1
        elif isinstance(m, ToolMessage) and m.name == "get_weather":
            u.weather += 1
        elif image_of(m):
            u.images += 1
        elif subagent_messages(m):
            u.subagents += 1
    return u


def subagent_usage(messages: list[BaseMessage]) -> Usage:
    """一轮里所有子 agent 加起来的用量。"""
    return usage_of([x for m in messages for x in subagent_messages(m)])
