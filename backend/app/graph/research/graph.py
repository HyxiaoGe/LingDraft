"""对话线程的存储：检查点（Postgres / SQLite / 内存）和线程列表。图本身就是对话 agent（见 agent.build_agent）。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from app import config
from app.graph.research.agent import build_agent
from app.research_llm import ResearchPlanner

ALLOWED_MODELS = [("app.graph.research.state", n) for n in ("Turn", "Usage", "Compaction")]


def make_serde() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=ALLOWED_MODELS)


def open_sqlite_checkpointer(path: Path) -> SqliteSaver:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    return SqliteSaver(conn, serde=make_serde())


def open_postgres_checkpointer(url: str) -> PostgresSaver:
    """连上 Postgres，表没建的先建（setup 会跑完 LangGraph 的迁移）。

    PostgresSaver 每次读写都持着自己的锁，同一时刻只用一个连接，所以池子开得很小；用池子是为了连接断了（比如本机的隧道断过）能换新的，
    取连接时先检查一下，坏的扔掉重连。连接参数照它的 from_conn_string。
    """
    pool = ConnectionPool(url, min_size=1, max_size=2, timeout=10, open=True, check=ConnectionPool.check_connection,
                          kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})
    saver = PostgresSaver(pool, serde=make_serde())
    try:
        saver.setup()
    except PoolTimeout as e:
        pool.close()
        raise RuntimeError("连不上 Postgres（LINGDRAFT_DATABASE_URL）。本机连 dev 的库要先开隧道：ssh -N -L 15432:127.0.0.1:5432 dev") from e
    return saver


def open_checkpointer(data_dir: Path) -> BaseCheckpointSaver:
    """配了 LINGDRAFT_DATABASE_URL 就存 Postgres，没配存数据目录下的 SQLite。"""
    if config.DATABASE_URL:
        return open_postgres_checkpointer(config.DATABASE_URL)
    return open_sqlite_checkpointer(Path(data_dir) / "checkpoints_research.sqlite")


def memory_checkpointer() -> InMemorySaver:
    return InMemorySaver(serde=make_serde())


THREADS_SQL = "SELECT DISTINCT thread_id FROM checkpoints WHERE checkpoint_ns = ''"  # 两种库的 checkpoints 表都有这两列


def thread_ids(checkpointer: BaseCheckpointSaver) -> list[str]:
    """检查点里有哪些线程。checkpointer.list(None) 会把所有检查点都反序列化一遍，这里直接查表。"""
    if isinstance(checkpointer, SqliteSaver):
        with checkpointer.cursor(transaction=False) as cur:  # 带锁，表还没建时先建表
            rows = cur.execute(THREADS_SQL).fetchall()
        return [r[0] for r in rows]
    if isinstance(checkpointer, PostgresSaver):
        with checkpointer._cursor() as cur:  # 带锁，从池子里取连接；它没有公开的取游标方法
            rows = cur.execute(THREADS_SQL).fetchall()
        return [r["thread_id"] for r in rows]
    if isinstance(checkpointer, InMemorySaver):
        return [t for t, by_ns in list(checkpointer.storage.items()) if by_ns.get("")]
    raise TypeError(f"不支持列出线程的检查点存储：{type(checkpointer).__name__}")


def build_graph(planner: ResearchPlanner, checkpointer: BaseCheckpointSaver):
    return build_agent(planner, checkpointer)
