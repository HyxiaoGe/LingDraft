"""手工解卡：修复前留下的、停在澄清卡或终审卡上的调研线程。

旧版节点在 interrupt() 之后自己 raise（回答了不存在的问题、终审目标不存在、改论断没给内容），坏回答会作为
待重放的回答留在检查点里，之后每次提交正确回答都会重放旧错误。新版本不会再产生这种线程，已有的用这个脚本处理：
从当前检查点复制出一个新检查点（丢掉那条待重放的回答），再跑一步，让同一张卡重新停下来。

澄清、终审两个节点不调模型；这里用假模型构图，万一跑到别的节点也不会花钱。卡的中断 id 会变，页面刷新即可。
先确认线程没在跑（后端开着也行，但这时别提交这个线程）。

  PYTHONPATH=. uv run python scripts/unstick_research_thread.py <thread_id> [<thread_id> ...] [--db data/checkpoints_research.sqlite]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from app import config
from app.graph.research.graph import build_graph, open_sqlite_checkpointer
from app.research_llm import FakeResearchPlanner

CARDS = {"research.scope": "澄清卡", "research.review": "终审卡"}
RESUME_CHANNEL = "__resume__"  # LangGraph 记录待重放回答的通道（langgraph.constants.RESUME，已标为私有）


def _pending(graph, cfg: dict):
    return next((it for task in graph.get_state(cfg).tasks for it in task.interrupts), None)


def unstick(graph, thread_id: str) -> str:
    cfg = {"configurable": {"thread_id": thread_id}}
    it = _pending(graph, cfg)
    kind = it.value.get("type") if it is not None and isinstance(it.value, dict) else None
    if kind not in CARDS:
        return f"{thread_id}：没停在澄清卡或终审卡上，不处理"
    tup = graph.checkpointer.get_tuple(cfg)
    if not any(w[1] == RESUME_CHANNEL for w in tup.pending_writes or []):
        return f"{thread_id}：{CARDS[kind]}上没有待重放的回答，没卡住，不处理"
    graph.update_state(cfg, None, as_node="__copy__")
    graph.invoke(None, cfg)
    after = _pending(graph, cfg)
    if after is None or after.value.get("type") != kind:
        raise RuntimeError(f"{thread_id}：没有重新停在{CARDS[kind]}上，请手工检查")
    return f"{thread_id}：已重新停在{CARDS[kind]}上，可以重新回答"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("thread_ids", nargs="+")
    ap.add_argument("--db", type=Path, default=config.DATA_DIR / "checkpoints_research.sqlite")
    ap.add_argument("--out", type=Path, default=config.OUTPUT_DIR / "research")
    args = ap.parse_args()
    graph = build_graph(FakeResearchPlanner(), open_sqlite_checkpointer(args.db), args.out)
    for tid in args.thread_ids:
        print(unstick(graph, tid))


if __name__ == "__main__":
    main()
