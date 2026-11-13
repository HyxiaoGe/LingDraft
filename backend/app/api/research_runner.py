"""对话场景的接口层运行器：接用户的话、把 agent 的流变成页面的事件、停止和重新生成、线程状态视图。

回答进行中用户又说的话不打断这一轮：排着（存进 run_store 的 pending_messages），这一轮答完或停下就接着发成新的一轮（见 queue、_aended）。
没发出去的话最多一句。排着的话也可以插进正在跑的这一轮（见 guide）：主 agent 下一次调模型之前放进对话；这一轮没再调模型就结束了的，退回去排着。
执行在对话的事件循环里异步地跑（见 app.aio），点停止马上取消这一轮（见 ResearchRunner.stop）。
模型做到一半可以停下来问用户（ask_user 工具里的 interrupt，见 app.tools.ask_user）：这次执行结束（ASKED），线程的状态是 asking，一直等；
用户回答或跳过（见 answer）另起一次执行，同一轮从停下的地方接着跑。等的时候不能往检查点里写东西（写一次，等着的问题就丢了）：
接着说、改名都先拒掉，点停止、重新回答是把这一轮以停止收尾。
定时任务（见 app.scheduled）到点了在任务挂着的那段对话里多跑一轮：没有请求进来，运行器自己发起（见“定时任务”一节）。
"""

from __future__ import annotations

import ast
import asyncio
import contextvars
import time
import traceback
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage, RemoveMessage, ToolMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import START
from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore
from langgraph.store.postgres.aio import AsyncPostgresStore
from langgraph.types import Command
from psycopg_pool import PoolTimeout
from pydantic import ValidationError

from app import aio, config, costs, free_used_up, quota
from app.api import export
from app.api.drafts import DraftTracker
from app.api.export import Exporter, ExportFailed
from app.api.run_store import ASKED, ERROR, GUIDE, QUEUE, STOPPED, SUCCESS, MemoryRunStore, Pending, PostgresRunStore, RunStore
from app.api.runner import BaseRunner, RunConflict, _sse_text, jsonable, log
from app.files.parse import PPTX, Part
from app.files.service import NOT_HERE, FileService, NotSendable
from app.files.artifacts import MARKDOWN
from app.files.store import GENERATED, FileRow, FileStore, MemoryFileStore, PostgresFileStore
from app.knowledge import remote as kb_remote
from app.knowledge.remote import HashEmbedder
from app.knowledge.libraries import Library, LibraryStore, MemoryLibraryStore, PostgresLibraryStore
from app.knowledge.service import KnowledgeBase
from app.knowledge.store import ChunkStore, MemoryChunkStore, PostgresChunkStore
from app.graph.research import history
from app.graph.research.agent import TURN_END, ChatContext, fallback_title, now, stop_turn
from app.graph.research.graph import (
    NO_POSTGRES,
    build_graph,
    keep_latest,
    latest_checkpoints,
    make_serde,
    postgres_pool,
    stale_threads,
    thread_ids,
)
from app.graph.research.memory import MemoryBook, next_at
from app.identity import Identity, is_guest_sub
from app.graph.research.sources import collect_sources, export_markdown, subagent_messages
from app.graph.research.state import AttachedFile, AttachedImage, Turn
from app.research_llm import THINKING_MS, FakeResearchPlanner, LiveResearchPlanner, ResearchPlanner, ThinkingClock
from app.scheduled import rule
from app.scheduled import service as scheduled
from app.scheduled.notice import opening
from app.scheduled.service import ScheduledTasks
from app.scheduled.store import MemoryTaskStore, PostgresTaskStore, Task, TaskStore
from app.model_guard import ContentBlocked, Interrupted, ModelUnavailable
from app.tools.ask_user import ASK, MAX_ANSWER, AskAnswer
from app.tools.schedule import when as say_when
from app.tools.location import DECLINED, PROVIDED, SAID, UNAVAILABLE, Device, PlaceAnswer, Places, holder
from app.tools.gateway import IMAGE_ID, SANDBOX_MAX_UPLOAD_BYTES, GatewayError
from app.tools.run_command import Sandbox


class NothingToRetry(Exception):
    """线程没有失败，不需要重试。"""


class NotIdle(Exception):
    """上一轮没跑完：这时不能接着说，也不能重新生成。停在问用户那里也算（先回答或跳过）。"""


class NothingToAnswer(Exception):
    """没有在等回答的问题，或者要回答的不是现在等着的那个（已经答过、这一轮已经停了）。"""


class BadAnswer(Exception):
    """回答不合格：空的、太长。"""


class AlreadyPending(Exception):
    """回答时又说的话已经有一句没发出去（排着或等着插进这一轮）：没发出去的话最多一句，先撤回改好再发。"""


class NothingToRegenerate(Exception):
    """还没有一轮回答。"""


class UnknownModel(Exception):
    """新对话选的模型不在能选的里面（没有这个、停用了、没配 key），或者想给已经开始的对话换模型。"""


CUT_SHORT = "上次运行没跑完就停了"
ASKING = "asking"  # 线程的状态：这一轮停下来问用户了，等回答（见 _status）
ASKING_FIRST = "先回答或跳过上面的问题"
# 这一轮没跑完的原因分几类给页面（状态里的 error_kind，事件流 error 事件的 kind），页面按类别说下一步能做什么（见 _failure）：
# model：模型服务不可用；blocked：内容审核没通过；cut_short：半路停了、没有错误记录（进程重启）；tool：工具服务（网关）出错；internal：别的
FAILURE_MODEL, FAILURE_BLOCKED, FAILURE_CUT, FAILURE_TOOL, FAILURE_INTERNAL = "model", "blocked", "cut_short", "tool", "internal"
NO_UNREAD = {"unread": 0, "notice": None}  # 线程列表里一行没有未读时的那两项（见 _asummary_view）
# 线程列表里一行从检查点算出来的那几项（见 _summary_of）记在库里时带的版本：哪一项的算法改了、多了一项，就加一，记着的全部作废、重算
SUMMARY_V = 2
_SHOWN = {ModelUnavailable.__name__: FAILURE_MODEL, Interrupted.__name__: FAILURE_MODEL, ContentBlocked.__name__: FAILURE_BLOCKED}  # 提示原样给的
FAILURE_TEXT = {FAILURE_TOOL: "查资料、地图或画图用的服务出了问题", FAILURE_INTERNAL: "处理这一轮时出了意外的错误"}
# 检查点只在一次运行开头（用户的话写进去，见 accept）和结束时各存一份，中间每一步不存：每份都带着整段对话，每步都存的话按平方增长。
# 出错、点停止也算结束，结束前把做到哪儿存下来，重试从断点接着跑；进程直接崩掉或被杀，这一轮做到一半的没存，重试从头跑这一轮。
# 执行中页面要的东西（来源、标题、进度）都跟着事件走，不从库里取。
DURABILITY = "exit"
# MCP 的工具（现在只有天气）：起服务时连不上就每隔几秒再试，到这么久还不行先不带它起来，之后在后台每分钟试一次
TOOLS_STARTUP_S = 30
TOOLS_STARTUP_RETRY_S = 5
TOOLS_RETRY_S = 60
SETTLE_S = 30  # 点停止以后，等这一轮在事件循环里起的任务（比如收尾存检查点）都结束，最多等这么久
_TURN: contextvars.ContextVar[object | None] = contextvars.ContextVar("research_turn", default=None)  # 这一轮起的任务上下文里都带着它
DELTA_S = 0.08  # 逐字片段攒这么久发一次
SENT_AT = "sent_at"  # 用户那句话的发送时间，记在消息的 additional_kwargs 里（不会发给模型）
SCHEDULED = "scheduled"  # 运行记录的 kind：定时任务到点发起的（用户点“立即执行”的也是）
# 定时任务这一次没交出去时 _afire 说的原因（交出去了是 scheduled.RUNNING）：那段对话正在跑；任务刚被删了、停了、改了
BUSY, GONE = "busy", "gone"


def _said(text: str, thinking: bool | None = None, images: Sequence[AttachedImage] = (), files: Sequence[AttachedFile] = (),
          work: bool | None = None, scheduled_by: dict | None = None, **kw: Any) -> HumanMessage:
    """用户的话。thinking 是发的时候输入框旁边的思考开关（没给按选项的默认，见 model_catalog.Catalog.plan）；images 是这句话带的图，files 是带的文件；
    work 是这句话是不是 Work（按这段对话是不是工作任务写上，没给算 Chat，见 history.WORK）。
    scheduled_by：这句话是定时任务到点触发的（{"task_id", "title"}，见 history.SCHEDULED），text 是任务里那份交代。"""
    extra: dict[str, Any] = {history.THINK: thinking} if thinking is not None else {}
    if work is not None:
        extra[history.WORK] = work
    if scheduled_by:
        extra[history.SCHEDULED] = scheduled_by
    if images:
        extra[history.ATTACHED] = [i.model_dump() for i in images]
    if files:
        extra[history.FILES] = [f.model_dump() for f in files]
    return HumanMessage(content=text.strip(), additional_kwargs={SENT_AT: now(), **extra}, **kw)


def _guided(text: str) -> HumanMessage:
    """插进这一轮的话：发送时间记成放进对话的时候。id 先定下来，页面实时收到的和存下来的对得上。"""
    return HumanMessage(content=text, id=str(uuid.uuid4()), additional_kwargs={SENT_AT: now(), history.GUIDE: True})


class _Streamed:
    """一条消息到现在流出来的全部思考和正文，以及思考了多久。"""

    def __init__(self, mid: str) -> None:
        self.mid, self.text, self.thinking, self.clock = mid, "", "", ThinkingClock()

    def message(self) -> AIMessage:
        meta: dict[str, Any] = {history.STOPPED: True}
        if (ms := self.clock.ms()) is not None:
            meta[THINKING_MS] = ms
        return AIMessage(id=self.mid, content=self.text, additional_kwargs={history.REASONING: self.thinking} if self.thinking else {},
                         response_metadata=meta)


class StreamBuffer:
    """把模型的流式片段攒成事件：思考和正文按（子任务，消息 id，种类）合并，约每 80ms 发一次。子 agent 的片段带上派它的 task 调用 id。
    一条消息想完了（下一个片段是正文或工具调用），思考那一段带上 ms（思考了多久，后端计的）：页面按收到片段的时间自己算不准，
    中途打开对话时前面的事件一下子补发过来、标签页在后台时浏览器拖着不处理，都会算短。
    同时记着主 agent 和每个子 agent 最后一条消息流出来的东西：停止时存下来（主 agent 的是这一轮的回复，子 agent 的进它的子任务卡）。
    主 agent 写文件、跑命令的工具调用，参数边收边解出来，同一批发给页面（draft、writing，见 api.drafts）；子 agent 没有这两种工具。"""

    def __init__(self) -> None:
        self._deltas: list[dict] = []
        self._last = time.monotonic()
        self._cur: dict[str | None, _Streamed] = {}  # None 是主 agent
        self._drafts = DraftTracker()

    def add(self, chunk: AIMessageChunk, task: str | None = None) -> list[dict]:
        mid = chunk.id or ""
        cur = self._cur.get(task)
        if cur is None or cur.mid != mid:
            cur = self._cur[task] = _Streamed(mid)
        thought = cur.clock.see(chunk)
        thinking = history.thinking_of(chunk)
        text = history.text_of(chunk)
        cur.thinking += thinking
        cur.text += text
        if thought:  # 思考到这里为止：用时记在思考那一段上发给页面，排在正文前面
            self._piece(mid, task, "thinking", "")["ms"] = cur.clock.ms()
        for kind, piece in (("thinking", thinking), ("text", text)):
            if piece:
                self._piece(mid, task, kind, piece)
        if task is None and chunk.tool_call_chunks:
            self._drafts.add(mid, chunk.tool_call_chunks)
        return self.flush() if time.monotonic() - self._last >= DELTA_S else []

    def _piece(self, mid: str, task: str | None, kind: str, piece: str) -> dict:
        """接上一段片段，返回它所在的那条。并行的几个子 agent 的片段交错着来：接在同一条消息最近的那一段后面，种类变了才另起一段。"""
        prev = next((d for d in reversed(self._deltas) if d["id"] == mid and d.get("task") == task), None)
        if prev is not None and prev["kind"] == kind:
            prev["text"] += piece
            return prev
        self._deltas.append(d := {"id": mid, "kind": kind, "text": piece, **({"task": task} if task else {})})
        return d

    def flush(self) -> list[dict]:
        out = [{"event": "delta", "data": d} for d in self._deltas] + [{"event": ev, "data": d} for ev, d in self._drafts.take()]
        self._deltas, self._last = [], time.monotonic()
        return out

    def drop(self, task: str | None = None) -> str | None:
        """这条消息不要了（调模型断了，正在再调一次）：不再当作流出来的半截，点停止时不存它。返回它的 id（页面去掉它的片段和写到一半的文件）。"""
        cur = self._cur.pop(task, None)
        if cur is not None and task is None:
            self._drafts.drop(cur.mid)
        return cur.mid if cur is not None else None

    def partial(self, task: str | None = None) -> AIMessage | None:
        cur = self._cur.get(task)
        return cur.message() if cur is not None and cur.mid else None


def conversation(messages: list[BaseMessage], turns: list[Turn]) -> list[dict]:
    """对话按轮：用户的话（定时任务触发的是任务里那份交代，带着 scheduled），然后这一轮里模型的思考、正文、工具步骤按先后排，被内容审核拦下时最后是一条 blocked；
    用户插进这一轮的话是一条 guide，排在它放进对话的地方；模型问用户的是一条 ask（问题、选项、回答，见 history.ask_of）。
    每轮带这一轮的记录（推荐问题、用量、结果）。子任务那一步带着它的子 agent 的条目（items），排法一样。"""
    by_id = {t.id: t for t in turns}
    results = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)}
    out: list[dict] = []
    cur: dict | None = None
    for m in messages:
        if history.is_guide(m):
            if cur is not None:
                cur["items"].append({"type": "guide", "id": m.id, "text": history.text_of(m), "sent_at": m.additional_kwargs.get(SENT_AT, "")})
        elif isinstance(m, HumanMessage):
            t = by_id.get(m.id)
            cur = {"id": m.id, "user": history.text_of(m), "sent_at": m.additional_kwargs.get(SENT_AT, ""), "items": [],
                   # 定时任务触发的一轮（{"task_id", "title"}，见 history.SCHEDULED）：页面不显示成用户说的话，只写一行由哪个任务触发
                   "scheduled": history.scheduled_of(m),
                   "images": [i.model_dump() for i in history.attached_images(m)],
                   "files": [f.model_dump() for f in history.attached_files(m)],
                   "record": {**t.model_dump(exclude={"id"}), "credits": costs.credits(t)} if t else None}
            out.append(cur)
        elif cur is not None and isinstance(m, AIMessage):
            cur["items"] += _items_of(m, results)
    return out


def _items_of(m: AIMessage, results: dict[str, ToolMessage]) -> list[dict]:
    """一条模型消息 → 条目：思考、正文、工具步骤、问用户的问题；被内容审核拦下的是一条 blocked。"""
    if reason := history.blocked_reason(m):
        return [{"type": "blocked", "id": m.id, "reason": reason}]
    items: list[dict] = []
    if thinking := history.thinking_of(m):
        items.append({"type": "thinking", "id": m.id, "text": thinking, "ms": m.response_metadata.get(THINKING_MS)})
    if text := history.text_of(m).strip():
        items.append({"type": "text", "id": m.id, "text": text})
    for tc in m.tool_calls:
        result = results.get(tc["id"])
        if ask := history.ask_of(tc, result):
            items.append(ask)
        elif step := history.step_of(tc, result):
            if said := history.where_of(tc, result):  # 要位置时用户直接说了个地方：排在没查成的那一步前面
                items.append(said)
            item = {"type": "step", **step.model_dump()}
            if step.kind == "task":
                item["items"] = subagent_items(result)
            items.append(item)
    items += [{"type": "step", **step.model_dump()} for tc in m.invalid_tool_calls if (step := history.broken_write(tc))]  # type: ignore[arg-type]
    return items


def subagent_items(result: ToolMessage | None) -> list[dict]:
    """子任务卡里的条目：它的子 agent 每次调模型的思考、正文（最后一段是报告）、搜索和读页。还没有结果时是空的，页面用实时的。"""
    sub = subagent_messages(result) if result is not None else []
    results = {x.tool_call_id: x for x in sub if isinstance(x, ToolMessage)}
    return [i for x in sub if isinstance(x, AIMessage) for i in _items_of(x, results)]


def _error_event(e: Exception) -> dict:
    """事件流里的 error 事件：和状态里的 error / error_kind 一样分类、转写（见 _failure）。"""
    kind, message = _failure(type(e).__name__, str(e))
    return {"message": message, "kind": kind}


def _public(run: dict) -> dict:
    return {k: v for k, v in run.items() if k != "traceback"}


def _parse_error(err: str) -> tuple[str, str | None]:
    """检查点里存的是异常的 repr（ModelUnavailable('…')）：拆成（类名，里面那句话）。解不开的（不止一个参数、不是这个样子）那句话是 None。"""
    name, sep, rest = err.partition("(")
    if sep and name.isidentifier() and rest.endswith(")"):
        try:
            msg = ast.literal_eval("(" + rest)
        except (ValueError, SyntaxError):
            return name, None
        if isinstance(msg, str):
            return name, msg
    return (name if sep and name.isidentifier() else ""), None


def _subclass_names(cls: type) -> set[str]:
    return {cls.__name__, *(n for c in cls.__subclasses__() for n in _subclass_names(c))}


def _failure(name: str, message: str | None) -> tuple[str, str]:
    """一个异常（类名、那句话）给页面看的（类别，说法）。只有我们写给用户看的提示原样给：模型服务不可用（带原因、多久以后再试）、内容审核没通过；
    别的异常原文不给页面（库的英文、网关的 HTTP 状态码、内部细节），换成这一类的通用说法。原文照旧在日志和运行记录里（见 run_store 的 error）"""
    kind = _SHOWN.get(name)
    if kind and message:
        return kind, message
    kind = FAILURE_TOOL if name in _subclass_names(GatewayError) else FAILURE_INTERNAL
    return kind, FAILURE_TEXT[kind]


def _failure_of(errors: list[str]) -> tuple[str, str]:
    """这一轮没跑完的（类别，说法）。errors 是检查点里各步的错误（见 _parse_error）。几个工具同时跑、一个抛了错，LangGraph 会取消其余还在跑的，
    被取消的也可能记一条 CancelledError：看真正出错的那条。只有被取消的（进程退出时整轮被掐断），和没有错误记录一样是半路停了。"""
    real = _real_error(errors)
    return _failure(*_parse_error(real)) if real else (FAILURE_CUT, CUT_SHORT)


def _errors(snap) -> list[str]:
    """检查点上这一步里没做成的任务记的错误。一步没走完，它的记录就不清：出过错、重试时做成了的任务，那次的错误还留着
    （见 langgraph.pregel._task_status）。做成了的有结果（result 不是 None），LangGraph 不会再跑它，它留着的错误不算。"""
    return [str(t.error) for t in snap.tasks if t.error and t.result is None]


def _real_error(errors: list[str]) -> str | None:
    return next((e for e in errors if e.partition("(")[0] != asyncio.CancelledError.__name__), None)


def _is_ask(value: Any) -> bool:
    """中断带的东西是不是在问用户（见 app.tools.ask_user）。别的中断（比如 MCP 的 elicitation）不认。"""
    return isinstance(value, dict) and value.get("type") == ASK


def _asks(snap) -> list[dict]:
    """检查点上还等着用户回答的问题，按模型发出的先后：{id, call_id, question, options}。id 是中断的 id，回答时带上（见 _aanswer_input）；
    call_id 是那次工具调用，对得上这一轮条目里的那条 ask。一条消息里问了两个、已经答了一个的：答过的那个有结果了，不算。
    要用户位置的（见 app.tools.location）也在里面：多两项 kind（location）、purpose（为了什么），没有问题和选项，call_id 是要位置的那次工具调用。"""
    msgs = (snap.values or {}).get("messages", [])
    answered = {m.tool_call_id for m in msgs if isinstance(m, ToolMessage)}
    order = [tc.get("id") for m in msgs if isinstance(m, AIMessage) for tc in m.tool_calls]
    out: dict[str, dict] = {}
    for it in getattr(snap, "interrupts", None) or ():
        v = it.value
        if _is_ask(v) and v.get("call_id") not in answered:
            where = {"kind": history.LOCATION, "purpose": str(v.get("purpose") or "")} if v.get("kind") == history.LOCATION else {}
            out[v.get("call_id") or it.id] = {"id": it.id, "call_id": v.get("call_id") or "", "question": str(v.get("question") or ""),
                                              "options": [str(o) for o in v.get("options") or []], **where}
    return sorted(out.values(), key=lambda a: order.index(a["call_id"]) if a["call_id"] in order else len(order))


async def open_storage(url: str) -> tuple[AsyncPostgresSaver, AsyncPostgresStore, PostgresRunStore]:
    """对话的存储：检查点、长期记忆的 Store、运行记录，存在同一个 Postgres 里、共用一个连接池（见 graph.postgres_pool）。
    表没建的先建（检查点的 setup 会跑完 LangGraph 的迁移）。没配库、连不上都抛 RuntimeError，服务启动时就报出来（见 main 的 lifespan）。
    Store 不开向量搜索：dev 的库没有 pgvector，记忆整份取出来放进系统提示词，用不上按相似度查（见 memory.py）。"""
    pool = await postgres_pool(url)
    try:
        saver = AsyncPostgresSaver(pool, serde=make_serde())
        await saver.setup()  # 连不上的话停在这里：Store 一建就起后台任务，连不上时不建它
        store, runs = AsyncPostgresStore(pool), PostgresRunStore(pool)
        await store.setup()
        await runs.setup()
    except BaseException as e:
        await pool.close()
        if isinstance(e, PoolTimeout):
            raise RuntimeError(NO_POSTGRES) from e
        raise
    return saver, store, runs


def open_files(runs: RunStore) -> FileStore:
    """用户发的文件的表：运行记录在 Postgres 里的，和它共用连接池；在内存里的（测试、评测）也记在内存里。"""
    return PostgresFileStore(runs.pool) if isinstance(runs, PostgresRunStore) else MemoryFileStore()


def open_chunks(runs: RunStore) -> ChunkStore:
    """资料库的块（pgvector）：和文件表一样，跟着运行记录放。"""
    return PostgresChunkStore(runs.pool) if isinstance(runs, PostgresRunStore) else MemoryChunkStore()


def open_libraries(runs: RunStore) -> LibraryStore:
    """资料库（名字、说明）：和文件表一样，跟着运行记录放。"""
    return PostgresLibraryStore(runs.pool) if isinstance(runs, PostgresRunStore) else MemoryLibraryStore()


def open_tasks(runs: RunStore) -> TaskStore:
    """定时任务的表：和文件表一样，跟着运行记录放。"""
    return PostgresTaskStore(runs.pool) if isinstance(runs, PostgresRunStore) else MemoryTaskStore()


def _memory_row(m: dict) -> dict:
    """记忆页上的一条：thread_id 是记下（或最后改它）的那段对话，页面拿它点回去；对话删了、整理时没记（很早的几条）就点不了。"""
    return {k: m.get(k) for k in ("id", "text", "created_at", "updated_at", "thread_id")}

class ResearchRunner(BaseRunner):
    """对话场景的运行器。执行都在对话的事件循环里（见 app.aio）：每次执行是一个任务，同时最多跑 workers 个，多出来的排队；
    点停止就取消正在跑的那一轮，正在等的模型、工具马上断开。下面这些 _ 开头的协程和占位、队列的状态都只在那个循环里用。
    接口层在线程池里调不带 a 的同步方法：活交给事件循环做、等结果；事件流照旧从事件日志里读（见 BaseRunner._stream）。"""

    api_prefix = "/api/research/threads"
    # 测试里打开（见 tests/conftest.py）：取列表时记着的每一行都和现算的对一遍，对不上就报错（见 _alist_threads）
    check_summaries = False

    def __init__(self, fake: bool | None = None, workers: int | None = None,
                 planner: ResearchPlanner | None = None, checkpointer: BaseCheckpointSaver | None = None,
                 run_store: RunStore | None = None, store: BaseStore | None = None, files: FileService | None = None):
        fake = config.USE_FAKE_PLANNER if fake is None else fake
        self.planner = planner or (FakeResearchPlanner() if fake else LiveResearchPlanner())
        if checkpointer is None:  # 检查点、长期记忆、运行记录都存在配置的 Postgres 里（见 open_storage）；没配库、连不上抛 RuntimeError
            checkpointer, db_store, db_runs = aio.run(open_storage(config.DATABASE_URL))
            store, run_store = store or db_store, run_store or db_runs
        # 传了检查点（测试、评测用内存的）：长期记忆、运行记录没传的也记在内存里，不碰配置的库
        self.memory = MemoryBook(store or InMemoryStore())
        self._checkpointer = checkpointer
        self.durability = DURABILITY  # 评测录像时换成 "sync"：每一步都存，决策点回放从中间接着跑（见 app.evals.decisions）
        self._tools_retry = None  # 起服务时没连上 MCP 的工具：后台接着试的那个任务（见 _keep_connecting）
        if not aio.run(self._connect_tools()):
            self._tools_retry = asyncio.run_coroutine_threadsafe(self._keep_connecting(), aio.loop())
        self.graph = build_graph(self.planner, checkpointer, self.memory)
        self.run_store = run_store or MemoryRunStore()
        self.quota = quota.Quota(quota.usage_for(self.run_store))  # 额度按账上的记录算（见 app.quota）
        if isinstance(self.run_store, PostgresRunStore):  # 免费额度用完的记在库里，重启后照样不用（见 app.free_used_up）
            aio.run(free_used_up.attach(self.planner.catalog, self.run_store._exec))
        self.images = self.planner.gateway  # 画好的图存在 image-service 里，按编号取、删（见 app.tools.gateway）
        # 沙箱：工作任务里跑命令（见 app.tools.run_command），经同一个网关；配置里关了就没有，模型拿不到这个工具
        self.sandbox = Sandbox(self.planner.gateway) if config.SANDBOX_ENABLED else None
        # 右栏“下载成 PDF / Word”（见 app.api.export）：和跑命令用同一个沙箱服务，沙箱没开就没有
        self.exporter = Exporter(self.planner.gateway) if config.SANDBOX_ENABLED else None
        # 用户发的文件：原件在数据卷上，文字和文件表在运行记录的库里；在这里起后台的解析、清理（见 app.files.service）
        self.files = files or FileService(open_files(self.run_store), config.FILES_DIR, isolated=config.FILE_PARSE_ISOLATED,
                                          timeout=config.FILE_PARSE_TIMEOUT_S)
        # 资料库：文件也在文件表里，解析好了交给它建索引（先挂上再起文件服务：重启时还没建完的会被交回来）；块的表连着文件表、
        # 资料库建表时要搬文件表里 11-10 以前的行，都在文件服务起来以后建。
        # 假模型跑的（本机、测试）用假向量、不重排，不调百炼
        embedder, reranker = (HashEmbedder(), None) if fake else kb_remote.live()
        self.kb = KnowledgeBase(self.files, open_chunks(self.run_store), embedder, reranker, open_libraries(self.run_store))
        aio.run(self.files.start())
        aio.run(self.kb.start())
        # 定时任务（见 app.scheduled）：表跟着运行记录放；到点发起的循环由服务启动时开（见 start_schedule），测试、命令行不开
        self.tasks = ScheduledTasks(open_tasks(self.run_store))
        aio.run(self.tasks.store.setup())
        self._schedule_job = None
        self._scheduled: dict[str, str] = {}  # 定时任务发起、还没结束的执行（运行的 id → 任务的编号）：结束时把结果记到任务上
        self.workers = workers or config.RESEARCH_WORKERS
        self._slots = asyncio.BoundedSemaphore(self.workers)  # 同时跑的执行
        self._inflight = 0  # 交出去、还没结束的执行（排队的也算）：据此告诉页面这次要不要排队
        self._running: dict[str, asyncio.Task] = {}  # 线程 → 正在跑的这一轮，点停止时取消它
        self._tasks: set[asyncio.Task] = set()  # 交出去的执行，留着引用，免得跑到一半被回收
        # 正文写完这一轮就结束，推荐问题在结束之后才起（见 _suggest）。线程 → 等着起推荐问题的那一轮（轮 id, 用户的话, 回复）。
        # 用户又说了话、重新回答或删掉对话时撤掉：起好了也不写。撤掉和写检查点都在锁里，不会和用户那边的写交错
        self._suggest_mu = asyncio.Lock()
        self._suggest_for: dict[str, tuple[str, str, str]] = {}
        # 线程 → 这次运行里起好、发给了页面的标题（见 _stream_graph）。答完的随调模型写进 state、停下的随停止写进去（见 _write_stop），
        # 这时就不用记了；第一次调模型就出错的写不了 state（写了重试就接不上），记在这里：页面上还显示它，重试时直接用（ChatContext.title）。
        # 只在进程里：服务重启了就没了，和以前一样跳回原话、重试时再起
        self._titles: dict[str, str] = {}
        # 用户给的位置（见 app.tools.location）：只在内存里，按人留一小段时间，这段时间里再用到的不再要
        self.places = Places()
        # 线程 → 正在跑的这一轮主 agent 用的模型（"厂商:模型"，见 agent 发的 model 事件）：一轮结束才进这一轮的记录，
        # 中途打开对话时页面按它显示回答开头的模型（Auto 判断完用的哪个、换了备用）。这一轮结束就不记了
        self._models: dict[str, str] = {}
        # 占位都经过这把锁（见 _aadmit）：话排进去时看线程在不在跑、这一轮结束时退掉占位并取走排着的话，两件事不会交错，
        # 不会出现话排进去了、这一轮却已经结束没人发的情况（中间要等运行记录的库）
        self._queue_mu = asyncio.Lock()

    async def _connect_tools(self) -> bool:
        """连上 MCP 的工具（搜索、读页、天气，见 research_llm.McpTools）。连不上每隔几秒再试，到 TOOLS_STARTUP_S 还有没连上的就先这样起来：
        哪个服务挂了都不该让整个对话起不来（没连上的搜索、读页调了会说服务暂时连不上）。密钥不对、网关没放行也一样，日志里报错。"""
        deadline = time.monotonic() + TOOLS_STARTUP_S
        while True:
            try:
                await self.planner.connect_tools()
                return True
            except Exception as e:
                if time.monotonic() + TOOLS_STARTUP_RETRY_S >= deadline:
                    log.error("MCP 的工具没连全，先这样起来，后台每 %d 秒再试：%s", TOOLS_RETRY_S, e)
                    return False
                log.warning("MCP 的工具没连全，%d 秒后再试：%s", TOOLS_STARTUP_RETRY_S, e)
            await asyncio.sleep(TOOLS_STARTUP_RETRY_S)

    async def _keep_connecting(self) -> None:
        """起服务时没连全 MCP 的工具：每分钟再试，每连上一个就换上新的 agent，都连上了就停。正在跑的那几轮用的还是旧的，下一轮起用新的。"""
        while True:
            await asyncio.sleep(TOOLS_RETRY_S)
            before = [id(t) for t in self.planner.tools()]
            try:
                await self.planner.connect_tools()
                done = True
            except Exception as e:
                log.warning("MCP 的工具还是没连全：%s", e)
                done = False
            if [id(t) for t in self.planner.tools()] != before:
                self.graph = build_graph(self.planner, self._checkpointer, self.memory)
                log.info("连上了 MCP 的工具（%s），换上新的 agent", "全部" if done else "部分")
            if done:
                return

    @staticmethod
    def _cfg(thread_id: str) -> dict:
        return {"configurable": {"thread_id": thread_id}}

    # ---- 给接口层的同步方法：在别的线程里调，活交给对话的事件循环 ----

    # option 是新对话选的模型（models.toml 里选项的 id，没给是 Auto），定下就不换；thinking 是这句话的思考开关（没给按选项的默认）；
    # work 是新对话是不是工作任务（没给是对话）：新建时定下，整段不变，之后的话都按它（2026-11-11 Sean 定，见 history.work_mode）；
    # images 是这句话带的图（先用 upload_image 一张张传上去；没开始这一轮要自己 discard_images，见 api.main）；
    # files 是带的文件的编号（先用 upload_file 传上来、解析好，发出去时绑到这段对话；不能发的抛 NotSendable，什么都没写）

    def start(self, thread_id: str, text: str, option: str | None = None, thinking: bool | None = None, owner: str | None = None,
              images: Sequence[AttachedImage] = (), files: Sequence[str] = (), work: bool | None = None) -> AsyncIterator[str]:
        """owner 是这段新对话归谁（见 app.identity 和 run_store 的 thread_owners）；命令行、评测不传，对话就没归谁（页面上谁都看不到）。"""
        return self._stream(aio.run(self._asubmit(
            thread_id, lambda: self._aaccept(thread_id, text, thinking, option, owner, images, files, work=work), "new")))

    def say(self, thread_id: str, text: str, thinking: bool | None = None, images: Sequence[AttachedImage] = (),
            files: Sequence[str] = ()) -> AsyncIterator[str]:
        return self._stream(aio.run(self._asubmit(
            thread_id, lambda: self._aaccept(thread_id, text, thinking, images=images, files=files), "say")))

    def upload_file(self, owner: str, tier: str, name: str | None, data: bytes) -> FileRow:
        """用户传的文件：存好原件、排进后台解析，马上返回（状态是解析中）。不收的抛 UploadRejected（太大、类型不对、今天传满了）。"""
        return aio.run(self.files.upload(owner, tier, name, data))

    def file(self, file_id: str, owner: str) -> FileRow | None:
        """这个人传的文件（页面问解析好了没有）；别人的、没有的是 None。"""
        return aio.run(self.files.get(file_id, owner))

    def remove_file(self, file_id: str, owner: str) -> bool:
        """去掉还没发出去的文件（输入框里点叉）。发出去的、别人的、没有的返回 False。"""
        return aio.run(self.files.remove(file_id, owner))

    def file_original(self, thread_id: str, file_id: str) -> tuple[Path, FileRow] | None:
        """这段对话里用户发的文件的原件（下载）：(路径, 那一行)。不是这段对话的、原件没了是 None。"""
        return aio.run(self.files.original(file_id, thread_id))

    def file_preview(self, thread_id: str, file_id: str) -> tuple[FileRow, list[Part], bool] | None:
        """这段对话里用户发的文件抽出来的文字（右栏预览，见 FileService.preview）。"""
        return aio.run(self.files.preview(file_id, thread_id))

    def export_file(self, thread_id: str, file_id: str, fmt: str, who: Identity) -> tuple[bytes, str]:
        """右栏“下载成 PDF / Word”：模型写的文档的这一版转成 fmt（export.PDF / DOCX），返回（内容, 文件名）。
        不经过模型、不扣额度，内容和页面上下载的 Markdown 一样（来源 id 换成编号、文末附来源）。转不了的抛 ExportFailed。
        这段对话里的 PPT（用户发的、跑命令产出的）也能要 PDF：排成一张一页，右栏拿它看每一页的样子。"""
        return aio.run(self._aexport_file(thread_id, file_id, fmt, who))

    async def _aexport_file(self, thread_id: str, file_id: str, fmt: str, who: Identity) -> tuple[bytes, str]:
        got = await self.files.original(file_id, thread_id)
        row = got[1] if got else None
        slides = row is not None and row.type == PPTX and fmt == export.PDF
        if row is None or not (slides or (row.origin == GENERATED and row.type == MARKDOWN and row.series_id)):
            raise ExportFailed(404, export.NOT_FOUND)  # 没有、不是这段对话的、不是模型写的文档也不是 PPT（网页、代码、表格不转）
        if who.guest:
            raise ExportFailed(403, export.LOGIN_REQUIRED)
        if self.exporter is None:
            raise ExportFailed(503, export.UNAVAILABLE)
        if slides:
            if row.size > SANDBOX_MAX_UPLOAD_BYTES:  # 放不进沙箱，不用把它读进来
                raise ExportFailed(413, export.TOO_BIG)
            try:
                data = await asyncio.to_thread(got[0].read_bytes)
            except OSError:  # 刚好被删了
                raise ExportFailed(404, export.NOT_FOUND) from None
            return await self.exporter.slides(file_id, data, who), f"{Path(row.name).stem}.pdf"
        # 来源从检查点里取，检查点一轮结束才写（见 DURABILITY）：写这一版的那一轮还没答完的话，这一轮查到的来源还不在里面，先不转
        messages = (await self.graph.aget_state(self._cfg(thread_id))).values.get("messages", [])
        written = next((w for m in messages if (w := history.written_of(m)) is not None and w.id == file_id), None)
        if written is None:
            raise ExportFailed(409, export.NOT_READY)
        text = await self.files.text(file_id)
        if text is None:
            raise ExportFailed(404, export.NOT_FOUND)
        doc = export_markdown(text, {s.id: s for s in collect_sources(messages)})
        data = await self.exporter.convert(file_id, fmt, doc, written.title, who)
        return data, f"{Path(row.name).stem}.{fmt}"

    # ---- 资料库（见 app.knowledge）----

    def kb_overview(self, who: Identity) -> dict:
        return aio.run(self.kb.overview(who))

    def kb_create(self, who: Identity, name: str, description: str) -> Library:
        """建一个资料库。不收的抛 LibraryRejected（游客、名字不对、满了、重名）。"""
        return aio.run(self.kb.create(who, name, description))

    def kb_update(self, who: Identity, library_id: str, name: str | None, description: str | None) -> Library | None:
        return aio.run(self.kb.update(who, library_id, name, description))

    def kb_remove(self, who: Identity, library_id: str) -> bool:
        return aio.run(self.kb.remove(who, library_id))

    def kb_upload(self, who: Identity, library_id: str, name: str | None, data: bytes) -> FileRow:
        """传进一个资料库：马上返回（解析中），后台解析、建索引。不收的抛 UploadRejected（游客、资料库没有、满了、太大、类型不对）。"""
        return aio.run(self.kb.upload(who, library_id, name, data))

    def kb_file(self, who: Identity, file_id: str) -> FileRow | None:
        return aio.run(self.kb.get(who, file_id))

    def kb_delete(self, who: Identity, file_id: str) -> bool:
        return aio.run(self.kb.delete(who, file_id))

    def kb_preview(self, who: Identity, file_id: str) -> tuple[FileRow, list[Part], bool] | None:
        """资料库里的文件抽出来的文字（右栏预览，和对话里的文件一样）。游客、别人的、没建好的是 None。"""
        async def go():
            row = await self.kb.get(who, file_id)
            return None if row is None else await self.files.preview(file_id, row.workspace_id)
        return aio.run(go())

    def kb_original(self, who: Identity, file_id: str) -> tuple[Path, FileRow] | None:
        async def go():
            row = await self.kb.get(who, file_id)
            return None if row is None else await self.files.original(file_id, row.workspace_id)
        return aio.run(go())

    def upload_image(self, data: bytes) -> AttachedImage:
        """用户发的图传进 image-service（归灵稿，和画的图放在一起），返回记在消息上的信息。
        服务不收抛 gateway.ImageRejected（太大、认不出），网关、服务出问题抛 GatewayError。"""
        got = aio.run(self.images.upload_image(data))
        return AttachedImage(id=got["id"], mime_type=got["mime_type"], width=got["width"], height=got["height"])

    def discard_images(self, image_ids: list[str]) -> None:
        """传上去了、这一轮却没开始（运行中、额度、模型不对，后面的图没传上去）：删掉，不留没人指着的图。删不掉只记日志。"""
        if image_ids:
            aio.run(self.images.delete_images(image_ids))

    def retry(self, thread_id: str) -> AsyncIterator[str]:
        return self._stream(aio.run(self._asubmit(thread_id, lambda: self._aretry_input(thread_id), "retry")))

    def regenerate(self, thread_id: str, text: str | None = None, thinking: bool | None = None) -> AsyncIterator[str]:
        kind = "regenerate" if text is None else "edit"
        return self._stream(aio.run(self._asubmit(thread_id, lambda: self._aregenerate_input(thread_id, text, thinking), kind)))

    def answer(self, thread_id: str, ask_id: str, text: str = "", skip: bool = False, mute: bool = False, *, location: Device | None = None,
               unavailable: bool = False) -> AsyncIterator[str]:
        """回答模型问的问题（skip 是跳过，不回答；mute 是“别再问了”：跳过，并且这段对话之后不再问），这一轮从停下的地方接着跑。
        ask_id 是状态里 ask 的 id。等着的不是这个问题（答过了、这一轮已经停了）抛 NothingToAnswer；回答是空的、太长抛 BadAnswer。
        等着的是要位置的（ask 的 kind 是 location）：location 是浏览器给的位置，unavailable 是浏览器取不到，skip 是用户不给，
        text 是用户没给位置、直接说了个地方。"""
        return self._stream(aio.run(self._asubmit(
            thread_id, lambda: self._aanswer_input(thread_id, ask_id, text, skip, mute, location=location, unavailable=unavailable), "answer")))

    def queue(self, thread_id: str, text: str, thinking: bool | None = None) -> dict | None:
        return aio.run(self._aqueue(thread_id, text, thinking))

    def guide(self, thread_id: str) -> dict | None:
        return aio.run(self._aguide(thread_id))

    def accept(self, thread_id: str, text: str, thinking: bool | None = None, option: str | None = None, owner: str | None = None,
               work: bool | None = None) -> None:
        aio.run(self._aaccept(thread_id, text, thinking, option, owner, work=work))

    def retry_input(self, thread_id: str) -> None:
        aio.run(self._aretry_input(thread_id))

    def regenerate_input(self, thread_id: str, text: str | None = None, thinking: bool | None = None) -> None:
        aio.run(self._aregenerate_input(thread_id, text, thinking))

    def answer_input(self, thread_id: str, ask_id: str, text: str = "", skip: bool = False, mute: bool = False, *,
                     location: Device | None = None, unavailable: bool = False) -> Command:
        """回答问题时交给 run_turn 的输入（命令行、评测和测试用，见 answer）。"""
        return aio.run(self._aanswer_input(thread_id, ask_id, text, skip, mute, location=location, unavailable=unavailable))

    def models(self, tier: str) -> list[dict]:
        """新对话能选的模型（按 models.toml 的顺序，Auto 在第一个）。thinking：toggle 是页面上显示思考开关，always、never 不显示。
        model 是这个选项主 agent 用的模型（"厂商:模型"），页面按它给每轮记下的模型找名字。
        locked 是 tier 这一档（见 model_catalog.TIERS）选不了的：照样列出来，页面上锁着（游客看得到要登录的模型）。"""
        return [{"id": o.id, "name": o.name, "description": o.description, "thinking": o.thinking, "model": o.model, "locked": not o.allows(tier)}
                for o in self.planner.catalog.offered(self.planner.usable)]

    def model_names(self) -> dict[str, dict[str, str]]:
        """选项以外、回答开头可能出现的模型（Auto 用的免费额度）："厂商:模型" → 名字、图标（厂商前缀）。"""
        return self.planner.catalog.names()

    def locked(self, option_id: str, tier: str) -> bool:
        """新对话能选的里面有这个选项，但 tier 这一档选不了。不在能选的里面的不算（开对话时照常报没有这个模型）。"""
        o = next((o for o in self.planner.catalog.offered(self.planner.usable) if o.id == option_id), None)
        return o is not None and not o.allows(tier)

    def set_asks(self, thread_id: str, on: bool) -> None:
        """这段对话 Work 的轮还问不问（页面的“恢复提问”是 on=True）。正在跑抛 RunConflict；没跑完、停在问用户那里抛 NotIdle。"""
        aio.run(self._aset_asks(thread_id, on))

    def rename(self, thread_id: str, title: str) -> None:
        aio.run(self._arename(thread_id, title))

    def delete(self, thread_id: str) -> None:
        aio.run(self._adelete(thread_id))

    def state_view(self, thread_id: str) -> dict:
        return aio.run(self._astate_view(thread_id))

    def image(self, thread_id: str, image_id: str) -> tuple[bytes, str] | None:
        """页面来取的图：(内容, MIME 类型)，经网关从 image-service 取（见 app.tools.gateway）。
        按文件表认是不是这段对话的：图发出去、画好时就登记了（不靠检查点，检查点一轮结束才写，见 DURABILITY）。
        不是这段对话的、编号格式不对（比如以前灵稿自己存的图）、服务里已经没有的是 None。网关、服务出问题照常抛出。"""
        if not IMAGE_ID.fullmatch(image_id) or not aio.run(self.files.image_in(image_id, thread_id)):
            return None
        return aio.run(self.images.image(image_id))

    def summary_view(self, thread_id: str) -> dict | None:
        return aio.run(self._asummary_view(thread_id))

    def list_threads(self, who: Identity) -> list[dict]:
        return aio.run(self._alist_threads(who))

    def list_page(self, who: Identity, limit: int | None = None, current: str | None = None) -> dict:
        """页面要的那一部分列表（左栏先取最近的，往下滚再多取）：最近有动静的 limit 段，加上不管排在哪都带上的——
        置顶的（左栏单独一组）、有未读的（左栏的红点、右上角的通知靠它）、在跑或排队的（页面靠它决定多久取一次列表、要不要接上去）、
        current（页面开着的那段：选中的那一行、是不是工作任务）。more 是还有没给的。limit 不给是全部（搜对话、记忆页对出处用）。
        多取就是把 limit 加大再取一次，不用游标：对话一有动静就排到前面，按位置接着取会漏、会重。"""
        rows = self.list_threads(who)
        if limit is None:
            return {"threads": rows, "more": False}
        shown = [x for i, x in enumerate(rows)
                 if i < limit or x["pinned_at"] or x["unread"] or x["status"] in ("queued", "running") or x["thread_id"] == current]
        return {"threads": shown, "more": len(shown) < len(rows)}

    def pin(self, thread_id: str, pinned: bool) -> dict | None:
        """置顶或取消（运行中也行：不碰检查点），返回列表里的这一行。"""
        return aio.run(self._apin(thread_id, pinned))

    async def _apin(self, thread_id: str, pinned: bool) -> dict | None:
        await self.run_store.pin(thread_id, pinned)
        return await self._asummary_view(thread_id)

    def mark_read(self, thread_id: str) -> dict | None:
        """用户看了这段对话：未读清零（运行中也行：不碰检查点），返回列表里的这一行。"""
        return aio.run(self._amark_read(thread_id))

    async def _amark_read(self, thread_id: str) -> dict | None:
        await self.run_store.mark_read(thread_id)
        return await self._asummary_view(thread_id)

    def owner_of(self, thread_id: str) -> str | None:
        return aio.run(self.run_store.owner_of(thread_id))

    def can_see(self, thread_id: str, who: Identity) -> bool:
        """这段对话 who 能不能看、能不能动：归 TA 的才行。没归谁的（登录以前的老对话）谁都看不到，相当于归档了（2026-11-06 Sean 定）。"""
        return self.owner_of(thread_id) == who.sub

    def claim(self, who: Identity, guest: Identity) -> int:
        """游客登录了：TA 游客期间的对话全改归账号（2026-11-06 Sean 定并进去），返回改了几段。游客没有记忆，不用搬。"""
        return aio.run(self.run_store.transfer(guest.sub, who.sub))

    def run_turn(self, thread_id: str, inp: Any = None, on_event: Callable[[dict], None] | None = None) -> str | None:
        """直接跑一轮、等它结束，事件交给 on_event（在事件循环里调，别在里面调这里的同步方法）：不占运行位、不记运行记录。
        命令行、评测和测试用；点停止要自己先占位（见 runs.admit）。inp 一般是 None（用户的话已经写进检查点），回答问题时是 answer_input 给的。
        返回 STOPPED、ASKED（停下来问用户了，这一轮还没完）或 None。
        这样跑的没有页面可以要用户的位置：模型选了当前位置的不停下来要，服务照没给位置回（见 app.tools.location）。"""
        return aio.run(self._run_turn(thread_id, inp, on_event or (lambda ev: None)))

    def suggest(self, thread_id: str) -> list[dict]:
        """这一轮结束之后起推荐问题（见 _suggest），返回发给页面的事件。和 run_turn 一样给命令行、评测和测试用。"""
        evs: list[dict] = []
        aio.run(self._suggest(thread_id, evs.append))
        return evs

    def stop(self, thread_id: str) -> bool:
        """让这个线程正在跑（或排队）的这一轮停下；没有在跑的返回 False。
        先记下要停，再到事件循环里取消正在跑的那一轮（见 _run_turn）；排队的轮到时看到要停，直接收尾。
        没在跑、停在问用户那里的：不回答了，这一轮以停止收尾（见 _adismiss）。"""
        key = self._run_key(thread_id)
        if not self.runs.request_stop(key):
            if aio.run(self._adismiss(thread_id)):
                return True
            if not self.runs.request_stop(key):  # 再看一次：要收尾时刚好有执行起来了（回答刚交上来），停它
                return False
        aio.loop().call_soon_threadsafe(self._cancel, thread_id)
        return True

    def unqueue(self, thread_id: str) -> str | None:
        """撤回排着的话，返回原文（页面放回输入框）；已经发出去了、或者没有排着的，返回 None。"""
        p = aio.run(self.run_store.take_pending(thread_id, QUEUE))
        return p.text if p else None

    def unguide(self, thread_id: str) -> str | None:
        """撤回等着插进这一轮的话，返回原文；已经插进去了、或者没有，返回 None。"""
        p = aio.run(self.run_store.take_pending(thread_id, GUIDE))
        return p.text if p else None

    async def _snapshot(self, thread_id: str) -> AsyncIterator[str]:
        yield _sse_text({"event": "state", "data": await aio.call(self._astate_view(thread_id))})
        yield _sse_text({"event": "end", "data": {}})

    # ---- 执行 ----

    async def _aadmit(self, key: str) -> bool:
        async with self._queue_mu:
            return self.runs.admit(key)

    async def _asubmit(self, thread_id: str, make_input: Callable[[], Awaitable[Any]], kind: str) -> str:
        """占位 → 把这一轮的输入写进检查点（校验在这一步，不通过就退位）→ 交出执行，返回事件日志的 id。占着位，别的请求改不了这个线程。
        kind 是这次执行怎么来的，记进运行记录。make_input 返回交给 agent 的输入：一般是 None（从检查点接着跑），回答问题时是带着回答的 Command。"""
        key = self._run_key(thread_id)
        if not await self._aadmit(key):
            raise RunConflict("这一步还在处理，等它完成后再提交")
        try:
            inp = await make_input()
            return await self._alaunch(thread_id, kind, inp)
        except BaseException:
            self.runs.release(key)
            raise

    async def _alaunch(self, thread_id: str, kind: str, inp: Any = None) -> str:
        """开一段新的事件日志、记一行运行记录，交出执行。执行结束才退掉占位：浏览器断开、没人读都照常跑完。
        定时任务发起的（kind 是 SCHEDULED）没有页面可以要用户的位置，别的都有。"""
        key = self._run_key(thread_id)
        turn = await self._aturn_of(thread_id)
        run = self.events.open(key)
        self._inflight += 1
        queued = self._inflight > self.workers
        if not queued:  # 不用等空位就算在跑：占位一开始记成排队，这时取的状态（比如排着的话接着发时上一次执行最后那次）会闪一下“排队中”
            self.runs.started(key)
        try:
            await self._arecord(lambda s: s.create(run, thread_id, kind, turn, config.VERSION or None))
            self.events.append(run, {"event": "thread", "data": {"thread_id": thread_id, "queued": queued}})  # 排队时也先把 id 给页面
            self._spawn(self._work(thread_id, run, inp, locate=kind != SCHEDULED))
        except BaseException as e:
            self._inflight -= 1
            self.events.close(run)
            await self._afinish(run, ERROR, e)
            raise
        return run

    def _spawn(self, work: Coroutine[Any, Any, None]) -> None:
        """另起一个任务跑这次执行，用空的上下文：不带交出它的那次执行（排着的话接着发时）里的东西。"""
        try:
            task = asyncio.get_running_loop().create_task(work, context=contextvars.Context())
        except BaseException:
            work.close()  # 没交出去：协程关掉，不留一个没跑过的
            raise
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _work(self, thread_id: str, run: str, inp: Any = None, locate: bool = True) -> None:
        key = self._run_key(thread_id)

        def out(ev: dict) -> None:
            self.events.append(run, ev)

        try:
            async with self._slots:
                self.runs.started(key)
                await self._arecord(lambda s: s.started(run))
                out({"event": "started", "data": {}})
                status, error = ERROR, None  # 被 Exception 以外的打断（进程要退出）也记成出错
                try:
                    status = await self._run_turn(thread_id, inp, out, locate=locate) or SUCCESS
                except Exception as e:  # noqa: BLE001
                    error = e
                    log.exception("执行出错：%s 运行 %s", key, run)
                    out({"event": "error", "data": _error_event(e)})
                finally:
                    await self._afinish(run, status, error)
                    await self._ascheduled_done(thread_id, run, status)
                    await self._aended(thread_id, status)
                try:
                    out({"event": "state", "data": await self._astate_view(thread_id)})
                    await self._suggest(thread_id, out)
                except Exception as e:  # noqa: BLE001
                    log.exception("这一轮结束后出错：%s 运行 %s", key, run)
                    out({"event": "error", "data": _error_event(e)})
            await self._akeep_latest(thread_id)  # 出了运行位再清：不占着位。这一轮的最后一次写（推荐问题）在上面
        finally:
            self._inflight -= 1
            self.events.close(run)

    async def _akeep_latest(self, thread_id: str) -> bool:
        """把这段对话以前的检查点清掉，只留最新的（见 graph.keep_latest）。每次执行结束时清一次，所以一段对话攒不下几个。
        清不成只打日志：不影响这一轮，留下的下次再清。"""
        try:
            await keep_latest(self.graph.checkpointer, thread_id)
            return True
        except Exception:  # noqa: BLE001
            log.warning("清以前的检查点没成：%s", thread_id, exc_info=True)
            return False

    def tidy_checkpoints(self) -> None:
        """服务启动时调一次，不等它做完：把攒着以前的检查点的对话都清一遍（见 _atidy_checkpoints）。"""
        asyncio.run_coroutine_threadsafe(self._atidy_checkpoints(), aio.loop())

    async def _atidy_checkpoints(self) -> int:
        """检查点不止一个的对话，一段一段清，返回清成了几段。平时每次执行结束都清，这里收的是：
        加这一步以前攒下的、执行以外写出来的（改名、没在跑时的停止收尾各多一个）、上次没清成的。只数主键找出要清的，都清过了就什么都不做。"""
        try:
            threads = await stale_threads(self.graph.checkpointer)
            done = [t for t in threads if await self._akeep_latest(t)]
        except Exception:  # noqa: BLE001
            log.warning("启动时清以前的检查点没成", exc_info=True)
            return 0
        if threads:
            log.info("启动时清以前的检查点：%d 段对话有，清成 %d 段", len(threads), len(done))
        return len(done)

    async def _arecord(self, write: Callable[[RunStore], Awaitable[None]]) -> None:
        """写运行记录。写不上只打日志，不影响执行本身。"""
        try:
            await write(self.run_store)
        except Exception:  # noqa: BLE001
            log.warning("运行记录没写上", exc_info=True)

    async def _afinish(self, run: str, status: str, error: BaseException | None = None) -> None:
        if error is None:
            await self._arecord(lambda s: s.finish(run, status))
            return
        tb = "".join(traceback.format_exception(error))
        await self._arecord(lambda s: s.finish(run, status, type(error).__name__, str(error), tb))

    def _cancel(self, thread_id: str) -> None:
        """在事件循环里取消这个线程正在跑的这一轮。只认点了停止的那次：停止传过来之前这一轮已经结束、下一轮已经开跑的，不动它。"""
        task = self._running.get(thread_id)
        if task is not None and not task.cancelling() and self._stop_requested(thread_id):  # 连点几次停止也只取消一次
            task.cancel()

    def _stop_requested(self, thread_id: str) -> bool:
        return self.runs.stop_requested(self._run_key(thread_id))

    async def _run_turn(self, thread_id: str, inp: Any, out: Callable[[dict], None], locate: bool = False) -> str | None:
        """跑一轮，事件交给 out。用户点了停止：排队时就点的不跑，直接收尾；跑着的马上取消（见 stop），正在等的模型、工具一起断开，
        已经流出来的正文、做完的工具结果留着（取消时照常存检查点，见 DURABILITY）。停下的返回 STOPPED（记进运行记录）。
        模型停下来问用户了（见 app.tools.ask_user）返回 ASKED：这次执行到此结束，这一轮还没完，检查点停在问的地方，
        用户回答以后另起一次执行接着跑（inp 是带着回答的 Command，见 _aanswer_input）。
        locate：有页面可以要用户的位置（经接口跑的），模型选了当前位置时停下来要（见 app.tools.location）。"""
        # 列表里记着的那几项（见 _alist_threads）先作废。有新的检查点它自己就对不上了，这里管的是不出新检查点就变了状态的：
        # 重试、回答以后又停在同一步（出错变成等回答、还是出错），检查点还是那个，变的只是它上面记的出错和问题
        await self._atouch(thread_id)
        # 这一轮调的模型、工具记账时记上对话、轮和归谁（见 app.costs；额度按归谁算，见 app.quota）
        scope = costs.Scope(thread_id=thread_id, turn_id=await self._aturn_of(thread_id), owner=await self.run_store.owner_of(thread_id))
        if self._stop_requested(thread_id):
            await self._write_stop(thread_id)
            out({"event": "node", "data": {"node": TURN_END}})
            return STOPPED
        buf = StreamBuffer()
        tasks: dict[str, str] = {}  # 子 agent 的命名空间 → 派它的 task 调用 id（task 工具开跑前写进自定义流的 subagent）
        done: dict[str | None, list[ToolMessage]] = {}  # 页面已经收到做完了的工具结果：主 agent 的在 None 下，子 agent 的在派它的 task 调用 id 下
        mark, ctx = object(), contextvars.copy_context()
        ctx.run(_TURN.set, mark)
        ctx.run(costs.SCOPE.set, scope)
        # 查过要不要停到登记好这一轮，中间不让出事件循环：停止（_cancel 也在这个循环里）要么上面已经看到，要么取消得到
        turn = asyncio.get_running_loop().create_task(self._stream_graph(thread_id, inp, out, buf, tasks, done, locate), context=ctx)
        self._running[thread_id] = turn
        asked = False
        try:
            asked = await turn
        except asyncio.CancelledError:
            me = asyncio.current_task()
            if (me is not None and me.cancelling()) or not turn.cancelled():  # 不是点了停止（比如进程要退出）：照常往外抛
                raise
            await _settle(mark)
            _put(out, buf.flush())
            # 检查点上这一步还有任务，就是没跑完。不能只看 next：它只列没做完的。停止正好落在一步做完、下一步还没开始的空档时，
            # 这一步的任务都做完了、结果还挂着没并进检查点，next 是空的，这一轮其实停在半路（模型还没调、工具结果还没给模型看）
            if (await self.graph.aget_state(self._cfg(thread_id))).tasks:
                await self._write_stop(thread_id, buf, tasks, done)
                out({"event": "node", "data": {"node": TURN_END}})
                return STOPPED
            # 点停止时这一轮刚好跑完了（最后一步已经并进检查点，没有任务了）：算答完
        finally:
            self._running.pop(thread_id, None)
            self._models.pop(thread_id, None)
            if not asked:  # 这一轮完了（答完、停了、出错）：为哪几次调用要过位置不用记了；停下来问用户的这一轮还没完，留着
                self.places.end_turn(thread_id)
        self._titles.pop(thread_id, None)  # 答完了（或者模型调完、停下来问用户了）：起好的标题随调模型写进 state 了
        if asked:
            return ASKED
        await self._want_suggestions(thread_id)
        return None

    async def _stream_graph(self, thread_id: str, inp: Any, out: Callable[[dict], None], buf: StreamBuffer, tasks: dict[str, str],
                            done: dict[str | None, list[ToolMessage]], locate: bool = False) -> bool:
        """agent 的流 → 页面事件。messages 只转发 model 节点的（快模型在别的节点里调用，不算）；
        custom 是中间件写的进度（step，做完的带来源）、标题（title）和阶段（phase：起标题、等模型这些空档在等什么）；updates 里改了 state 的节点做完发 node（页面不靠它）。
        模型生成到一半断了、正在再调一次（restart，见 agent.InterruptedRetryMiddleware）：带上刚才流出来的那条消息的 id，页面去掉它；没流出东西是 None。
        子 agent（task 工具里跑的另一个 agent）的流要开 subgraphs 才有，按命名空间认出是哪个 task 调用派的：
        它的片段、进度、阶段带上 task（调用 id），页面放进那张子任务卡；它的节点做完不发 node。它的片段 langgraph_node 也是 model，只按节点名分不开。
        做完的 step 带着工具结果（result，不发给页面），记进 done：点停止时它那一步可能还没提交，收尾时补上（见 _write_stop）。
        模型停下来问用户（ask_user 里的 interrupt）时流照常结束，updates 里有一条 __interrupt__：返回 True（见 _run_turn）。
        别的中断（现在没有，比如 MCP 的 elicitation）不认，和以前一样这一轮算没跑完。"""
        asked = False
        owner = await self.run_store.owner_of(thread_id)
        ctx = ChatContext(take_guide=lambda: self._take_guide(thread_id), title=self._titles.get(thread_id),
                          owner=owner, files=self.files, thread_id=thread_id, kb=self.kb, places=self.places if locate else None,
                          sandbox=self.sandbox, tasks=self.tasks, tier=self.quota.tiers.get(owner or ""))
        async for ns, mode, chunk in self.graph.astream(inp, self._cfg(thread_id), context=ctx, stream_mode=["messages", "updates", "custom"],
                                                        subgraphs=True, durability=self.durability):
            task = tasks.get(ns[0]) if ns else None
            if ns and task is None:  # 不是子 agent 的嵌套图（现在没有）
                continue
            if mode == "messages":
                msg, meta = chunk
                if meta.get("langgraph_node") == "model" and isinstance(msg, AIMessageChunk):
                    _put(out, buf.add(msg, task))
                continue
            _put(out, buf.flush())
            if mode == "custom":
                if chunk["type"] == "subagent":
                    tasks[chunk["ns"]] = chunk["id"]
                    continue
                if chunk["type"] == "restart":  # 前面的片段上面已经发出去了，页面收到这条再去掉
                    chunk = {"type": "restart", "id": buf.drop(task) if chunk.get("streamed") else None}
                if chunk["type"] == "title" and not task:
                    self._titles[thread_id] = chunk["title"]
                if chunk["type"] == "model" and not task:
                    self._models[thread_id] = chunk["ref"]
                if isinstance(result := chunk.get("result"), ToolMessage):
                    done.setdefault(task, []).append(result)
                data = {k: v for k, v in chunk.items() if k not in ("type", "result")} | ({"task": task} if task else {})
                out({"event": chunk["type"], "data": jsonable(data)})
                continue
            if task:
                continue
            for node, update in chunk.items():
                if node == "__interrupt__":
                    asked = asked or any(_is_ask(i.value) for i in update)
                elif update:  # 没改 state 的中间件节点不发
                    out({"event": "node", "data": {"node": node}})
        _put(out, buf.flush())
        return asked

    async def _write_stop(self, thread_id: str, buf: StreamBuffer | None = None, tasks: dict[str, str] | None = None,
                          done: dict[str | None, list[ToolMessage]] | None = None, ended_at: str | None = None) -> None:
        """这一轮以停止收尾，写成这一轮最后一个节点（TURN_END）的更新：线程停在这一轮结束的地方，可以接着说。
        ended_at 是这一轮算到什么时候为止（没给是现在，见 agent.stop_turn）。
        tasks 是这次运行里派出去的子 agent（命名空间 → task 调用 id）：还没做完的，把它检查点里已经做了的和流出来的半截存进它的子任务卡，
        它没做完的工具调用标成停止了。done 是页面已经收到做完了的工具结果（见 _stream_graph）：取消时它那一步可能还没提交，检查点里没有，这里补上，
        页面上做完的一步停下以后还是做完的，子 agent 交回的来源也算上它。"""
        cfg = self._cfg(thread_id)
        done = done or {}
        v = (await self.graph.aget_state(cfg)).values  # 主 agent 被取消那一步里已经提交的结果，get_state 已经算进去了
        v = {**v, "messages": _with_results(v["messages"], done.get(None, []))}
        partial = buf.partial() if buf else None
        if partial is not None and any(m.id == partial.id for m in v["messages"]):  # 这条消息已经完整存下来了
            partial = None
        subs: dict[str, list[BaseMessage]] = {}
        for ns, call_id in (tasks or {}).items():
            tup = await self.graph.checkpointer.aget_tuple({"configurable": {"thread_id": thread_id, "checkpoint_ns": ns}})
            msgs = list(tup.checkpoint["channel_values"].get("messages", [])) if tup else []
            msgs = _with_results(msgs, [*_pending_messages(tup), *done.get(call_id, [])])
            msgs += history.dangling(msgs, stopped=True)
            p = buf.partial(call_id) if buf else None
            if p is not None and history.streamed_something(p) and not any(m.id == p.id for m in msgs):
                msgs.append(p)
            if msgs:
                subs[call_id] = msgs
        await self.graph.aupdate_state(cfg, stop_turn(v, partial, subs, title=self._titles.get(thread_id), ended_at=ended_at), as_node=TURN_END)
        self._titles.pop(thread_id, None)
        self.places.end_turn(thread_id)  # 这一轮以停止收尾（问着的时候点了停止、重新回答也走这里）：同上，给过的位置照留着

    async def _adismiss(self, thread_id: str) -> bool:
        """停在问用户那里的这一轮，用户点了停止：不回答了，这一轮以停止收尾（没回答的问题留在过程里），之后可以接着说。
        这时没有执行在跑，占着位直接写；这一轮的用时算到问出来的时候。排着的话和点停止时一样，接着发成新的一轮（见 _aended）。
        没在等回答的（空闲、没跑完、刚好有执行占着位）返回 False。"""
        key = self._run_key(thread_id)
        if not await self._aadmit(key):
            return False
        try:
            snap = await self.graph.aget_state(self._cfg(thread_id))
            if not snap.values or self._status(snap, None)[0] != ASKING:
                self.runs.release(key)
                return False
            await self._write_stop(thread_id, ended_at=snap.created_at)
        except BaseException:
            self.runs.release(key)
            raise
        await self._aended(thread_id, STOPPED)
        return True

    # ---- 回答时又说的话 ----

    async def _aqueue(self, thread_id: str, text: str, thinking: bool | None = None) -> dict | None:
        """正在回答时又说的话：排着，这一轮答完或停下就接着发（见 _aended）。返回排着的那句。
        没发出去的话最多一句：已经有一句排着或等着插进这一轮的，抛 AlreadyPending（页面上这时发不出去，这里兜底）。
        刚好没在跑（这一轮刚结束）就直接发、返回 None，页面去接这次执行的事件流。上一轮没跑完的抛 NotIdle（先重试）。"""
        key = self._run_key(thread_id)
        async with self._queue_mu:
            if not self.runs.admit(key):
                if (await self.run_store.get_pending(thread_id, QUEUE)
                        or await self.run_store.get_pending(thread_id, GUIDE)):
                    raise AlreadyPending("上面那句还没发出去，先撤回改好再发")
                return await self.run_store.put_pending(thread_id, QUEUE, text.strip(), thinking=thinking)
        try:
            await self._aaccept(thread_id, text, thinking)
            await self._alaunch(thread_id, "say")
        except BaseException:
            self.runs.release(key)
            raise
        return None

    async def _aguide(self, thread_id: str) -> dict | None:
        """排着的话不等这一轮答完，插进这一轮：主 agent 下一次调模型之前放进对话（见 agent.TurnMiddleware.abefore_model）。
        已经有一句等着插的，接在它后面。返回等着插的那句；没在跑（排着的话已经发出去了）返回 None。"""
        async with self._queue_mu:  # 和这一轮结束时的交接错开：在锁里看到还在跑，结束时一定看得到这句话
            if self.runs.get(self._run_key(thread_id)) is None:
                return None
            if p := await self.run_store.take_pending(thread_id, QUEUE):
                return await self.run_store.put_pending(thread_id, GUIDE, p.text, thinking=p.thinking)
            return await self.run_store.get_pending(thread_id, GUIDE)

    async def _take_guide(self, thread_id: str) -> HumanMessage | None:
        """主 agent 调模型之前取等着插进这一轮的话。取不到就当没有：话留在库里，这一轮结束时退回去排着。
        取的时候点了停止：等它取完，取到的放回去（这一轮收尾时照常退回去排着），不然这句话就丢了。"""
        job = asyncio.ensure_future(self.run_store.take_pending(thread_id, GUIDE))
        try:
            p = await asyncio.shield(job)
        except asyncio.CancelledError:
            await self._untake_guide(thread_id, job)
            raise
        except Exception:  # noqa: BLE001
            log.warning("插进这一轮的话没取出来：%s", self._run_key(thread_id), exc_info=True)
            return None
        return _guided(p.text) if p else None  # 插进这一轮的话不开新的一轮，思考开关不用

    async def _untake_guide(self, thread_id: str, job: asyncio.Future) -> None:
        try:
            if p := await job:
                await self.run_store.put_pending(thread_id, GUIDE, p.text, front=True, thinking=p.thinking)
        except Exception:  # noqa: BLE001
            log.warning("停下时取出来的插话没放回去：%s", self._run_key(thread_id), exc_info=True)

    async def _aended(self, thread_id: str, status: str) -> None:
        """一次执行结束：退掉占位。答完或停下了、又排着话，就把它发成新的一轮（kind 是 say），占位直接交过去，中间别的请求插不进来；
        这次执行最后的状态里线程已经又在跑了，页面看到就接着读下一次执行的事件。
        出错的不发：排着的话留着，页面把它放回输入框，用户重试完再发。
        等着插进这一轮、这一轮却没再调模型的话，先退回去排着（排在这期间又说的话前面），和排着的一样处理。
        停下来问用户的（ASKED）这一轮还没完：排着的、等着插的话都原样留着，用户回答以后接着跑时照常插进去、答完再发；
        用户不回答、点了停止的，和停下一样处理（见 _adismiss）。"""
        key = self._run_key(thread_id)
        async with self._queue_mu:
            if status == ASKED:
                self.runs.release(key)
                return
            try:
                if left := await self.run_store.take_pending(thread_id, GUIDE):
                    await self.run_store.put_pending(thread_id, QUEUE, left.text, front=True, thinking=left.thinking)
            except Exception:  # noqa: BLE001
                log.warning("没插进去的话没退回去排着：%s", key, exc_info=True)
            p = None
            if status in (SUCCESS, STOPPED):
                try:
                    p = await self.run_store.take_pending(thread_id, QUEUE)
                except Exception:  # noqa: BLE001  取不到就当没有，话留在库里，页面会放回输入框
                    log.warning("排着的话没取出来：%s", key, exc_info=True)
            self.runs.release(key)
            if p is None:
                return
            if not self.runs.admit(key):  # 占位都经过这把锁，不会被抢；万一抢了，话放回去
                await self._put_back(thread_id, p)
                return
        try:
            await self._aaccept(thread_id, p.text, p.thinking)
            await self._alaunch(thread_id, "say")
        except Exception:  # noqa: BLE001
            self.runs.release(key)
            log.exception("排着的话没发出去：%s", key)
            await self._put_back(thread_id, p)

    async def _put_back(self, thread_id: str, p: Pending) -> None:
        """取出来却没发出去的话放回去，排在这期间又说的话前面；页面看到线程空闲还排着话，会放回输入框。"""
        try:
            await self.run_store.put_pending(thread_id, QUEUE, p.text, front=True, thinking=p.thinking)
        except Exception:  # noqa: BLE001
            log.warning("排着的话没放回去：%s", self._run_key(thread_id), exc_info=True)

    # ---- 一轮的输入 ----

    async def _aaccept(self, thread_id: str, text: str, thinking: bool | None = None, option: str | None = None, owner: str | None = None,
                       images: Sequence[AttachedImage] = (), files: Sequence[str] = (), work: bool | None = None,
                       scheduled_by: dict | None = None, title: str | None = None) -> None:
        """把用户的话写进检查点，agent 从这里接着跑（输入为 None）。

        先落盘再交出执行：排队时服务重启也查得到这句话，算没跑完，可以重试。
        option 是新对话选的模型，和第一句话一起写进去（第一轮开始时定下，见 agent.TurnMiddleware）；已经开始的对话只能给定下的那个。
        owner 是新对话归谁：第一句话写进去之前记下（已经记过的不改）。
        work 是新对话是不是工作任务，和第一句话一起定下（记进 state）；已经开始的对话不看它，按定下的来，每句话都按它写上
        （工作任务不能变回对话；对话以后能不能转成工作任务还没定，现在也不行）。
        files 是带的文件的编号：校验都过了才绑到这段对话（绑不上抛 NotSendable，什么都没写）。
        scheduled_by：这句话是定时任务到点触发的（见 _said）；title 是新对话直接定下的标题（定时任务新开的那段用任务的名字，不让模型按那份交代再起）。
        """
        await self._adrop_suggestions(thread_id)
        cfg = self._cfg(thread_id)
        snap = await self.graph.aget_state(cfg)
        if snap.values and snap.next:
            raise NotIdle(ASKING_FIRST if self._status(snap, None)[0] == ASKING else "上一轮没跑完，先重试")
        update: dict[str, Any] = {}
        if option is not None:
            if option not in {o.id for o in self.planner.catalog.offered(self.planner.usable)}:
                raise UnknownModel(f"没有这个模型可选：{option}")
            if (snap.values or {}).get("messages") and snap.values.get("option") != option:
                raise UnknownModel("对话开始后不能换模型，想换模型请开新对话")
            update["option"] = option
        attached: list[AttachedFile] = []
        who = owner or (await self.run_store.owner_of(thread_id) if files or images else None)
        if files:
            if who is None:
                raise NotSendable(NOT_HERE, "这段对话不能带文件")
            attached = await self.files.bind(list(files), who, thread_id)
        for img in images:  # 带的图登记进文件表（图已经在 image-service 里了）
            await self.files.add_image(img.id, owner=who or "", workspace_id=thread_id, generated=False, turn_id=None,
                                       mime_type=img.mime_type, width=img.width, height=img.height)
        started = bool((snap.values or {}).get("messages"))
        mode = history.work_mode(snap.values) if started else bool(work)
        update[history.WORK] = mode
        update["messages"] = [_said(text, thinking, images, attached, work=mode or None, scheduled_by=scheduled_by)]
        if title and not started:
            update["title"] = title
        if owner is not None:
            await self.run_store.claim(thread_id, owner)
        await self.graph.aupdate_state(cfg, update, as_node=START)

    async def _aretry_input(self, thread_id: str) -> None:
        """只有没跑完的线程能重试：从最后一个检查点接着跑，出错那一步重做，之前做过的搜索、读页都不重来。
        停在问用户那里的不算没跑完：回答（或跳过）就接着跑。"""
        snap = await self.graph.aget_state(self._cfg(thread_id))
        if not snap.values or not snap.next or self._status(snap, None)[0] == ASKING:
            raise NothingToRetry("这个线程没有失败，不需要重试")

    async def _aanswer_input(self, thread_id: str, ask_id: str, text: str = "", skip: bool = False, mute: bool = False, *,
                             location: Device | None = None, unavailable: bool = False) -> Command:
        """用户回答了（或跳过了）模型问的问题：校验过了返回带着回答的 Command，agent 拿它从停下的地方接着跑（回答按中断的 id 交给那次提问）。
        什么都不往检查点里写：问着的时候写一次状态，等着的问题就丢了。回答连同问出来、回答的时间一起交过去，算这一轮用时的时候去掉等的这段。
        一条消息里问了两个的一次答一个：答了前一个，接着跑会马上停在后一个上。
        mute 是点了“别再问了”：这个问题当跳过，这段对话记成不再问（工具接着跑时写进 state，见 app.tools.ask_user）；
        同一条消息里还等着的别的问题一起跳过，不再一个个出卡（一起等着的要位置的当用户不给）。
        等着的是要位置的（见 app.tools.location）：位置放进内存（self.places，记在这段对话归的那个人名下），交给 agent 的只有给没给；
        同一条消息里几次调用都要位置的一起回，不一张张出卡。"""
        snap = await self.graph.aget_state(self._cfg(thread_id))
        asking = bool(snap.values) and self._status(snap, None)[0] == ASKING
        waiting = _asks(snap) if asking else []
        ask = next((a for a in waiting if a["id"] == ask_id), None)
        if ask is None:
            raise NothingToAnswer("这个问题已经不用回答了")
        skip = skip or mute
        times = {"asked_at": snap.created_at or "", "answered_at": datetime.now(UTC).isoformat()}
        wheres = [a for a in waiting if a.get("kind") == history.LOCATION]
        if ask.get("kind") == history.LOCATION:
            try:
                status = PROVIDED if location is not None else SAID if text.strip() else UNAVAILABLE if unavailable else DECLINED if skip else None
                if status is None:
                    raise BadAnswer("没有收到位置")
                where = PlaceAnswer(status=status, answer=text.strip() if status == SAID else "", **times)
            except ValidationError as e:
                raise BadAnswer(f"回答不能超过 {MAX_ANSWER} 个字") from e
            if location is not None:
                who = holder(await self.run_store.owner_of(thread_id), thread_id)
                self.places.put(who, location, thread_id, {a["call_id"] for a in wheres})
            return Command(resume={a["id"]: where.model_dump() for a in wheres})
        if location is not None or unavailable:
            raise BadAnswer("现在等着的不是要位置的问题")
        try:
            got = AskAnswer(answer="" if skip else text, skip=skip, mute=mute, **times)
        except ValidationError as e:
            raise BadAnswer(f"回答不能是空的，也不能超过 {MAX_ANSWER} 个字") from e
        if not mute:
            return Command(resume={ask_id: got.model_dump()})
        skipped = got.model_copy(update={"mute": False}).model_dump()
        no_place = PlaceAnswer(status=DECLINED, **times).model_dump()
        return Command(resume={a["id"]: got.model_dump() if a["id"] == ask_id else no_place if a in wheres else skipped for a in waiting})

    async def _aregenerate_input(self, thread_id: str, text: str | None = None, thinking: bool | None = None,
                                 scheduled_by: dict | None = None) -> None:
        """最后一轮重新回答：去掉用户最后那句话之后的消息和这一轮的记录，从那句话接着跑。
        带 text 是用户改了最后那句话：先换成 text（消息 id 不变，还是同一轮，发送时间记成改的时候），再重新回答。标题不重起。
        thinking 是这次的思考开关：给了就按它（只改开关，发送时间不变），没给按那句话原来的；模型照对话定下的（Auto 的重新选）。
        是不是 Work 按这段对话定下的（见 history.work_mode）：11-11 以前的对话里那句话和对话对不上的，这时改成对话的。
        停在问用户那里的也可以（不回答了，改了那句话重来）：先把这一轮以停止收尾，再照常重新回答。
        scheduled_by：换上的 text 是定时任务的交代（见 _ascheduled_input）。用户自己改了定时任务触发的那句话，改完就是 TA 说的话，不再带这个标记。"""
        await self._adrop_suggestions(thread_id)
        cfg = self._cfg(thread_id)
        snap = await self.graph.aget_state(cfg)
        v = snap.values
        if v and snap.next:
            if self._status(snap, None)[0] != ASKING:
                raise NotIdle("上一轮没跑完，先重试")
            # 这一步里已经做完、还没落进检查点的工具结果（和提问一起发出的搜索）先随收尾写进去，下面才删得到
            await self._write_stop(thread_id)
            v = (await self.graph.aget_state(cfg)).values
        msgs = (v or {}).get("messages", [])
        start = history.last_human(msgs)
        if start < 0:
            raise NothingToRegenerate("还没有回答，不需要重新生成")
        human = msgs[start]
        mode = history.work_mode(v)
        work = mode if mode != bool(history.work_switch(human)) else None
        turns = [t for t in v.get("turns", []) if t.id != human.id]
        if text is not None:  # 带的图、文件留着：只改了字
            edited = [_said(text, history.thinking_switch(human) if thinking is None else thinking, history.attached_images(human),
                            history.attached_files(human), work=history.work_switch(human) if work is None else work, scheduled_by=scheduled_by,
                            id=human.id)]
        elif thinking is not None or work is not None:
            switches = {k: v for k, v in ((history.THINK, thinking), (history.WORK, work)) if v is not None}
            edited = [human.model_copy(update={"additional_kwargs": {**human.additional_kwargs, **switches}})]
        else:
            edited = []
        await self.graph.aupdate_state(
            cfg, {"messages": edited + [RemoveMessage(id=m.id) for m in msgs[start + 1:]], "turns": turns, history.WORK: mode}, as_node=START)
        await self.images.delete_images(history.owned_image_ids(msgs[start + 1:]))  # 这一轮原来画的图不要了（用户那句话带的图留着）
        if msgs[start].id:  # 这一轮原来写的文件各版也不要了（按这一轮记的，进程崩掉时写了、对话里没记下的也删）
            await self.files.delete_turn(thread_id, msgs[start].id)

    # ---- 改名、删除 ----

    @asynccontextmanager
    async def _ahold(self, thread_id: str, what: str = "改名、删除") -> AsyncIterator[None]:
        """占着这个线程的运行位做一件事（改标题、删除、恢复提问）：做的时候这个线程跑不起来，在跑的也不让做。"""
        key = self._run_key(thread_id)
        if not await self._aadmit(key):
            raise RunConflict(f"正在回答，答完或停下后才能{what}")
        try:
            yield
        finally:
            self.runs.release(key)

    async def _arename(self, thread_id: str, title: str) -> None:
        """改标题。没跑完的线程不能改：写一次状态，出错的那一步就丢了，重试接不上。停在问用户那里的也一样：写一次，等着的问题就丢了。"""
        cfg = self._cfg(thread_id)
        async with self._ahold(thread_id), self._suggest_mu:  # 和写推荐问题错开
            snap = await self.graph.aget_state(cfg)
            if snap.next:
                raise NotIdle(ASKING_FIRST if self._status(snap, None)[0] == ASKING else "上一轮没跑完，先重试")
            await self.graph.aupdate_state(cfg, {"title": title}, as_node=TURN_END)

    async def _aset_asks(self, thread_id: str, on: bool) -> None:
        """这段对话 Work 的轮还问不问（history.ASKS_OFF）。关是用户在问题卡上点“别再问了”（随回答走，见 _aanswer_input），这里主要是恢复。
        和改标题一样要等空闲：没跑完的、停在问用户那里的写一次状态，出错的那一步、等着的问题就丢了。"""
        cfg = self._cfg(thread_id)
        async with self._ahold(thread_id, "改这一项"), self._suggest_mu:  # 和写推荐问题错开
            snap = await self.graph.aget_state(cfg)
            if snap.next:
                raise NotIdle(ASKING_FIRST if self._status(snap, None)[0] == ASKING else "上一轮没跑完，先重试")
            off = not on
            if bool((snap.values or {}).get(history.ASKS_OFF)) != off:  # 已经是要的样子就不写
                await self.graph.aupdate_state(cfg, {history.ASKS_OFF: off}, as_node=TURN_END)

    async def _adelete(self, thread_id: str) -> None:
        """删掉整段对话（这个线程的所有检查点、运行记录、画的图、用户发的图和文件、沙箱里的工作区、整理记忆的记录），找不回来；记下的记忆、定时任务不删。
        检查点和归谁最后删，前面每一步都能再做一遍：哪一步出错就整个报错，对话还在、还归这个人，再删一次就删干净了。
        对话接口都先看这两样（见 main 的 _mine）：哪一样先没了，后面出错剩下的东西就没人删得到了。出错以后留着的对话里，图和文件可能已经没了。
        删运行记录之前先把账定下来（见 quota 的 Usage.settle）：出错的轮不算给用户是靠运行记录认的，运行记录一删，那几轮的花费会重新算进额度。
        删图没成功只记日志：留下的图没人指着，只占服务的磁盘。"""
        async with self._ahold(thread_id):
            snap = await self.graph.aget_state(self._cfg(thread_id))
            msgs = (snap.values or {}).get("messages", [])
            images = history.owned_image_ids(msgs)
            # 调过跑命令的才有沙箱里的工作区（看调用不看结果：跑到一半停下的也有）
            ran = any(tc["name"] == history.RUN_TOOL for m in msgs if isinstance(m, AIMessage) for tc in m.tool_calls)
            await self._adrop_suggestions(thread_id)
            self._titles.pop(thread_id, None)
            self.places.end_turn(thread_id)
            await self.quota.usage.settle(thread_id)
            await self.run_store.delete_thread(thread_id)
            await self.tasks.store.detach(thread_id)  # 挂在这段对话上的定时任务还在：下次跑时新开一段（见 app.scheduled）
            await self.memory.aforget_thread(thread_id)  # 只删整理过哪几轮的记录；从这段对话记下的记忆不动（记的是人）
            await self.images.delete_images(images)
            await self.files.delete_workspace(thread_id)  # 文件按工作区（现在就是这段对话）删，原件没删掉只记日志
            if ran and self.sandbox is not None:
                await self.sandbox.forget(thread_id)  # 没删掉只记日志：服务 7 天没用自己清
            await self.graph.checkpointer.adelete_thread(thread_id)
            await self.run_store.disown(thread_id)

    # ---- 推荐问题：这一轮结束之后再起 ----

    async def _want_suggestions(self, thread_id: str) -> None:
        """这一轮跑完、有回复，记下要起推荐问题。还占着运行位时记：记下之前用户插不进话，插进来的话一定会把它撤掉。"""
        snap = await self.graph.aget_state(self._cfg(thread_id))
        msgs = snap.values["messages"]
        start = history.last_human(msgs)
        reply = history.final_text(msgs[start + 1:])
        if snap.next or not reply:
            return
        async with self._suggest_mu:
            self._suggest_for[thread_id] = (msgs[start].id, history.text_of(msgs[start]), reply)

    async def _adrop_suggestions(self, thread_id: str) -> None:
        """用户又说了话（或重新回答、删掉对话）：上一轮还没起好的推荐问题不要了。正在写的写完才返回，之后不会再写。"""
        async with self._suggest_mu:
            self._suggest_for.pop(thread_id, None)

    async def _suggest(self, thread_id: str, out: Callable[[dict], None]) -> None:
        """起推荐问题。这时这一轮已经结束，页面拿到了最终状态，线程可以接着说；起好了写进这一轮的记录，再发一次状态。"""
        async with self._suggest_mu:
            job = self._suggest_for.get(thread_id)
        if job is None:
            return
        turn_id, question, reply = job
        out({"event": "phase", "data": {"phase": "suggest"}})  # 页面在回答下面先说一声正在起
        try:
            with costs.within(costs.Scope(thread_id=thread_id, turn_id=turn_id, owner=await self.run_store.owner_of(thread_id))):
                items = (await self.planner.suggest(question, reply))[:3]
        except Exception:  # noqa: BLE001  推荐问题失败就不给
            items = []
        async with self._suggest_mu:
            if self._suggest_for.get(thread_id) is not job:  # 起的时候用户又说了话：不要了
                return
            del self._suggest_for[thread_id]
            cfg = self._cfg(thread_id)
            snap = await self.graph.aget_state(cfg)
            turns = list(snap.values.get("turns", []))
            if not items or snap.next or not turns or turns[-1].id != turn_id:
                return
            turns[-1] = turns[-1].model_copy(update={"suggestions": items})
            await self.graph.aupdate_state(cfg, {"turns": turns}, as_node=TURN_END)
        out({"event": "state", "data": await self._astate_view(thread_id)})

    async def _aturn_of(self, thread_id: str) -> str | None:
        """这次执行跑的是哪一轮：用户最后那句话的消息 id（接着说、重试、重新回答都是）。"""
        msgs = (await self.graph.aget_state(self._cfg(thread_id))).values.get("messages", [])
        i = history.last_human(msgs)
        return msgs[i].id if i >= 0 else None

    # ---- 定时任务（见 app.scheduled） ----
    # 到点了没有请求进来，运行器自己发起：占位 → 把任务里那份交代写成新的一轮 → 交出执行，和排着的话接着发是一条路（_aaccept + _alaunch）。
    # 一个任务同一时间只欠一次：到点了（next_at 过了）就发起，交出去以后 next_at 挪到现在之后的下一次，
    # 服务停着、那段对话正忙时错过的几次并成这一次。

    def start_schedule(self) -> None:
        """服务启动时调：开到点发起的循环（在对话的事件循环里），每 config.SCHEDULED_TICK_S 秒看一次。"""
        if self._schedule_job is None:
            self._schedule_job = asyncio.run_coroutine_threadsafe(self._aschedule_loop(), aio.loop())

    def stop_schedule(self) -> None:
        if self._schedule_job is not None:
            self._schedule_job.cancel()
            self._schedule_job = None

    async def _aschedule_loop(self) -> None:
        while True:
            try:
                await self._afire_due()
            except Exception:  # noqa: BLE001  库一时连不上之类：下一拍再看
                log.exception("定时任务这一拍没看成")
            await asyncio.sleep(config.SCHEDULED_TICK_S)

    def fire_due(self) -> list[tuple[str, str]]:
        """看一遍到点了的任务，能发起的都发起（测试、手动用；服务里是循环在调）。返回每个到点的任务这一次怎么样（见 _afire）。"""
        return aio.run(self._afire_due())

    async def _afire_due(self) -> list[tuple[str, str]]:
        out = []
        for t in await self.tasks.store.due(self.tasks.now()):
            try:
                out.append((t.id, await self._afire(t)))
            except Exception:  # noqa: BLE001  这一个没发起（比如存检查点出错）：记一次没跑成、等下一次，别的任务照常
                log.exception("定时任务没发起：%s", t.id)
                await self.tasks.skipped(t, scheduled.FAILED)
                await self.tasks.finished(t.id, scheduled.FAILED)
                out.append((t.id, scheduled.FAILED))
        return out

    async def _afire(self, task: Task, *, manual: bool = False) -> str:
        """发起任务的这一次，返回怎么样了：
        RUNNING 交出去了；BUSY 那段对话正在跑，什么都没动（到点的下一拍再试）；
        GONE 占位的工夫任务被删了、停了、改了时间，或者它挂着的那段对话刚删了，没动（还到点的下一拍照新的来）；
        NO_CREDITS 这个人额度用完了、BLOCKED 那段对话卡着（上一轮出错了没重试、在等用户回答）：这一次不跑了，记一笔等下一次。
        manual 是用户点的“立即执行”：额度在接口那层查过了；不动原来定的下一次；BLOCKED 时不记。"""
        if not manual:
            try:
                await self.quota.check(Identity(task.owner, task.tier))
            except quota.Exhausted:
                await self.tasks.skipped(task, scheduled.NO_CREDITS)
                return scheduled.NO_CREDITS
        thread_id = task.thread_id or str(uuid.uuid4())
        key = self._run_key(thread_id)
        if not await self._aadmit(key):
            return BUSY
        skip = run = None
        try:
            now_ = await self.tasks.store.get(task.id)  # 查额度、占位的工夫，用户可能刚删了、停了、改了它，或者删了它挂着的那段对话
            if now_ is None or now_.thread_id != task.thread_id or (
                    not manual and (now_.status != scheduled.ACTIVE or now_.next_at != task.next_at)):
                skip = GONE
            else:
                try:
                    await self._ascheduled_input(now_, thread_id)
                except NotIdle:
                    skip = scheduled.BLOCKED
            if skip is None:
                await self.tasks.launched(now_, thread_id, manual=manual)
                try:
                    run = await self._alaunch(thread_id, SCHEDULED)
                except BaseException:
                    await self.tasks.store.patch(task.id, last_status=scheduled.FAILED)  # 不留着“在跑”
                    raise
        except BaseException:
            self.runs.release(key)
            raise
        if run is not None:
            self._scheduled[run] = task.id  # 交出去的执行这时还没开始跑（_alaunch 返回以后没让出过事件循环）
            return scheduled.RUNNING
        self.runs.release(key)
        if skip == scheduled.BLOCKED and not manual:
            await self.tasks.skipped(now_, scheduled.BLOCKED)
        return skip

    async def _ascheduled_input(self, task: Task, thread_id: str) -> None:
        """把任务这一次要跑的写进检查点（占着位调）：一般是任务里那份交代写成新的一轮；还没有这段对话就新开，归任务的主人，标题用任务的名字。
        这段对话上一轮没跑完、而且就是这个任务触发的那一轮（上一次出错了，或者被服务重启打断）：不另起一轮，那一轮重来——
        先以停止收尾，再和“重新回答”一样去掉那句话后面的（当时查到的到这次已经旧了），那句话换成任务现在的交代，触发时间记成现在。
        别的没跑完（用户自己的一轮出错了没重试、停在问用户那里）抛 NotIdle：不替用户收尾。"""
        snap = await self.graph.aget_state(self._cfg(thread_id))
        v = snap.values or {}
        msgs = v.get("messages", [])
        mark = {"task_id": task.id, "title": task.title}
        if v and snap.next:
            i = history.last_human(msgs)
            mine = i >= 0 and (history.scheduled_of(msgs[i]) or {}).get("task_id") == task.id
            if not mine or self._status(snap, None)[0] == ASKING:
                raise NotIdle("这段对话上一轮没跑完")
            await self._write_stop(thread_id)
            await self._aregenerate_input(thread_id, task.prompt, scheduled_by=mark)
            return
        new = not msgs
        option = task.option if new and task.option in {o.id for o in self.planner.catalog.offered(self.planner.usable)} else None
        await self._aaccept(thread_id, task.prompt, option=option, owner=task.owner if new else None, work=task.work, scheduled_by=mark,
                            title=task.title)

    async def _ascheduled_done(self, thread_id: str, run: str, status: str) -> None:
        """定时任务发起的那次执行结束了：结果记到任务上，那段对话多一条未读（用户点了停止的不算：人就在场）。
        记不上只打日志，不耽误这次执行收尾（退占位）。"""
        if (task_id := self._scheduled.pop(run, None)) is None:
            return
        result = {ERROR: scheduled.FAILED, STOPPED: scheduled.STOPPED}.get(status, scheduled.OK)
        try:
            await self.tasks.finished(task_id, result)
        except Exception:  # noqa: BLE001
            log.warning("定时任务这一次的结果没记上：%s", task_id, exc_info=True)
        if result == scheduled.STOPPED:
            return
        try:
            await self._anotify(thread_id, result == scheduled.OK)
        except Exception:  # noqa: BLE001
            log.warning("定时任务这一次的未读没记上：%s", task_id, exc_info=True)

    async def _anotify(self, thread_id: str, ok: bool) -> None:
        """这段对话多一条未读，带一条通知（页面在左栏标数字、右上角弹出来，见 run_store 的 notify）：
        任务名用触发这一轮的那句话上记的（就是对话里这一轮上面显示的）；跑成了的带回答的开头，没跑成、被内容审核拦下的不带（页面说没跑成）。"""
        msgs = ((await self.graph.aget_state(self._cfg(thread_id))).values or {}).get("messages", [])
        i = history.last_human(msgs)
        mark = (history.scheduled_of(msgs[i]) if i >= 0 else None) or {}
        last = next((m for m in reversed(msgs[i + 1:]) if isinstance(m, AIMessage)), None)
        ok = ok and last is not None and not history.blocked_reason(last)
        await self.run_store.notify(thread_id, {"title": mark.get("title", ""), "ok": ok,
                                                "text": opening(history.final_text(msgs[i + 1:])) if ok else ""})

    async def _arecover_scheduled(self) -> None:
        """服务启动时：任务上还记着“在跑”、其实上个进程已经没了的，记成被打断（LOST），马上补一次（见 ScheduledTasks.finished）。"""
        for t in await self.tasks.store.running():
            if t.id not in self._scheduled.values():
                await self.tasks.finished(t.id, scheduled.LOST)

    def read_schedule(self, who: Identity, text: str) -> dict:
        """表单里“自定义”的时间：一句话（“每个月的第 1、15 号早上 9 点半”）由轻量模型转成 cron 或一个时间点，照样过校验。
        返回 {cron, once_at, schedule（给人看的一行）, next_at}。没看懂、转出来的不合规矩抛 scheduled.TaskError（UNCLEAR_TIME，
        太密、已经过了的照原来的 code）；模型出错照常抛出。这次调模型记在这个人名下，不扣他的额度（同起标题）。"""
        return aio.run(self._aread_schedule(who.sub, text))

    async def _aread_schedule(self, owner: str, text: str) -> dict:
        with costs.within(costs.Scope(owner=owner)):
            cron, run_at = await self.planner.when(" ".join(text.split()), say_when(self.tasks.now()))
        if bool(cron) == bool(run_at):
            raise scheduled.TaskError(scheduled.UNCLEAR_TIME, text)
        try:
            return self.tasks.schedule_of(cron or None, run_at or None)
        except scheduled.TaskError as e:  # 写法不对的算没看懂；太密、时间过了、永远到不了的，照实告诉用户
            raise scheduled.TaskError(scheduled.UNCLEAR_TIME, text) if e.code in (rule.BAD_CRON, rule.BAD_TIME) else e from None

    def scheduled_tasks(self, who: Identity) -> list[dict]:
        """这个人的定时任务，先建的在前。"""
        return [self.tasks.view(t) for t in aio.run(self.tasks.of_owner(who.sub))]

    def create_task(self, who: Identity, **fields: Any) -> dict:
        """接口建的任务：第一次跑时新开一段对话。校验不过抛 scheduled.TaskError；选的模型没有抛 UnknownModel。"""
        if (option := fields.get("option")) is not None and option not in {o.id for o in self.planner.catalog.offered(self.planner.usable)}:
            raise UnknownModel(f"没有这个模型可选：{option}")
        return self.tasks.view(aio.run(self.tasks.create(who.sub, who.tier, **fields)))

    def update_task(self, who: Identity, task_id: str, **fields: Any) -> dict:
        return self.tasks.view(aio.run(self.tasks.update(who.sub, task_id, tier=who.tier, **fields)))

    def delete_task(self, who: Identity, task_id: str) -> None:
        aio.run(self.tasks.delete(who.sub, task_id))

    def run_task(self, who: Identity, task_id: str) -> dict:
        """立即执行一次（不动原来定的下一次；停着的、跑完了的也能执行），返回任务现在的样子（thread_id 是跑在哪段对话里）。
        没跑起来抛 scheduled.TaskError：那段对话正在跑（THREAD_BUSY）、卡着（上一轮没跑完、在等回答，THREAD_STUCK）、
        点的工夫任务刚被改了（CHANGED）、没有这个任务（NOT_FOUND）。"""
        return aio.run(self._arun_task(who, task_id))

    async def _arun_task(self, who: Identity, task_id: str) -> dict:
        t = await self.tasks.get(who.sub, task_id)
        if t.tier != who.tier:
            t = await self.tasks.store.patch(t.id, tier=who.tier) or t
        got = await self._afire(t, manual=True)
        if got in (BUSY, GONE):  # GONE：点的工夫任务被删了（下面取任务时报没有），或者它挂着的那段对话刚删了
            await self.tasks.get(who.sub, task_id)
            raise scheduled.TaskError(scheduled.THREAD_BUSY if got == BUSY else scheduled.CHANGED)
        if got == scheduled.BLOCKED:
            raise scheduled.TaskError(scheduled.THREAD_STUCK)
        return self.tasks.view(await self.tasks.get(who.sub, task_id))

    # ---- 运行记录 ----

    def recover(self) -> None:
        """服务启动时调一次：上次进程退出时还在排队、在跑的运行，记成没跑完（lost）。单进程部署，这时它们都已经停了。
        其中定时任务发起的，任务上也记一笔，马上再补一次（见 _arecover_scheduled）。"""
        if n := aio.run(self.run_store.sweep()):
            log.warning("上次进程退出时有 %d 次运行没跑完，已记成 lost", n)
        aio.run(self._arecover_scheduled())

    def thread_runs(self, thread_id: str) -> list[dict]:
        """这段对话的每次运行，新的在前。堆栈不给：网站没有登录，堆栈只留在库和日志里。"""
        return [_public(r) for r in aio.run(self.run_store.for_thread(thread_id))]

    def recent_runs(self, status: str | None = None, limit: int = 50) -> list[dict]:
        """所有对话里最近的运行，可以只看某个状态（比如 error）。"""
        return [_public(r) for r in aio.run(self.run_store.recent(status, limit))]

    # ---- 长期记忆 ----

    def dream(self) -> dict | None:
        """整理一次记忆（见 graph.research.memory）：每段对话按检查点里最新的状态。已经有一次在整理返回 None。
        在整理记忆的后台线程里调：检查点从对话的事件循环里取，记忆的读写用 Store 的同步方法（见 graph.research.graph）。
        记忆按人分：只整理归了登录用户的对话，游客的（登录后并进账号再整理）、没归谁的老对话不整理。"""
        def threads():
            for t in aio.run(thread_ids(self.graph.checkpointer)):
                owner = aio.run(self.run_store.owner_of(t))
                if owner is not None and not is_guest_sub(owner):
                    yield t, aio.run(self.graph.aget_state(self._cfg(t))).values, owner
        return self.memory.dream(threads(), self.planner.dream)

    def memory_view(self, who: Identity) -> dict:
        """记忆页：这个人的全部记忆（游客没有）、上次整理（没整理过为 None）、下次按时整理的时间（关了按时整理为 None）。"""
        items = [] if who.guest else self.memory.items(who.sub)
        return {"memories": [_memory_row(m) for m in items],
                "last_dream": self.memory.last_dream(),
                "next_dream": next_at(datetime.now(UTC), config.DREAM_HOUR).isoformat() if config.DREAM_ENABLED else None}

    def edit_memory(self, who: Identity, item_id: str, text: str) -> dict | None:
        """记忆页改一条（游客没有记忆）。不是这个人的、没有这一条是 None。"""
        item = None if who.guest else self.memory.edit(who.sub, item_id, text)
        return _memory_row(item) if item else None

    def forget_memory(self, who: Identity, item_id: str) -> bool:
        """记忆页删一条。不是这个人的、没有这一条是 False。"""
        return not who.guest and self.memory.forget(who.sub, item_id)

    # ---- 视图 ----

    async def _astate_view(self, thread_id: str) -> dict:
        # 先查占位再取检查点：执行恰好在两步之间结束时，拿到的是结束后的检查点
        run = self.runs.get(self._run_key(thread_id))
        snap = await self.graph.aget_state(self._cfg(thread_id))
        v = snap.values
        if not v:  # 线程 id 交出去之前用户的话已经写进检查点（见 _aaccept），没有就是不存在
            return {"thread_id": thread_id, "exists": False}
        status, failure = self._status(snap, run)
        return {
            "thread_id": thread_id,
            "exists": True,
            "scenario": "research",
            "status": status,
            # 没跑完（failed）时的说法和类别（见 _failure）；别的状态都是 None
            "error": failure and failure[1],
            "error_kind": failure and failure[0],
            # 这段对话是不是工作任务（新建时定下，整段不变，见 history.work_mode）：页面按它显示，发话时不用再带
            "work": history.work_mode(v),
            # 对话定下的模型（选项的 id）：没定的（还没开始的老对话）、定的那个停了的，下一轮起是 Auto
            "model": self.planner.catalog.plan(v.get("option"), None, self.planner.usable).option,
            # 正在跑的这一轮主 agent 用的模型（"厂商:模型"，见 _models）：页面在这一轮的记录出来之前按它显示。没在跑、还没定下的是 None
            "model_ref": self._models.get(thread_id) if status in ("queued", "running") else None,
            "stopping": bool(run and run.get("stopping")),
            # 这一轮已经结束、推荐问题还在起（见 _suggest）：页面打开时接上事件流等着，起好了会再来一次状态
            "suggesting": status == "idle" and thread_id in self._suggest_for,
            "title": self._title_of(thread_id, v),
            "updated_at": snap.created_at,  # 最近一个检查点的时间
            "running_since": run.get("since") if run else None,  # 这次执行开始的时间；排队中、没在跑为 None
            "turns": jsonable(conversation(v["messages"], v.get("turns", []))),
            "sources": {s.id: s.model_dump() for s in collect_sources(v["messages"])},
            # 排着、这一轮答完就发的话（{text, created_at, updated_at}）；没有为 None
            "queued_message": await self.run_store.get_pending(thread_id, QUEUE),
            "guided_message": await self.run_store.get_pending(thread_id, GUIDE),  # 等着插进这一轮的话，同上
            # 停下来等用户回答的问题（asking 时才有）：{id, call_id, question, options}，回答时带上 id（见 answer）；
            # call_id 对得上这一轮条目里那条 pending 的 ask。一次问了两个的先给前一个
            "ask": _asks(snap)[0] if status == ASKING else None,
            # 用户在问题卡上点过“别再问了”（见 history.ASKS_OFF）：这段对话 Work 的轮也不出问题卡，页面给“恢复提问”
            "asks_off": bool(v.get(history.ASKS_OFF)),
        }

    def failure_detail(self, thread_id: str) -> str | None:
        """开发用（评测、命令行）：这一轮没跑完时检查点里记的异常原文（页面上只给转写过的，见 _failure）。没跑完以外的、没有错误记录的是 None。"""
        snap = aio.run(self.graph.aget_state(self._cfg(thread_id)))
        return _real_error(_errors(snap)) if snap.next else None

    def _title_of(self, thread_id: str, v: dict) -> str:
        """页面上的标题：起好写进 state 的、改过的；没写进去的先看这次进程里起好过的（见 _titles），
        都没有用这段对话的第一句，和起标题时用原话一样整理（见 agent.fallback_title：纯数字、纯符号的叫“新对话”）。"""
        return v.get("title") or self._titles.get(thread_id) or fallback_title(history.requests(v["messages"])[0])

    def _summary_of(self, snap) -> dict:
        """线程列表里一行从检查点算出来的那几项：标题、没在跑时的状态、是不是工作任务、是不是定时任务的对话、最近一个检查点的时间。
        只看这一个检查点（和它上面记着的出错、等着的问题），算好的记在库里，下次取列表时最新的检查点还是 checkpoint 这个就直接用（见 _alist_threads）。
        这里哪一项的算法改了（包括它调的 history.work_mode、scheduled_here、fallback_title、_status），把 SUMMARY_V 加一。
        标题只记 state 里的；没写进 state 的先看这次进程里起好的（_titles，不记进库），再没有用 fallback（第一句话整理出来的，有标题的不算）。"""
        v = snap.values
        title = v.get("title")
        return {
            "v": SUMMARY_V,
            "checkpoint": snap.config["configurable"]["checkpoint_id"],
            "title": title,
            "fallback": None if title else fallback_title(history.requests(v["messages"])[0]),
            "status": self._status(snap, None)[0],
            "work": history.work_mode(v),
            "scheduled": history.scheduled_here(v.get("messages") or []),  # 定时任务的对话：页面在左栏用钟的图标
            "updated_at": snap.created_at,
        }

    def _row(self, thread_id: str, s: dict, run: dict | None, pinned_at: str | None, unread: dict | None) -> dict:
        """线程列表里的一行：s 是从检查点算出来的那几项（见 _summary_of），run 是这段对话占着位的执行（没有是 None）。"""
        return {
            "thread_id": thread_id,
            # 和 _title_of 一样的先后：起好写进 state 的、这次进程里起好的、第一句话
            "title": s["title"] or self._titles.get(thread_id) or s["fallback"],
            "status": ("queued" if run["queued"] else "running") if run else s["status"],
            "work": s["work"],
            "scheduled": s["scheduled"],
            "updated_at": s["updated_at"],
            "running_since": run.get("since") if run else None,
            "pinned_at": pinned_at,
            **(unread or NO_UNREAD),
        }

    async def _asummary_view(self, thread_id: str) -> dict | None:
        """线程列表里的一行（单取：改名、置顶、标已读以后还给页面的）：标题、状态、是不是工作任务、是不是定时任务的对话、置顶的时间（没置顶是 None）、
        几条未读和最近那条通知（没有是 0、None）。不存在返回 None。单取的直接读检查点，不看记着的。
        未读要在状态之后取（见 _alist_threads）。"""
        owner = await self.run_store.owner_of(thread_id)
        pins = await self.run_store.pins_of(owner) if owner else {}
        run = self.runs.get(self._run_key(thread_id))  # 先查占位再取检查点，同 _astate_view
        snap = await self.graph.aget_state(self._cfg(thread_id))
        if not snap.values:
            return None
        unread = await self.run_store.unread_of(owner) if owner else {}
        return self._row(thread_id, self._summary_of(snap), run, pins.get(thread_id), unread.get(thread_id))

    async def _alist_threads(self, who: Identity) -> list[dict]:
        """who 的对话，最近有动静的在前。
        每行从检查点算出来的那几项记在库里（见 _summary_of、run_store 的模块说明），这里不用把每段对话的检查点都读一遍：
        先一次查齐各段最新检查点的编号，记着的是按这个检查点算的就直接用；对不上的（对话有了新的检查点、还没记过、版本旧了）才读检查点重算、记下。
        正在跑、排队的不看记着的也不记：这时检查点和它上面记的东西正在变。跑一轮开头还会把记着的作废（见 _run_turn）。"""
        rows = await self.run_store.rows_of(who.sub)
        latest = await latest_checkpoints(self.graph.checkpointer, [r["thread_id"] for r in rows])
        out: list[dict] = []
        for r in rows:
            t = r["thread_id"]
            if t not in latest:  # 归了人、检查点里却没有（第一句话没写成）：不存在
                continue
            run = self.runs.get(self._run_key(t))  # 先查占位再取检查点，同 _astate_view
            s = r["summary"]
            kept = run is None and bool(s) and s.get("v") == SUMMARY_V and s.get("checkpoint") == latest[t]
            if not kept or self.check_summaries:
                snap = await self.graph.aget_state(self._cfg(t))
                if not snap.values:
                    continue
                fresh = self._summary_of(snap)
                if kept:  # 只在测试里走到（check_summaries）：检查点没变，记着的就该和现算的一样
                    if fresh["checkpoint"] == s["checkpoint"] and fresh != s and self.runs.get(self._run_key(t)) is None:
                        raise AssertionError(f"线程列表里记着的和检查点对不上：{t} 记着 {s}，现算 {fresh}")
                else:
                    s = fresh
                    if run is None:
                        await self._akeep_summary(t, r["rev"], s)
            out.append(self._row(t, s, run, r["pinned_at"], None))
        # 未读在各行的状态都看完以后取：定时任务那一轮是先记未读、再退占位（见 _work），这样哪一行看到它跑完了，这次列表里就一定带着它的未读。
        # 反过来先取的话，取完未读到看状态之间跑完的那一轮会是“跑完了、没有未读”，页面要等下一次取列表（空闲时 20 秒）才弹通知
        unread = await self.run_store.unread_of(who.sub)
        for x in out:
            x.update(unread.get(x["thread_id"]) or NO_UNREAD)
        return sorted(out, key=lambda x: x["updated_at"] or "", reverse=True)

    async def _akeep_summary(self, thread_id: str, rev: int, summary: dict) -> None:
        """把列表里这一行算好的记下。没记上只打日志：列表照常给，下次再算。"""
        try:
            await self.run_store.put_summary(thread_id, rev, summary)
        except Exception:  # noqa: BLE001
            log.warning("列表里算好的那几项没记上：%s", self._run_key(thread_id), exc_info=True)

    async def _atouch(self, thread_id: str) -> None:
        """这段对话要跑一轮了：列表里记着的那几项作废（见 _run_turn）。没作废成只打日志，不耽误这一轮。"""
        try:
            await self.run_store.touch(thread_id)
        except Exception:  # noqa: BLE001
            log.warning("列表里记着的那几项没作废：%s", self._run_key(thread_id), exc_info=True)

    @staticmethod
    def _status(snap, run: dict | None) -> tuple[str, tuple[str, str] | None]:
        """queued / running：有执行占着位；idle：这一轮跑完了（或停下了），可以接着说；
        asking：没在跑，这一轮停下来问用户了（见 app.tools.ask_user），回答或跳过以后接着跑，不是失败；
        failed：没在跑、也没跑完——某一步抛了错（错误记在检查点里），或执行半路被掐断（进程重启，没有错误记录）。
        和提问同一步的别的工具抛了错也算 failed：重试时出错的重做，问题会再问出来；重做成了，就是 asking（见 _errors）。
        failed 时第二项是原因的（类别，说法）（见 _failure_of），别的状态是 None。"""
        if run is not None:
            return ("queued" if run["queued"] else "running"), None
        if not snap.next:
            return "idle", None
        errors = _errors(snap)
        if not errors and _asks(snap):
            return ASKING, None
        return "failed", _failure_of(errors)


async def _settle(mark: object) -> None:
    """点停止以后，等这一轮起的任务都结束了再看检查点。停止正好落在 LangGraph 收尾的时候，收尾的任务跟着被取消（取消一个任务会取消它正在等的），
    但它已经交出去的存检查点还在后台跑：不等它，读到的是开跑前的检查点，再写停止就把这一轮冲掉了。
    这一轮起的任务认上下文里的标记（_TURN）：任务建的时候复制当时的上下文，一层层往下都带着。"""
    me, end = asyncio.current_task(), time.monotonic() + SETTLE_S
    while left := [t for t in asyncio.all_tasks() if t is not me and t.get_context().get(_TURN) is mark]:
        if time.monotonic() >= end:
            log.warning("点了停止，这一轮还有 %d 个任务没结束，不等了", len(left))
            return
        await asyncio.wait(left, timeout=end - time.monotonic())


def _with_results(messages: list[BaseMessage], results: list[BaseMessage]) -> list[BaseMessage]:
    """补上还没进检查点的工具结果：同一个调用只要一条，已经有的不再加。"""
    out, have = list(messages), {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    for m in results:
        if isinstance(m, ToolMessage) and m.tool_call_id not in have:
            out.append(m)
            have.add(m.tool_call_id)
    return out


def _pending_messages(tup) -> list[BaseMessage]:
    """检查点上记着、还没并进 state 的写入里的消息：子 agent 被取消的那一步里已经做完的任务写的（它的 state 不经过 get_state，自己取）。"""
    out: list = []
    for _, channel, value in (tup.pending_writes or []) if tup else []:
        if channel == "messages":
            out += value if isinstance(value, list) else [value]
    return [m for m in out if isinstance(m, BaseMessage)]


def _put(out: Callable[[dict], None], evs: list[dict]) -> None:
    for ev in evs:
        out(ev)
