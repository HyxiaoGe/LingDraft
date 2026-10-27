"""命令行驱动（M1）。

  uv run python -m app.cli new "需求" [--fake] [--image 路径]
  uv run python -m app.cli resume <thread_id> '<json>' [--fake]
  uv run python -m app.cli state <thread_id>

每次运行到 interrupt 时打印确认点载荷；用 resume 回传答案继续。
"""
from __future__ import annotations

import argparse
import json
import uuid

from langgraph.types import Command

from app import config
from app.graph.poster.graph import build_graph, open_sqlite_checkpointer
from app.llm import ArkPlanner, FakePlanner


def _graph(fake: bool):
    planner = FakePlanner() if fake else ArkPlanner()
    saver = open_sqlite_checkpointer(config.DATA_DIR / "checkpoints.sqlite")
    return build_graph(planner, saver, config.OUTPUT_DIR)


def _report(result: dict, thread_id: str) -> None:
    print(f"thread_id: {thread_id}")
    interrupts = result.get("__interrupt__") or []
    if interrupts:
        print("== 等待你的输入 ==")
        print(json.dumps(interrupts[0].value, ensure_ascii=False, indent=2))
    elif result.get("exported"):
        print("== 已导出 ==")
        print(json.dumps(result["exported"], ensure_ascii=False, indent=2))
    else:
        print("== 运行结束，无待确认项 ==")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_new = sub.add_parser("new")
    p_new.add_argument("request")
    p_new.add_argument("--image", default=None)
    p_new.add_argument("--fake", action="store_true")
    p_res = sub.add_parser("resume")
    p_res.add_argument("thread_id")
    p_res.add_argument("payload")
    p_res.add_argument("--fake", action="store_true")
    p_state = sub.add_parser("state")
    p_state.add_argument("thread_id")
    p_state.add_argument("--fake", action="store_true")
    args = ap.parse_args()

    graph = _graph(getattr(args, "fake", False))
    if args.cmd == "new":
        thread_id = uuid.uuid4().hex[:8]
        cfg = {"configurable": {"thread_id": thread_id}}
        result = graph.invoke({"request": args.request, "product_image": args.image}, cfg)
        _report(result, thread_id)
    elif args.cmd == "resume":
        cfg = {"configurable": {"thread_id": args.thread_id}}
        result = graph.invoke(Command(resume=json.loads(args.payload)), cfg)
        _report(result, args.thread_id)
    else:
        cfg = {"configurable": {"thread_id": args.thread_id}}
        snap = graph.get_state(cfg)
        view = {
            "next": snap.next,
            "facts": {k: f.model_dump() for k, f in snap.values.get("facts", {}).items()},
            "style": snap.values.get("style").model_dump() if snap.values.get("style") else None,
            "elements": [e.model_dump() for e in snap.values.get("elements", [])],
            "versions": [{"n": v.n, "note": v.note, "png": v.png_path} for v in snap.values.get("versions", [])],
            "notes": snap.values.get("notes", []),
        }
        print(json.dumps(view, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
