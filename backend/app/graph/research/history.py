"""从对话消息里读出来的东西：用户的原话、工具步骤、画好的图、每轮的用量。

消息是唯一的记录，这些都现算，不另外存。子 agent 的消息在 task 工具结果的 artifact 里（见 sources.subagent_messages）。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from pydantic import ValidationError

from app.costs import COST
from app.graph.research import prompts
from app.graph.research.sources import (DOCS_TOOL, KB_TOOL, LIBRARY_TOOL, SUBAGENT_TOOL, collect_sources, doc_snippets, kb_hits, parse, search_hits,
                                         subagent_messages)
from app.graph.research.state import (
    AttachedFile,
    AttachedImage,
    CommandRun,
    Forecast,
    GeneratedImage,
    Itinerary,
    Place,
    PlaceList,
    Route,
    RouteLeg,
    RoutePlan,
    ToolStep,
    TransitPlan,
    TravelList,
    TravelService,
    TravelStop,
    Usage,
    WeatherDay,
    WeatherHour,
    WrittenFile,
)

BLOCKED = "lingdraft_blocked"  # 一轮因内容审核停下时，系统补的那条模型消息 response_metadata 里的键，值是原因（见 agent.ContentGuard）
STOPPED = "lingdraft_stopped"  # 用户点了停止时，已经流出来的那部分正文存成的模型消息 response_metadata 里的键
GUIDE = "lingdraft_guide"  # 用户插进正在跑的这一轮的话：这条用户消息 additional_kwargs 里的键（见 agent.TurnMiddleware.abefore_model）
ATTACHED = "lingdraft_image"  # 用户这句话带的图（AttachedImage 的列表，11-08 以前是一张）：用户消息 additional_kwargs 里的键（给模型的怎么组织见 app.tools.vision）
MAX_ATTACHED = 4  # 一句话最多带几张图
FILES = "lingdraft_files"  # 用户这句话带的文件（AttachedFile 的列表）：用户消息 additional_kwargs 里的键（给模型的怎么组织见 app.tools.files）
MAX_FILES = 5  # 一句话最多带几个文件（2026-11-08 Sean 定）
FILES_BRIEF = "lingdraft_files_brief"  # 压缩第一层压过的那句话（只在发给模型的那份里）：带的文件只给目录和开头（见 app.tools.files）
IMAGE_TOOL = "generate_image"
VIEW_TOOL = "view_image"  # 看图工具：看不见图的模型问图里有什么（见 app.tools.vision）
FILE_TOOL = "read_file"  # 读用户发的文件、模型写的文件：按段读、在文件里找（见 app.tools.files）
CREATE_TOOL, EDIT_TOOL = "create_file", "edit_file"  # 模型写文件：新建、出新一版（见 app.tools.write_file）
WRITE_TOOLS = (CREATE_TOOL, EDIT_TOOL)
RUN_TOOL = "run_command"  # 在沙箱里跑命令（工作任务里才给，见 app.tools.run_command）
ASK_TOOL = "ask_user"  # 模型做到一半停下来问用户，等回答了接着做（见 app.tools.ask_user）
MAX_ASKS = 2  # 一轮最多问几次（2026-11-10 Sean 定）：再问的不问，让模型按自己的判断做
MAX_ASK_OPTIONS = 4  # 一个问题最多给几个选项
PLACE_TOOL, ROUTE_TOOL = "place_search", "route_compare"  # 地图的两个工具（见 app.tools.maps_mcp）
FLIGHT_TOOL, TRAIN_TOOL = "search_flights", "search_trains"  # flyai 的两个工具（见 app.tools.flyai_mcp）
ITINERARY_TOOL = "present_itinerary"  # 灵稿自己的排行程（见 app.tools.flyai_mcp）
TRAVEL_KINDS = {FLIGHT_TOOL: "flight", TRAIN_TOOL: "train"}
STEP_TOOLS = {"search_web": "search", "read_page": "read", "get_weather": "weather", PLACE_TOOL: "place", ROUTE_TOOL: "route",
              **TRAVEL_KINDS, ITINERARY_TOOL: "itinerary", IMAGE_TOOL: "image", VIEW_TOOL: "view", FILE_TOOL: "file", CREATE_TOOL: "write", EDIT_TOOL: "write", SUBAGENT_TOOL: "task",
              LIBRARY_TOOL: "library", DOCS_TOOL: "docs", KB_TOOL: "knowledge", RUN_TOOL: "command"}
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
IMAGE_GONE = "这张图已经不在了"  # 2026-11-04 以前灵稿自己存的图，没搬到 image-service（见 state.GeneratedImage）
# 子任务没做成的原因在页面上的说法（task 工具结果 artifact 的 failure，见 app.graph.research.subagent）
TASK_FAILURES = {
    "limit": "这一轮派的子任务已经到上限，没有派",
    "no_report": "子任务没有交回结果",
    "blocked": "子任务查到的内容没通过模型服务的内容审核，停下了",
    "unavailable": "子任务的模型服务暂时不可用，没做完",
    "error": "子任务出错了，没做完",
}
TASK_FAILED = "子任务没做成"
# 写文件没写成的原因在页面上的说法（create_file / edit_file 结果 artifact 的 failure，见 app.tools.write_file）
WRITE_FAILURES = {
    "cut_off": "内容太长，输出被截断了，没写完",
    "broken": "内容没写完整，没写成",
    "not_found": "没找到要改的文件",
    "read_only": "用户发的文件不能改，没写成",
    "no_match": "要改的地方没对上，没改成",
    "no_change": "内容没有变化，没出新版本",
    "too_long": "文件太长了，没写成",
    "empty": "内容是空的，没写成",
    "unavailable": "文件暂时存不了",
}
WRITE_FAILED = "没写成"
# 命令没跑成的原因在页面上的说法（run_command 结果 artifact 的 failure，见 app.tools.run_command）。跑了、命令自己失败的不在这里（见 state.CommandRun）
RUN_FAILURES = {
    "empty": "命令是空的，没跑",
    "busy": "运行环境正忙，没跑成",
    "unavailable": "运行环境暂时用不了",
    "limit": "今天运行命令的次数用完了",
    "reset": "工作目录满了，已经清空，没跑成",
}
RUN_FAILED = "没跑成"


def text_of(m: BaseMessage) -> str:
    if isinstance(m.content, str):
        return m.content
    return "".join(b.get("text", "") for b in m.content if isinstance(b, dict) and b.get("type") == "text")


# 模型类把思考放进 additional_kwargs 的键（见 research_llm.make_chat_model）。reasoning_content 是给人看的思考：DeepSeek 给原文，
# 豆包 2.x 给摘要；encrypted_content 是豆包加密的思考原文，只用来原样传回。读思考用 thinking_of，这两个键只在收发、压缩去掉思考时用。
REASONING = "reasoning_content"
ENCRYPTED = "encrypted_content"
THINKING_KEYS = (REASONING, ENCRYPTED)


def without_thinking(m: BaseMessage) -> BaseMessage:
    """去掉模型消息里的思考（豆包的加密原文也去掉），别的消息原样返回。压缩时用，换了模型时发给新模型的之前几轮也这样（见 agent.ModelSelect）。"""
    if isinstance(m, AIMessage) and any(k in m.additional_kwargs for k in THINKING_KEYS):
        return m.model_copy(update={"additional_kwargs": {k: v for k, v in m.additional_kwargs.items() if k not in THINKING_KEYS}})
    return m


# 用户这句话开没开思考（输入框旁边的开关），记在用户消息的 additional_kwargs 里（不会发给模型）；没有的按选项的默认（见 model_catalog.Catalog.plan）
THINK = "think"


def thinking_switch(m: BaseMessage) -> bool | None:
    v = m.additional_kwargs.get(THINK)
    return v if isinstance(v, bool) else None


# 对话 / 工作（2026-11-10 Sean 定分开；11-11 定：新建时选，整段对话不变，工作任务不能变回对话）。
# 这段对话是哪种记在 state 的这一项里（见 work_mode）；每句话是不是 Work 也记在用户消息的 additional_kwargs 里（同一个键），
# 由运行器按对话的写上（见 research_runner._aaccept），没有的是 Chat。
# Work 的一轮：系统提示词多一段、多一个问用户的工具（见 agent.TurnMiddleware、ModelSelect）
WORK = "work"


def work_switch(m: BaseMessage) -> bool | None:
    v = m.additional_kwargs.get(WORK)
    return v if isinstance(v, bool) else None


def work_mode(state: Mapping[str, Any]) -> bool:
    """这段对话是不是工作任务。新建时定下、记在 state 里；11-11 以前的对话每句话都能换、没记这一项：
    有过 Work 的轮（或者发了还没开始的那句是 Work）就算工作任务，之后不变回去。"""
    v = state.get(WORK)
    if isinstance(v, bool):
        return v
    return (any(t.work for t in state.get("turns") or [])
            or any(bool(work_switch(m)) for m in state.get("messages") or [] if isinstance(m, HumanMessage)))


# 这段对话用户说了“别再问了”（问题卡上点的，2026-11-11 Sean 定）：记在 state 的这一项里。之后 Work 的轮也不给问用户的工具，
# 系统提示词的 work 一段换成 work_quiet（见 agent.asking、prompt_keys）；页面上能恢复（见 research_runner._aset_asks）。按对话记，不按每句话
ASKS_OFF = "asks_off"


def thinking_of(m: BaseMessage) -> str:
    """消息里的思考，按 LangChain 的标准 content blocks 读：DeepSeek、豆包放在 additional_kwargs 里，Claude 这类放在 content 里，读法一样。
    模型类要把提供方标成没有专门解析的名字（见 ChatArk）：标成 openai 的话，content_blocks 按 OpenAI 的格式读，不看 additional_kwargs。"""
    return "".join(b.get("reasoning") or "" for b in m.content_blocks if b.get("type") == "reasoning")


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
    return bool(text_of(m).strip() or thinking_of(m))


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
    elif kind == "place":
        text, extra = str(args.get("query", "")), {"limits": place_limits(args)}
    elif kind == "route":
        text, extra = f"{_endpoint(args.get('origin'))} → {_endpoint(args.get('destination'))}", {"limits": route_limits(args)}
    elif kind in ("flight", "train"):
        text, extra = f"{_text(args.get('origin'))} → {_text(args.get('destination'))}", {"limits": travel_limits(args)}
    elif kind == "itinerary":
        text, extra = _text(args.get("title")), {}
    elif kind == "library":
        text, extra = _text(args.get("libraryName")), {}
    elif kind == "docs":
        text, extra = _text(args.get("query")), {"limits": [_text(args.get("libraryId"))] if args.get("libraryId") else []}
    elif kind == "view":
        text, extra = _text(args.get("question")), {}
    elif kind == "knowledge":  # 只查了一个资料库的：limits 是那个库的名字
        text, extra = _text(args.get("query")), {"limits": [_text(args.get("library"))] if args.get("library") else []}
    elif kind == "write":  # 标题在参数里，写好了用结果里的（改文件没给新标题的是空的）；改的是哪份记在 target，页面按它对上标题、归到同一份文件
        w = written_of(result) if result is not None else None
        text = w.title if w else _text(args.get("title"))
        extra = {"edit": name == EDIT_TOOL, "target": _text(args.get("file_id")) if name == EDIT_TOOL else ""}
    elif kind == "file":  # 读的哪个文件：名字在结果的 artifact 里（参数只有编号）；还没结果、没读成的是编号，页面按这段对话发的文件对上名字。
        # 在文件里找的，limits 是找的词；按段读的，parts 是哪几段（有找的词时工具不看段号）
        art = result.artifact if result is not None and isinstance(result.artifact, dict) else {}
        text = _text(art.get("name")) or _text(args.get("file_id"))
        q = _text(args.get("query"))
        extra = {"limits": [q] if q else [], "parts": "" if q else _text(args.get("parts"))}
    elif kind == "command":
        text, extra = _text(args.get("command")), {}
    elif kind == "read":
        text, extra = str(args.get("url", "")), {}
    else:  # 天气：选了当前位置的，地名那一项是 current_location
        text, extra = _endpoint(args.get("location")), {}
    if result is None:
        return ToolStep(id=cid, kind=kind, text=text, pending=True, **extra)
    if isinstance(result.artifact, dict) and result.artifact.get("stopped"):
        return ToolStep(id=cid, kind=kind, text=text, ok=False, message="已停止生成" if kind == "image" else "停止了，没有做完", **extra)
    if kind == "image":
        return _image_step(cid, text, extra, result)
    if kind == "task":
        return _task_step(cid, text, extra, result)
    if kind in ("place", "route"):
        return _maps_step(kind, cid, text, extra, result)
    if kind in ("flight", "train"):
        return _travel_step(kind, cid, text, extra, result)
    if kind == "itinerary":
        return _itinerary_step(cid, text, result)
    if kind in ("library", "docs"):
        return _docs_step(kind, cid, text, extra, result)
    if kind == "view":
        return ToolStep(id=cid, kind=kind, text=text, ok=result.status != "error", message="" if result.status != "error" else "没看成", **extra)
    if kind == "knowledge":  # 查到几块、它们的来源（同一段的几块是一个来源）
        if result.status == "error":
            return ToolStep(id=cid, kind=kind, text=text, ok=False, message="没查成", **extra)
        hits = kb_hits(result)
        return ToolStep(id=cid, kind=kind, text=text, hits=len(hits), source_ids=list(dict.fromkeys(h["id"] for h in hits)), **extra)
    if kind == "write":
        if w := written_of(result):
            return ToolStep(id=cid, kind=kind, text=text, file=w, **extra)
        code = result.artifact.get("failure") if isinstance(result.artifact, dict) else None
        return ToolStep(id=cid, kind=kind, text=text, ok=False, message=WRITE_FAILURES.get(code, WRITE_FAILED), **extra)
    if kind == "file":
        ok = result.status != "error"
        art = result.artifact if isinstance(result.artifact, dict) else {}
        return ToolStep(id=cid, kind=kind, text=text, ok=ok, hits=int(art.get("found") or 0), message="" if ok else "没读成", **extra)
    if kind == "command":  # 跑了的带着结果（命令自己失败、被杀掉的也是跑了，页面按退出码和原因说）；没跑成的说原因
        if run := run_of(result):
            return ToolStep(id=cid, kind=kind, text=text, ok=run.ok, run=run)
        code = result.artifact.get("failure") if isinstance(result.artifact, dict) else None
        return ToolStep(id=cid, kind=kind, text=text, ok=False, message=RUN_FAILURES.get(code, RUN_FAILED))
    data = parse(result)
    if kind == "weather":
        return _weather_step(cid, text, data, result)
    if kind == "search":
        if (hits := search_hits(data)) is not None and result.status != "error":
            dropped = result.artifact.get("blocked", 0) if isinstance(result.artifact, dict) else 0
            return ToolStep(id=cid, kind=kind, text=text, hits=len(hits), source_ids=[h["id"] for h in hits if h.get("id")],
                            message=f"{dropped} 条没通过内容审核，已去掉" if dropped else "", **extra)
        return ToolStep(id=cid, kind=kind, text=text, ok=False, message=_failure(data, result, SEARCH_FAILURES), **extra)
    if isinstance(data, dict) and data.get("ok") and (data.get("content") or "").strip():
        return ToolStep(id=cid, kind=kind, text=text, body=True, title=data.get("title") or "", source_ids=[data["id"]] if data.get("id") else [])
    return ToolStep(id=cid, kind=kind, text=text, ok=False, message=_failure(data, result, READ_FAILURES) or "没有读到正文")


# 搜索、读页没做成时的错误码 → 页面上的说法。给模型的说明是服务给的英文（见 agent-tools 的 search、reader 的 MCP）；
# unavailable 是没调成（网关、服务连不上，见 app.tools.web_mcp）
SEARCH_FAILURES = {
    "bad_arguments": "搜索的参数不对",
    "quota_exhausted": "搜索今天的额度用完了",
    "timeout": "搜索超时了",
    "rate_limited": "搜索服务被限流了",
    "upstream_error": "搜索服务出错了",
    "not_configured": "搜索服务没配置好",
    "unavailable": "搜索服务暂时连不上",
    "tool_error": "搜索没调成",
}
READ_FAILURES = {
    "invalid_url": "网址不对",
    "unsafe_url": "这个网址不能读",
    "page_not_found": "网页不存在",
    "page_error": "网站返回了错误页",
    "access_blocked": "网站拦了自动读取",
    "empty_content": "网页上没有能读的正文",
    "timeout": "网页没在时限内打开",
    "rate_limited": "读网页的服务被限流了",
    "quota_exhausted": "读网页今天的额度用完了",
    "upstream_error": "读网页的服务出错了",
    "unavailable": "读网页的服务暂时连不上",
    "tool_error": "读网页没调成",
}


# 找库、查文档没做成时的错误码 → 页面上的说法（见 app.tools.docs_mcp）。tool_error 是 Context7 回了错，多半是找不到这个库、这个库的文档，
# 给模型的是 Context7 的原话
DOCS_FAILURES = {
    "tool_error": "Context7 没给出结果",
    "rate_limited": "Context7 限流了，这个月的额度可能用完了",
    "quota_exhausted": "查文档今天的次数用完了",
    "unavailable": "查文档的服务暂时连不上",
    "content_blocked": BLOCKED_NOTE,
}


def _docs_step(kind: str, cid: str, text: str, extra: dict, result: ToolMessage) -> ToolStep:
    """找库：找到几个候选库；查文档：几段文档、它们的来源。"""
    if result.status == "error":
        return ToolStep(id=cid, kind=kind, text=text, ok=False, message=_failure(None, result, DOCS_FAILURES), **extra)
    if kind == "library":
        libraries = result.artifact.get("libraries") if isinstance(result.artifact, dict) else None
        return ToolStep(id=cid, kind=kind, text=text, hits=len(libraries or []), **extra)
    snippets = doc_snippets(result)
    return ToolStep(id=cid, kind=kind, text=text, hits=len(snippets), source_ids=list(dict.fromkeys(s["id"] for s in snippets)), **extra)


def tool_error(result: ToolMessage) -> dict | None:
    """搜索、读页没做成时的错误（{code, retryable, status?}，在结果的 artifact 里，见 app.tools.web_mcp）。11-05 以前存下的没有。"""
    art = result.artifact
    err = art.get("error") if isinstance(art, dict) else None
    return err if isinstance(err, dict) and err.get("code") else None


def _failure(data: Any, result: ToolMessage, failures: dict[str, str]) -> str:
    """没做成的原因（页面上显示）：只给表里我们自己写的说法，内容审核去掉的也一样。给模型的说明是服务给的英文，不上页面。
    表里没有的错误码、HTTP 状态码、不是结构化的结果（比如替没回来的调用补的英文说明）都不显示，给空，页面上用通用说法（“没搜成：出错了”）。
    11-05 以前存下的结果没有错误码，按结果里的 kind 对表；那时的 message 里带着 HTTP 状态码（“搜索服务出错：HTTP 502”），也不显示。"""
    if isinstance(data, dict) and data.get("kind") == "content_blocked":
        return BLOCKED_NOTE
    if err := tool_error(result):
        return failures.get(err["code"], "")
    if isinstance(data, dict):
        return failures.get(str(data.get("kind") or ""), "")
    return ""


# weather-service 的错误码（见 agent-tools 的 services/weather/README.md）→ 页面上的说法；给模型的说明是服务给的英文
WEATHER_FAILURES = {
    "ambiguous_location": "对得上好几个地方",
    "location_not_found": "和风天气里没找到这个地方",
    "invalid_arguments": "查询的参数不对",
    "location_unavailable": "拿不到当前位置",
    "location_declined": "没用当前位置",  # 灵稿自己回的：用户没给位置（见 app.tools.location）
}


def forecast_of(data: dict) -> Forecast:
    """weather-service 的整份结果（MCP 的 structuredContent）换成卡片要的样子。字段不对抛 ValidationError、KeyError、TypeError。"""
    days = [WeatherDay(date=d["date"], day=d.get("day_weather", ""), night=d.get("night_weather", ""), high=d["high_c"], low=d["low_c"],
                       wind=" ".join(x for x in (d.get("day_wind_direction"), d.get("day_wind_power")) if x))
            for d in data["forecast_days"]]
    hours = [WeatherHour(time=h["time"], temp=h["temp_c"], text=h.get("weather", ""), pop=h.get("pop"), precip=h.get("precip_mm"))
             for h in data.get("hourly") or []]
    return Forecast(location=data["resolved_location"], today=data["local_today"], fetched_at=data["fetched_at"],
                    requested_date=data.get("requested_date"), days=days, hourly=hours)


def _month_day(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.month}月{d.day}日"


def _not_covered(data: dict) -> str:
    """问的那天不在预报范围里：服务一天都不给，只给能查的头尾两天（forecast_range）。"""
    rng = data.get("forecast_range") or {}
    try:
        asked = _month_day(data["requested_date"])
        return f"{asked}不在预报范围里，能查 {_month_day(rng['first_date'])}–{_month_day(rng['last_date'])}"
    except (KeyError, TypeError, ValueError):
        return "问的那天不在预报范围里"


def _weather_step(cid: str, text: str, data: Any, result: ToolMessage) -> ToolStep:
    """卡片要的整份预报在 artifact 里（MCP 的 structuredContent，见 app.tools.weather_mcp）；模型看的没有逐小时明细。"""
    art = result.artifact if isinstance(result.artifact, dict) else {}
    if "forecast" in art or "structured_content" not in art:  # 11-04 以前自己查和风时存下的
        return _old_weather_step(cid, text, data, result)
    sc = art["structured_content"]
    if isinstance(sc, dict) and sc.get("ok") is True and result.status != "error":
        try:
            fc = forecast_of(sc)
        except (ValidationError, KeyError, TypeError):
            return ToolStep(id=cid, kind="weather", text=text, ok=False, message="天气结果的格式不对")
        if not fc.days:  # 没有卡片可画
            return ToolStep(id=cid, kind="weather", text=text, title=fc.location, ok=False, message=_not_covered(sc))
        return ToolStep(id=cid, kind="weather", text=text, title=fc.location, weather=fc)
    err = sc.get("error") if isinstance(sc, dict) else None
    err = err if isinstance(err, dict) else {}
    message = WEATHER_FAILURES.get(err.get("code"), "天气服务暂时查不到")
    if err.get("code") == "ambiguous_location" and err.get("candidates"):
        message += "：" + "、".join(str(c) for c in err["candidates"])
    return ToolStep(id=cid, kind="weather", text=text, ok=False, message=message)


def _old_weather_step(cid: str, text: str, data: Any, result: ToolMessage) -> ToolStep:
    if isinstance(data, dict) and data.get("ok") and result.status != "error":
        try:
            fc = Forecast.model_validate(result.artifact.get("forecast") if isinstance(result.artifact, dict) else None)
            return ToolStep(id=cid, kind="weather", text=text, title=fc.location, weather=fc)
        except ValidationError:
            return ToolStep(id=cid, kind="weather", text=text, ok=False, message="天气结果的格式不对")
    return ToolStep(id=cid, kind="weather", text=text, ok=False, message=_failure(data, result, WEATHER_FAILURES) or "没查到")


# ---- 地图（maps-service 的 place_search、route_compare，见 app.tools.maps_mcp）----

MODE_NAMES = {"driving": "驾车", "transit": "公共交通", "walking": "步行", "bicycling": "骑行"}
CURRENT_LOCATION = "current_location"
# maps-service 的错误码（见 agent-tools 的 services/maps/README.md）→ 页面上的说法；给模型的说明是服务给的英文。
# unavailable、quota_exhausted 也可能是灵稿自己回的（服务没连上、网关今天的上限到了，见 maps_mcp）
MAPS_FAILURES = {
    "ambiguous_location": "对得上好几个地方",
    "place_not_found": "地图里没找到这个地方",
    "place_id_unresolved": "地点编号查不到",
    "location_unavailable": "拿不到当前位置",
    "location_declined": "没用当前位置",  # 灵稿自己回的：用户没给位置（见 app.tools.location）
    "no_route": "算不出路线",
    "invalid_arguments": "查询的参数不对",
    "quota_exhausted": "地图今天的额度用完了",
    "unavailable": "地图服务暂时查不到",
    "timeout": "地图查询超时了",
    "tool_error": "地图没调成",
    "station_city": "车站所在的城市没填对，没去查",  # 灵稿自己核对的（见 flyai_mcp.check_route_stations），模型会改了再查
}
MAPS_FAILED = "地图服务暂时查不到"


def _endpoint(value: Any) -> str:
    text = str(value or "").strip()
    return "当前位置" if text == CURRENT_LOCATION else text


def place_limits(args: dict) -> list[str]:
    """查地点时在哪查：某地附近、当前位置附近，或者哪个城市。"""
    if args.get("anchor_source") == CURRENT_LOCATION:
        return ["当前位置附近"]
    if args.get("anchor_source") == "named" and args.get("near"):
        return [f"{args['near']}附近"]
    return [str(args["city"])] if args.get("city") else []


def route_limits(args: dict) -> list[str]:
    modes = args.get("modes") if isinstance(args.get("modes"), list) else []
    return [MODE_NAMES.get(str(m), str(m)) for m in modes]


def _text(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else ""


def places_of(data: dict, fetched_at: str = "") -> PlaceList:
    """place_search 的整份结果（MCP 的 structuredContent）换成地点卡要的样子。字段不对抛 ValidationError、KeyError、TypeError。"""
    anchor = data.get("anchor") if isinstance(data.get("anchor"), dict) else {}
    near = CURRENT_LOCATION if anchor.get("source") == CURRENT_LOCATION else _text(anchor.get("label"))
    places = [Place(name=p["name"], address=_text(p.get("address")), district=_text(p.get("district")), city=_text(p.get("city")),
                    category=_text(p.get("category")), distance_m=p.get("distance_m"), rating=p.get("rating"),
                    cost=p.get("reference_cost_yuan"), open_hours=_text(p.get("open_hours")), photo_url=_text(p.get("photo_url")),
                    map_url=_text(p.get("map_url")), provider=_text(p.get("provider")))
              for p in data["places"]]
    return PlaceList(query=_text(data.get("query")), near=_endpoint(near), city=_text(data.get("city")), places=places,
                     limitations=[str(x) for x in data.get("limitations") or []], fetched_at=fetched_at)


def _legs(raw: Any) -> list[RouteLeg]:
    return [RouteLeg(kind=_text(g.get("kind")) or "other", line_name=_text(g.get("line_name")), departure_stop=_text(g.get("departure_stop")),
                     arrival_stop=_text(g.get("arrival_stop")), via_stop_count=g.get("via_stop_count"), entrance=_text(g.get("entrance")),
                     exit=_text(g.get("exit")), distance_m=g.get("distance_m"), duration_s=g.get("duration_s"))
            for g in raw or []]


def routes_of(data: dict, fetched_at: str = "") -> RoutePlan:
    """route_compare 的整份结果换成路线卡要的样子。字段不对抛 ValidationError、KeyError、TypeError。"""
    def label(end: Any) -> str:
        end = end if isinstance(end, dict) else {}
        return "当前位置" if end.get("source") == CURRENT_LOCATION else _text(end.get("label"))

    routes = [Route(mode=r["mode"], distance_m=r.get("distance_m"), duration_s=r.get("duration_s"), summary=_text(r.get("summary")),
                    toll_yuan=r.get("toll_yuan"), fare_yuan=r.get("fare_yuan"), transit_type=_text(r.get("transit_type")),
                    transfers=r.get("transfers"), walking_distance_m=r.get("walking_distance_m"), legs=_legs(r.get("legs")),
                    alternatives=[TransitPlan(duration_s=a.get("duration_s"), fare_yuan=a.get("fare_yuan"), transit_type=_text(a.get("transit_type")),
                                              transfers=a.get("transfers"), walking_distance_m=a.get("walking_distance_m"), legs=_legs(a.get("legs")))
                                  for a in r.get("alternatives") or []],
                    provider=_text(r.get("provider")))
              for r in data["routes"]]
    return RoutePlan(origin=label(data.get("origin")), destination=label(data.get("destination")), routes=routes,
                     unavailable_modes=[str(m) for m in data.get("unavailable_modes") or []],
                     departure_time=_text(data.get("requested_departure_time")), fetched_at=fetched_at)


def _candidate_names(err: dict) -> list[str]:
    """重名时的候选，页面上写“城市区县”，不写坐标。"""
    names = []
    for c in err.get("candidates") or []:
        if isinstance(c, dict) and (name := "".join(_text(c.get(k)) for k in ("city", "district"))):
            names.append(name)
    return list(dict.fromkeys(names))


def _maps_step(kind: str, cid: str, text: str, extra: dict, result: ToolMessage) -> ToolStep:
    """整份结果在 artifact 的 structured_content 里，查的时间在 retrieved_at（见 maps_mcp.MapsMiddleware）。"""
    art = result.artifact if isinstance(result.artifact, dict) else {}
    sc = art.get("structured_content")
    if isinstance(sc, dict) and sc.get("ok") is True and result.status != "error":
        try:
            card = places_of(sc, _text(art.get("retrieved_at"))) if kind == "place" else routes_of(sc, _text(art.get("retrieved_at")))
        except (ValidationError, KeyError, TypeError):
            return ToolStep(id=cid, kind=kind, text=text, ok=False, message="地图结果的格式不对", **extra)
        return ToolStep(id=cid, kind=kind, text=text, **{"places" if kind == "place" else "routes": card}, **extra)
    err = sc.get("error") if isinstance(sc, dict) else None
    err = err if isinstance(err, dict) else {}
    message = MAPS_FAILURES.get(err.get("code"), MAPS_FAILED)
    if err.get("code") == "ambiguous_location" and (names := _candidate_names(err)):
        message += "：" + "、".join(names)
    return ToolStep(id=cid, kind=kind, text=text, ok=False, message=message, **extra)


# ---- 航班、火车（flyai 的 search_flights、search_trains）和排好的行程（present_itinerary），见 app.tools.flyai_mcp ----

CABIN_NAMES = {"economy": "经济舱", "premium_economy": "超级经济舱", "business": "公务舱", "first": "头等舱"}
SEAT_NAMES = {"second_class": "二等座", "first_class": "一等座", "business_class": "商务座", "premium_class": "特等座", "hard_seat": "硬座",
              "soft_seat": "软座", "hard_sleeper": "硬卧", "soft_sleeper": "软卧", "no_seat": "无座"}
# flyai 的错误码（见 agent-tools 的 services/flyai/README.md）→ 页面上的说法；给模型的说明是服务给的英文。
# unavailable、quota_exhausted、turn_limit 是灵稿自己回的（服务没连上、网关今天的上限到了、这一轮查的次数到了）
TRAVEL_FAILURES = {
    "invalid_arguments": "查询的条件不对",
    "origin_not_recognized": "飞猪认不出出发地",
    "destination_not_recognized": "飞猪认不出目的地",
    "no_services": "没查到符合条件的直达班次",
    "provider_no_result": "飞猪没查到直达班次",
    "busy": "查询的人多，没排上",
    "timeout": "查询超时了",
    "unavailable": "航班火车暂时查不到",
    "quota_exhausted": "航班火车今天的额度用完了",
    "turn_limit": "这一轮查的次数到上限了",
    "tool_error": "查询的条件不对",
}
TRAVEL_FAILED = "航班火车暂时查不到"
ITINERARY_FAILED = "行程方案没排成"


def _date_text(value: Any) -> str:
    try:
        d = date.fromisoformat(_text(value))
    except ValueError:
        return _text(value)
    return f"{d.month} 月 {d.day} 日"


def travel_limits(args: dict) -> list[str]:
    """查航班、火车的日期和条件，过程里写在起终点后面。"""
    out = [_date_text(args.get("departure_date"))] if args.get("departure_date") else []
    if args.get("train_category") == "high_speed":
        out.append("只要高铁动车")
    if cls := CABIN_NAMES.get(str(args.get("cabin_class"))) or SEAT_NAMES.get(str(args.get("seat_class"))):
        out.append(cls)
    after, before, arrive = args.get("departure_after_hour"), args.get("departure_before_hour"), args.get("arrival_before_hour")
    if after is not None and before is not None:
        out.append(f"{after} 点到 {before} 点出发")
    elif after is not None:
        out.append(f"{after} 点后出发")
    elif before is not None:
        out.append(f"{before} 点前出发")
    if arrive is not None:
        out.append(f"{arrive} 点前到")
    if args.get("max_price_yuan") is not None:
        out.append(f"{args['max_price_yuan']} 元以内")
    return out


def _stop(raw: dict) -> TravelStop:
    return TravelStop(city=raw["city"], station=raw["station"], terminal=_text(raw.get("terminal")), time=raw["time"])


def service_of(s: dict) -> TravelService:
    """flyai 结果里的一个班次换成卡片要的样子。字段不对抛 ValidationError、KeyError、TypeError。"""
    return TravelService(option_id=s["option_id"], number=s["number"], carrier=_text(s.get("airline") or s.get("train_type")),
                         departure=_stop(s["departure"]), arrival=_stop(s["arrival"]), day_offset=s["arrival"].get("day_offset") or 0,
                         duration_minutes=s["duration_minutes"], travel_class=_text(s.get("travel_class")), price_yuan=s.get("price_yuan"),
                         booking_url=_text(s.get("booking_url")))


def travel_of(kind: str, data: dict) -> TravelList:
    """search_flights、search_trains 的整份结果（MCP 的 structuredContent）换成航班卡、火车卡要的样子。字段不对抛 ValidationError、KeyError、TypeError。"""
    q = data.get("query") if isinstance(data.get("query"), dict) else {}
    return TravelList(kind=kind, origin=_text(q.get("origin")), destination=_text(q.get("destination")), date=_text(q.get("departure_date")),
                      services=[service_of(s) for s in data["services"]], limitations=[str(x) for x in data.get("limitations") or []],
                      observed_at=_text(data.get("observed_at")))


def _travel_step(kind: str, cid: str, text: str, extra: dict, result: ToolMessage) -> ToolStep:
    """整份结果在 artifact 的 structured_content 里（见 flyai_mcp.TravelMiddleware）。"""
    art = result.artifact if isinstance(result.artifact, dict) else {}
    sc = art.get("structured_content")
    if isinstance(sc, dict) and sc.get("ok") is True and result.status != "error":
        try:
            return ToolStep(id=cid, kind=kind, text=text, travel=travel_of(kind, sc), **extra)
        except (ValidationError, KeyError, TypeError):
            return ToolStep(id=cid, kind=kind, text=text, ok=False, message="查到的结果格式不对", **extra)
    err = sc.get("error") if isinstance(sc, dict) else None
    code = err.get("code") if isinstance(err, dict) else None
    return ToolStep(id=cid, kind=kind, text=text, ok=False, message=TRAVEL_FAILURES.get(code, TRAVEL_FAILED), **extra)


def _itinerary_step(cid: str, text: str, result: ToolMessage) -> ToolStep:
    """排好的在 artifact 的 itinerary 里；没排成（编号不对、排过了）的模型会改了再排，过程里照实写没排成。"""
    art = result.artifact if isinstance(result.artifact, dict) else {}
    if result.status != "error" and art.get("itinerary"):
        try:
            return ToolStep(id=cid, kind="itinerary", text=text, itinerary=Itinerary.model_validate(art["itinerary"]))
        except ValidationError:
            pass
    return ToolStep(id=cid, kind="itinerary", text=text, ok=False, message=ITINERARY_FAILED)


def image_of(m: BaseMessage) -> GeneratedImage | None:
    """一条生图结果里画好的图；没画成是 None。图的信息在 artifact 里（见 image_mcp.ImageMiddleware）。"""
    art = m.artifact if isinstance(m, ToolMessage) and m.name == IMAGE_TOOL else None
    if not isinstance(art, dict) or not art.get("image"):
        return None
    try:
        return GeneratedImage.model_validate(art["image"])
    except ValidationError:
        return None


def attached_images(m: BaseMessage) -> list[AttachedImage]:
    """用户这句话带的图，按发的先后；没带是空的。11-08 以前存的是一张（不是列表），一样认；格式不对的那张不算。"""
    raw = m.additional_kwargs.get(ATTACHED) if isinstance(m, HumanMessage) else None
    out = []
    for r in [raw] if isinstance(raw, dict) else raw if isinstance(raw, list) else []:
        try:
            out.append(AttachedImage.model_validate(r))
        except ValidationError:
            pass
    return out


def attached_files(m: BaseMessage) -> list[AttachedFile]:
    """用户这句话带的文件，按发的先后；没带是空的，格式不对的那个不算。"""
    raw = m.additional_kwargs.get(FILES) if isinstance(m, HumanMessage) else None
    out = []
    for r in raw if isinstance(raw, list) else []:
        try:
            out.append(AttachedFile.model_validate(r))
        except ValidationError:
            pass
    return out


def files_briefed(m: BaseMessage) -> BaseMessage:
    """压缩用：带了文件的用户消息标上只给目录和开头；没带的原样。"""
    if not isinstance(m, HumanMessage) or not m.additional_kwargs.get(FILES) or m.additional_kwargs.get(FILES_BRIEF):
        return m
    return m.model_copy(update={"additional_kwargs": {**m.additional_kwargs, FILES_BRIEF: True}})


def owned_file_ids(messages: list[BaseMessage]) -> list[str]:
    """这些消息里用户发的文件的编号，按先后：read_file 只认它们。"""
    return [f.id for m in messages for f in attached_files(m)]


def written_of(m: BaseMessage | None) -> WrittenFile | None:
    """一条写文件的结果里写好的那一版；没写成是 None。"""
    art = m.artifact if isinstance(m, ToolMessage) and m.name in WRITE_TOOLS and m.status != "error" else None
    if not isinstance(art, dict) or not art.get("file"):
        return None
    try:
        return WrittenFile.model_validate(art["file"])
    except ValidationError:
        return None


def written_files(messages: list[BaseMessage]) -> dict[str, WrittenFile]:
    """这些消息里模型写的文件：第一版的编号 → 最新的一版。read_file、edit_file 只认它们；下一版的版本号按它数。"""
    out: dict[str, WrittenFile] = {}
    for m in messages:
        if w := written_of(m):
            out[w.series] = w
    return out


def run_of(m: BaseMessage | None) -> CommandRun | None:
    """一条跑命令的结果里跑完的那次（退出码、输出、产出的文件）；没跑成是 None。"""
    art = m.artifact if isinstance(m, ToolMessage) and m.name == RUN_TOOL else None
    if not isinstance(art, dict) or not art.get("run"):
        return None
    try:
        return CommandRun.model_validate(art["run"])
    except ValidationError:
        return None


def broken_write(call: dict) -> ToolStep | None:
    """参数解析不了的写文件调用（多半是整篇内容太长、JSON 没闭合）：工具没执行，过程里照实写没写成，不让流出来的半截悄悄没了。
    别的工具参数坏了不列（模型多半改好再调一次）。"""
    if call.get("name") not in WRITE_TOOLS or not call.get("id"):
        return None
    return ToolStep(id=call["id"], kind="write", text="", ok=False, message=WRITE_FAILURES["broken"], edit=call["name"] == EDIT_TOOL)


# ---- 用户的当前位置（见 app.tools.location）----

LOCATION = "location"  # 停下来等用户时 state.ask 的 kind：要的是位置，页面出位置卡
LOCATED = "located"  # 工具结果的 artifact 里：这次调用停下来要过位置，用户怎么回的（{status, answer, asked_at, answered_at}）
LOCATION_UNAVAILABLE = "location_unavailable"  # 没拿到位置时结果里的错误码：浏览器取不到（服务没收到位置时回的也是这个）
LOCATION_DECLINED = "location_declined"  # 用户没给（点了不用，或者直接说了个地方）


def located_of(m: BaseMessage) -> dict | None:
    """这条工具结果是停下来要过位置以后得到的：用户怎么回的；没要过是 None。"""
    art = m.artifact if isinstance(m, ToolMessage) else None
    got = art.get(LOCATED) if isinstance(art, dict) else None
    return got if isinstance(got, dict) else None


def where_of(call: dict, result: ToolMessage | None) -> dict | None:
    """位置卡出着的时候用户没给位置、直接说了个地方：过程里列一条，和回答过的问题一个样子（kind 是 location，没有问题的字）。
    别的回法（给了、不用、取不到）不另外列，那一步自己看得出来。"""
    got = located_of(result) if result is not None else None
    if not got or got.get("status") != "said" or not call.get("id"):
        return None
    return {"type": "ask", "kind": LOCATION, "id": f"{call['id']}:where", "question": "", "options": [], "pending": False,
            "answer": _text(got.get("answer")), "skipped": False, "muted": False}


# ---- 模型问用户（ask_user，见 app.tools.ask_user）----


def ask_args(args: dict) -> tuple[str, list[str]]:
    """提问的参数整理成（问题，选项）：选项去掉空的、重复的，最多 MAX_ASK_OPTIONS 个。工具和页面都按这个取，问的和显示的一样。"""
    raw = args.get("options")
    options = list(dict.fromkeys(o.strip() for o in raw if isinstance(o, str) and o.strip())) if isinstance(raw, list) else []
    return _text(args.get("question")), options[:MAX_ASK_OPTIONS]


def ask_answer(m: BaseMessage) -> dict | None:
    """一条提问结果里用户的回答（{answer, skip, mute, asked_at, answered_at}）；没问成的、点了停止没回答的是 None。"""
    art = m.artifact if isinstance(m, ToolMessage) and m.name == ASK_TOOL else None
    got = art.get("ask") if isinstance(art, dict) else None
    return got if isinstance(got, dict) else None


def ask_of(call: dict, result: ToolMessage | None) -> dict | None:
    """一次提问 → 页面上的一条：问题、选项、用户的回答。还没回答的是 pending；点了跳过的是 skipped，点了“别再问了”的还带 muted；
    没等到回答这一轮就停了的，这几样都没有。没问成的（参数不对、这一轮问的次数到了，工具没停下来问）不列。"""
    if call.get("name") != ASK_TOOL or not call.get("id"):
        return None
    question, options = ask_args(call.get("args") or {})
    item = {"type": "ask", "id": call["id"], "question": question, "options": options, "pending": False, "answer": "", "skipped": False,
            "muted": False}
    if result is None:
        return {**item, "pending": True} if question else None
    if isinstance(result.artifact, dict) and result.artifact.get("stopped"):
        return item if question else None
    got = ask_answer(result)
    if got is None:
        return None
    return {**item, "answer": _text(got.get("answer")), "skipped": bool(got.get("skip")), "muted": bool(got.get("mute"))}


def asks_before(messages: list[BaseMessage], call_id: str) -> int:
    """这一轮里排在这次提问前面的提问有几次（按模型发出的先后；没问成的不算）。messages 是这一轮的消息。"""
    results = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)}
    n = 0
    for m in messages:
        for tc in m.tool_calls if isinstance(m, AIMessage) else []:
            if tc.get("id") == call_id:
                return n
            if tc.get("name") == ASK_TOOL and ((r := results.get(tc.get("id"))) is None or ask_answer(r) is not None):
                n += 1
    return n


def waited_ms(messages: list[BaseMessage]) -> int:
    """这一轮里等用户回答一共等了多久（毫秒）：每次提问（要位置也算）从问出来到回答的那段时间，重叠的（一次问了两个）只算一遍。
    算这一轮用时的时候去掉。"""
    spans = []
    for m in messages:
        if got := ask_answer(m) or located_of(m):
            try:
                a, b = datetime.fromisoformat(got["asked_at"]), datetime.fromisoformat(got["answered_at"])
            except (KeyError, TypeError, ValueError):
                continue
            if b > a:
                spans.append((a, b))
    total, end = 0.0, None
    for a, b in sorted(spans):
        if end is None or a > end:
            total, end = total + (b - a).total_seconds(), b
        elif b > end:
            total, end = total + (b - end).total_seconds(), b
    return round(total * 1000)


def owned_image_ids(messages: list[BaseMessage]) -> list[str]:
    """这些消息里存在 image-service 里的图的编号：用户发的、画好的、跑命令产出的（图表），按先后。
    删对话、重新回答时删掉它们；改图、看图只认它们。"""
    ids = []
    for m in messages:
        if att := attached_images(m):
            ids += [a.id for a in att]
        elif (img := image_of(m)) and img.stored:
            ids.append(img.id)
        elif run := run_of(m):
            ids += [f.id for f in run.files if f.kind == "image"]
    return ids


def _image_step(cid: str, text: str, extra: dict, result: ToolMessage) -> ToolStep:
    if img := image_of(result):
        # 以前的图：画成了，只是不在了。卡片上说一声（有 message 的图片卡片页面按说明画），过程里照样是画成了
        gone = {} if img.stored else {"message": IMAGE_GONE}
        return ToolStep(id=cid, kind="image", text=text, image=img, **{**extra, "aspect_ratio": img.aspect_ratio}, **gone)
    code = result.artifact.get("error_code") if isinstance(result.artifact, dict) else None  # 参数没通过校验的没有 artifact
    return ToolStep(id=cid, kind="image", text=text, ok=False, message=IMAGE_FAILURES.get(code, IMAGE_FAILED), **extra)


def _task_step(cid: str, text: str, extra: dict, result: ToolMessage) -> ToolStep:
    """子任务：来源是它的子 agent 查到的。子 agent 每一步做了什么由运行器从同一条结果里展开（见 api.research_runner.conversation）。"""
    sids = [s.id for s in collect_sources(subagent_messages(result))]
    if result.status != "error":
        return ToolStep(id=cid, kind="task", text=text, source_ids=sids, **extra)
    code = result.artifact.get("failure") if isinstance(result.artifact, dict) else None
    return ToolStep(id=cid, kind="task", text=text, ok=False, source_ids=sids, message=TASK_FAILURES.get(code, TASK_FAILED), **extra)


# ---- 用量 ----


def usage_of(messages: list[BaseMessage]) -> Usage:
    """一轮里 agent 的模型调用和搜索、读页、查天气次数，画成了几张图，派了几个子 agent。token 来自模型返回的用量（流式要开 stream_usage）。
    只算这些消息自己的：子 agent 的另算（见 subagent_usage）。"""
    u, cost = Usage(), 0.0
    for m in messages:
        # 工具调用的钱（做成了的按次收，见 agent.ToolChargeMiddleware）；没定价的工具没有这个键，不收
        if isinstance(m, ToolMessage) and cost is not None and isinstance(c := m.response_metadata.get(COST), int | float):
            cost += c
        if isinstance(m, AIMessage) and not blocked_reason(m) and not m.response_metadata.get(STOPPED):  # 系统补的消息不是一次完整的模型调用
            meta: dict[str, Any] = m.usage_metadata or {}  # type: ignore[assignment]
            c = m.response_metadata.get(COST)
            cost = cost + c if cost is not None and isinstance(c, int | float) else None
            u.model_calls += 1
            u.input_tokens += meta.get("input_tokens") or 0
            u.output_tokens += meta.get("output_tokens") or 0
            u.cached_tokens += (meta.get("input_token_details") or {}).get("cache_read") or 0
            u.reasoning_tokens += (meta.get("output_token_details") or {}).get("reasoning") or 0
        elif isinstance(m, ToolMessage) and (not_run(m) or _refused(m)):  # 没真执行的不算
            continue
        elif isinstance(m, ToolMessage) and m.name == "search_web":
            u.searches += 1
        elif isinstance(m, ToolMessage) and m.name == "read_page":
            u.reads += 1
        elif isinstance(m, ToolMessage) and m.name == "get_weather":
            u.weather += 1
        elif isinstance(m, ToolMessage) and m.name in (PLACE_TOOL, ROUTE_TOOL):
            u.maps += 1
        elif isinstance(m, ToolMessage) and m.name in TRAVEL_KINDS:
            u.travel += 1
        elif isinstance(m, ToolMessage) and m.name in (LIBRARY_TOOL, DOCS_TOOL):
            u.docs += 1
        elif image_of(m):
            u.images += 1
        elif subagent_messages(m):
            u.subagents += 1
    u.cost = cost
    return u


def _refused(m: ToolMessage) -> bool:
    """灵稿自己没交给服务的：比路线时车站城市没填对、这一轮查航班火车的次数到了（见 flyai_mcp）。"""
    sc = m.artifact.get("structured_content") if isinstance(m.artifact, dict) else None
    err = sc.get("error") if isinstance(sc, dict) else None
    return isinstance(err, dict) and err.get("code") in ("station_city", "turn_limit")


def subagent_usage(messages: list[BaseMessage]) -> Usage:
    """一轮里所有子 agent 加起来的用量。"""
    return usage_of([x for m in messages for x in subagent_messages(m)])
