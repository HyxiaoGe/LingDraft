from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest
from langgraph.types import Command

from app import config
from app.api import run_store
from app.graph.poster.graph import build_graph, memory_checkpointer
from app.graph.research import graph as research_graph
from app.llm import FakePlanner

REQUEST = "某品牌蓝牙耳机黑五预热，5 折，11.20 到 11.27"

# .env 里的 LINGDRAFT_DATABASE_URL 是 dev 上的库，测试不碰。设了 LINGDRAFT_TEST_DATABASE_URL 时，对话的检查点改存这个库，
# 整套测试在 Postgres 上再跑一遍（每个测试前清空；别指向有用的库）
config.DATABASE_URL = os.environ.get("LINGDRAFT_TEST_DATABASE_URL", "")


@pytest.fixture(autouse=True)
def _postgres(monkeypatch):
    if not config.DATABASE_URL:
        yield
        return
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as c:
        for sql in ("TRUNCATE checkpoints, checkpoint_blobs, checkpoint_writes", "TRUNCATE runs", "TRUNCATE pending_messages"):
            try:
                c.execute(sql)
            except psycopg.errors.UndefinedTable:  # 还没有哪个测试建过表
                pass
    opened = []
    real = research_graph.open_postgres_checkpointer
    monkeypatch.setattr(research_graph, "open_postgres_checkpointer", lambda url: opened.append(real(url)) or opened[-1])
    stores = []
    real_store = run_store.PostgresRunStore
    monkeypatch.setattr(run_store, "PostgresRunStore", lambda url: stores.append(real_store(url)) or stores[-1])
    yield
    for pool in [saver.conn for saver in opened] + [store.pool for store in stores]:  # 每个运行器各有连接池（检查点一个、运行记录一个），用完关掉，免得连接数攒满
        pool.close()


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
