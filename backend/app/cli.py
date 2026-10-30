"""命令行驱动。

海报：
  uv run python -m app.cli new "需求" [--fake] [--image 路径]
  uv run python -m app.cli resume <thread_id> '<json>' [--fake]
  uv run python -m app.cli state <thread_id>

  每次运行到 interrupt 时打印确认点载荷；用 resume 回传答案继续。终审除了按钮的几种操作，
  也可以 {"action": "instruct", "text": "用户原话"}，由模型理解成改文案 / 改事实 / 换风格 / 导出。

调研（--scenario research）：一条线程就是一段对话，和页面走同一个运行器、同一份校验。
  uv run python -m app.cli --scenario research new "第一句话" [--fake]
  uv run python -m app.cli --scenario research say <thread_id> "接着说" [--quote '<json>'] [--action '<json>']
  uv run python -m app.cli --scenario research resume <thread_id> '{"answer": "..."}'   （或 {"skip": true}）
  uv run python -m app.cli --scenario research retry <thread_id>
  uv run python -m app.cli --scenario research state <thread_id>

  运行时逐字打印思考和正文、每一步工具；停在提问上时打印提问卡。
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid

from langgraph.types import Command

from app import config
from app.graph.poster.graph import build_graph as build_poster
from app.graph.poster.graph import open_sqlite_checkpointer as open_poster_saver
from app.llm import ArkPlanner, FakePlanner


def _poster_report(result: dict, thread_id: str) -> None:
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


def poster(args) -> None:
    planner = FakePlanner() if args.fake else ArkPlanner()
    graph = build_poster(planner, open_poster_saver(config.DATA_DIR / "checkpoints.sqlite"), config.OUTPUT_DIR)
    if args.cmd == "new":
        thread_id = uuid.uuid4().hex[:8]
        cfg = {"configurable": {"thread_id": thread_id}}
        _poster_report(graph.invoke({"request": args.text, "product_image": args.image}, cfg), thread_id)
    elif args.cmd == "resume":
        cfg = {"configurable": {"thread_id": args.thread_id}}
        _poster_report(graph.invoke(Command(resume=json.loads(args.payload)), cfg), args.thread_id)
    elif args.cmd == "state":
        snap = graph.get_state({"configurable": {"thread_id": args.thread_id}})
        v = snap.values
        print(json.dumps({"next": snap.next, "facts": {k: f.model_dump() for k, f in v.get("facts", {}).items()},
                          "versions": [{"n": x.n, "note": x.note, "png": x.png_path} for x in v.get("versions", [])],
                          "notes": v.get("notes", [])}, ensure_ascii=False, indent=2))
    else:
        raise SystemExit(f"海报场景没有 {args.cmd}")


def _print_events(events) -> None:
    """逐字打印：思考和正文按片段接着写，工具步骤、提问、出错各占一行。"""
    current = None
    for ev in events:
        name, data = ev["event"], ev["data"]
        if name == "delta":
            key = (data["id"], data["kind"])
            if key != current:
                sys.stdout.write("\n[思考] " if data["kind"] == "thinking" else "\n[正文] ")
                current = key
            sys.stdout.write(data["text"])
            sys.stdout.flush()
            continue
        current = None
        if name == "step" and not data["step"]["pending"]:
            s = data["step"]
            print(f"\n[{s['kind']}] {s['text']}  {'✓' if s['ok'] else '✗ ' + s['message']}")
        elif name == "title":
            print(f"[标题] {data['title']}")
        elif name == "interrupt" and data:
            print("\n== 提问 ==\n" + json.dumps(data, ensure_ascii=False, indent=2))
    print()


def research(args) -> None:
    from app.api.research_runner import ResearchRunner, StaleInterrupt
    from app.graph.research.answers import InvalidAnswer

    r = ResearchRunner(fake=args.fake)
    tid = getattr(args, "thread_id", None) or uuid.uuid4().hex[:8]
    try:
        if args.cmd == "new":
            r.accept(tid, args.text)
            inp = None
        elif args.cmd == "say":
            r.accept(tid, args.text, json.loads(args.quote) if args.quote else None, json.loads(args.action) if args.action else None)
            inp = None
        elif args.cmd == "resume":
            it = r.pending(tid)
            inp = r.resume_input(tid, json.loads(args.payload), it.id if it else None)
        elif args.cmd == "retry":
            inp = r.retry_input(tid)
        else:
            v = r.state_view(tid)
            print(json.dumps({k: v.get(k) for k in ("status", "error", "title", "versions", "interrupt")}, ensure_ascii=False, indent=2))
            return
    except (InvalidAnswer, StaleInterrupt) as e:
        raise SystemExit(f"不合法：{e}") from None
    print(f"thread_id: {tid}")
    _print_events(r._graph_events(tid, inp))
    v = r.state_view(tid)
    print(f"== {v['status']}" + (f"：{v['error']}" if v.get("error") else "") + (f"，报告第 {v['report']['version']} 版" if v.get("report") else ""))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", choices=["poster", "research"], default="poster")
    ap.add_argument("--fake", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_new = sub.add_parser("new")
    p_new.add_argument("text")
    p_new.add_argument("--image", default=None)
    p_say = sub.add_parser("say")
    p_say.add_argument("thread_id")
    p_say.add_argument("text")
    p_say.add_argument("--quote", default=None)
    p_say.add_argument("--action", default=None)
    p_res = sub.add_parser("resume")
    p_res.add_argument("thread_id")
    p_res.add_argument("payload")
    p_retry = sub.add_parser("retry")
    p_retry.add_argument("thread_id")
    p_state = sub.add_parser("state")
    p_state.add_argument("thread_id")
    args = ap.parse_args()
    (research if args.scenario == "research" else poster)(args)


if __name__ == "__main__":
    main()
