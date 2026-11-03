"""上下文压缩：对话太长时，发给主 agent 的那份里把之前几轮压一压。state 里的消息不改，页面照常从消息里取（2026-11-03 Sean 定）。

发给模型的对话估算超过 config.COMPACT_TRIGGER_TOKENS 时（一轮里每次调模型之前看一次），从最早的一轮起往后压，压到 COMPACT_TARGET_TOKENS 以下：
- 第一层：之前几轮的搜索、读页、取回的结果换成一行说明（来源 id、标题、网址，要原文用 recall 取回），模型的思考不带。
  模型的工具调用本身、子任务的报告、天气、画图的结果都留着。
- 第一层压完还超，第二层：再从最早的一轮起，模型那边（思考、正文、工具调用和结果）整个换成快模型写的短版，用户的话照原样留着；
  特别长的用户消息（贴了一大段的）也换成短的。短版一轮一份，写一次存进 state（Compaction.summaries），以后不再改，也不会拿短版再去压。
- 这一轮（还在跑的）不压。

压到哪一轮记在 state 里（Compaction），只往后挪，不是每次现算：到了边界的轮每次调模型拼出来的都一样（拼的时候不放时间、随机的东西），
前缀缓存接得上，只在边界往后挪的那一次断一次。所以一次多压一些（压到触发线的一半左右），而不是每次刚过线压一点。
LangChain 的 ContextEditingMiddleware 每次调模型都现算从哪儿清（边界跟着对话往后走，缓存每次都断），清掉的只剩 "[cleared]"，没有来源 id；
SummarizationMiddleware 会改写 state，页面上之前几轮就没了。所以自己写（见 docs/LangGraph研究/03-上下文压缩.md）。

token 是估算的：照 DeepSeek 文档（Token 用量计算）的换算，英文字符约 0.3 个 token，中文字符约 0.6 个。
算的是系统提示词、工具定义和消息。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import ToolException, tool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.config import get_config
from pydantic import BaseModel, Field

from app import config
from app.graph.research import history, prompts
from app.graph.research.agent import emit, memory_text, system_prompt, turn_start
from app.graph.research.sources import SUBAGENT_TOOL, flatten, parse
from app.graph.research.state import Compaction, ResearchState

log = logging.getLogger(__name__)

RECALL_TOOL = "recall"
CLEARED_TOOLS = ("search_web", "read_page", RECALL_TOOL)  # 第一层换成说明的工具结果
PER_MESSAGE = 4  # 每条消息另算的 token（角色、分隔）
SUMMARY_GUESS = 600  # 还没写出来时，一份短版按这么多 token 估
LONG_USER_CHARS = 20000  # 换成短版的那几轮里，用户消息超过这么多字的也压
SUMMARY_WORKERS = 3  # 同时写几份短版
TURN_RESULT_CHARS = 2000  # 交给快模型写短版时，每条工具结果最多带这么多字（子任务的报告见下）
TURN_REPORT_CHARS = 8000  # 子任务的报告最多带这么多字
TURN_USER_CHARS = 4000  # 用户的话最多带这么多字（短版不用复述它）
CACHE_CHECK_MIN = 20000  # 上一次输入超过这么多 token 才查缓存接没接上
CACHE_DROP = 0.5  # 这次命中的不到上一次输入的这个比例，记一条警告
MARK = "lingdraft_compaction"  # 模型消息 response_metadata 里的键：这次调模型时压缩的边界，下一次据此知道边界挪没挪


# ---- 估算 ----


def tokens(text: str) -> float:
    """照 DeepSeek 文档的换算估 token：英文字符约 0.3 个，中文字符约 0.6 个。非 ASCII 的都按中文算（UTF-8 里中文 3 个字节）。"""
    n = len(text)
    wide = (len(text.encode()) - n) // 2
    return 0.3 * (n - wide) + 0.6 * wide


def message_tokens(m: BaseMessage) -> float:
    parts = [history.text_of(m)]
    if isinstance(m, AIMessage):
        parts.append(str(m.additional_kwargs.get("reasoning_content") or ""))
        parts += [json.dumps(tc.get("args") or {}, ensure_ascii=False) + tc["name"] for tc in m.tool_calls]
    return tokens("".join(parts)) + PER_MESSAGE


def size_of(messages: list[BaseMessage]) -> float:
    return sum(message_tokens(m) for m in messages)


# ---- 按轮拼发给模型的那份 ----


def turns_of(messages: list[BaseMessage]) -> list[list[BaseMessage]]:
    """按轮分开：每轮从开始一轮的用户消息起（插进来的话算在这一轮里）。"""
    out: list[list[BaseMessage]] = []
    for m in messages:
        if history.is_request(m) or not out:
            out.append([])
        out[-1].append(m)
    return out


def turn_order(messages: list[BaseMessage]) -> dict[str, int]:
    """轮 id → 第几轮（从 0 数）。按 state 里全部的消息数，被内容审核停下的那几轮也算：轮号在整段对话里不变，说明里写的和 recall 认的对得上。"""
    return {m.id: i for i, m in enumerate(x for x in messages if history.is_request(x)) if m.id}


def _cleared(m: BaseMessage) -> BaseMessage:
    """第一层：搜索、读页、取回的结果换成一行说明；模型消息不带思考。没做成的结果本来就短，留着。"""
    if isinstance(m, AIMessage) and "reasoning_content" in m.additional_kwargs:
        return m.model_copy(update={"additional_kwargs": {k: v for k, v in m.additional_kwargs.items() if k != "reasoning_content"}})
    if not isinstance(m, ToolMessage) or m.name not in CLEARED_TOOLS or m.status == "error":
        return m
    data = parse(m)
    if m.name == RECALL_TOOL:
        note = prompts.render("compact.recall_cleared")
    elif m.name == "search_web" and isinstance(data, list):
        found = [{"id": h.get("id"), "title": h.get("title") or "", "url": h.get("url") or ""} for h in data if h.get("id")]
        note = prompts.render("compact.search_cleared", sources=json.dumps(found, ensure_ascii=False))
    elif m.name == "read_page" and isinstance(data, dict) and data.get("ok") and data.get("id"):
        note = prompts.render("compact.read_cleared", id=data["id"], title=data.get("title") or "", url=data.get("url") or "")
    else:
        return m
    return m.model_copy(update={"content": note})


def _summarized(turn: list[BaseMessage], k: int, c: Compaction) -> list[BaseMessage]:
    """第二层：用户的话（特别长的换成短的），后面一条模型消息是这一轮的短版。
    没有短版的（模型那边本来就短、不值得写，或者写失败了）模型那边按第一层。"""

    def user(m: HumanMessage) -> BaseMessage:
        short = c.shortened.get(m.id or "")
        return m.model_copy(update={"content": prompts.render("compact.user_shortened", turn=k + 1, text=short)}) if short else m

    tid = turn[0].id or ""
    if tid not in c.summaries:
        return [user(m) if isinstance(m, HumanMessage) else _cleared(m) for m in turn]
    out = [user(m) for m in turn if isinstance(m, HumanMessage)]
    out.append(AIMessage(content=prompts.render("compact.summary", turn=k + 1, summary=c.summaries[tid]), id=f"compact-{tid}"))
    return out


def _form(turn: list[BaseMessage], order: dict[str, int], c: Compaction) -> list[BaseMessage]:
    k = order.get(turn[0].id or "", -1) if history.is_request(turn[0]) else -1
    if k < 0:
        return turn
    if k <= order.get(c.summarized_through, -1):
        return _summarized(turn, k, c)
    if k <= order.get(c.cleared_through, -1):
        return [_cleared(m) for m in turn]
    return turn


def compact(messages: list[BaseMessage], c: Compaction | None, order: dict[str, int]) -> list[BaseMessage]:
    """发给模型的那份：到了边界的轮按 c 压，之后的原样。不改传进来的消息。"""
    if c is None or not (c.cleared_through or c.summarized_through):
        return messages
    return [m for t in turns_of(messages) for m in _form(t, order, c)]


# ---- 写短版 ----


def render_turn(turn: list[BaseMessage]) -> str:
    """一轮交给快模型写短版：用户的话、模型的正文、工具调用和结果，按先后排。长的截掉，思考不带。"""

    def cut(text: str, n: int) -> str:
        return text if len(text) <= n else text[:n] + " …(cut)"

    parts: list[str] = []
    for m in turn:
        if isinstance(m, HumanMessage):
            parts.append(f"<user>\n{cut(history.text_of(m), TURN_USER_CHARS)}\n</user>")
        elif isinstance(m, AIMessage):
            if text := history.text_of(m).strip():
                parts.append(f"<assistant>\n{text}\n</assistant>")
            parts += [f'<tool_call name="{tc["name"]}">{json.dumps(tc.get("args") or {}, ensure_ascii=False)}</tool_call>'
                      for tc in m.tool_calls]
        elif isinstance(m, ToolMessage):
            n = TURN_REPORT_CHARS if m.name == SUBAGENT_TOOL else TURN_RESULT_CHARS
            parts.append(f'<tool_result name="{m.name}">\n{cut(history.text_of(m), n)}\n</tool_result>')
    return "\n".join(parts)


class CompactionMiddleware(AgentMiddleware[ResearchState]):
    """只挂在主 agent 上（子 agent 有搜索、读页的预算，上下文长不到这里）。
    每次调模型之前（abefore_model）估一下，超了就把边界往后挪、写好短版，存进 state；调模型时（awrap_model_call）按 state 里的边界拼。
    在 ContentGuard 里面：被内容审核停下的那几轮先去掉，再压。"""

    state_schema = ResearchState

    def __init__(self, planner, tools: list, memory=None) -> None:
        super().__init__()
        self.planner = planner
        self.memory = memory  # 长期记忆在系统提示词里，估的时候算上
        self.tools_tokens = tokens(json.dumps([convert_to_openai_tool(t) for t in tools], ensure_ascii=False))  # 工具定义每次都发

    async def abefore_model(self, state: ResearchState, runtime) -> dict[str, Any] | None:
        all_msgs = state["messages"]
        msgs = history.without_blocked_turns(all_msgs)
        order = turn_order(all_msgs)
        c = state.get("compaction") or Compaction()
        turns = turns_of(msgs)
        base = tokens(system_prompt(turn_start(all_msgs, state), memory=await memory_text(self.memory))) + self.tools_tokens
        costs = [size_of(_form(t, order, c)) for t in turns]
        size = base + sum(costs)
        if size <= config.COMPACT_TRIGGER_TOKENS:
            return None
        before, target = size, config.COMPACT_TARGET_TOKENS
        old = [(i, t) for i, t in enumerate(turns[:-1]) if history.is_request(t[0]) and t[0].id in order]  # 这一轮不压
        new = c.model_copy(deep=True)
        # 第一层：从最早的一轮起，搜索、读页的结果换成说明，压到目标以下或者之前几轮都压过
        for i, t in old:
            if size <= target:
                break
            if order[t[0].id] <= order.get(new.cleared_through, -1):
                continue
            new.cleared_through = t[0].id
            cost = size_of(_form(t, order, new))
            size, costs[i] = size + cost - costs[i], cost
        # 第二层：还超，再从最早的一轮起换成短版。模型那边压过第一层以后已经比一份短版还短的，不写短版，照第一层；
        # 特别长的用户消息也压。两样都没有的轮不用挑
        if size > target:
            picked: list[tuple[list[BaseMessage], bool, list[HumanMessage]]] = []  # (这一轮, 要不要短版, 要压的用户消息)
            for i, t in old:
                if size <= target:
                    break
                if order[t[0].id] <= order.get(new.summarized_through, -1):
                    continue
                users = [m for m in t if isinstance(m, HumanMessage)]
                long = [m for m in users if len(history.text_of(m)) > LONG_USER_CHARS]
                written = new.summaries.get(t[0].id)  # 上次写好了、边界没挪过来的（前面有一轮写失败了）
                summary = bool(written) or costs[i] - size_of(users) > SUMMARY_GUESS
                if not summary and not long:
                    continue
                picked.append((t, summary, long))
                after = size_of([m for m in users if m not in long]) + SUMMARY_GUESS * len(long)
                after += (tokens(written) if written else SUMMARY_GUESS) if summary else costs[i] - size_of(users)
                size += after - costs[i]
            await self._summarize(picked, [t for _, t in old], order, new)
            size = base + sum(size_of(_form(t, order, new)) for t in turns)
        if new == c:
            return None
        log.info("上下文压缩：线程 %s 估算 %d → %d token，第一层到第 %s 轮，第二层到第 %s 轮", _thread_id(), before, size,
                 order.get(new.cleared_through, -1) + 1, order.get(new.summarized_through, -1) + 1)
        if size > config.COMPACT_TRIGGER_TOKENS:
            log.warning("上下文压缩后还是超过触发线：线程 %s 估算 %d token", _thread_id(), size)
        return {"compaction": new}

    async def _summarize(self, picked: list[tuple[list[BaseMessage], bool, list[HumanMessage]]], old: list[list[BaseMessage]],
                         order: dict[str, int], new: Compaction) -> None:
        """给挑出来的几轮写短版、压特别长的用户消息（同时最多 SUMMARY_WORKERS 个），写好的存进 new，写一次就不再写。
        第二层的边界从最早起往后挪到最后挑的那一轮：中间没挑的（本来就短）直接过；要短版的写好了才过得去，写失败的那一轮停住，
        它先按第一层发，下次再超时接着从它写。用户消息压不成就照原样，不挡边界。
        在这次运行里调快模型：用户点了停止，跟着这次运行一起取消；片段不进页面（标了 nostream，见 planner.summarize）。"""
        if not picked:
            return
        jobs: dict[tuple[str, str], str] = {}
        for t, summary, long in picked:
            if summary and (t[0].id or "") not in new.summaries:
                jobs[("turn", t[0].id or "")] = prompts.render("compact.turn", turn=render_turn(t))
            for m in long:
                if (m.id or "") not in new.shortened:
                    jobs[("user", m.id or "")] = prompts.render("compact.user_message", message=history.text_of(m))
        if jobs:
            emit({"type": "phase", "phase": "model"})  # 页面上不说在压缩（2026-11-03 Sean 定），按等模型显示
        slots = asyncio.Semaphore(SUMMARY_WORKERS)

        async def write(p: str) -> str:
            async with slots:
                return (await self.planner.summarize(p)).strip()

        done: dict[tuple[str, str], str] = {}
        results = await asyncio.gather(*(write(p) for p in jobs.values()), return_exceptions=True)  # 点了停止时整个取消，照常抛出
        for key, r in zip(jobs, results, strict=True):
            if isinstance(r, BaseException):
                if not isinstance(r, Exception):
                    raise r
                log.warning("上下文压缩：写短版失败（%s %s）：%s", key[0], key[1], r)  # 写失败的先按第一层发
            elif r:
                done[key] = r
        new.shortened.update({mid: text for (kind, mid), text in done.items() if kind == "user"})
        new.summaries.update({tid: text for (kind, tid), text in done.items() if kind == "turn"})
        need = {t[0].id for t, summary, _ in picked if summary}
        last = picked[-1][0][0].id
        for t in old:
            tid = t[0].id or ""
            if order[tid] <= order.get(new.summarized_through, -1):
                continue
            if tid in need and tid not in new.summaries:
                break
            new.summarized_through = tid
            if tid == last:
                break

    async def awrap_model_call(self, request: ModelRequest, handler):
        c = request.state.get("compaction")
        sent = compact(request.messages, c, turn_order(request.state["messages"])) if c else request.messages
        resp = await handler(request.override(messages=sent) if sent is not request.messages else request)
        mark = _mark(c)
        _check_cache(request.messages, resp, mark)
        for m in getattr(resp, "result", []):
            if isinstance(m, AIMessage):
                m.response_metadata[MARK] = mark
        return resp


def _thread_id() -> str:
    try:
        return str(get_config()["configurable"].get("thread_id", ""))
    except (RuntimeError, KeyError):
        return ""


def _usage(m: BaseMessage) -> dict:
    return (m.usage_metadata or {}) if isinstance(m, AIMessage) else {}  # type: ignore[return-value]


def _mark(c: Compaction | None) -> str:
    return f"{c.cleared_through}/{c.summarized_through}" if c else ""


def _check_cache(messages: list[BaseMessage], resp: Any, mark: str) -> None:
    """这次调模型命中的缓存比上一次的整个输入少得多：前缀断了（或者服务那边没留住缓存），记一条警告，在 Loki 里搜得到。
    压缩边界这次刚挪过的不查：那一次断是应该的。"""
    prev = next((m for m in reversed(messages) if isinstance(m, AIMessage) and _usage(m).get("input_tokens")), None)
    cur = next((m for m in getattr(resp, "result", []) if isinstance(m, AIMessage)), None)
    if prev is None or cur is None or not _usage(cur) or prev.response_metadata.get(MARK, "") != mark:
        return
    before = _usage(prev).get("input_tokens") or 0
    hit = (_usage(cur).get("input_token_details") or {}).get("cache_read") or 0
    if before >= CACHE_CHECK_MIN and hit < before * CACHE_DROP:
        log.warning("前缀缓存没接上：线程 %s 上一次输入 %d token，这次输入 %d、命中 %d", _thread_id(), before,
                    _usage(cur).get("input_tokens") or 0, hit)


# ---- 取回 ----


class RecallArgs(BaseModel):
    source_id: str | None = Field(default=None, description=prompts.render("tools.recall.source_id"))
    turn: int | None = Field(default=None, description=prompts.render("tools.recall.turn"))


def _failed(kind: str, message: str) -> ToolException:
    return ToolException(json.dumps({"ok": False, "kind": kind, "message": message}, ensure_ascii=False))


def recall_source(messages: list[BaseMessage], sid: str) -> str:
    """一个来源当初拿到的样子：读过的是读页的结果（最后读的那次），没读过的是搜索结果里的那一条。子 agent 查到的也算。
    被内容审核停下的那几轮里的不算：发给模型就又会被拦。"""
    page, hit = None, None
    for m in flatten(history.without_blocked_turns(messages)):
        if not isinstance(m, ToolMessage) or m.status == "error":
            continue
        data = parse(m)
        if m.name == "read_page" and isinstance(data, dict) and data.get("ok") and data.get("id") == sid and (data.get("content") or "").strip():
            page = m
        elif m.name == "search_web" and isinstance(data, list) and hit is None:
            hit = next((h for h in data if h.get("id") == sid), None)
    if page is not None:
        return history.text_of(page)
    if hit is not None:
        return json.dumps({"id": sid, "url": hit.get("url") or "", "title": hit.get("title") or "", "published_at": hit.get("published_at"),
                           "snippet": hit.get("content") or hit.get("description") or "", "page_read": False}, ensure_ascii=False)
    raise _failed("not_found", prompts.render("tools.recall.no_source", source_id=sid))


def recall_turn(messages: list[BaseMessage], n: int) -> str:
    """第 n 轮（从 1 数）用户的原话、插进来的话和模型最后的回答，都是原文。"""
    starts = [i for i, m in enumerate(messages) if history.is_request(m)]
    if not 1 <= n <= len(starts):
        raise _failed("not_found", prompts.render("tools.recall.no_turn", turn=n, count=len(starts)))
    end = starts[n] if n < len(starts) else len(messages)
    turn = messages[starts[n - 1]:end]
    if any(history.blocked_reason(m) for m in turn):  # 轮号照算（和说明里的对得上），内容不给：发给模型就又会被拦
        raise _failed("blocked", prompts.render("tools.recall.blocked_turn", turn=n))
    out: dict[str, Any] = {"turn": n, "user": history.text_of(turn[0])}
    if guides := [history.text_of(m) for m in turn if history.is_guide(m)]:
        out["user_follow_ups"] = guides
    out["answer"] = history.final_text(turn[1:])
    return json.dumps(out, ensure_ascii=False)


def make_recall_tool():
    @tool(RECALL_TOOL, description=prompts.render("tools.recall.description"), args_schema=RecallArgs)
    def recall(runtime: ToolRuntime, source_id: str | None = None, turn: int | None = None) -> str:
        msgs = runtime.state.get("messages", [])
        sid = (source_id or "").strip()
        if bool(sid) == (turn is not None):
            raise _failed("bad_arguments", prompts.render("tools.recall.bad_arguments"))
        return recall_source(msgs, sid) if sid else recall_turn(msgs, turn)  # type: ignore[arg-type]

    recall.handle_tool_error = True
    return recall
