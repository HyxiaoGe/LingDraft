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
import re
import string
from datetime import UTC, datetime
from typing import Any, NotRequired
from urllib.parse import quote, urlsplit

from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware, ModelCallLimitMiddleware
from langchain.agents.middleware.types import AgentMiddleware, AgentState, ModelRequest, hook_config
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.config import get_stream_writer

from app.graph.research import prompts, skills
from app.graph.research.state import (
    CLOSE_RETRIES,
    INVALID_TOOL_CALL_RETRIES,
    MAX_ASKS,
    MAX_MODEL_CALLS,
    MAX_READS,
    MAX_SEARCHES,
    MAX_SKILL_LOADS,
    AskRecord,
    Source,
    ToolStep,
)

EXCERPT_CHARS = 400
LIMIT_MARK = "lingdraft_call_limit"  # 调用超限时中间件注入的那条消息带这个标记，不是模型的答案
ASK_BLOCKED = "ask_user_over_budget"  # 超额的 ask_user 调用改成这个名字，HITL 中间件就不会再把它变成问题卡
DANGLING_NOTE = prompts.render("agent.dangling_call")


def _search_limits(args: dict) -> list[str]:
    return ([f"近 {args['recency_days']} 天"] if args.get("recency_days") else []) + [str(d) for d in args.get("domains") or []]


def _emit(event: dict) -> None:
    try:
        get_stream_writer()(event)
    except RuntimeError:
        pass


class ToolProgressMiddleware(AgentMiddleware):
    """每次调工具前后往自定义流里写进度：开始时说在搜什么、读什么，做完带上这一步的记录（搜到几条、读没读到正文）和找到的网页。

    两条用同一个 id（工具调用 id），页面据此把它们、以及线程状态里存下来的记录对上。排在额度中间件里面，被额度拦下的调用不出现。
    """

    def wrap_tool_call(self, request, handler):
        call = request.tool_call
        name = call["name"]
        args = call.get("args") or {}
        text = None
        if name == "search_web":
            limits = _search_limits(args)
            text, kind = f"搜索：{args.get('query', '')}" + (f"（{'，'.join(limits)}）" if limits else ""), "search"
        elif name == "read_page":
            text, kind = f"读取：{args.get('url', '')}", "read"
        elif name == "load_skill" and (skill := skills.catalog().get(str(args.get("name", "")).strip())):
            text, kind = f"按「{skill.title}」的方法调研", "skill"
        if not text:
            return handler(request)
        start = {"stage": text[:160], "kind": kind, "id": call.get("id")}
        _emit(start)
        result = handler(request)
        step = tool_step(call, result) if isinstance(result, ToolMessage) else None
        if step is not None:
            _emit({**start, "done": True, "step": step.model_dump(), "found": _found(name, result)})
        return result


def _found(name: str, result: ToolMessage) -> list[dict]:
    """这一步找到的网页（网址、标题），页面在调研还没结束时先列出来；编号要等这一轮结束、来源入库时才定。"""
    data = _parse(result)
    if name == "search_web" and isinstance(data, list):
        return [{"url": h.get("url") or "", "title": h.get("title") or "", "body": False} for h in data if (h.get("url") or "").strip()]
    if name == "read_page" and isinstance(data, dict) and data.get("ok") and (data.get("content") or "").strip():
        return [{"url": data.get("url") or "", "title": data.get("title") or "", "body": True}]
    return []


class ToolBudgetMiddleware(AgentMiddleware):
    """按工具名限制一次运行里的调用次数：用完就从模型可见的工具里摘掉，并在执行层硬拦。

    同一批里发多个调用也算额度（第 9 个搜索不会因为前 8 个还没执行就放过）。
    ask_user 由 HITL 中间件在执行前拦截、执行层拦不到，所以超额的提问在 after_model 里改名——
    本中间件排在 HITL 之后，after_model 反序执行，先于 HITL 看到这条消息。
    提问只算呈现给了用户的：同一批里多出来的提问没呈现（结果里标了 unshown），不占额度。
    """

    def __init__(self, limits: dict[str, int]) -> None:
        super().__init__()
        self.limits = limits

    def _used(self, messages: list[BaseMessage]) -> dict[str, int]:
        used: dict[str, int] = {}
        for m in messages:
            if isinstance(m, ToolMessage) and m.name in self.limits and not _unshown(m):
                used[m.name] = used.get(m.name, 0) + 1
        return used

    def _over_budget(self, messages: list[BaseMessage], tool_call_id: str) -> bool:
        """这条调用（算上同一批里排在它前面的同名调用）是否超额。

        这一批不一定是最后一条消息：提问回答后，HITL 会在它后面追加提问的工具结果，再执行同批其余的调用。
        """
        i = next((i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], AIMessage)
                  and any(tc["id"] == tool_call_id for tc in messages[i].tool_calls)), None)
        if i is None:
            return False
        used = self._used(messages[:i])
        for tc in messages[i].tool_calls:
            name = tc["name"]
            lim = self.limits.get(name)
            if tc["id"] == tool_call_id:
                return lim is not None and used.get(name, 0) >= lim
            if lim is not None:
                used[name] = used.get(name, 0) + 1
        return False

    def after_model(self, state: AgentState, runtime) -> dict[str, Any] | None:
        msgs = state.get("messages") or []
        last = msgs[-1] if msgs else None
        if not isinstance(last, AIMessage) or "ask_user" not in self.limits:
            return None
        if not any(tc["name"] == "ask_user" and self._over_budget(msgs, tc["id"]) for tc in last.tool_calls):
            return None
        calls = [dict(tc, name=ASK_BLOCKED) if tc["name"] == "ask_user" and self._over_budget(msgs, tc["id"]) else tc
                 for tc in last.tool_calls]
        return {"messages": [last.model_copy(update={"tool_calls": calls})]}  # 同一个 id：替换，不是追加

    def wrap_model_call(self, request: ModelRequest, handler):
        used = self._used(request.messages)
        exhausted = {n for n, lim in self.limits.items() if used.get(n, 0) >= lim}
        if not exhausted:
            return handler(request)
        tools = [t for t in request.tools if getattr(t, "name", None) not in exhausted]
        note = prompts.render("agent.tools_exhausted", names="、".join(sorted(exhausted)))
        base = request.system_message.content if request.system_message else ""
        base = base if isinstance(base, str) else str(base)
        return handler(request.override(tools=tools, system_message=SystemMessage(f"{base}\n\n{note}")))

    def wrap_tool_call(self, request, handler):
        name = request.tool_call["name"]
        if name == ASK_BLOCKED:
            return ToolMessage(content=prompts.render("agent.ask_over_budget"),
                               name=ASK_BLOCKED, tool_call_id=request.tool_call["id"], status="error")
        if name in self.limits and self._over_budget(request.state.get("messages", []), request.tool_call["id"]):
            return ToolMessage(content=prompts.render("agent.tool_over_budget", name=name), name=name,
                               tool_call_id=request.tool_call["id"], status="error")
        return handler(request)


class CallLimitMiddleware(ModelCallLimitMiddleware):
    """超限时父类会注入一条说明消息再结束；这里给它打上标记，识别时看标记，不看消息文字。"""

    @property
    def name(self) -> str:
        return "ModelCallLimitMiddleware"  # 节点名沿用父类的，已有检查点里的 agent 子图才能接着跑

    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime) -> dict[str, Any] | None:
        out = super().before_model(state, runtime)
        if out and out.get("messages"):
            out["messages"] = [m.model_copy(update={"response_metadata": {**m.response_metadata, LIMIT_MARK: True}})
                               for m in out["messages"]]
        return out

    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state, runtime) -> dict[str, Any] | None:
        return self.before_model(state, runtime)


def is_limit_message(m: BaseMessage) -> bool:
    return isinstance(m, AIMessage) and bool(m.response_metadata.get(LIMIT_MARK))


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


def build_agent(model: BaseChatModel, tools: list, system_prompt: str, *, ask_limit: int = MAX_ASKS):
    """ask_limit 是本次运行还能问的次数（整线程上限减去已问过的）；0 就不绑 ask_user、不装 HITL。"""
    allow_ask = ask_limit > 0
    tools = [t for t in tools if allow_ask or t.name != "ask_user"]
    limits = {"search_web": MAX_SEARCHES, "read_page": MAX_READS, "load_skill": MAX_SKILL_LOADS}
    middleware: list = []
    if allow_ask:
        middleware.append(HumanInTheLoopMiddleware(interrupt_on={"ask_user": {"allowed_decisions": ["respond"]}}))
        limits["ask_user"] = ask_limit
    middleware += [
        ToolBudgetMiddleware(limits),
        ToolProgressMiddleware(),  # 在额度中间件里面：被拦下的调用不报进度
        InvalidToolCallRetryMiddleware(INVALID_TOOL_CALL_RETRIES),
        CallLimitMiddleware(run_limit=MAX_MODEL_CALLS, exit_behavior="end"),
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
            if m.tool_calls or m.invalid_tool_calls or not text.strip() or is_limit_message(m):
                continue
            return text.strip()
    return None


def answer_dangling_calls(messages: list[BaseMessage]) -> list[BaseMessage]:
    """没有工具结果的工具调用（无效调用重试耗尽、或超限结束）补一条错误结果，历史才是合法的对话。"""
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    out: list[BaseMessage] = []
    for m in messages:
        out.append(m)
        if not isinstance(m, AIMessage):
            continue
        for tc in list(m.tool_calls) + list(m.invalid_tool_calls):
            if tc.get("id") and tc["id"] not in answered:
                out.append(ToolMessage(content=DANGLING_NOTE, name=tc.get("name") or "unknown", tool_call_id=tc["id"], status="error"))
                answered.add(tc["id"])
    return out


def close_out(model: BaseChatModel, messages: list[BaseMessage]) -> tuple[str | None, str]:
    """不带工具再要一次答案。返回 (文字或 None, 说明)。"""
    convo = [m for m in messages if not is_limit_message(m)]
    convo = answer_dangling_calls(convo) + [HumanMessage(prompts.render("agent.close"))]
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


SKIP_NOTE = prompts.render("agent.ask_skipped")
UNSHOWN_NOTE = prompts.render("agent.ask_unshown")


def _reply(**fields) -> str:
    """ask_user 的工具结果由系统生成：用户的回答、跳过、没呈现分别是不同的键，读回来时看键，不看文字。"""
    return json.dumps(fields, ensure_ascii=False)


def ask_resume(answer: str | None, skip: bool = False, pending: dict | None = None) -> dict:
    """用户的回答 → 中间件的 decisions。模型若违反“一次只问一个”在同一步发了多个 ask_user，
    只有第一个呈现给了用户，其余按未呈现处理（decisions 数量必须与 action_requests 一致）。"""
    message = _reply(skipped=True, note=SKIP_NOTE) if skip or not (answer or "").strip() else _reply(answer=answer.strip())
    n = len(pending.get("action_requests") or []) if pending else 1
    decisions = [{"type": "respond", "message": message}]
    decisions += [{"type": "respond", "message": _reply(unshown=True, note=UNSHOWN_NOTE)} for _ in range(max(n, 1) - 1)]
    return {"decisions": decisions}


def ask_result(m: ToolMessage) -> dict:
    """读回 ask_user 的工具结果。不是系统生成的格式就按用户原话处理。"""
    data = _parse(m)
    return data if isinstance(data, dict) else {"answer": str(m.content).strip()}


# ---- 从工具消息里解析候选来源 ----


def _parse(msg: ToolMessage) -> Any:
    try:
        return json.loads(msg.content if isinstance(msg.content, str) else "")
    except (TypeError, ValueError):
        return None


_DEFAULT_PORT = {"http": 80, "https": 443}
_UNRESERVED = frozenset(string.ascii_letters + string.digits + "-._~")
_PRINTABLE_ASCII = "".join(chr(i) for i in range(0x21, 0x7F))


def _pct(s: str) -> str:
    """百分号编码统一成一种写法：非 ASCII 字符编码成 %XX，编了码的非保留字符解开，十六进制大写。

    保留字符（/ ? & = 等）编码与否意思不同（%2F 不是路径分隔，%26 不是参数分隔），保持原样。
    """
    s = quote(s, safe=_PRINTABLE_ASCII)
    return re.sub(r"%([0-9A-Fa-f]{2})",
                  lambda m: c if (c := chr(int(m.group(1), 16))) in _UNRESERVED else "%" + m.group(1).upper(), s)


def url_key(url: str) -> str:
    """去重用：只做不改变指向的规范化——协议和域名的大小写、协议的默认端口、#锚点、百分号编码的写法、空路径。

    www、http / https、结尾斜杠、保留字符的编码都可能指向不同的页面，不合并：
    误合并会把一页的正文挂到另一页的来源上（证据错配），漏合并只是多一个来源。
    """
    url = url.strip()
    try:
        p = urlsplit(url)
        scheme, host, port = p.scheme.lower(), (p.hostname or "").lower(), p.port
    except ValueError:  # 端口之类写坏了：按原样比较
        return url
    if not host:
        return url
    netloc = (f"[{host}]" if ":" in host else host) + (f":{port}" if port is not None and port != _DEFAULT_PORT.get(scheme) else "")
    return f"{scheme}://{netloc}{_pct(p.path) or '/'}" + (f"?{_pct(p.query)}" if p.query else "")


def collect_sources(messages: list[BaseMessage], existing: list[Source], next_index: int) -> tuple[list[Source], dict[str, str]]:
    """返回新增的来源和 url→id 映射（含已有的）。读到正文的来源用正文片段替换搜索摘要。

    同一个网页按 url_key 算一个来源，网址保留第一次见到的写法：读页时网址只是写法不同（大小写、锚点、编码），也落在搜到的那个来源上。
    """
    by_url = {url_key(s.url): s for s in existing}
    new: list[Source] = []
    now = datetime.now(UTC).isoformat(timespec="seconds")
    for m in messages:
        if not isinstance(m, ToolMessage):
            continue
        data = _parse(m)
        if m.name == "search_web" and isinstance(data, list):
            for hit in data:
                url = (hit.get("url") or "").strip()
                if not url or url_key(url) in by_url:
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
                by_url[url_key(url)] = src
                new.append(src)
        elif m.name == "read_page" and isinstance(data, dict) and data.get("ok"):
            content = data.get("content") or ""
            if not content.strip():  # 没有正文不算读到，不能拿空串顶掉搜索摘要（网关已拦下，这里兜住回放和旧消息）
                continue
            url = (data.get("url") or "").strip()
            src = by_url.get(url_key(url))
            if src is None:
                src = Source(id=f"s{next_index}", url=url, title=data.get("title") or "", retrieved_at=now)
                next_index += 1
                by_url[url_key(url)] = src
                new.append(src)
            src.excerpt = content[:EXCERPT_CHARS]
            src.excerpt_kind = "body"
            if data.get("title") and not src.title:
                src.title = data["title"]
    return new, {s.url: s.id for s in by_url.values()}


def tool_step(call: dict, result: ToolMessage | None, url_ids: dict[str, str] | None = None) -> ToolStep | None:
    """一次工具调用 → 过程记录。没有结果的、被额度拦下的（结果不是系统的 JSON）、没呈现给用户的提问不记。

    url_ids 是 url_key → 来源 id：给了就把这一步得到的来源编号记上。
    """
    if result is None:
        return None
    name, args, cid = call["name"], call.get("args") or {}, call.get("id") or ""
    data = _parse(result)
    ids = url_ids or {}

    def sids(urls) -> list[str]:
        return [ids[k] for k in dict.fromkeys(url_key(u) for u in urls if u) if k in ids]

    if name == "search_web":
        query, limits = str(args.get("query", "")), _search_limits(args)
        if isinstance(data, list):
            return ToolStep(id=cid, kind="search", text=query, limits=limits, hits=len(data),
                            source_ids=sids((h.get("url") or "").strip() for h in data))
        if isinstance(data, dict):
            return ToolStep(id=cid, kind="search", text=query, limits=limits, ok=False, message=str(data.get("message") or data.get("kind") or ""))
        return None
    if name == "read_page":
        if not isinstance(data, dict):
            return None
        url = str(args.get("url", ""))
        if data.get("ok") and (data.get("content") or "").strip():
            return ToolStep(id=cid, kind="read", text=url, body=True, title=data.get("title") or "",
                            source_ids=sids([(data.get("url") or url).strip()]))
        return ToolStep(id=cid, kind="read", text=url, ok=False, message=str(data.get("message") or data.get("kind") or "没有读到正文"))
    if name == "load_skill":
        skill = skills.catalog().get(str(args.get("name", "")).strip())
        return ToolStep(id=cid, kind="skill", text=skill.title) if skill and result.status != "error" else None
    if name == "ask_user":
        r = ask_result(result)
        if r.get("unshown") or result.status == "error":
            return None
        return ToolStep(id=cid, kind="ask", text=str(args.get("question") or ""), ok=not r.get("skipped"),
                        message="" if r.get("skipped") else str(r.get("answer") or ""))
    return None


def collect_steps(messages: list[BaseMessage], url_ids: dict[str, str] | None = None) -> list[ToolStep]:
    """一次运行里的工具调用，按发起的顺序。"""
    results = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)}
    steps = (tool_step(tc, results.get(tc["id"]), url_ids) for m in messages if isinstance(m, AIMessage) for tc in m.tool_calls)
    return [s for s in steps if s is not None]


def search_failures(messages: list[BaseMessage]) -> list[str]:
    """这次运行里搜索服务出错的搜索（工具结果是 ok=false 的 JSON；额度用完被拦下的不算）。"""
    out = []
    for m in messages:
        if isinstance(m, ToolMessage) and m.name == "search_web" and m.status == "error":
            data = _parse(m)
            if isinstance(data, dict) and data.get("ok") is False:
                out.append(str(data.get("message") or data.get("kind") or ""))
    return out


def collect_skills(messages: list[BaseMessage]) -> list[str]:
    """这次运行里读取成功的调研方法，按读取顺序、去重。"""
    ok = {m.tool_call_id for m in messages if isinstance(m, ToolMessage) and m.name == "load_skill" and m.status != "error"}
    out: list[str] = []
    for m in messages:
        if not isinstance(m, AIMessage):
            continue
        for tc in m.tool_calls:
            name = str((tc.get("args") or {}).get("name") or "").strip()
            if tc["name"] == "load_skill" and tc["id"] in ok and name in skills.catalog() and name not in out:
                out.append(name)
    return out


def collect_asks(messages: list[BaseMessage]) -> list[AskRecord]:
    """从一次运行的消息里取出呈现给用户的提问和回答。没呈现的（未呈现、超额）不算。"""
    results = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage) and m.name == "ask_user"}
    out: list[AskRecord] = []
    for m in messages:
        if not isinstance(m, AIMessage):
            continue
        for tc in m.tool_calls:
            res = results.get(tc["id"])
            if tc["name"] != "ask_user" or res is None:
                continue
            data = ask_result(res)
            if data.get("unshown") or res.status == "error":
                continue
            q = str((tc.get("args") or {}).get("question") or "").strip()
            # 只有卡片上的“跳过”按钮算跳过（载荷里有明确标记）；自由文本原样记录，哪些条件算确认由整理步骤判断
            if data.get("skipped"):
                out.append(AskRecord(question=q, answer=None, skipped=True))
            else:
                out.append(AskRecord(question=q, answer=str(data.get("answer") or "")))
    return out


def _unshown(m: ToolMessage) -> bool:
    return m.name == "ask_user" and bool(ask_result(m).get("unshown"))
