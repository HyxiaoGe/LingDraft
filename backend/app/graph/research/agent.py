"""对话 agent：一条线程就是一个 create_agent 的对话，检查点挂在 agent 上，没有外层的图。

用户每说一句话：先写进消息（update_state，as_node=START），再接着跑（stream(None)），直到模型给出不带工具调用的回复。
- TurnMiddleware：还没有标题时按第一句话起标题；每轮开始、结束时记下这一轮；模型每次调用前换上系统提示词（日期按当天）。
  推荐问题不在这一轮里起：正文写完这一轮就结束，运行器在结束之后再起（见 api.research_runner）。
- ToolProgressMiddleware：工具开始、结束时往自定义流里写一步，页面据此显示进度。
- InvalidToolCallRetryMiddleware：模型返回无法解析的工具调用时，让它带着解析错误重发，最多几次。
- ContentGuard：模型服务的内容审核拦下请求时，去掉没通过的搜索、读页结果接着跑；去不掉就停下这一轮，之后不再带进上下文。
用户点了停止时，运行器掐断这次运行，再用 stop_turn 把这一轮收尾（见 api.research_runner）。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any, NotRequired

from langchain.agents import create_agent
from langchain.agents.middleware.types import AgentMiddleware, AgentState, ExtendedModelResponse, ModelRequest, hook_config
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.config import get_stream_writer
from langgraph.types import Command

from app.graph.research import history, prompts
from app.graph.research.sources import parse
from app.graph.research.state import INVALID_TOOL_CALL_RETRIES, ResearchState, Turn
from app.model_guard import ContentBlocked

TITLE_CHARS = 24  # 起标题失败时截取用户原话的长度
TURN_END = "TurnMiddleware.after_agent"  # 一轮的最后一个节点：在它名下写状态，线程就停在这一轮结束的地方


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def emit(event: dict) -> None:
    try:
        get_stream_writer()(event)
    except RuntimeError:  # 不在流式运行里（例如测试直接 invoke）
        pass


def system_prompt() -> str:
    return prompts.render("agent.system", today=date.today().isoformat())


class TurnMiddleware(AgentMiddleware[ResearchState]):
    state_schema = ResearchState

    def __init__(self, planner) -> None:
        super().__init__()
        self.planner = planner

    def before_agent(self, state: ResearchState, runtime) -> dict[str, Any] | None:
        msgs = state["messages"]
        human = msgs[history.last_human(msgs)]
        out: dict[str, Any] = {"turns": list(state.get("turns", [])) + [Turn(id=human.id, started_at=now())]}
        if not state.get("title"):  # 按这段对话的第一句起（第一轮排队时就停了的，下一轮补上）
            text = history.requests(msgs)[0]
            try:
                title = self.planner.title(text).strip() or text[:TITLE_CHARS]
            except Exception:  # noqa: BLE001  起标题失败不影响回答，截原话
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
        out: dict[str, Any] = {}
        fixes = history.dangling(turn_msgs)
        if fixes:
            out["messages"] = fixes
        reply = history.final_text(turn_msgs)
        blocked = any(history.blocked_reason(m) for m in turn_msgs)
        turns[-1] = turns[-1].model_copy(update={"ended_at": now(), "suggestions": [], "usage": history.usage_of(turn_msgs),
                                                 "outcome": "blocked" if blocked else "ok" if reply else "no_reply"})
        out["turns"] = turns
        return out


def stop_turn(values: dict, partial: AIMessage | None) -> dict[str, Any]:
    """用户点了停止，这一轮到此为止：没跑完的工具调用补上结果，已经流出来的正文（partial）存成这一轮的回复，记下这一轮。
    返回的更新以 TURN_END 的名义写进检查点。这一轮还没开始（排队时就停了）也要补一条记录。

    values 是 get_state 拿到的：被掐断的那一步里已经做完的任务（比如并行的几个搜索里先回来的）的结果算在里面，
    但还没落进检查点，update_state 时会被丢掉。所以这一轮的消息原样再写一遍（同一个 id 覆盖，不会重复）。"""
    msgs = values["messages"]
    start = history.last_human(msgs)
    human, turn_msgs = msgs[start], msgs[start + 1:]
    new: list = turn_msgs + history.dangling(turn_msgs, stopped=True)
    if partial is not None and history.text_of(partial).strip():
        new.append(partial)
    turns = list(values.get("turns", []))
    if not turns or turns[-1].id != human.id:
        turns.append(Turn(id=human.id, started_at=now()))
    turns[-1] = turns[-1].model_copy(update={"ended_at": now(), "suggestions": [], "usage": history.usage_of(new), "outcome": "stopped"})
    return {"messages": new, "turns": turns}


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
    - 新加进来的是用户的话，或者去掉资料后还是拦：这一轮停下；
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
        # 停下时的原因：上一条模型消息之后只有用户的话（这一轮的第一句）是 input，有工具结果是 material
        reason = "material" if tail else "input"
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


def build_agent(planner, checkpointer: BaseCheckpointSaver):
    return create_agent(
        model=planner.agent_model(),
        tools=planner.tools(),
        state_schema=ResearchState,
        middleware=[
            TurnMiddleware(planner),
            ContentGuard(planner),  # 在 TurnMiddleware 里面：重发的请求也带着换好的系统提示词
            ToolProgressMiddleware(),
            InvalidToolCallRetryMiddleware(INVALID_TOOL_CALL_RETRIES),
        ],
        checkpointer=checkpointer,
        name="research",
    )
