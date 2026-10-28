from __future__ import annotations

import sqlite3
from pathlib import Path

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from app.graph.poster import nodes as N
from app.graph.poster.state import PosterState
from app.llm import Planner

# 检查点里会存 state.py 中的 Pydantic 模型，需要显式允许反序列化
ALLOWED_MODELS = [("app.graph.poster.state", n) for n in ("Fact", "StyleOption", "Element", "Version", "CheckResult")]


def make_serde() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=ALLOWED_MODELS)


def open_sqlite_checkpointer(path: Path) -> SqliteSaver:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    return SqliteSaver(conn, serde=make_serde())


def memory_checkpointer() -> InMemorySaver:
    return InMemorySaver(serde=make_serde())


def build_graph(planner: Planner, checkpointer: BaseCheckpointSaver, out_dir: Path):
    fns = N.make_nodes(planner, Path(out_dir))
    g = StateGraph(PosterState)
    for name, fn in fns.items():
        g.add_node(name, fn)

    g.add_edge(START, "intake")
    g.add_edge("intake", "confirm_facts")
    g.add_conditional_edges("confirm_facts", N.route_after_facts, ["render", "gen_background", "gen_style_options"])
    g.add_conditional_edges("gen_style_options", N.route_after_style_gen, ["gen_background", "ask_style"])
    g.add_conditional_edges("ask_style", N.route_after_ask_style, ["gen_background", "gen_style_options"])
    g.add_conditional_edges("gen_background", N.route_after_background, ["render", "plan_layout"])
    g.add_edge("plan_layout", "render")
    g.add_edge("render", "check")
    g.add_conditional_edges("check", N.route_after_check, ["plan_layout", "review"])
    after_review = ["export", "revise", "manual_edit", "confirm_facts", "ask_style", "gen_style_options"]
    g.add_conditional_edges("review", N.route_after_review, after_review + ["interpret"])
    g.add_conditional_edges("interpret", N.route_after_interpret, after_review + ["review"])
    g.add_conditional_edges("revise", N.route_after_revise, ["render", "review"])
    g.add_conditional_edges("manual_edit", N.route_after_revise, ["render", "review"])
    g.add_edge("export", END)
    return g.compile(checkpointer=checkpointer)
