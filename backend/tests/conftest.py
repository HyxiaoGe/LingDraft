from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command

from app import aio, config
from app.api import research_runner
from app.api.run_store import MemoryRunStore
from app.graph.poster.graph import build_graph, memory_checkpointer
from app.graph.research import graph as research_graph
from app.llm import FakePlanner

REQUEST = "某品牌蓝牙耳机黑五预热，5 折，11.20 到 11.27"

# .env 里的 LINGDRAFT_DATABASE_URL 是 dev 上的库，测试不碰。对话的存储默认换成内存的（见 _storage）；
# 设了 LINGDRAFT_TEST_DATABASE_URL 时存这个库，整套测试在 Postgres 上跑（每个测试前清空；别指向有用的库）
config.DATABASE_URL = os.environ.get("LINGDRAFT_TEST_DATABASE_URL", "")
config.DREAM_ENABLED = False  # 不开整理记忆的定时任务（它会建真实模型的运行器）


@pytest.fixture(autouse=True)
def _storage(monkeypatch):
    """不传检查点建的对话运行器，存储从 research_runner.open_storage 来。
    没设测试库：换成内存的，同一个测试里建的运行器共用一份，再建一个就像服务重启（运行器的状态丢了，库里的还在）。
    设了：开真的 Postgres，每个测试前清空，测试完关掉每个运行器的连接池，免得连接数攒满。"""
    if not config.DATABASE_URL:
        shared = (research_graph.memory_checkpointer(), InMemoryStore(), MemoryRunStore())

        async def in_memory(url):
            return shared
        monkeypatch.setattr(research_runner, "open_storage", in_memory)
        yield
        return
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as c:
        for sql in ("TRUNCATE checkpoints, checkpoint_blobs, checkpoint_writes", "TRUNCATE runs", "TRUNCATE pending_messages", "TRUNCATE store"):
            try:
                c.execute(sql)
            except psycopg.errors.UndefinedTable:  # 还没有哪个测试建过表
                pass
    opened = []
    real = research_runner.open_storage

    async def keep(url):
        opened.append(await real(url))
        return opened[-1]
    monkeypatch.setattr(research_runner, "open_storage", keep)
    yield
    for saver, _, _ in opened:
        aio.run(saver.conn.close())  # 三样共用这一个池子


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
