from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest
from langgraph.types import Command

from app import aio, config
from app.api import run_store
from app.graph.poster.graph import build_graph, memory_checkpointer
from app.graph.research import graph as research_graph
from app.llm import FakePlanner

REQUEST = "某品牌蓝牙耳机黑五预热，5 折，11.20 到 11.27"

# .env 里的 LINGDRAFT_DATABASE_URL 是 dev 上的库，测试不碰。设了 LINGDRAFT_TEST_DATABASE_URL 时，对话的检查点改存这个库，
# 整套测试在 Postgres 上再跑一遍（每个测试前清空；别指向有用的库）
config.DATABASE_URL = os.environ.get("LINGDRAFT_TEST_DATABASE_URL", "")
config.DREAM_ENABLED = False  # 不开整理记忆的定时任务（它会建真实模型的运行器）


@pytest.fixture(autouse=True)
def _postgres(monkeypatch):
    if not config.DATABASE_URL:
        yield
        return
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as c:
        for sql in ("TRUNCATE checkpoints, checkpoint_blobs, checkpoint_writes", "TRUNCATE runs", "TRUNCATE pending_messages", "TRUNCATE store"):
            try:
                c.execute(sql)
            except psycopg.errors.UndefinedTable:  # 还没有哪个测试建过表
                pass
    opened = []  # 检查点和长期记忆的是异步的连接池（在对话的事件循环里），运行记录的是同步的
    for name in ("open_postgres_checkpointer", "open_postgres_store"):
        monkeypatch.setattr(research_graph, name, _keep(getattr(research_graph, name), opened))
    stores = []
    real_store = run_store.PostgresRunStore
    monkeypatch.setattr(run_store, "PostgresRunStore", lambda url: stores.append(real_store(url)) or stores[-1])
    yield
    # 每个运行器各有连接池（检查点、运行记录、长期记忆各一个），用完关掉，免得连接数攒满
    for x in opened:
        aio.run(x.conn.close())
    for store in stores:
        store.pool.close()


def _keep(open_, opened: list):
    async def wrapped(url):
        opened.append(await open_(url))
        return opened[-1]
    return wrapped


@pytest.fixture
def planner() -> FakePlanner:
    return FakePlanner()


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    return tmp_path / "out"


@pytest.fixture
def graph(planner, out_dir):
    return build_graph(planner, memory_checkpointer(), out_dir)


def cfg(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def interrupt_of(result: dict) -> dict | None:
    ints = result.get("__interrupt__") or []
    return ints[0].value if ints else None


def resume(graph, thread_id: str, payload: dict) -> dict:
    return graph.invoke(Command(resume=payload), cfg(thread_id))


def run_to_review(graph, thread_id: str = "t1", request: str = REQUEST, style_id: str = "s1") -> dict:
    """走完 新建 → 确认事实 → 选风格，停在 review。返回 review 载荷。"""
    r = graph.invoke({"request": request, "product_image": None}, cfg(thread_id))
    assert interrupt_of(r)["type"] == "confirm_facts"
    values = {f["key"]: f["value"] for f in interrupt_of(r)["fields"]}
    r = resume(graph, thread_id, {"values": values})
    payload = interrupt_of(r)
    if payload["type"] == "choose_style":
        r = resume(graph, thread_id, {"style_id": style_id})
        payload = interrupt_of(r)
    assert payload["type"] == "review", payload
    return payload
