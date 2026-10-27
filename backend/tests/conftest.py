from __future__ import annotations

from pathlib import Path

import pytest
from langgraph.types import Command

from app.graph.poster.graph import build_graph, memory_checkpointer
from app.llm import FakePlanner

REQUEST = "某品牌蓝牙耳机黑五预热，5 折，11.20 到 11.27"


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
