"""成本记账：每次调模型按这一刻的单价算多少钱，记一行账，钱数也写进这条模型消息（每轮的用量从消息里加）。

2026-11-06 Sean 定：1 credit = ¥0.001 的 API 成本（CREDITS_PER_YUAN），按各模型命中缓存、没命中、输出的单价分开算，要和厂商的账单对得上。
价格表在 app/models.toml 的 [prices]、[peak]（手动维护，查单价见 model_catalog.Catalog.rate）。额度按这里的账扣（见 app.quota）。
工具调用也算钱（2026-11-06 Sean 定“都扣”）：画图、搜索、读页这些按次计价（models.toml 的 [tools]），只算成功的，没列的（task、recall、排行程）不收。
免费额度（models.toml 的 [[free]]）是灵稿省下的钱，不是用户的（2026-11-09 Sean 定）：账上照实记 0 元（yuan），用户的额度照常扣，
按没有免费额度时会用的那个模型的价格算（billed，见 model_catalog.Catalog.billed_as）。别的调用 billed 就是 yuan。

- 记在哪：model_costs 表（和对话同一个 Postgres），每次调用一行，起标题、推荐问题、压缩、整理记忆、评测这些不算给用户的也记，对账才对得上。
  工具调用也是一行（provider、purpose 都是 tool，model 是工具名）。每行记着对话归谁（owner，登录用户的 id 或 guest:xxx），额度按这个人算。
  删对话时不删：钱已经花了。本机跑评测记在本机的库里，对账时两边加起来。
- 怎么记：聊天模型类混进 MeteredChatModel（research_llm 建的每个模型都有），调用结束时算钱，交给 LEDGER，后台线程写库。
  是哪段对话、哪一轮、是不是子 agent 由调用方用 within 标上（运行器、子 agent 的 task 工具、起标题，见 Scope）。
- 不知道用了多少的：流到一半停了、断了的，厂商照样收钱（混元官网写明了，别家按一样算），可接口没返回用量。
  v1 不估算，记一行“不知道”（yuan 是空的），跑几天看有多少再定（2026-11-06）。还没流出东西就失败的不记：没生成，不收钱。
- 对账：uv run python -m app.costs [--days 7]，按北京时间的天、厂商、模型汇总，拿去和各家后台的账单比。
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import contextvars
import functools
import logging
import queue
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessageChunk
from langchain_core.messages.ai import UsageMetadata, add_usage
from langchain_core.outputs import ChatGenerationChunk

from app import config, model_catalog

log = logging.getLogger(__name__)

COST = "lingdraft_cost"  # 模型消息 response_metadata 里的键：这次调用算给用户多少元（billed；没填价格、接口没返回用量的没有这个键）
CREDITS_PER_YUAN = 1000
AGENT = "agent"  # 主 agent、子 agent 用的模型的用途；内部用途是 models.toml [roles] 的名字（title、suggest……）
SUBAGENT = "subagent"
TOOL = "tool"  # 工具调用的用途（也是它的 provider）；算给用户的就是 AGENT、SUBAGENT、TOOL 这三样（见 quota）
OK, NO_USAGE, STOPPED, ERROR = "ok", "no_usage", "stopped", "error"  # 一行账的状态：算了钱的、接口没给用量的、流到一半停了的、断了的


# ---- 是哪段对话、哪一轮 ----


@dataclass(frozen=True)
class Scope:
    """调模型时在做什么，记进账里。用 contextvars 往下传：运行器跑一轮时标上对话和轮（见 ResearchRunner._run_turn），
    子 agent 的 task 工具标上 subagent，起标题另起任务用空的上下文，要自己带过去（见 agent._TitleJob）。"""

    thread_id: str | None = None
    turn_id: str | None = None
    subagent: bool = False
    owner: str | None = None  # 对话归谁（thread_owners 里的），额度按人算；没归谁的（登录以前的老对话、整理记忆）是 None


SCOPE: contextvars.ContextVar[Scope] = contextvars.ContextVar("lingdraft_cost_scope", default=Scope())


@contextmanager
def within(base: Scope | None = None, **changes: Any) -> Iterator[None]:
    """这一段里调的模型记成 base（不给是现在的）改了 changes 的样子。"""
    token = SCOPE.set(replace(base or SCOPE.get(), **changes))
    try:
        yield
    finally:
        SCOPE.reset(token)


# ---- 算钱 ----


@functools.cache
def _catalog() -> model_catalog.Catalog:
    return model_catalog.load()


_warned: set[str] = set()
_warned_mu = threading.Lock()


def _warn_once(msg: str) -> None:
    with _warned_mu:
        if msg in _warned:
            return
        _warned.add(msg)
    log.warning("价格表要更新：%s", msg)


def now() -> datetime:
    return datetime.now(UTC)


def _row(ref: str, purpose: str, started: datetime, status: str) -> dict[str, Any]:
    scope = SCOPE.get()
    provider, _, model = ref.partition(":")
    return {"started_at": started, "provider": provider, "model": model, "purpose": SUBAGENT if purpose == AGENT and scope.subagent else purpose,
            "thread_id": scope.thread_id, "turn_id": scope.turn_id, "owner": scope.owner, "status": status,
            "input_tokens": None, "cached_tokens": None, "output_tokens": None, "yuan": None, "billed": None, "rate": None,
            "version": config.VERSION or None}


def _rate(kind: str, rate: model_catalog.Rate) -> dict[str, Any]:
    return {"kind": kind, "cached": rate.cached, "input": rate.input, "output": rate.output}


def charge(ref: str, purpose: str, started: datetime, usage: UsageMetadata | None) -> float | None:
    """一次调完的调用：算钱、记一行，返回算给用户多少元（免费额度的按正常价，见模块说明）。没填价格、接口没给用量的钱数记成不知道，返回 None。"""
    row, billed = _row(ref, purpose, started, NO_USAGE), None
    if usage:
        inp, out = usage.get("input_tokens") or 0, usage.get("output_tokens") or 0
        cached = (usage.get("input_token_details") or {}).get("cache_read") or 0
        row.update(status=OK, input_tokens=inp, cached_tokens=cached, output_tokens=out)
        cat = _catalog()
        billed_ref = cat.billed_as(ref)
        for msg in {*cat.stale(ref, started), *cat.stale(billed_ref, started)}:
            _warn_once(msg)
        if found := cat.rate(ref, started, inp):
            kind, rate = found
            row.update(yuan=rate.yuan(inp, cached, out), rate=_rate(kind, rate))
            billed = row["yuan"]
            if billed_ref != ref and (found := cat.rate(billed_ref, started, inp)):  # 免费额度：按哪个模型、哪组单价扣的也记上，对得上
                billed = found[1].yuan(inp, cached, out)
                row["rate"]["billed_as"] = {"model": billed_ref, **_rate(*found)}
            row["billed"] = billed
    LEDGER.put(row)
    return billed


def lost(ref: str, purpose: str, started: datetime, status: str) -> None:
    """发出去了、没调完的（点了停止、流到一半断了）：不知道用了多少，记一行。"""
    LEDGER.put(_row(ref, purpose, started, status))


def tool_price(name: str) -> float | None:
    """这个工具每次调用多少元（models.toml 的 [tools]）；没列的不收钱。"""
    return _catalog().tools.get(name)


def charge_tool(name: str, started: datetime) -> float | None:
    """一次成功的工具调用：按次记一行，返回多少元；没定价的不记、返回 None。调用方（agent.ToolChargeMiddleware）把钱数写进工具结果，
    这一轮的用量从消息里加（和模型调用一样，见 history.usage_of）。"""
    if (yuan := tool_price(name)) is None:
        return None
    row = _row(f"{TOOL}:{name}", TOOL, started, OK)
    row.update(yuan=yuan, billed=yuan, rate={"kind": "per_call", "call": yuan})
    LEDGER.put(row)
    return yuan


def credits(turn: Any) -> float | None:
    """一轮算给用户的 credits：主 agent 和子 agent 的模型调用加上工具调用（起标题、推荐问题这些不算）。没调完过模型、有不知道钱数的调用
    （没填价格、这次改动之前的轮）是 None。点了停止的那一下不知道用了多少，不算在里面（见模块说明）。"""
    u, s = turn.usage, turn.subagent_usage
    if u.cost is None or s.cost is None or not (u.model_calls or s.model_calls):
        return None
    return round((u.cost + s.cost) * CREDITS_PER_YUAN, 2)


def _status(e: BaseException) -> str:
    return STOPPED if isinstance(e, asyncio.CancelledError | GeneratorExit) else ERROR


def _with_cost(msg: Any, yuan: float | None) -> None:
    if yuan is not None:
        msg.response_metadata = {**msg.response_metadata, COST: yuan}


def _add(usage: UsageMetadata | None, chunk: ChatGenerationChunk) -> UsageMetadata | None:
    """片段里的用量加起来（和 LangChain 合片段时一样加）。"""
    u = getattr(chunk.message, "usage_metadata", None)
    return add_usage(usage, u) if u else usage


def _cost_chunk(last: ChatGenerationChunk | None, yuan: float) -> ChatGenerationChunk:
    """流式调用最后多发的空片段，带上钱数。正文的类型、消息 id 跟着前面的片段：合起来不出错，页面的流（按 id 认是哪条消息，
    见 api.research_runner.StreamBuffer）也不会当成另一条。前面的片段没带 id 的，LangChain 发出去前已经补上了（同一个对象）。"""
    empty: Any = [] if last is not None and isinstance(last.message.content, list) else ""
    return ChatGenerationChunk(message=AIMessageChunk(content=empty, id=last.message.id if last else None, response_metadata={COST: yuan}))


@functools.cache
def _falls_back(cls: type, name: str) -> bool:
    """这个类的异步调用（_agenerate、_astream）是不是 LangChain 的默认实现：它转去线程池里调同步的那个，同步的那边已经记了，这边不能再记。
    各家真的类都自己实现了异步，这里防的是以后接的类没实现、一次调用记两遍。"""
    after = cls.__mro__[cls.__mro__.index(MeteredChatModel) + 1:]
    return next(c for c in after if name in c.__dict__) is BaseChatModel


class MeteredChatModel:
    """放在聊天模型类的前面（多继承，排在 GuardedChatModel 后面：熔断拦下的没发出去，不记）：每次调用记一行账，钱数写进这条消息。
    建模型的地方（research_llm 各家的函数）改 _cost_ref、_cost_purpose 说明这是哪个模型、什么用途。
    流式调用在最后多发一个空片段带上钱数：LangChain 把片段合成一条消息，只有这一个片段有这个键，合起来就是它。"""

    def _cost_ref(self) -> str:
        return f"unknown:{getattr(self, 'model_name', '')}"

    def _cost_purpose(self) -> str:
        return AGENT

    def _generate(self, messages, stop=None, run_manager=None, **kwargs: Any):
        started = now()
        res = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)  # type: ignore[misc]
        self._charge_result(res, started)
        return res

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs: Any):
        if _falls_back(type(self), "_agenerate"):
            return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)  # type: ignore[misc]
        started = now()
        try:
            res = await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)  # type: ignore[misc]
        except asyncio.CancelledError:  # 发出去了才被取消的：厂商可能照样生成完、照样收钱
            lost(self._cost_ref(), self._cost_purpose(), started, STOPPED)
            raise
        self._charge_result(res, started)
        return res

    def _charge_result(self, res: Any, started: datetime) -> None:
        msg = res.generations[0].message if res.generations else None
        _with_cost(msg, charge(self._cost_ref(), self._cost_purpose(), started, getattr(msg, "usage_metadata", None)))

    def _stream(self, messages, stop=None, run_manager=None, **kwargs: Any) -> Iterator:
        started, usage, last = now(), None, None
        try:
            for chunk in super()._stream(messages, stop=stop, run_manager=run_manager, **kwargs):  # type: ignore[misc]
                usage, last = _add(usage, chunk), chunk
                yield chunk
        except BaseException as e:
            if last is not None:
                lost(self._cost_ref(), self._cost_purpose(), started, _status(e))
            raise
        if (yuan := charge(self._cost_ref(), self._cost_purpose(), started, usage)) is not None:
            yield _cost_chunk(last, yuan)

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs: Any) -> AsyncIterator:
        if _falls_back(type(self), "_astream"):
            async for chunk in super()._astream(messages, stop=stop, run_manager=run_manager, **kwargs):  # type: ignore[misc]
                yield chunk
            return
        started, usage, last = now(), None, None
        try:
            async for chunk in super()._astream(messages, stop=stop, run_manager=run_manager, **kwargs):  # type: ignore[misc]
                usage, last = _add(usage, chunk), chunk
                yield chunk
        except BaseException as e:
            if last is not None:
                lost(self._cost_ref(), self._cost_purpose(), started, _status(e))
            raise
        if (yuan := charge(self._cost_ref(), self._cost_purpose(), started, usage)) is not None:
            yield _cost_chunk(last, yuan)


# ---- 账本 ----

_END = object()


class Ledger:
    """账本：调模型的地方（事件循环里、线程池里都有）把一行交给 put，后台一个线程写库，调模型不等它。
    写不上只打日志、那几行丢掉，不影响调模型。没开（start 没调：单元测试、没配库的命令）就不记。"""

    def __init__(self) -> None:
        self._q: queue.SimpleQueue = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._write: Callable[[list[dict]], None] | None = None

    def start(self, write: Callable[[list[dict]], None]) -> None:
        if self._thread is not None:
            return
        self._write = write
        self._thread = threading.Thread(target=self._loop, name="cost-ledger", daemon=True)
        self._thread.start()
        atexit.register(self.stop)  # 命令行跑完退出时把还没写的写完

    def put(self, row: dict) -> None:
        if self._thread is not None:
            self._q.put(row)

    def stop(self, timeout: float = 10) -> None:
        """把排着的写完再停。"""
        thread, self._thread = self._thread, None
        if thread is not None:
            self._q.put(_END)
            thread.join(timeout)

    def _loop(self) -> None:
        while True:
            rows = [self._q.get()]
            while True:  # 攒着的一起写
                try:
                    rows.append(self._q.get_nowait())
                except queue.Empty:
                    break
            end = any(r is _END for r in rows)
            rows = [r for r in rows if r is not _END]
            if rows and self._write is not None:
                try:
                    self._write(rows)
                except Exception:  # noqa: BLE001
                    log.warning("成本没记上，丢了 %d 行", len(rows), exc_info=True)
            if end:
                return


LEDGER = Ledger()

_COLUMNS = ("started_at", "provider", "model", "purpose", "thread_id", "turn_id", "owner", "status",
            "input_tokens", "cached_tokens", "output_tokens", "yuan", "billed", "rate", "version")
_DDL = (
    """CREATE TABLE IF NOT EXISTS model_costs (
        seq BIGSERIAL PRIMARY KEY,
        started_at TIMESTAMPTZ NOT NULL,
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        purpose TEXT NOT NULL,
        thread_id TEXT,
        turn_id TEXT,
        owner TEXT,
        status TEXT NOT NULL,
        input_tokens INTEGER,
        cached_tokens INTEGER,
        output_tokens INTEGER,
        yuan DOUBLE PRECISION,
        billed DOUBLE PRECISION,
        rate JSONB,
        version TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS model_costs_started ON model_costs (started_at)",
    "CREATE INDEX IF NOT EXISTS model_costs_turn ON model_costs (thread_id, turn_id)",
    "ALTER TABLE model_costs ADD COLUMN IF NOT EXISTS owner TEXT",  # 2026-11-06 加的（额度按人算），之前建好的表补上
    "CREATE INDEX IF NOT EXISTS model_costs_owner ON model_costs (owner, started_at)",
    # 2026-11-09 加的：算给用户的（免费额度按正常价）。之前的行是空的，额度按 yuan 算（免费额度的那些就是 0）
    "ALTER TABLE model_costs ADD COLUMN IF NOT EXISTS billed DOUBLE PRECISION",
)


class PostgresLedger:
    """写进 model_costs 表。自己开一个连接，不用对话的连接池：同步调用（整理记忆、评测的裁判）也要记，后台线程里用同步的连接最简单。
    第一次写时建表；连接断了重连一次。"""

    def __init__(self, url: str) -> None:
        self.url = url
        self._conn: Any = None

    def __call__(self, rows: list[dict]) -> None:
        import psycopg

        try:
            self._insert(rows)
        except psycopg.OperationalError:
            self._close()
            self._insert(rows)

    def _insert(self, rows: list[dict]) -> None:
        import psycopg
        from psycopg.types.json import Jsonb

        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self.url, autocommit=True, connect_timeout=10)
            for sql in _DDL:
                self._conn.execute(sql)
        values = [tuple(Jsonb(r[k]) if k == "rate" and r[k] is not None else r[k] for k in _COLUMNS) for r in rows]
        with self._conn.cursor() as cur:
            cur.executemany(f"INSERT INTO model_costs ({', '.join(_COLUMNS)}) VALUES ({', '.join(['%s'] * len(_COLUMNS))})", values)

    def _close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None


def open_ledger(url: str | None = None) -> bool:
    """服务、命令行起来时调：配了库就开始记账，返回开没开。"""
    url = config.DATABASE_URL if url is None else url
    if not url:
        return False
    LEDGER.start(PostgresLedger(url))
    return True


# ---- 对账 ----

_REPORT = """
SELECT (started_at AT TIME ZONE 'Asia/Shanghai')::date AS day, provider, model,
       count(*) FILTER (WHERE yuan IS NOT NULL) AS calls, count(*) FILTER (WHERE yuan IS NULL) AS unknown,
       coalesce(sum(input_tokens), 0) AS input, coalesce(sum(cached_tokens), 0) AS cached, coalesce(sum(output_tokens), 0) AS output,
       coalesce(sum(yuan), 0) AS yuan
FROM model_costs WHERE started_at >= now() - make_interval(days => %s)
GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
"""


def report(url: str, days: int) -> str:
    """按北京时间的天、厂商、模型汇总，每天每家一个小计。unknown 是不知道用了多少的调用（见模块说明）。"""
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(url, row_factory=dict_row, connect_timeout=10) as conn:
        try:
            rows = conn.execute(_REPORT, (days,)).fetchall()
        except psycopg.errors.UndefinedTable:  # 表在第一次记账时才建
            return "还没记过账"
    lines = [f"{'日期':<10}  {'厂商':<10}  {'模型':<32} {'算了钱':>5} {'不知道':>5} {'输入':>11} {'命中缓存':>11} {'输出':>10} {'元':>9}"]
    for day in sorted({r["day"] for r in rows}):
        for provider in sorted({r["provider"] for r in rows if r["day"] == day}):
            group = [r for r in rows if r["day"] == day and r["provider"] == provider]
            for r in group:
                lines.append(f"{day!s:<10}  {provider:<10}  {r['model']:<32} {r['calls']:>5} {r['unknown']:>5} {r['input']:>11,} "
                             f"{r['cached']:>11,} {r['output']:>10,} {r['yuan']:>9.4f}")
            if len(group) > 1:
                lines.append(f"{'':<10}  {provider:<10}  {'（小计）':<32} {sum(r['calls'] for r in group):>5} {sum(r['unknown'] for r in group):>5} "
                             f"{'':>11} {'':>11} {'':>10} {sum(r['yuan'] for r in group):>9.4f}")
    lines.append(f"合计 {sum(r['yuan'] for r in rows):.4f} 元：算了钱的调用 {sum(r['calls'] for r in rows)} 次，另有 {sum(r['unknown'] for r in rows)} 次不知道用了多少")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="对账：按天、厂商、模型汇总记下的成本（读 LINGDRAFT_DATABASE_URL 那个库）")
    ap.add_argument("--days", type=int, default=7, help="最近几天（默认 7）")
    args = ap.parse_args(argv)
    if not config.DATABASE_URL:
        print("没配 LINGDRAFT_DATABASE_URL")
        return 1
    print(report(config.DATABASE_URL, args.days))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
