"""对话线程的存储：检查点（SQLite / 内存）和线程列表。图本身就是对话 agent（见 agent.build_agent）。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

from app.graph.research.agent import build_agent
from app.research_llm import ResearchPlanner

ALLOWED_MODELS = [("app.graph.research.state", n) for n in ("Turn", "Usage")]


def make_serde() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=ALLOWED_MODELS)


def open_sqlite_checkpointer(path: Path) -> SqliteSaver:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    return SqliteSaver(conn, serde=make_serde())


def memory_checkpointer() -> InMemorySaver:
    return InMemorySaver(serde=make_serde())


def thread_ids(checkpointer: BaseCheckpointSaver) -> list[str]:
    """检查点里有哪些线程。checkpointer.list(None) 会把所有检查点都反序列化一遍，这里直接查表。

    换成 Postgres 时它的 checkpoints 表也有 thread_id、checkpoint_ns 两列，照这条 SQL 写一份即可。
    """
    if isinstance(checkpointer, SqliteSaver):
        with checkpointer.cursor(transaction=False) as cur:  # 带锁，表还没建时先建表
            rows = cur.execute("SELECT DISTINCT thread_id FROM checkpoints WHERE checkpoint_ns = ''").fetchall()
        return [r[0] for r in rows]
    if isinstance(checkpointer, InMemorySaver):
        return [t for t, by_ns in list(checkpointer.storage.items()) if by_ns.get("")]
    raise TypeError(f"不支持列出线程的检查点存储：{type(checkpointer).__name__}")


def build_graph(planner: ResearchPlanner, checkpointer: BaseCheckpointSaver):
    return build_agent(planner, checkpointer)
