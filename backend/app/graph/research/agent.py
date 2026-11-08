"""对话 agent：一条线程就是一个 create_agent 的对话，检查点挂在 agent 上，没有外层的图。

用户每说一句话：先写进消息（update_state，as_node=START），再接着跑（stream(None)），直到模型给出不带工具调用的回复。
- TurnMiddleware：每轮开始、结束时记下这一轮；开始时定下这一轮用哪个模型（见 model_catalog.Catalog.plan，记进这一轮的记录）；
  Auto 的新对话第一句话先让模型判断难易，定下一个模型，整段对话沿用（state 的 auto_model，2026-11-08 Sean 定）；
  模型每次调用前换上系统提示词（日期按这一轮开始时北京时间的当天）。
  还没有标题时，按第一句话起标题，和调模型同时跑，不耽误回答（见 _TitleJob）。
  几点钟不放进发给模型的内容，要用时模型自己调 get_current_time（见 tools.research_tools）。
  调模型之前往自定义流里写一个阶段（phase），页面在还没有字和步骤的空档里据此说明在等什么。
  用户在这一轮里插了话（见 api.research_runner.guide）：主 agent 每次调模型之前取一次，有就放进对话，模型接着做时看得到；
  不开新的一轮（见 history.is_guide），页面上排在这一轮的过程里它起作用的地方。
  推荐问题不在这一轮里起：正文写完这一轮就结束，运行器在结束之后再起（见 api.research_runner）。
- ToolProgressMiddleware：工具开始、结束时往自定义流里写一步，页面据此显示进度；做完的那一步带上它得到的来源。
- ToolChargeMiddleware：做成了的工具调用按次记一行账（见 app.costs），钱数写进工具结果。
- InvalidToolCallRetryMiddleware：模型返回无法解析的工具调用时，让它带着解析错误重发，最多几次。
- ModelCallCapMiddleware：一轮里调模型到了上限，最后一次不许调工具，用已经查到的回答（和子 agent 一样）。
- InterruptedRetryMiddleware：模型生成到一半断了，自动再调一次，页面去掉断掉的半截（子 agent 也挂）。
- ModelSelect：每次调模型换上这一轮定下的模型；之前几轮是别的模型答的，发过去时不带那几轮的思考（子 agent 也挂）。
  主 agent 的模型出问题（不可用、首字超时、免费额度用完）换备用重调：用户选的模型走免费额度的先换直连，别的换 Flash（2026-11-08 Sean 定）。
- ContentGuard：模型服务的内容审核拦下请求时，去掉没通过的搜索、读页结果接着跑；去不掉就停下这一轮，之后不再带进上下文。
- CompactionMiddleware：对话太长时，发给模型的那份里把之前几轮压一压，state 不改（见 compaction）；压掉的原文用 recall 工具取回。
工具里有 task：把一件查资料的事交给子 agent（见 subagent），一次回复里调几次就并行跑几个。
用户点了停止时，运行器取消这次运行（正在等的模型、工具马上断开），再用 stop_turn 把这一轮收尾（见 api.research_runner）。
agent 在对话的事件循环里异步地跑（见 app.aio）：中间件里调模型、读写存储的都写成 a 开头的异步钩子，不做事的照常写同步的。
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import unicodedata
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any, NotRequired

from langchain.agents import create_agent
from langchain.agents.middleware.types import AgentMiddleware, AgentState, ExtendedModelResponse, ModelRequest, hook_config
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage, messages_to_dict
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.config import get_stream_writer
from langgraph.types import Command

from app import costs
from app.graph.research import history, prompts
from app.graph.research.memory import MemoryBook
from app.identity import is_guest_sub
from app.graph.research.sources import DOCS_TOOL, SEPARATORS, SUBAGENT_TOOL, collect_sources, parse, search_hits
from app.graph.research.state import INVALID_TOOL_CALL_RETRIES, ResearchState, Source, Turn
from app.model_catalog import AUTO, LEGACY_MODEL, LEGACY_USE, ModelUse
from app.model_guard import ContentBlocked, FreeTierUsedUp, Interrupted, ModelUnavailable

TITLE_CHARS = 30  # 标题最长这么多字（汉字算一个、字母算半个，见 _width）：模型给的超过了不用（见 usable_title）；用户原话超过了截掉（照 Fusion）
NEW_CHAT = "新对话"  # 模型给的不能用、用户原话里也没有字（纯数字、纯符号）时的标题
TITLE_WAIT = 10  # 第一次调模型结束时标题还没起好，最多再等这么久（秒），再等不到用原话
MAX_MODEL_CALLS = 20  # 主 agent 一轮最多调几次模型（2026-11-03 Sean 定）：只防跑飞，到 11-03 的真实对话每轮最多 2 次
RESTART_WAIT_S = 2  # 模型生成到一半断了，等这么久再调一次（DeepSeek 资源不足时马上再发多半还是不够）
ROUTE_WAIT_S = 10  # Auto 第一句话判断难易最多等这么久（秒），等不到当复杂
log = logging.getLogger(__name__)
TURN_END = "TurnMiddleware.after_agent"  # 一轮的最后一个节点：在它名下写状态，线程就停在这一轮结束的地方
LOOP_ENTRY = "TurnMiddleware.before_model"  # 每次调模型从这个节点开始（工具做完也回到这里）：停在它前面的检查点就是“要调模型了”
CHINA_TZ = timezone(timedelta(hours=8))
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
# 系统提示词按这个顺序拼（见 prompts.toml 的 [agent]）
# memory 是长期记忆，没有记忆时不拼（见 memory.py）
SYSTEM_SECTIONS = ("identity", "current_date", "tool_selection", "tool_failure", "tool_usage", "sources", "citations", "memory", "language")
MEMORY_SECTION = "agent.memory"
AGENT_PROMPT = tuple(f"agent.{k}" for k in SYSTEM_SECTIONS)


async def _no_guide() -> HumanMessage | None:
    return None


@dataclass(frozen=True)
class ChatContext:
    """一次运行的上下文（create_agent 的 context，不进检查点），中间件从 runtime.context 拿。"""

    take_guide: Callable[[], Awaitable[HumanMessage | None]] = _no_guide  # 取走用户插进这一轮的话（取一次就没了）；没有是 None
    # 上一次运行已经起好、页面上已经显示了，却没写进 state 的标题（那次第一次调模型出错了，出错的线程写不了 state）：这次直接用，不再起一个不一样的
    title: str | None = None
    # 这段对话归谁（见 app.identity）：系统提示词里放这个人的记忆。游客的、没归谁的（登录以前的老对话）没有记忆这一段
    owner: str | None = None


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def emit(event: dict) -> None:
    try:
        get_stream_writer()(event)
    except RuntimeError:  # 不在流式运行里（例如测试直接 invoke）
        pass


def turn_started(request: ModelRequest) -> datetime:
    """这一轮开始的北京时间。照 Fusion 在准备这一轮的请求时取一次：一轮里调好几次模型都一样，前缀缓存用得上；
    重新回答按重新回答的时候。取不到这一轮的记录就按现在。"""
    return turn_start(request.messages, request.state)


def turn_start(msgs: list[BaseMessage], state: dict) -> datetime:
    """同 turn_started，按 state 里的消息和每轮的记录取（工具里没有 ModelRequest，用这个）。"""
    i = history.last_human(msgs)
    turn = next((t for t in reversed(state.get("turns", [])) if i >= 0 and t.id == msgs[i].id), None)
    try:
        return datetime.fromisoformat(turn.started_at).astimezone(CHINA_TZ) if turn else datetime.now(CHINA_TZ)
    except ValueError:
        return datetime.now(CHINA_TZ)


def _day(d: date) -> str:
    return f"{d.isoformat()} ({WEEKDAYS[d.weekday()]})"


def system_prompt(at: datetime, keys: Sequence[str] = AGENT_PROMPT, memory: str = "", **extra: Any) -> str:
    """照 Fusion：日期按北京时间，带星期和明天、这周末、下周末的日期，模型不用自己推星期几。keys 是按顺序拼的各段（子 agent 用另一套），
    memory 是长期记忆（MemoryBook.prompt_text，空的就没有这一段），extra 是这些段里另外要填的变量。"""
    dates = date_anchors(at.astimezone(CHINA_TZ).date())
    return "\n\n".join(prompts.render(k, **dates, memory=memory, **extra) for k in keys if k != MEMORY_SECTION or memory)


def date_anchors(today: date) -> dict[str, str]:
    """系统提示词里给模型的日期：今天、明天、这周末、下周末，带星期（评测核对引用的裁判也用同一份，见 app.evals.research）。"""
    saturday = today - timedelta(days=today.weekday()) + timedelta(days=5)
    return {"current_date": _day(today), "tomorrow": _day(today + timedelta(days=1)),
            "this_saturday": _day(saturday), "this_sunday": _day(saturday + timedelta(days=1)),
            "next_saturday": _day(saturday + timedelta(days=7)), "next_sunday": _day(saturday + timedelta(days=8))}


class TurnMiddleware(AgentMiddleware[ResearchState]):
    state_schema = ResearchState

    def __init__(self, planner, memory: MemoryBook | None = None) -> None:
        super().__init__()
        self.planner = planner
        self.memory = memory

    def before_agent(self, state: ResearchState, runtime) -> dict[str, Any] | None:
        """同步跑时（测试直接 invoke）：Auto 不判断难易，当复杂。"""
        return self._begin(state, runtime, None)

    async def abefore_agent(self, state: ResearchState, runtime) -> dict[str, Any] | None:
        """开始一轮：记下开始时间和这一轮用的模型。用哪个模型只在这里定（以后查权限、余额也在这里），一轮里不换，出错重试接着用；
        出问题换备用的在 ModelSelect。对话没定选项的（页面没传、2026-11-05 之前的对话）这时定成 Auto；定下的选项停了，改用 Auto，之后都是 Auto。
        Auto 的对话还没定模型、又是第一轮的：先让模型判断这句话难不难（见 Catalog.pick），判断出错、超时当复杂。"""
        simple = None
        if not state.get("auto_model") and not state.get("turns") and self._plan(state, runtime).option == AUTO:
            human = state["messages"][history.last_human(state["messages"])]
            try:
                simple = await asyncio.wait_for(self.planner.route(history.text_of(human)), ROUTE_WAIT_S)
            except Exception as e:  # 点了停止（CancelledError）照常往外抛
                log.warning("Auto 判断难易失败，当复杂：%s: %s", type(e).__name__, e)
                simple = False
        return self._begin(state, runtime, simple)

    def _plan(self, state: ResearchState, runtime, simple: bool | None = None):
        """这一轮的模型。Auto 的对话按定下的 auto_model；还没定的这时定：simple 是第一句话判断的难易，没判断的（老对话、同步跑）当复杂。"""
        catalog, msgs = self.planner.catalog, state["messages"]
        thinking = history.thinking_switch(msgs[history.last_human(msgs)])
        now = self.planner.clock.now()
        pick = state.get("auto_model") or catalog.pick(bool(simple), _guest(runtime), self.planner.usable, now)
        return catalog.plan(state.get("option"), thinking, self.planner.usable, pick=pick, now=now)

    def _begin(self, state: ResearchState, runtime, simple: bool | None) -> dict[str, Any]:
        msgs = state["messages"]
        human = msgs[history.last_human(msgs)]
        # 按 planner 的时钟（评测回放时挪到录像那时，见 app.clock）；结束时间这些模型看不到的照常用系统时钟
        started = self.planner.clock.now().isoformat(timespec="seconds")
        plan = self._plan(state, runtime, simple)
        turn = Turn(id=human.id, started_at=started, option=plan.option, model=plan.agent, subagent_model=plan.subagent)
        out: dict[str, Any] = {"turns": list(state.get("turns", [])) + [turn]}
        if state.get("option") != plan.option:
            out["option"] = plan.option
        # Auto 用的模型记下来，之后每轮都用它：免费的到期了、用完了换成备用，也记成备用，不再换回来
        if plan.option == AUTO and state.get("auto_model") != plan.pick:
            out["auto_model"] = plan.pick  # 走免费额度以前的（直连的）：下一轮再看免费额度还能不能用
        emit({"type": "model", "ref": plan.agent.ref})  # 页面在回答开头显示这一轮用的模型（Auto 选到哪个，一轮结束才进记录）
        return out

    async def abefore_model(self, state: ResearchState, runtime) -> dict[str, Any] | None:
        """用户插进这一轮的话：这次调模型之前放进对话（排在已有的工具结果后面），页面同时收到一条 guide 把它放进过程里。
        子 agent 不取：它的话等主 agent 下一次调模型（子 agent 都做完以后）。"""
        msg = await runtime.context.take_guide() if runtime.context else None
        if msg is None:
            return None
        emit({"type": "guide", "id": msg.id, "text": history.text_of(msg), "sent_at": msg.additional_kwargs.get("sent_at", "")})
        return {"messages": [msg]}

    async def awrap_model_call(self, request: ModelRequest, handler):
        # 还没有标题就和这次调模型同时起，按这段对话的第一句；起好了随这次调模型的结果写进 state。
        # 停下的随停止写进去（stop_turn）；出错的写不了，上次起好的从 ChatContext.title 来，不再起
        job = None
        if not request.state.get("title"):
            known = request.runtime.context.title if request.runtime and request.runtime.context else None
            job = _TitleJob(self.planner, history.requests(request.state["messages"])[0], known=known)
        try:
            emit({"type": "phase", "phase": "retry" if _retrying(request.messages) else "model"})
            prompt = system_prompt(turn_started(request), memory=await memory_text(self.memory, request.runtime))
            # 之前几轮点停止时只想了没写的半截不发（见 history.is_stopped_thought）
            resp = await handler(request.override(system_message=SystemMessage(prompt), messages=history.without_stopped_thoughts(request.messages)))
            if job is None:
                return resp
            return ExtendedModelResponse(model_response=resp, command=Command(update={"title": await job.result(TITLE_WAIT)}))
        finally:
            if job is not None:
                job.cancel()  # 出错、点了停止时标题不要了（下一次调模型再起）；已经定下来的不受影响

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
                                                 "subagent_usage": history.subagent_usage(turn_msgs),
                                                 "outcome": "blocked" if blocked else "ok" if reply else "no_reply"})
        out["turns"] = turns
        return out


def _retrying(msgs: list) -> bool:
    """这次调模型是让它重发：上一条模型消息之后只有系统替它补的纠错结果（InvalidToolCallRetryMiddleware 写的，标了 hidden）。"""
    last = next((i for i in range(len(msgs) - 1, -1, -1) if isinstance(msgs[i], AIMessage)), None)
    tail = msgs[last + 1:] if last is not None else []
    return bool(tail) and all(isinstance(m, ToolMessage) for m in tail) and any(isinstance(m.artifact, dict) and m.artifact.get("hidden") for m in tail)


def stop_turn(values: dict, partial: AIMessage | None, subagents: dict[str, list[BaseMessage]] | None = None,
              title: str | None = None) -> dict[str, Any]:
    """用户点了停止，这一轮到此为止：没跑完的工具调用补上结果，已经流出来的思考和正文（partial）存成这一轮的回复，记下这一轮。
    只想了、正文还没写的也存：页面上停下前后看到的一样；它不发给模型（见 history.is_stopped_thought）。
    返回的更新以 TURN_END 的名义写进检查点。这一轮还没开始（排队时就停了）也要补一条记录。
    subagents 是还没跑完的子 agent 已经做了的（task 调用 id → 消息），存进补上的那条结果，页面上的子任务卡留着它做过的事；
    它已经查到了来源的，补上的结果里给主 agent 列出这些来源（来源卡上的内容），下一轮主 agent 用得上（2026-11-02 Sean 定）。

    values 是 get_state 拿到的：被掐断的那一步里已经做完的任务（比如并行的几个搜索里先回来的）的结果算在里面，
    但还没落进检查点，update_state 时会被丢掉。所以这一轮的消息原样再写一遍（同一个 id 覆盖，不会重复）。"""
    msgs = values["messages"]
    start = history.last_human(msgs)
    human, turn_msgs = msgs[start], msgs[start + 1:]
    fixes = history.dangling(turn_msgs, stopped=True)
    for m in fixes:
        if m.name == SUBAGENT_TOOL and (sub := (subagents or {}).get(m.tool_call_id)):
            m.artifact = {**(m.artifact or {}), "messages": messages_to_dict(sub)}
            if found := collect_sources(sub):
                m.content = prompts.render("tools.task.stopped", sources=found_sources_brief(found))
    new: list = turn_msgs + fixes
    if partial is not None and history.streamed_something(partial):
        new.append(partial)
    turns = list(values.get("turns", []))
    if not turns or turns[-1].id != human.id:
        turns.append(Turn(id=human.id, started_at=now()))
    turns[-1] = turns[-1].model_copy(update={"ended_at": now(), "suggestions": [], "usage": history.usage_of(new),
                                             "subagent_usage": history.subagent_usage(new), "outcome": "stopped"})
    out: dict[str, Any] = {"messages": new, "turns": turns}
    if title and not values.get("title"):  # 停下时标题已经起好、页面已经显示了：存下来，不然停下后跳回原话，下一轮又起一个不一样的
        out["title"] = title
    return out


EXCERPT_FROM = {"body": "page", "docs": "documentation", "summary": "search"}  # 来源卡上那段开头是从哪来的（给模型看的说法）


def found_sources_brief(found: list[Source]) -> str:
    """没交回报告的子 agent（用户点了停止、出错了）已经查到的来源，给主 agent 看的：只有来源卡上的内容（标题、网址、开头一段），读过的整页正文不给。"""
    return json.dumps([{"id": s.id, "title": s.title, "url": s.url, **({"published_at": s.published_at} if s.published_at else {}),
                        "excerpt_from": EXCERPT_FROM[s.excerpt_kind], "excerpt": s.excerpt} for s in found],
                      ensure_ascii=False)


def _result_message(result) -> ToolMessage | None:
    if isinstance(result, ToolMessage):
        return result
    if isinstance(result, Command) and isinstance(result.update, dict):
        return next((m for m in result.update.get("messages", []) if isinstance(m, ToolMessage)), None)
    return None


def _has_words(text: str) -> bool:
    """有没有字：汉字、字母都算（str.isalpha 认各种文字），数字、符号、表情不算。"""
    return any(c.isalpha() for c in text)


def _width(text: str) -> int:
    """占几格：汉字、全角符号两格，字母、数字、半角符号一格。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def usable_title(title: str) -> bool:
    """模型给的能不能当标题（批次 D，2026-11-07 Sean 定）：不空、一行、有字、不超过 TITLE_CHARS 个字。
    超长或者分了行，说明它没在起标题，而是去回答了（“抱歉，我无法提供……”这种拒答原句就是这样），不用；纯数字、纯符号（比如“2”）也不用。
    长短按格子算（30 个汉字、60 个字母）：英文标题照提示词的 15 个字符写不下，模型一般写四五个词，三四十个字母，不能当成没照做。
    只看长相，不看说的是什么：不建拒答语的关键词表。"""
    return bool(title) and "\n" not in title and _width(title) <= 2 * TITLE_CHARS and _has_words(title)


def fallback_title(text: str) -> str:
    """用用户原话当标题：空白并成一个空格，超过 TITLE_CHARS 截掉加省略号（照 Fusion）；原话里没有字的叫“新对话”。"""
    text = " ".join(text.split())
    if not _has_words(text):
        return NEW_CHAT
    return text[:TITLE_CHARS] + "..." if len(text) > TITLE_CHARS else text


class _TitleJob:
    """照 Fusion，起标题和回答同时跑，不让回答等它：起好了马上发给页面；这次调模型结束时写进 state，还没起好就再等一会儿。
    模型返回的只去掉首尾空白（引号、书名号是标题的一部分，不删）；不像标题的（见 usable_title）、出错、等不到用用户原话（见 fallback_title）。先定下来的算数，之后的不再发、不再改。
    另起一个任务调模型，用空的上下文：不带这次运行的回调，它的字不进这次运行的流（不然页面上会当成回答）。
    往流里写要在图的上下文里（写的时候按上下文认是哪个图），所以起头时复制一份上下文，发标题时在它里面写。
    记账要知道是哪段对话、哪一轮（见 app.costs.Scope），也是起头时取下来带过去。
    known 是上一次运行已经起好的（见 ChatContext.title）：直接定下来，不调模型。"""

    def __init__(self, planner, text: str, known: str | None = None) -> None:
        self._title: str | None = None
        self._text = text
        try:
            self._write = get_stream_writer()
        except RuntimeError:  # 不在流式运行里（例如测试直接 invoke）
            self._write = None
        self._ctx = contextvars.copy_context()
        self._scope = costs.SCOPE.get()
        self._task: asyncio.Task | None = None
        if known:
            self._settle(known)
        else:
            self._task = asyncio.get_running_loop().create_task(self._run(planner), context=contextvars.Context())

    async def _run(self, planner) -> None:
        try:
            with costs.within(self._scope):
                title = (await planner.title(self._text)).strip()
        except Exception as e:  # noqa: BLE001  起标题失败不影响回答，用原话
            log.warning("起标题失败，用原话：%s", e)
            title = ""
        if title and not usable_title(title):
            log.info("模型给的标题不像标题（%d 个字），用原话", len(title))  # 不记原文：里面是用户的话或者模型的回答
        self._settle(title if usable_title(title) else fallback_title(self._text))

    def _settle(self, title: str) -> None:
        if self._title is not None:  # 都在同一个事件循环里，不会同时进来
            return
        self._title = title
        if self._write is not None:  # 只有先定下来的那一次走到这里，复制的上下文不会同时进两次
            self._ctx.run(self._write, {"type": "title", "title": title})

    async def result(self, wait: float) -> str:
        task = self._task
        if task is not None and not (await asyncio.wait({task}, timeout=wait))[0]:
            log.warning("起标题超过 %s 秒，先用原话", wait)
            self._settle(fallback_title(self._text))
        return self._title or fallback_title(self._text)

    def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()


class ToolProgressMiddleware(AgentMiddleware):
    """工具开始时写一步（pending），做完再写一次带结果的。两次用同一个 id（工具调用 id），页面据此把它们和存下来的记录对上。
    做完的那一步带上它得到的来源（来源卡的内容）：一轮跑完才存检查点（见 research_runner.DURABILITY），
    执行中页面从库里取不到这一轮的来源，正文里的引用要靠这里给的才能显示成编号。
    还带上工具结果本身（result，runner 不发给页面）：点了停止时这一步可能还没提交，runner 靠它把页面上已经做完的留下。"""

    async def awrap_tool_call(self, request, handler):
        call = request.tool_call
        step = history.step_of(call, None)
        if step is None:
            return await handler(request)
        emit({"type": "step", "step": step.model_dump()})
        result = await handler(request)
        msg = _result_message(result)
        done = history.step_of(call, msg)
        if done is not None:
            emit({"type": "step", "step": done.model_dump(), "sources": _step_sources(request.state, msg, done.source_ids), "result": msg})
        return result


class ToolChargeMiddleware(AgentMiddleware):
    """工具调用也算钱（2026-11-06 Sean 定“都扣”）：做成了的按次记一行账（价格见 models.toml 的 [tools]），钱数写进工具结果的 response_metadata，
    这一轮的用量从消息里加、算进这一轮的 credits（和模型调用一样，见 history.usage_of、costs.credits）。
    没做成的（服务报错、点了停止、灵稿自己没交给服务的、没真执行的）不收；没定价的工具（task、recall、排行程）不记。"""

    async def awrap_tool_call(self, request, handler):
        name = request.tool_call["name"]
        if costs.tool_price(name) is None:
            return await handler(request)
        started = costs.now()
        result = await handler(request)
        msg = _result_message(result)
        step = history.step_of(request.tool_call, msg) if msg is not None else None
        if step is not None and step.ok and not step.pending and (yuan := costs.charge_tool(name, started)) is not None:
            msg.response_metadata = {**msg.response_metadata, costs.COST: yuan}
        return result


def _step_sources(state, result: ToolMessage | None, ids: list[str]) -> dict[str, dict]:
    """这一步得到的来源，按这条线程（子 agent 是它自己的那段）到这一步为止的全部结果算：读到正文的用正文开头，标题沿用搜索时的。"""
    if result is None or not ids:
        return {}
    by_id = {s.id: s for s in collect_sources([*(state or {}).get("messages", []), result])}
    return {sid: by_id[sid].model_dump() for sid in ids if sid in by_id}


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


MATERIAL_TOOLS = ("search_web", "read_page", DOCS_TOOL)


def _withdrawn(m: ToolMessage) -> ToolMessage:
    """整条结果去掉：搜索按搜索失败的样子（status=error），读页按读页失败的样子（ok=false），同一个 id 覆盖原来的。
    查文档按没做成的样子（status=error），带来源的那几段也去掉（页面上的来源从那里取，见 sources.doc_snippets）。"""
    note = prompts.render("agent.material_blocked")
    if m.name == DOCS_TOOL:
        art = {k: v for k, v in (m.artifact if isinstance(m.artifact, dict) else {}).items() if k != "snippets"}
        return m.model_copy(update={"content": note, "status": "error", "artifact": {**art, "error": {"code": "content_blocked"}}})
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
    - 新加进来的是用户的话（包括这一轮里插进来的），或者去掉资料后还是拦：这一轮停下；
    - 模型的输出被截断：这一轮停下，已经流出去的思考和正文不留。
    去掉的结果写回消息（同一个 id 覆盖），之后的调用不会再带上。停下的那一轮整轮不再带进之后的上下文，
    不然以后每次请求都会被拦。停下时补一条不带工具调用的模型消息，agent 就此结束这一轮，页面据它显示固定的说明。
    """

    def __init__(self, planner) -> None:
        super().__init__()
        self.planner = planner

    async def awrap_model_call(self, request: ModelRequest, handler):
        request = request.override(messages=history.without_blocked_turns(request.messages))
        try:
            return await handler(request)
        except ContentBlocked as e:
            if e.output:
                return _stop("output")
            return await self._recover(request, handler)

    async def _recover(self, request: ModelRequest, handler):
        msgs = request.messages
        start = next((i + 1 for i in range(len(msgs) - 1, -1, -1) if isinstance(msgs[i], AIMessage)), 0)
        tail = [m for m in msgs[start:] if isinstance(m, ToolMessage)]
        material = [m for m in tail if m.name in MATERIAL_TOOLS and m.status != "error" and m.id]
        # 停下时的原因：上一条模型消息之后只有用户的话（这一轮的第一句）是 input，有工具结果是 material；
        # 用户在这一轮里插了话的，去掉资料还拦就算 input
        guided = any(history.is_guide(m) for m in msgs[start:])
        reason = "material" if tail and not guided else "input"
        if not material:
            return _stop(reason)
        emit({"type": "phase", "phase": "guard"})
        whole = {m.id: _withdrawn(m) for m in material}
        found = await self._drop_blocked(material)
        tries = [found] if found else []
        if any(k not in found or found[k].content != w.content for k, w in whole.items()):  # 单条去掉的就是整批时不重复试
            tries.append(whole)
        for repl in tries:
            try:
                resp = await handler(request.override(messages=[repl.get(m.id, m) for m in msgs]))
            except ContentBlocked as e:
                if e.output:
                    return _stop("output")
                continue
            return ExtendedModelResponse(model_response=resp, command=Command(update={"messages": list(repl.values())}))
        return _stop(reason)

    async def _drop_blocked(self, material: list[ToolMessage]) -> dict[str, ToolMessage]:
        """单条送审，返回 {消息 id: 去掉没通过内容后的结果}；一条都没拦下是空的。"""
        pieces: list[tuple[int, int | None, str]] = []  # (第几条结果, 搜索结果里的第几条（None 是整条）, 送审的文字)
        for i, m in enumerate(material):
            hits = search_hits(parse(m)) if m.name == "search_web" else None
            if hits is not None:
                pieces += [(i, k, json.dumps(h, ensure_ascii=False)) for k, h in enumerate(hits)]
            else:
                pieces.append((i, None, history.text_of(m)))
        bad: dict[int, set[int | None]] = {}
        for (i, k, _), hit in zip(pieces, await self.planner.blocked([t for _, _, t in pieces]), strict=True):
            if hit:
                bad.setdefault(i, set()).add(k)
        out: dict[str, ToolMessage] = {}
        for i, ks in bad.items():
            m = material[i]
            data = parse(m)
            hits = search_hits(data) if m.name == "search_web" else None
            kept = [h for k, h in enumerate(hits) if k not in ks] if None not in ks and hits is not None else []
            if not kept:
                out[m.id] = _withdrawn(m)
                continue
            art = {**(m.artifact if isinstance(m.artifact, dict) else {}), "blocked": len(ks)}
            body = kept if isinstance(data, list) else {**data, "results": kept}  # 11-05 以前存下的是一串条目
            out[m.id] = m.model_copy(update={"content": json.dumps(body, ensure_ascii=False, separators=SEPARATORS), "artifact": art})
        return out


class ModelCallCapMiddleware(AgentMiddleware):
    """一轮里调模型到了上限（MAX_MODEL_CALLS）：最后一次不许调工具（tool_choice=none），请求末尾加一条说明（不存进记录），
    让模型用已经查到的回答。页面上不另外说（2026-11-03 Sean 定）。做法照子 agent（见 subagent.SubagentPrompt）。
    - 没有这个时主 agent 只靠 LangGraph 的 recursion_limit（默认 10007 步，要调两千来次模型才停），停下时这一轮失败、没有回答。
    - 没用 LangChain 的 ModelCallLimitMiddleware：到了上限它塞一句英文就结束，模型拿不到最后一次；
      run_limit 出错重试时从 0 重新数，thread_limit 按整段对话算，到了以后每一轮都直接结束。
    - 次数按 state 里这一轮的模型消息数：出错重试接着数；用户插进这一轮的话不开新的一轮，也不重新数。
    - 排在最里面：末尾那条说明是用户消息，压缩（CompactionMiddleware）按用户消息分轮，排在它外面会把这一轮当成之前的压掉。"""

    async def awrap_model_call(self, request: ModelRequest, handler):
        msgs = request.state["messages"]
        calls = sum(isinstance(m, AIMessage) for m in msgs[history.last_human(msgs) + 1:])
        if calls >= MAX_MODEL_CALLS - 1:
            last = HumanMessage(prompts.render("agent.last_call"))
            request = request.override(messages=[*request.messages, last], tool_choice="none")
        return await handler(request)


class InterruptedRetryMiddleware(AgentMiddleware):
    """模型生成到一半断了（model_guard.Interrupted：流读到一半断开、流里来了错误、DeepSeek 资源不足、太久没片段，SDK 都不重试）：
    等一会儿把这次调用整个再发一次，只再发一次；还不行就照常失败，页面给原因和重试（2026-11-03 Sean 定做）。
    - 再发之前往自定义流里写一条 restart：运行器让页面去掉刚才流出来的半截，说明在重新生成；点停止时也不存那半截（见 api.research_runner）。
    - 没用 LangChain 的 ModelRetryMiddleware：它分不出这几种和发请求时的错误（那些 SDK 已经重试过，再叠一层请求数是乘出来的），
      也没法告诉页面去掉半截（见 docs/LangChain研究/01-出错时的处理.md）。
    - 熔断照常记：这次失败算一次，再发也失败就是连续两次，断开；和用户点一次重试又失败一样。这次失败已经让熔断断开的不是 Interrupted，不再发。
    - 排在最里面，只重发调模型这一下：系统提示词、压缩、标题都不重来。主 agent、子 agent 都挂。"""

    async def awrap_model_call(self, request: ModelRequest, handler):
        try:
            return await handler(request)
        except Interrupted as e:
            log.warning("模型生成到一半断了，%s 秒后再调一次：%s", RESTART_WAIT_S, e)
            emit({"type": "restart", "streamed": e.streamed})
            await asyncio.sleep(RESTART_WAIT_S)
            return await handler(request)


class ModelSelect(AgentMiddleware):
    """每次调模型换上这一轮定下的模型：主 agent 的在这一轮的记录里（TurnMiddleware 定的），子 agent 的在它的 state 里（派它时给的）。
    create_agent 在调模型时才绑工具，换了模型工具照样带上。挂在最里面（InterruptedRetryMiddleware 外面），看到的是压缩、拼好以后要发的那份。
    之前几轮是别的模型答的（Auto 里换了模型、选的模型停了改用 Auto），发过去时不带那几轮的思考：各家的思考格式不一样，
    豆包的加密原文换了模型也验不过；同一个模型的照常带上，前缀缓存接得上。"""

    def __init__(self, planner, *, sub: bool = False) -> None:
        super().__init__()
        self.planner, self.sub = planner, sub

    async def awrap_model_call(self, request: ModelRequest, handler):
        if self.sub:
            use = request.state.get("model") or self.planner.catalog.plan(None, None, self.planner.usable).subagent
            return await handler(request.override(model=self.planner.chat_model(use, sub=True)))
        turns = request.state.get("turns", [])
        use = (turns[-1].model if turns else None) or LEGACY_USE  # 2026-11-05 之前开始、之后重试的那一轮
        msgs = _others_without_thinking(request.messages, turns, use)
        update: dict[str, Any] | None = None
        while True:  # 出问题换备用，备用再出问题接着换（免费额度 → 直连 → Flash），换到头了照常失败
            try:
                resp = await handler(request.override(model=self.planner.chat_model(use), messages=msgs))
                break
            except ModelUnavailable as e:
                if not turns or (switch := self._backup(request, turns, use, e)) is None:
                    raise
                backup, update = switch
                log.warning("模型出问题，这次换备用 %s：%s: %s", backup.ref, use.ref, e)
                if e.streamed:  # 页面去掉断掉的半截，说明在重新生成；没流出东西的接着说在等模型
                    emit({"type": "restart", "streamed": True})
                emit({"type": "model", "ref": backup.ref})
                use, turns = backup, update["turns"]
                msgs = _others_without_thinking(request.messages, turns, use)
        return resp if update is None else ExtendedModelResponse(model_response=resp, command=Command(update=update))

    def _backup(self, request: ModelRequest, turns: list[Turn], use: ModelUse, e: ModelUnavailable) -> tuple[ModelUse, dict] | None:
        """换成什么（2026-11-08 Sean 定）：返回换上的模型和要写进 state 的（这一轮记录里的模型换成它，后面几次调模型都用它；
        Auto 的对话以后都用它）。换到头了返回 None，照常失败。
        - 走的是某个模型的免费额度（[[free]] 的 same_as，用户选的、Auto 判为复杂的）：换直连的同一个模型，只有这一轮换。
        - 别的换备用（models.toml 的 [auto] fallback，Flash）；已经是备用的不换。用户自己选的模型只有这一轮换，下一轮还是他选的。
        这一轮前面已经有别的模型发的工具调用、又没带思考的（百炼上的 DeepSeek 带工具时不给思考），这一轮剩下的不开思考：
        DeepSeek 开思考时这一轮的工具调用要带着思考，不带报 400（别家的思考它收，2026-11-08 实测）。"""
        catalog, usable = self.planner.catalog, self.planner.usable
        if isinstance(e, FreeTierUsedUp):
            catalog.used_up(use.ref)
        turn = turns[-1]
        if direct := catalog.same_as(use.ref):
            plan = catalog.plan(turn.option, use.thinking, usable, pick=direct, free=False)
        else:
            plan = catalog.plan(catalog.auto.fallback, use.thinking, usable)
        if plan.agent.ref == use.ref:
            return None
        backup = plan.agent
        if backup.thinking and _bare_tool_calls(request.messages, turn.id):
            backup = backup.model_copy(update={"thinking": False, "effort": ""})
        sub = turn.subagent_model if turn.option != AUTO else plan.subagent
        update: dict[str, Any] = {"turns": [*turns[:-1], turn.model_copy(update={"model": backup, "subagent_model": sub})]}
        if turn.option == AUTO:
            update["auto_model"] = plan.pick
        return backup, update


def _guest(runtime) -> bool:
    ctx = getattr(runtime, "context", None)
    return bool(ctx and is_guest_sub(ctx.owner))


def _bare_tool_calls(messages: list[BaseMessage], turn_id: str) -> bool:
    """这一轮（从 turn_id 那条用户消息往后）有没有不带思考的工具调用。找不到那条消息就看全部。"""
    start = next((k for k, m in enumerate(messages) if m.id == turn_id), -1)
    return any(isinstance(m, AIMessage) and m.tool_calls and not m.additional_kwargs.get(history.REASONING) for m in messages[start + 1:])


def _others_without_thinking(messages: list[BaseMessage], turns: list[Turn], use: ModelUse) -> list[BaseMessage]:
    """别的模型答的那几轮去掉思考，这一轮和同一个模型答的原样。按每轮的记录认是哪个模型答的，没记的是 LEGACY_MODEL。"""
    refs = {t.id: t.model.ref if t.model else LEGACY_MODEL for t in turns}
    out: list[BaseMessage] = []
    ref = None
    for m in messages:
        if history.is_request(m):
            ref = refs.get(m.id, LEGACY_MODEL)
        out.append(history.without_thinking(m) if ref not in (None, use.ref) else m)
    return out


def _stop(reason: str) -> AIMessage:
    return AIMessage(content="", response_metadata={history.BLOCKED: reason})


async def memory_text(memory: MemoryBook | None, runtime) -> str:
    """这段对话归的那个人的记忆（归谁从 runtime.context 来，见 ChatContext.owner）。每次调主 agent 都现取：记忆只在每天整理时变，取到的一天里都一样。
    游客的、没归谁的没有记忆（游客的对话不整理，见 ResearchRunner.dream）。"""
    owner = runtime.context.owner if runtime and runtime.context else None
    if memory is None or owner is None or is_guest_sub(owner):
        return ""
    return await memory.aprompt_text(owner)


def build_agent(planner, checkpointer: BaseCheckpointSaver, memory: MemoryBook | None = None):
    # subagent、compaction 用到本模块的东西，放在这里导入（weather_mcp、web_mcp、maps_mcp、flyai_mcp、docs_mcp 用到 prompts，一起放这里）
    from app.graph.research.compaction import CompactionMiddleware, make_recall_tool
    from app.graph.research.subagent import make_task_tool
    from app.tools.docs_mcp import DocsMiddleware
    from app.tools.flyai_mcp import TravelMiddleware, itinerary_tool
    from app.tools.image_mcp import ImageMiddleware
    from app.tools.maps_mcp import MapsMiddleware
    from app.tools.weather_mcp import WeatherMiddleware
    from app.tools.web_mcp import WebToolsMiddleware

    # recall 一直都在（工具列表变了前缀缓存整个断），只有压缩换成说明的内容才用得上；页面上不显示这一步。
    # 排行程（present_itinerary）也一直在，查过航班火车才用得上
    tools = [*planner.tools(), make_task_tool(planner), make_recall_tool(), itinerary_tool()]
    return create_agent(
        model=planner.chat_model(planner.catalog.plan(None, None, planner.usable).agent),  # 只是占位：每次调模型由 ModelSelect 换上这一轮的
        tools=tools,
        state_schema=ResearchState,
        context_schema=ChatContext,
        middleware=[
            TurnMiddleware(planner, memory),
            ContentGuard(planner),  # 在 TurnMiddleware 里面：重发的请求也带着换好的系统提示词
            CompactionMiddleware(planner, tools, memory),  # 在 ContentGuard 里面：被内容审核停下的那几轮先去掉再压；用户插的话放进对话以后再估
            ToolChargeMiddleware(),  # 做成了的工具调用记账；看的结果和 ToolProgressMiddleware 一样是整理好的
            ToolProgressMiddleware(),
            WeatherMiddleware(),  # 在 ToolProgressMiddleware 里面：过程记录看到的是整理好的天气结果
            TravelMiddleware(),  # 同上：航班火车的结果、每轮的次数、排行程；在 MapsMiddleware 外面：车站城市不对的比路线不交给地图
            MapsMiddleware(),  # 同上：查地点、比路线的结果（地点卡、路线卡要的在 artifact 里）
            WebToolsMiddleware(),  # 同上：搜索、读页的结果带上来源 id
            DocsMiddleware(),  # 同上：查文档的每段带上来源 id（带来源的那几段在 artifact 里）
            ImageMiddleware(),  # 同上：画图每轮的上限、改图只认这段对话的图、卡片要的东西
            InvalidToolCallRetryMiddleware(INVALID_TOOL_CALL_RETRIES),
            ModelCallCapMiddleware(),  # 在压缩里面：末尾加的说明不经过压缩
            ModelSelect(planner),
            InterruptedRetryMiddleware(),  # 最里面：只重发调模型这一下
        ],
        checkpointer=checkpointer,
        store=memory.store if memory else None,
        name="research",
    )
