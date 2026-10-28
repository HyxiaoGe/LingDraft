from __future__ import annotations

import sqlite3
from pathlib import Path

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from app.graph.research import nodes as N
from app.graph.research.state import ResearchState
from app.research_llm import ResearchPlanner

ALLOWED_MODELS = [
    ("app.graph.research.state", n)
    for n in ("Scope", "ClarifyQuestion", "OutlineNode", "Source", "Claim", "SectionText", "Sentence", "ReportVersion", "CheckResult")
]


def make_serde() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=ALLOWED_MODELS)


def open_sqlite_checkpointer(path: Path) -> SqliteSaver:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    return SqliteSaver(conn, serde=make_serde())


def memory_checkpointer() -> InMemorySaver:
    return InMemorySaver(serde=make_serde())


def build_graph(planner: ResearchPlanner, checkpointer: BaseCheckpointSaver, out_dir: Path):
    """按需澄清 → 全局调研 agent → 结构化整理（缺口回补一轮）→ 拼装 → 校验 → 校验过就导出 → 终审（可选的修改，改完再导出）。"""
    fns = N.make_nodes(planner, Path(out_dir))
    g = StateGraph(ResearchState)
    for name, fn in fns.items():
        g.add_node(name, fn)
    g.add_edge(START, "intake")
    g.add_edge("intake", "confirm_scope")
    g.add_edge("confirm_scope", "research")
    g.add_edge("research", "consolidate")
    g.add_conditional_edges("consolidate", N.route_after_consolidate, ["research", "write"])
    g.add_edge("write", "check")
    g.add_conditional_edges("check", N.route_after_check, ["export", "review"])
    g.add_edge("export", "review")
    g.add_conditional_edges("review", N.route_after_review, ["write", "review", END])
    return g.compile(checkpointer=checkpointer)
