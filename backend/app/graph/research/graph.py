"""对话线程的存储：检查点（Postgres / SQLite / 内存）、长期记忆的 Store 和线程列表。图本身就是对话 agent（见 agent.build_agent）。

agent 在对话的事件循环里异步地跑（见 app.aio），检查点和 Store 用异步的那几种，在那个循环里打开（open_* 都是协程，用 aio.run 调）。
它们的同步方法在别的线程里也能用（转到那个循环里做、等结果），整理记忆的后台线程就这样用；在那个循环里调同步方法会报错或卡住。
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.store.base import BaseStore
from langgraph.store.postgres.aio import AsyncPostgresStore
from langgraph.store.sqlite.aio import AsyncSqliteStore
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from app import config
from app.graph.research.agent import build_agent
from app.graph.research.memory import MemoryBook
from app.research_llm import ResearchPlanner

ALLOWED_MODELS = [("app.graph.research.state", n) for n in ("Turn", "Usage", "Compaction")]
NO_POSTGRES = "连不上 Postgres（LINGDRAFT_DATABASE_URL）。本机连 dev 的库要先开隧道：ssh -N -L 15432:127.0.0.1:5432 dev"


def make_serde() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=ALLOWED_MODELS)


async def open_sqlite_checkpointer(path: Path) -> AsyncSqliteSaver:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return AsyncSqliteSaver(await aiosqlite.connect(str(path)), serde=make_serde())


async def _postgres_pool(url: str) -> AsyncConnectionPool:
    """AsyncPostgresSaver / AsyncPostgresStore 每次读写都持着自己的锁，同一时刻只用一个连接，所以池子开得很小；
    用池子是为了连接断了（比如本机的隧道断过）能换新的，取连接时先检查一下，坏的扔掉重连。连接参数照它们的 from_conn_string。"""
    pool = AsyncConnectionPool(url, min_size=1, max_size=2, timeout=10, open=False, check=AsyncConnectionPool.check_connection,
                               kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})
    await pool.open()
    return pool


async def open_postgres_checkpointer(url: str) -> AsyncPostgresSaver:
    """连上 Postgres，表没建的先建（setup 会跑完 LangGraph 的迁移）。"""
    pool = await _postgres_pool(url)
    saver = AsyncPostgresSaver(pool, serde=make_serde())
    try:
        await saver.setup()
    except PoolTimeout as e:
        await pool.close()
        raise RuntimeError(NO_POSTGRES) from e
    return saver


async def open_checkpointer(data_dir: Path) -> BaseCheckpointSaver:
    """配了 LINGDRAFT_DATABASE_URL 就存 Postgres，没配存数据目录下的 SQLite。"""
    if config.DATABASE_URL:
        return await open_postgres_checkpointer(config.DATABASE_URL)
    return await open_sqlite_checkpointer(Path(data_dir) / "checkpoints_research.sqlite")


def memory_checkpointer() -> InMemorySaver:
    return InMemorySaver(serde=make_serde())


THREADS_SQL = "SELECT DISTINCT thread_id FROM checkpoints WHERE checkpoint_ns = ''"  # 两种库的 checkpoints 表都有这两列


async def thread_ids(checkpointer: BaseCheckpointSaver) -> list[str]:
    """检查点里有哪些线程。checkpointer.alist(None) 会把所有检查点都反序列化一遍，这里直接查表。"""
    if isinstance(checkpointer, AsyncSqliteSaver):
        await checkpointer.setup()  # 表还没建时先建表
        async with checkpointer.lock, checkpointer.conn.execute(THREADS_SQL) as cur:  # 和它自己的读写一样持着锁
            rows = await cur.fetchall()
        return [r[0] for r in rows]
    if isinstance(checkpointer, AsyncPostgresSaver):
        async with checkpointer._cursor() as cur:  # 带锁，从池子里取连接；它没有公开的取游标方法
            await cur.execute(THREADS_SQL)
            rows = await cur.fetchall()
        return [r["thread_id"] for r in rows]
    if isinstance(checkpointer, InMemorySaver):
        return [t for t, by_ns in list(checkpointer.storage.items()) if by_ns.get("")]
    raise TypeError(f"不支持列出线程的检查点存储：{type(checkpointer).__name__}")


async def open_store(data_dir: Path) -> BaseStore:
    """长期记忆的 Store，和检查点在一处：配了 LINGDRAFT_DATABASE_URL 是 Postgres 的 store 表，没配是数据目录下的 SQLite。
    不开向量搜索：dev 的库没有 pgvector，记忆整份取出来放进系统提示词，用不上按相似度查（见 memory.py）。"""
    if config.DATABASE_URL:
        return await open_postgres_store(config.DATABASE_URL)
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    store = AsyncSqliteStore(await aiosqlite.connect(str(Path(data_dir) / "store.sqlite"), isolation_level=None))  # 照它的 from_conn_string
    await store.setup()
    return store


async def open_postgres_store(url: str) -> AsyncPostgresStore:
    """连接参数和池子照检查点（见 _postgres_pool）；表没建的先建。"""
    pool = await _postgres_pool(url)
    store = AsyncPostgresStore(pool)
    try:
        await store.setup()
    except PoolTimeout as e:
        await pool.close()
        raise RuntimeError(NO_POSTGRES) from e
    return store


def build_graph(planner: ResearchPlanner, checkpointer: BaseCheckpointSaver, memory: MemoryBook | None = None):
    return build_agent(planner, checkpointer, memory)
