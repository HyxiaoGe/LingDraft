"""对话场景的接口层运行器：接用户的话、把 agent 的流变成页面的事件、停止和重新生成、线程状态视图。

回答进行中用户又说的话不打断这一轮：排着（存进 run_store 的 pending_messages），这一轮答完或停下就接着发成新的一轮（见 queue、_aended）。
没发出去的话最多一句。排着的话也可以插进正在跑的这一轮（见 guide）：主 agent 下一次调模型之前放进对话；这一轮没再调模型就结束了的，退回去排着。
执行在对话的事件循环里异步地跑（见 app.aio），点停止马上取消这一轮（见 ResearchRunner.stop）。
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
from psycopg_pool import PoolTimeout

from app import aio, config, costs, quota
from app.api.drafts import DraftTracker
from app.api.run_store import ERROR, GUIDE, QUEUE, STOPPED, SUCCESS, MemoryRunStore, Pending, PostgresRunStore, RunStore
from app.api.runner import BaseRunner, RunConflict, _sse_text, jsonable, log
from app.files.service import NOT_HERE, FileService, NotSendable
from app.files.store import FileRow, FileStore, MemoryFileStore, PostgresFileStore
from app.graph.research import history
from app.graph.research.agent import TURN_END, ChatContext, fallback_title, now, stop_turn
from app.graph.research.graph import NO_POSTGRES, build_graph, make_serde, postgres_pool, thread_ids
from app.graph.research.memory import MemoryBook, next_at
from app.identity import Identity, is_guest_sub
from app.graph.research.sources import collect_sources, subagent_messages
from app.graph.research.state import AttachedFile, AttachedImage, Turn
from app.research_llm import THINKING_MS, FakeResearchPlanner, LiveResearchPlanner, ResearchPlanner, ThinkingClock
from app.model_guard import ContentBlocked, Interrupted, ModelUnavailable
from app.tools.gateway import IMAGE_ID, GatewayError


class NothingToRetry(Exception):
    """线程没有失败，不需要重试。"""


class NotIdle(Exception):
    """上一轮没跑完：这时不能接着说，也不能重新生成。"""


class AlreadyPending(Exception):
    """回答时又说的话已经有一句没发出去（排着或等着插进这一轮）：没发出去的话最多一句，先撤回改好再发。"""


class NothingToRegenerate(Exception):
    """还没有一轮回答。"""


class UnknownModel(Exception):
    """新对话选的模型不在能选的里面（没有这个、停用了、没配 key），或者想给已经开始的对话换模型。"""


CUT_SHORT = "上次运行没跑完就停了"
# 这一轮没跑完的原因分几类给页面（状态里的 error_kind，事件流 error 事件的 kind），页面按类别说下一步能做什么（见 _failure）：
# model：模型服务不可用；blocked：内容审核没通过；cut_short：半路停了、没有错误记录（进程重启）；tool：工具服务（网关）出错；internal：别的
FAILURE_MODEL, FAILURE_BLOCKED, FAILURE_CUT, FAILURE_TOOL, FAILURE_INTERNAL = "model", "blocked", "cut_short", "tool", "internal"
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


def _said(text: str, thinking: bool | None = None, images: Sequence[AttachedImage] = (), files: Sequence[AttachedFile] = (), **kw: Any) -> HumanMessage:
    """用户的话。thinking 是发的时候输入框旁边的思考开关（没给按选项的默认，见 model_catalog.Catalog.plan）；images 是这句话带的图，files 是带的文件。"""
    extra: dict[str, Any] = {history.THINK: thinking} if thinking is not None else {}
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
    主 agent 写文件的工具调用，参数边收边解出来，同一批发给页面（draft，见 api.drafts）；子 agent 没有写文件的工具。"""

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
        out = [{"event": "delta", "data": d} for d in self._deltas] + [{"event": "draft", "data": d} for d in self._drafts.take()]
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
    """对话按轮：用户的话，然后这一轮里模型的思考、正文、工具步骤按先后排，被内容审核拦下时最后是一条 blocked；
    用户插进这一轮的话是一条 guide，排在它放进对话的地方。
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
                   "images": [i.model_dump() for i in history.attached_images(m)],
                   "files": [f.model_dump() for f in history.attached_files(m)],
                   "record": {**t.model_dump(exclude={"id"}), "credits": costs.credits(t)} if t else None}
            out.append(cur)
        elif cur is not None and isinstance(m, AIMessage):
            cur["items"] += _items_of(m, results)
    return out


def _items_of(m: AIMessage, results: dict[str, ToolMessage]) -> list[dict]:
    """一条模型消息 → 条目：思考、正文、工具步骤；被内容审核拦下的是一条 blocked。"""
    if reason := history.blocked_reason(m):
        return [{"type": "blocked", "id": m.id, "reason": reason}]
    items: list[dict] = []
    if thinking := history.thinking_of(m):
        items.append({"type": "thinking", "id": m.id, "text": thinking, "ms": m.response_metadata.get(THINKING_MS)})
    if text := history.text_of(m).strip():
        items.append({"type": "text", "id": m.id, "text": text})
    for tc in m.tool_calls:
        result = results.get(tc["id"])
        if step := history.step_of(tc, result):
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


def _real_error(errors: list[str]) -> str | None:
    return next((e for e in errors if e.partition("(")[0] != asyncio.CancelledError.__name__), None)


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


def _memory_row(m: dict) -> dict:
    """记忆页上的一条：thread_id 是记下（或最后改它）的那段对话，页面拿它点回去；对话删了、整理时没记（很早的几条）就点不了。"""
    return {k: m.get(k) for k in ("id", "text", "created_at", "updated_at", "thread_id")}

class ResearchRunner(BaseRunner):
    """对话场景的运行器。执行都在对话的事件循环里（见 app.aio）：每次执行是一个任务，同时最多跑 workers 个，多出来的排队；
    点停止就取消正在跑的那一轮，正在等的模型、工具马上断开。下面这些 _ 开头的协程和占位、队列的状态都只在那个循环里用。
    接口层在线程池里调不带 a 的同步方法：活交给事件循环做、等结果；事件流照旧从事件日志里读（见 BaseRunner._stream）。"""

    api_prefix = "/api/research/threads"

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
        self.images = self.planner.gateway  # 画好的图存在 image-service 里，按编号取、删（见 app.tools.gateway）
        # 用户发的文件：原件在数据卷上，文字和文件表在运行记录的库里；在这里起后台的解析、清理（见 app.files.service）
        self.files = files or FileService(open_files(self.run_store), config.FILES_DIR, isolated=config.FILE_PARSE_ISOLATED,
                                          timeout=config.FILE_PARSE_TIMEOUT_S)
        aio.run(self.files.start())
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
    # images 是这句话带的图（先用 upload_image 一张张传上去；没开始这一轮要自己 discard_images，见 api.main）；
    # files 是带的文件的编号（先用 upload_file 传上来、解析好，发出去时绑到这段对话；不能发的抛 NotSendable，什么都没写）

    def start(self, thread_id: str, text: str, option: str | None = None, thinking: bool | None = None, owner: str | None = None,
              images: Sequence[AttachedImage] = (), files: Sequence[str] = ()) -> AsyncIterator[str]:
        """owner 是这段新对话归谁（见 app.identity 和 run_store 的 thread_owners）；命令行、评测不传，对话就没归谁（页面上谁都看不到）。"""
        return self._stream(aio.run(self._asubmit(thread_id, lambda: self._aaccept(thread_id, text, thinking, option, owner, images, files), "new")))

    def say(self, thread_id: str, text: str, thinking: bool | None = None, images: Sequence[AttachedImage] = (),
            files: Sequence[str] = ()) -> AsyncIterator[str]:
        return self._stream(aio.run(self._asubmit(thread_id, lambda: self._aaccept(thread_id, text, thinking, images=images, files=files), "say")))

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

    def queue(self, thread_id: str, text: str, thinking: bool | None = None) -> dict | None:
        return aio.run(self._aqueue(thread_id, text, thinking))

    def guide(self, thread_id: str) -> dict | None:
        return aio.run(self._aguide(thread_id))

    def accept(self, thread_id: str, text: str, thinking: bool | None = None, option: str | None = None, owner: str | None = None) -> None:
        aio.run(self._aaccept(thread_id, text, thinking, option, owner))

    def retry_input(self, thread_id: str) -> None:
        aio.run(self._aretry_input(thread_id))

    def regenerate_input(self, thread_id: str, text: str | None = None, thinking: bool | None = None) -> None:
        aio.run(self._aregenerate_input(thread_id, text, thinking))

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

    def rename(self, thread_id: str, title: str) -> None:
        aio.run(self._arename(thread_id, title))

    def delete(self, thread_id: str) -> None:
        aio.run(self._adelete(thread_id))

    def state_view(self, thread_id: str) -> dict:
        return aio.run(self._astate_view(thread_id))

    def image(self, image_id: str) -> tuple[bytes, str] | None:
        """页面来取的图：(内容, MIME 类型)，经网关从 image-service 取（见 app.tools.gateway）。
        不查是不是这段对话的：图画好、页面就去取，这一轮还没跑完，检查点里还没有它（运行结束才写，见 DURABILITY）；
        服务只给灵稿的图，编号是随机的 uuid。编号格式不对（比如以前灵稿自己存的图）、服务里已经没有的是 None。网关、服务出问题照常抛出。"""
        if not IMAGE_ID.fullmatch(image_id):
            return None
        return aio.run(self.images.image(image_id))

    def summary_view(self, thread_id: str) -> dict | None:
        return aio.run(self._asummary_view(thread_id))

    def list_threads(self, who: Identity) -> list[dict]:
        return aio.run(self._alist_threads(who))

    def pin(self, thread_id: str, pinned: bool) -> dict | None:
        """置顶或取消（运行中也行：不碰检查点），返回列表里的这一行。"""
        return aio.run(self._apin(thread_id, pinned))

    async def _apin(self, thread_id: str, pinned: bool) -> dict | None:
        await self.run_store.pin(thread_id, pinned)
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
        命令行、评测和测试用；点停止要自己先占位（见 runs.admit）。返回 STOPPED 或 None。"""
        return aio.run(self._run_turn(thread_id, inp, on_event or (lambda ev: None)))

    def suggest(self, thread_id: str) -> list[dict]:
        """这一轮结束之后起推荐问题（见 _suggest），返回发给页面的事件。和 run_turn 一样给命令行、评测和测试用。"""
        evs: list[dict] = []
        aio.run(self._suggest(thread_id, evs.append))
        return evs

    def stop(self, thread_id: str) -> bool:
        """让这个线程正在跑（或排队）的这一轮停下；没有在跑的返回 False。
        先记下要停，再到事件循环里取消正在跑的那一轮（见 _run_turn）；排队的轮到时看到要停，直接收尾。"""
        if not self.runs.request_stop(self._run_key(thread_id)):
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

    async def _asubmit(self, thread_id: str, make_input: Callable[[], Awaitable[None]], kind: str) -> str:
        """占位 → 把这一轮的输入写进检查点（校验在这一步，不通过就退位）→ 交出执行，返回事件日志的 id。占着位，别的请求改不了这个线程。
        kind 是这次执行怎么来的，记进运行记录。"""
        key = self._run_key(thread_id)
        if not await self._aadmit(key):
            raise RunConflict("这一步还在处理，等它完成后再提交")
        try:
            await make_input()
            return await self._alaunch(thread_id, kind)
        except BaseException:
            self.runs.release(key)
            raise

    async def _alaunch(self, thread_id: str, kind: str) -> str:
        """开一段新的事件日志、记一行运行记录，交出执行。执行结束才退掉占位：浏览器断开、没人读都照常跑完。"""
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
            self._spawn(self._work(thread_id, run))
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

    async def _work(self, thread_id: str, run: str) -> None:
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
                    status = await self._run_turn(thread_id, None, out) or SUCCESS
                except Exception as e:  # noqa: BLE001
                    error = e
                    log.exception("执行出错：%s 运行 %s", key, run)
                    out({"event": "error", "data": _error_event(e)})
                finally:
                    await self._afinish(run, status, error)
                    await self._aended(thread_id, status)
                try:
                    out({"event": "state", "data": await self._astate_view(thread_id)})
                    await self._suggest(thread_id, out)
                except Exception as e:  # noqa: BLE001
                    log.exception("这一轮结束后出错：%s 运行 %s", key, run)
                    out({"event": "error", "data": _error_event(e)})
        finally:
            self._inflight -= 1
            self.events.close(run)

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

    async def _run_turn(self, thread_id: str, inp: Any, out: Callable[[dict], None]) -> str | None:
        """跑一轮，事件交给 out。用户点了停止：排队时就点的不跑，直接收尾；跑着的马上取消（见 stop），正在等的模型、工具一起断开，
        已经流出来的正文、做完的工具结果留着（取消时照常存检查点，见 DURABILITY）。停下的返回 STOPPED（记进运行记录）。"""
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
        turn = asyncio.get_running_loop().create_task(self._stream_graph(thread_id, inp, out, buf, tasks, done), context=ctx)
        self._running[thread_id] = turn
        try:
            await turn
        except asyncio.CancelledError:
            me = asyncio.current_task()
            if (me is not None and me.cancelling()) or not turn.cancelled():  # 不是点了停止（比如进程要退出）：照常往外抛
                raise
            await _settle(mark)
            _put(out, buf.flush())
            if (await self.graph.aget_state(self._cfg(thread_id))).next:
                await self._write_stop(thread_id, buf, tasks, done)
                out({"event": "node", "data": {"node": TURN_END}})
                return STOPPED
            # 点停止时这一轮刚好跑完了（检查点里已经是答完的样子）：算答完
        finally:
            self._running.pop(thread_id, None)
            self._models.pop(thread_id, None)
        self._titles.pop(thread_id, None)  # 答完了：起好的标题随调模型写进 state 了
        await self._want_suggestions(thread_id)
        return None

    async def _stream_graph(self, thread_id: str, inp: Any, out: Callable[[dict], None], buf: StreamBuffer, tasks: dict[str, str],
                            done: dict[str | None, list[ToolMessage]]) -> None:
        """agent 的流 → 页面事件。messages 只转发 model 节点的（快模型在别的节点里调用，不算）；
        custom 是中间件写的进度（step，做完的带来源）、标题（title）和阶段（phase：起标题、等模型这些空档在等什么）；updates 里改了 state 的节点做完发 node（页面不靠它）。
        模型生成到一半断了、正在再调一次（restart，见 agent.InterruptedRetryMiddleware）：带上刚才流出来的那条消息的 id，页面去掉它；没流出东西是 None。
        子 agent（task 工具里跑的另一个 agent）的流要开 subgraphs 才有，按命名空间认出是哪个 task 调用派的：
        它的片段、进度、阶段带上 task（调用 id），页面放进那张子任务卡；它的节点做完不发 node。它的片段 langgraph_node 也是 model，只按节点名分不开。
        做完的 step 带着工具结果（result，不发给页面），记进 done：点停止时它那一步可能还没提交，收尾时补上（见 _write_stop）。"""
        ctx = ChatContext(take_guide=lambda: self._take_guide(thread_id), title=self._titles.get(thread_id),
                          owner=await self.run_store.owner_of(thread_id), files=self.files, thread_id=thread_id)
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
                if update:  # 没改 state 的中间件节点不发
                    out({"event": "node", "data": {"node": node}})
        _put(out, buf.flush())

    async def _write_stop(self, thread_id: str, buf: StreamBuffer | None = None, tasks: dict[str, str] | None = None,
                          done: dict[str | None, list[ToolMessage]] | None = None) -> None:
        """这一轮以停止收尾，写成这一轮最后一个节点（TURN_END）的更新：线程停在这一轮结束的地方，可以接着说。
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
        await self.graph.aupdate_state(cfg, stop_turn(v, partial, subs, title=self._titles.get(thread_id)), as_node=TURN_END)
        self._titles.pop(thread_id, None)

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
        等着插进这一轮、这一轮却没再调模型的话，先退回去排着（排在这期间又说的话前面），和排着的一样处理。"""
        key = self._run_key(thread_id)
        async with self._queue_mu:
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
                       images: Sequence[AttachedImage] = (), files: Sequence[str] = ()) -> None:
        """把用户的话写进检查点，agent 从这里接着跑（输入为 None）。

        先落盘再交出执行：排队时服务重启也查得到这句话，算没跑完，可以重试。
        option 是新对话选的模型，和第一句话一起写进去（第一轮开始时定下，见 agent.TurnMiddleware）；已经开始的对话只能给定下的那个。
        owner 是新对话归谁：第一句话写进去之前记下（已经记过的不改）。
        files 是带的文件的编号：校验都过了才绑到这段对话（绑不上抛 NotSendable，什么都没写）。
        """
        await self._adrop_suggestions(thread_id)
        cfg = self._cfg(thread_id)
        snap = await self.graph.aget_state(cfg)
        if snap.values and snap.next:
            raise NotIdle("上一轮没跑完，先重试")
        update: dict[str, Any] = {}
        if option is not None:
            if option not in {o.id for o in self.planner.catalog.offered(self.planner.usable)}:
                raise UnknownModel(f"没有这个模型可选：{option}")
            if (snap.values or {}).get("messages") and snap.values.get("option") != option:
                raise UnknownModel("对话开始后不能换模型，想换模型请开新对话")
            update["option"] = option
        attached: list[AttachedFile] = []
        if files:
            who = owner or await self.run_store.owner_of(thread_id)
            if who is None:
                raise NotSendable(NOT_HERE, "这段对话不能带文件")
            attached = await self.files.bind(list(files), who, thread_id)
        update["messages"] = [_said(text, thinking, images, attached)]
        if owner is not None:
            await self.run_store.claim(thread_id, owner)
        await self.graph.aupdate_state(cfg, update, as_node=START)

    async def _aretry_input(self, thread_id: str) -> None:
        """只有没跑完的线程能重试：从最后一个检查点接着跑，出错那一步重做，之前做过的搜索、读页都不重来。"""
        snap = await self.graph.aget_state(self._cfg(thread_id))
        if not snap.values or not snap.next:
            raise NothingToRetry("这个线程没有失败，不需要重试")

    async def _aregenerate_input(self, thread_id: str, text: str | None = None, thinking: bool | None = None) -> None:
        """最后一轮重新回答：去掉用户最后那句话之后的消息和这一轮的记录，从那句话接着跑。
        带 text 是用户改了最后那句话：先换成 text（消息 id 不变，还是同一轮，发送时间记成改的时候），再重新回答。标题不重起。
        thinking 是这次的思考开关：给了就按它（只改开关，发送时间不变），没给按那句话原来的；模型照对话定下的（Auto 的重新选）。"""
        await self._adrop_suggestions(thread_id)
        cfg = self._cfg(thread_id)
        snap = await self.graph.aget_state(cfg)
        v = snap.values
        if v and snap.next:
            raise NotIdle("上一轮没跑完，先重试")
        msgs = (v or {}).get("messages", [])
        start = history.last_human(msgs)
        if start < 0:
            raise NothingToRegenerate("还没有回答，不需要重新生成")
        human = msgs[start]
        turns = [t for t in v.get("turns", []) if t.id != human.id]
        if text is not None:  # 带的图、文件留着：只改了字
            edited = [_said(text, history.thinking_switch(human) if thinking is None else thinking, history.attached_images(human),
                            history.attached_files(human), id=human.id)]
        elif thinking is not None:
            edited = [human.model_copy(update={"additional_kwargs": {**human.additional_kwargs, history.THINK: thinking}})]
        else:
            edited = []
        await self.graph.aupdate_state(cfg, {"messages": edited + [RemoveMessage(id=m.id) for m in msgs[start + 1:]], "turns": turns},
                                       as_node=START)
        await self.images.delete_images(history.owned_image_ids(msgs[start + 1:]))  # 这一轮原来画的图不要了（用户那句话带的图留着）
        if msgs[start].id:  # 这一轮原来写的文件各版也不要了（按这一轮记的，进程崩掉时写了、对话里没记下的也删）
            await self.files.delete_turn(thread_id, msgs[start].id)

    # ---- 改名、删除 ----

    @asynccontextmanager
    async def _ahold(self, thread_id: str) -> AsyncIterator[None]:
        """占着这个线程的运行位做一件事（改标题、删除）：做的时候这个线程跑不起来，在跑的也不让做。"""
        key = self._run_key(thread_id)
        if not await self._aadmit(key):
            raise RunConflict("正在回答，答完或停下后才能改名、删除")
        try:
            yield
        finally:
            self.runs.release(key)

    async def _arename(self, thread_id: str, title: str) -> None:
        """改标题。没跑完的线程不能改：写一次状态，出错的那一步就丢了，重试接不上。"""
        cfg = self._cfg(thread_id)
        async with self._ahold(thread_id), self._suggest_mu:  # 和写推荐问题错开
            if (await self.graph.aget_state(cfg)).next:
                raise NotIdle("上一轮没跑完，先重试")
            await self.graph.aupdate_state(cfg, {"title": title}, as_node=TURN_END)

    async def _adelete(self, thread_id: str) -> None:
        """删掉整段对话（这个线程的所有检查点、运行记录、画的图、用户发的图和文件、整理记忆的记录），找不回来；记下的记忆不删。
        运行记录先删：删不掉就整个报错，对话还在，可以再删一次；反过来对话先没了，剩下的记录就再也删不到了。
        图在对话删掉以后再删（编号先记下）：删图没成功只记日志，留下的图没人指着，只占服务的磁盘。"""
        async with self._ahold(thread_id):
            snap = await self.graph.aget_state(self._cfg(thread_id))
            images = history.owned_image_ids((snap.values or {}).get("messages", []))
            await self._adrop_suggestions(thread_id)
            self._titles.pop(thread_id, None)
            await self.run_store.delete_thread(thread_id)
            await self.memory.aforget_thread(thread_id)  # 只删整理过哪几轮的记录；从这段对话记下的记忆不动（记的是人）
            await self.graph.checkpointer.adelete_thread(thread_id)
            await self.images.delete_images(images)
            await self.files.delete_workspace(thread_id)  # 文件按工作区（现在就是这段对话）删，原件没删掉只记日志

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

    # ---- 运行记录 ----

    def recover(self) -> None:
        """服务启动时调一次：上次进程退出时还在排队、在跑的运行，记成没跑完（lost）。单进程部署，这时它们都已经停了。"""
        if n := aio.run(self.run_store.sweep()):
            log.warning("上次进程退出时有 %d 次运行没跑完，已记成 lost", n)

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
        }

    def failure_detail(self, thread_id: str) -> str | None:
        """开发用（评测、命令行）：这一轮没跑完时检查点里记的异常原文（页面上只给转写过的，见 _failure）。没跑完以外的、没有错误记录的是 None。"""
        snap = aio.run(self.graph.aget_state(self._cfg(thread_id)))
        return _real_error([str(t.error) for t in snap.tasks if t.error]) if snap.next else None

    def _title_of(self, thread_id: str, v: dict) -> str:
        """页面上的标题：起好写进 state 的、改过的；没写进去的先看这次进程里起好过的（见 _titles），
        都没有用这段对话的第一句，和起标题时用原话一样整理（见 agent.fallback_title：纯数字、纯符号的叫“新对话”）。"""
        return v.get("title") or self._titles.get(thread_id) or fallback_title(history.requests(v["messages"])[0])

    async def _asummary_view(self, thread_id: str, pins: dict[str, str] | None = None) -> dict | None:
        """线程列表里的一行：标题、状态、置顶的时间（没置顶是 None）。不存在返回 None。
        pins 是这个人置顶的那几段（列表一次取齐）；没给就按这段对话归谁去查。"""
        if pins is None:
            owner = await self.run_store.owner_of(thread_id)
            pins = await self.run_store.pins_of(owner) if owner else {}
        run = self.runs.get(self._run_key(thread_id))
        snap = await self.graph.aget_state(self._cfg(thread_id))
        v = snap.values
        if not v:
            return None
        status, _ = self._status(snap, run)
        return {
            "thread_id": thread_id,
            "title": self._title_of(thread_id, v),
            "status": status,
            "updated_at": snap.created_at,
            "running_since": run.get("since") if run else None,
            "pinned_at": pins.get(thread_id),
        }

    async def _alist_threads(self, who: Identity) -> list[dict]:
        """who 的对话，最近有动静的在前。"""
        pins = await self.run_store.pins_of(who.sub)
        rows = [x for t in await self.run_store.threads_of(who.sub) if (x := await self._asummary_view(t, pins))]
        return sorted(rows, key=lambda x: x["updated_at"] or "", reverse=True)

    @staticmethod
    def _status(snap, run: dict | None) -> tuple[str, tuple[str, str] | None]:
        """queued / running：有执行占着位；idle：这一轮跑完了（或停下了），可以接着说；
        failed：没在跑、也没跑完——某一步抛了错（错误记在检查点里），或执行半路被掐断（进程重启，没有错误记录）。
        failed 时第二项是原因的（类别，说法）（见 _failure_of），别的状态是 None。"""
        if run is not None:
            return ("queued" if run["queued"] else "running"), None
        if not snap.next:
            return "idle", None
        return "failed", _failure_of([str(t.error) for t in snap.tasks if t.error])


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
