"""子 agent：主 agent 用 task 工具把一件查资料的事交给它，它在自己的上下文里搜、读，最后交回一份报告。

- 子 agent 是另一个 create_agent：模型用快一些、便宜一些的（planner.subagent_model）；工具只有搜索、读页、取时间；
  挂着内容审核、进度、无效调用重发这几个 middleware，和主 agent 的一样。它不能再派子 agent。
- 主 agent 一次回复里调几次 task，这几个子 agent 就并行跑（每个工具调用是一个 Send 任务）。每轮最多 MAX_PER_TURN 个；
  同一个对话同时最多跑 MAX_RUNNING 个，多的排队：几个子 agent 各自再并行搜，搜索服务容易被打到限流。
- 子 agent 调模型最多 MAX_MODEL_CALLS 次，最后一次不让它再调工具，用已经查到的写报告：这次请求的最后加一条说明（不存进记录），工具关掉。
  说明不放系统提示词里：上下文长了模型注意不到开头那段，接着想搜，工具又关了，就把工具调用的原始标记写进了正文（2026-11-01 真实模型跑出来的）。
- 搜索、读页各有预算（TOOL_BUDGET）。只写在提示词里管不住 Flash，它会一直查到调用上限（见 docs/LangGraph研究/06-子agent查个没完.md）：
  - 超了的调用不执行，模型收到一条“不要再调”（LangChain 的 ToolCallLimitMiddleware，continue 模式）；
  - 每次工具结果回来，请求末尾加一条各工具用了几次、还剩几次（BATS 的 Budget Tracker，不存进记录）。真实模型里守住预算靠的是这一条。
- 子 agent 的检查点跟着主 agent 的存储（create_agent 不传 checkpointer），存在 tools:<任务 id> 这个命名空间下：
  这一轮中途出错，重试时同一个任务接着它自己的检查点跑，做过的搜索、读页不重来。
- 交回给主 agent 的只有报告（工具结果的 content）。子 agent 的整段消息放在 artifact 里，模型看不到；
  用户点了停止、还没写完报告的，交回的是它已经查到的来源（见 agent.stop_turn）。
  页面的子任务卡、这一轮的来源和用量从这里读（见 sources.subagent_messages）。
- 运行器要知道子 agent 的流是哪个 task 调用的：开跑前往自定义流里写一条 subagent（调用 id 和命名空间），运行器自己用，不发给页面。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import NotRequired
from weakref import WeakValueDictionary

from langchain.agents import create_agent
from langchain.agents.middleware import ToolCallLimitMiddleware
from langchain.agents.middleware.types import AgentMiddleware, AgentState, ModelRequest
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage, messages_to_dict
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app import config
from app.graph.research import history, prompts
from app.graph.research.agent import (
    CHINA_TZ,
    ChatContext,
    ContentGuard,
    InvalidToolCallRetryMiddleware,
    ToolProgressMiddleware,
    _retrying,
    emit,
    system_prompt,
    turn_start,
)
from app.graph.research.sources import SUBAGENT_TOOL, known_ids
from app.graph.research.state import INVALID_TOOL_CALL_RETRIES
from app.tools.research_tools import TIME_TOOL

SUBAGENT_TOOLS = ("search_web", "read_page", TIME_TOOL)
MAX_PER_TURN = 5  # 每轮最多派几个（2026-11-01 Sean 定 3～5 个）
MAX_RUNNING = 3  # 同一个对话同时最多跑几个，多的排队
MAX_MODEL_CALLS = 12  # 一个子 agent 最多调几次模型
TOOL_BUDGET = {"search_web": config.SUBAGENT_SEARCH_BUDGET, "read_page": config.SUBAGENT_READ_BUDGET}  # 一个子 agent 每种工具最多调几次
# 系统提示词按这个顺序拼（见 prompts.toml 的 [subagent]）；日期、工具失败、来源不可信这几段和主 agent 的一样
SYSTEM_PROMPT = ("subagent.identity", "agent.current_date", "subagent.process", "subagent.source_quality", "agent.tool_failure",
                 "agent.sources", "subagent.citations", "subagent.report", "subagent.language")


class SubagentState(AgentState):
    started_at: NotRequired[str]  # 主 agent 这一轮开始的时间：日期按它算，和主 agent 一样
    parent_sources: NotRequired[dict[str, str]]  # 派它时主 agent 那边已有的来源 {url_key: id}：同一个网址编出同一个 id


class SubagentPrompt(AgentMiddleware[SubagentState]):
    """每次调模型前换上子 agent 的系统提示词（每次一样，前缀缓存接得上）。请求末尾按情况加一条说明（不存进记录）：
    到了次数上限，最后一次说明要写报告，工具关掉；不然工具结果刚回来，就写上各工具用了几次、还剩几次（ToolBudget 记的数）。
    也写一个阶段（phase），卡片上说明在等什么。"""

    state_schema = SubagentState

    def wrap_model_call(self, request: ModelRequest, handler):
        emit({"type": "phase", "phase": "retry" if _retrying(request.messages) else "model"})
        try:
            at = datetime.fromisoformat(request.state.get("started_at") or "")
        except ValueError:
            at = datetime.now(CHINA_TZ)
        budget = {"search_budget": TOOL_BUDGET.get("search_web"), "read_budget": TOOL_BUDGET.get("read_page")}
        request = request.override(system_message=SystemMessage(system_prompt(at, SYSTEM_PROMPT, **budget)))
        if sum(isinstance(m, AIMessage) for m in request.messages) >= MAX_MODEL_CALLS - 1:
            last = HumanMessage(prompts.render("subagent.last_call"))
            request = request.override(messages=[*request.messages, last], tool_choice="none")
        elif TOOL_BUDGET and request.messages and isinstance(request.messages[-1], ToolMessage):
            used = request.state.get("thread_tool_call_count") or {}
            tools = [(t, used.get(t, 0), max(0, n - used.get(t, 0))) for t, n in TOOL_BUDGET.items()]
            request = request.override(messages=[*request.messages, HumanMessage(prompts.render("subagent.budget", tools=tools))])
        return handler(request)


class ToolBudget(ToolCallLimitMiddleware):
    """一种工具的预算：用完了，再调就不执行，模型收到 LangChain 写的“不要再调这个工具”，接着自己决定（continue 模式）。
    次数记在子 agent 的状态里（thread_tool_call_count，跟着检查点存），出错重试接着跑也接着算。
    补的这条结果标上 over_budget：页面上不列，用量不算（history.not_run）。"""

    def __init__(self, tool: str, limit: int) -> None:
        super().__init__(tool_name=tool, thread_limit=limit, exit_behavior="continue")

    def after_model(self, state, runtime):
        out = super().after_model(state, runtime)
        if out and out.get("messages"):
            out["messages"] = [m.model_copy(update={"artifact": {"over_budget": True}}) if isinstance(m, ToolMessage) else m
                               for m in out["messages"]]
        return out


def build_subagent(planner):
    return create_agent(
        model=planner.subagent_model(),
        tools=[t for t in planner.tools() if t.name in SUBAGENT_TOOLS],
        state_schema=SubagentState,
        context_schema=ChatContext,
        middleware=[
            SubagentPrompt(),
            ContentGuard(planner),
            ToolProgressMiddleware(),
            InvalidToolCallRetryMiddleware(INVALID_TOOL_CALL_RETRIES),
            *(ToolBudget(t, n) for t, n in TOOL_BUDGET.items()),
        ],
        name="subagent",
    )


def launched_before(messages: list[BaseMessage], call_id: str | None) -> int:
    """这一轮里排在这次调用前面、真派出去了的子 agent 有几个。超了上限没派的不算；同一条模型消息里并行的几个还没有结果，都算派了。"""
    turn = messages[history.last_human(messages) + 1:]
    results = {m.tool_call_id: m for m in turn if isinstance(m, ToolMessage)}
    n = 0
    for m in turn:
        if not isinstance(m, AIMessage):
            continue
        for tc in m.tool_calls:
            if tc["name"] != SUBAGENT_TOOL:
                continue
            if tc.get("id") == call_id:
                return n
            r = results.get(tc.get("id"))
            if r is None or not (isinstance(r.artifact, dict) and r.artifact.get("failure") == "limit"):
                n += 1
    return n


_slots: WeakValueDictionary[str, threading.BoundedSemaphore] = WeakValueDictionary()
_slots_mu = threading.Lock()


@contextmanager
def _slot(thread_id: str, call_id: str, check_stop: Callable[[], None]) -> Iterator[None]:
    """占一个这个对话的子 agent 名额；满了就等，等的时候点了停止就不等了。等的时候页面的卡片上说一声在排队。"""
    with _slots_mu:
        sem = _slots.get(thread_id)
        if sem is None:
            sem = _slots[thread_id] = threading.BoundedSemaphore(MAX_RUNNING)
    if not sem.acquire(blocking=False):
        emit({"type": "phase", "phase": "queued", "task": call_id})
        while not sem.acquire(timeout=0.2):
            check_stop()
    try:
        yield
    finally:
        sem.release()


class TaskArgs(BaseModel):
    title: str = Field(min_length=1, max_length=60, description=prompts.render("tools.task.title"))
    prompt: str = Field(min_length=1, description=prompts.render("tools.task.prompt"))


def make_task_tool(planner):
    worker = build_subagent(planner)

    @tool(SUBAGENT_TOOL, description=prompts.render("tools.task.description", max_per_turn=MAX_PER_TURN), args_schema=TaskArgs)
    def task(title: str, prompt: str, runtime: ToolRuntime) -> ToolMessage:
        msgs = runtime.state.get("messages", [])

        def result(content: str, ok: bool = True, sub: list[BaseMessage] | None = None, **art) -> ToolMessage:
            if sub:
                art["messages"] = messages_to_dict(sub)
            return ToolMessage(content=content, tool_call_id=runtime.tool_call_id, name=SUBAGENT_TOOL,
                               status="success" if ok else "error", artifact=art)

        if launched_before(msgs, runtime.tool_call_id) >= MAX_PER_TURN:
            return result(prompts.render("tools.task.limit", max_per_turn=MAX_PER_TURN), ok=False, failure="limit")
        conf = runtime.config["configurable"]
        emit({"type": "subagent", "id": runtime.tool_call_id, "ns": conf.get("checkpoint_ns", "")})
        inp = {"messages": [HumanMessage(prompt.strip())], "started_at": turn_start(msgs, runtime.state).isoformat(),
               "parent_sources": known_ids(msgs)}
        check_stop = runtime.context.check_stop if runtime.context else (lambda: None)  # 测试里直接 invoke 时没有 context
        with _slot(conf["thread_id"], runtime.tool_call_id, check_stop):
            out = worker.invoke(inp, context=runtime.context)
        sub = out["messages"]
        if any(history.blocked_reason(m) for m in sub):
            return result(prompts.render("tools.task.blocked"), ok=False, sub=sub, failure="blocked")
        report = history.final_text(sub)
        if not report:
            return result(prompts.render("tools.task.no_report"), ok=False, sub=sub, failure="no_report")
        return result(report, sub=sub)

    return task
