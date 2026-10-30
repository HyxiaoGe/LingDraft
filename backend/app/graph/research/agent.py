"""调研 agent：一条线程就是一个 create_agent 的对话，检查点挂在 agent 上，没有外层的图。

用户每说一句话：先写进消息（update_state，as_node=START），再接着跑（stream(None)），直到模型给出不带工具调用的回复。
- TurnMiddleware：第一轮开始前起标题；每轮开始、结束时记下这一轮；模型每次调用前换上系统提示词（日期按当天）；
  一轮结束时工作稿有改动就落成新的一版，再要推荐问题。
- ask_user 由 HumanInTheLoopMiddleware 在执行前拦截（respond），用户的回答直接成为工具结果。
- ReportToolGuard：改报告的工具同一批里只执行第一个，后面的直接返回错误（并行执行会互相覆盖工作稿）。
- ToolProgressMiddleware：工具开始、结束时往自定义流里写一步，页面据此显示进度。
- InvalidToolCallRetryMiddleware：模型返回无法解析的工具调用时，让它带着解析错误重发，最多几次。
- ContentGuard：模型服务的内容审核拦下请求时，去掉没通过的搜索、读页结果接着跑；去不掉就停下这一轮，之后不再带进上下文。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, NotRequired

from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain.agents.middleware.types import AgentMiddleware, AgentState, ExtendedModelResponse, ModelRequest, hook_config
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.config import get_config, get_stream_writer
from langgraph.types import Command

from app.graph.research import history, prompts, skills, workbench
from app.graph.research.sources import parse
from app.graph.research.state import INVALID_TOOL_CALL_RETRIES, ResearchState, Turn
from app.graph.research.workbench import REPORT_TOOLS
from app.model_guard import ContentBlocked

TITLE_CHARS = 24  # 起标题失败时截取用户原话的长度


def emit(event: dict) -> None:
    try:
        get_stream_writer()(event)
    except RuntimeError:  # 不在流式运行里（例如测试直接 invoke）
        pass


def system_prompt() -> str:
    method = prompts.render("agent.skill_catalog", catalog=skills.catalog_text()) if skills.catalog() else ""
    return prompts.render("agent.system", today=date.today().isoformat(), method=method)


def _thread_id() -> str:
    return str(get_config()["configurable"]["thread_id"])


class TurnMiddleware(AgentMiddleware[ResearchState]):
    state_schema = ResearchState

    def __init__(self, planner, out_dir: Path) -> None:
        super().__init__()
        self.planner = planner
        self.out_dir = Path(out_dir)

    def before_agent(self, state: ResearchState, runtime) -> dict[str, Any] | None:
        msgs = state["messages"]
        human = msgs[history.last_human(msgs)]
        out: dict[str, Any] = {"turns": list(state.get("turns", [])) + [Turn(id=human.id, started_at=workbench.now())]}
        if not state.get("title"):
            text = history.said(human)["text"]
            try:
                title = self.planner.title(text).strip() or text[:TITLE_CHARS]
            except Exception:  # noqa: BLE001  起标题失败不影响调研，截原话
                title = text[:TITLE_CHARS]
            out["title"] = title
            emit({"type": "title", "title": title})
        return out

    def wrap_model_call(self, request: ModelRequest, handler):
        return handler(request.override(system_message=SystemMessage(system_prompt())))

    def after_agent(self, state: ResearchState, runtime) -> dict[str, Any] | None:
        msgs = state["messages"]
        start = history.last_human(msgs)
        turn_msgs = msgs[start + 1:]
        turns = list(state.get("turns", []))
        turn = turns[-1]
        out: dict[str, Any] = {}
        fixes = history.dangling(turn_msgs)
        if fixes:
            out["messages"] = fixes
        version = None
        if state.get("changes"):
            out.update(workbench.publish(state, self.out_dir / _thread_id(), turn.id))
            version = out["versions"][-1].n
        reply = history.final_text(turn_msgs)
        blocked = any(history.blocked_reason(m) for m in turn_msgs)
        suggestions: list[str] = []
        if reply:
            try:
                suggestions = self.planner.suggest(history.said(msgs[start])["text"], reply, bool(state.get("outline")))[:3]
            except Exception:  # noqa: BLE001  推荐问题失败就不给
                suggestions = []
        turns[-1] = turn.model_copy(update={"ended_at": workbench.now(), "version": version, "suggestions": suggestions,
                                            "usage": history.usage_of(turn_msgs),
                                            "outcome": "blocked" if blocked else "ok" if reply else "no_reply"})
        out["turns"] = turns
        return out


def _batch(messages: list, tool_call_id: str) -> list[dict]:
    """这个工具调用所在的那一批。不一定是最后一条消息：提问回答后，HITL 会在它后面追加提问的结果，再执行同批其余的调用。"""
    for m in reversed(messages):
        if isinstance(m, AIMessage) and any(tc["id"] == tool_call_id for tc in m.tool_calls):
            return list(m.tool_calls)
    return []


class ReportToolGuard(AgentMiddleware):
    def wrap_tool_call(self, request, handler):
        call = request.tool_call
        if call["name"] in REPORT_TOOLS:
            first = next((tc for tc in _batch(request.state.get("messages", []), call["id"]) if tc["name"] in REPORT_TOOLS), None)
            if first is not None and first["id"] != call["id"]:
                return ToolMessage(content=prompts.render("agent.report_tool_busy"), name=call["name"], tool_call_id=call["id"],
                                   status="error", artifact={"hidden": True})
        return handler(request)


def _result_message(result) -> ToolMessage | None:
    if isinstance(result, ToolMessage):
        return result
    if isinstance(result, Command) and isinstance(result.update, dict):
        return next((m for m in result.update.get("messages", []) if isinstance(m, ToolMessage)), None)
    return None


class ToolProgressMiddleware(AgentMiddleware):
    """工具开始时写一步（pending），做完再写一次带结果的。两次用同一个 id（工具调用 id），页面据此把它们和存下来的记录对上。"""

    def wrap_tool_call(self, request, handler):
        call = request.tool_call
        step = history.step_of(call, None)
        if step is None:
            return handler(request)
        emit({"type": "step", "step": step.model_dump()})
        result = handler(request)
        done = history.step_of(call, _result_message(result))
        if done is not None:
            emit({"type": "step", "step": done.model_dump()})
        return result


class _RetryState(AgentState):
    invalid_retries: NotRequired[int]


class InvalidToolCallRetryMiddleware(AgentMiddleware[_RetryState]):
    """最后一条模型消息只有无效工具调用、没有有效调用时，让模型带着解析错误重发。次数每轮重新算。

    无效的调用在下一次请求里仍作为 tool_calls 发给模型接口，每个都要先有一条工具结果，否则接口直接报错。
    """

    state_schema = _RetryState

    def __init__(self, max_retries: int) -> None:
        super().__init__()
        self.max_retries = max_retries

    def before_agent(self, state: _RetryState, runtime) -> dict[str, Any] | None:
        return {"invalid_retries": 0} if state.get("invalid_retries") else None

    @hook_config(can_jump_to=["model"])
    def after_model(self, state: _RetryState, runtime) -> dict[str, Any] | None:
        last = state["messages"][-1] if state.get("messages") else None
        if not isinstance(last, AIMessage) or last.tool_calls or not last.invalid_tool_calls:
            return None
        n = state.get("invalid_retries", 0)
        if n >= self.max_retries:
            return None
        errors = [ToolMessage(content=prompts.render("agent.invalid_call", error=str(tc.get("error") or "")[:200]), name=tc.get("name") or "unknown",
                              tool_call_id=tc["id"], status="error", artifact={"hidden": True})
                  for tc in last.invalid_tool_calls if tc.get("id")]
        return {"messages": errors, "jump_to": "model", "invalid_retries": n + 1}


MATERIAL_TOOLS = ("search_web", "read_page")


def _withdrawn(m: ToolMessage) -> ToolMessage:
    """整条结果去掉：搜索按搜索失败的样子（status=error），读页按读页失败的样子（ok=false），同一个 id 覆盖原来的。"""
    note = prompts.render("agent.material_blocked")
    if m.name == "search_web":
        body = {"ok": False, "kind": "content_blocked", "message": note, "retryable": False}
        return m.model_copy(update={"content": json.dumps(body, ensure_ascii=False), "status": "error"})
    data = parse(m)
    url = data.get("url") if isinstance(data, dict) else None
    body = {"ok": False, "url": url, "kind": "content_blocked", "message": note, "retryable": False}
    return m.model_copy(update={"content": json.dumps(body, ensure_ascii=False)})


class ContentGuard(AgentMiddleware):
    """模型服务的内容审核拦下了请求（整个请求被拒）：
    - 新加进来的有搜索、读页的结果：一条条单独送审（搜索结果按条），去掉没通过的再试；单条都能过（合在一起才拦），这一批整个去掉再试；
    - 新加进来的没有资料（用户的话、提问的回答、改报告返回的报告），或者去掉资料后还是拦：这一轮停下；
    - 模型的输出被截断：这一轮停下，已经流出去的思考和正文不留。
    去掉的结果写回消息（同一个 id 覆盖），之后的调用不会再带上。停下的那一轮整轮不再带进之后的上下文，
    不然以后每次请求都会被拦。停下时补一条不带工具调用的模型消息，agent 就此结束这一轮，页面据它显示固定的说明。
    """

    def __init__(self, planner) -> None:
        super().__init__()
        self.planner = planner

    def wrap_model_call(self, request: ModelRequest, handler):
        request = request.override(messages=history.without_blocked_turns(request.messages))
        try:
            return handler(request)
        except ContentBlocked as e:
            if e.output:
                return _stop("output")
            return self._recover(request, handler)

    def _recover(self, request: ModelRequest, handler):
        msgs = request.messages
        start = next((i + 1 for i in range(len(msgs) - 1, -1, -1) if isinstance(msgs[i], AIMessage)), 0)
        tail = [m for m in msgs[start:] if isinstance(m, ToolMessage)]
        material = [m for m in tail if m.name in MATERIAL_TOOLS and m.status != "error" and m.id]
        # 停下时的原因：新加进来的只有用户的话（这一轮的第一句、提问的回答）是 input，别的（资料、改报告返回的报告）是 material
        reason = "input" if not tail or any(m.name == "ask_user" for m in tail) else "material"
        if not material:
            return _stop(reason)
        whole = {m.id: _withdrawn(m) for m in material}
        found = self._drop_blocked(material)
        tries = [found] if found else []
        if any(k not in found or found[k].content != w.content for k, w in whole.items()):  # 单条去掉的就是整批时不重复试
            tries.append(whole)
        for repl in tries:
            try:
                resp = handler(request.override(messages=[repl.get(m.id, m) for m in msgs]))
            except ContentBlocked as e:
                if e.output:
                    return _stop("output")
                continue
            return ExtendedModelResponse(model_response=resp, command=Command(update={"messages": list(repl.values())}))
        return _stop(reason)

    def _drop_blocked(self, material: list[ToolMessage]) -> dict[str, ToolMessage]:
        """单条送审，返回 {消息 id: 去掉没通过内容后的结果}；一条都没拦下是空的。"""
        pieces: list[tuple[int, int | None, str]] = []  # (第几条结果, 搜索结果里的第几条（None 是整条）, 送审的文字)
        for i, m in enumerate(material):
            data = parse(m)
            if m.name == "search_web" and isinstance(data, list):
                pieces += [(i, k, json.dumps(h, ensure_ascii=False)) for k, h in enumerate(data)]
            else:
                pieces.append((i, None, history.text_of(m)))
        bad: dict[int, set[int | None]] = {}
        for (i, k, _), hit in zip(pieces, self.planner.blocked([t for _, _, t in pieces]), strict=True):
            if hit:
                bad.setdefault(i, set()).add(k)
        out: dict[str, ToolMessage] = {}
        for i, ks in bad.items():
            m = material[i]
            data = parse(m)
            kept = [h for k, h in enumerate(data) if k not in ks] if None not in ks and isinstance(data, list) else []
            if not kept:
                out[m.id] = _withdrawn(m)
                continue
            art = {**(m.artifact if isinstance(m.artifact, dict) else {}), "blocked": len(ks)}
            out[m.id] = m.model_copy(update={"content": json.dumps(kept, ensure_ascii=False), "artifact": art})
        return out


def _stop(reason: str) -> AIMessage:
    return AIMessage(content="", response_metadata={history.BLOCKED: reason})


def build_agent(planner, checkpointer: BaseCheckpointSaver, out_dir: Path):
    skills.catalog()  # 技能文件写坏了，构建时（服务启动）就报错
    return create_agent(
        model=planner.agent_model(),
        tools=planner.tools() + workbench.make_report_tools(planner),
        state_schema=ResearchState,
        middleware=[
            TurnMiddleware(planner, out_dir),
            ContentGuard(planner),  # 在 TurnMiddleware 里面：重发的请求也带着换好的系统提示词
            HumanInTheLoopMiddleware(interrupt_on={"ask_user": {"allowed_decisions": ["respond"]}}),
            ReportToolGuard(),
            ToolProgressMiddleware(),  # 在 ReportToolGuard 里面：被拦下的调用不报进度
            InvalidToolCallRetryMiddleware(INVALID_TOOL_CALL_RETRIES),
        ],
        checkpointer=checkpointer,
        name="research",
    )
