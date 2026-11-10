"""命令行驱动。

海报：
  uv run python -m app.cli new "需求" [--fake] [--image 路径]
  uv run python -m app.cli resume <thread_id> '<json>' [--fake]
  uv run python -m app.cli state <thread_id>

  每次运行到 interrupt 时打印确认点载荷；用 resume 回传答案继续。终审除了按钮的几种操作，
  也可以 {"action": "instruct", "text": "用户原话"}，由模型理解成改文案 / 改事实 / 换风格 / 导出。

对话（--scenario research）：一条线程就是一段对话，和页面走同一个运行器。
  uv run python -m app.cli --scenario research new "第一句话" [--fake]
  uv run python -m app.cli --scenario research say <thread_id> "接着说"
  uv run python -m app.cli --scenario research retry <thread_id>
  uv run python -m app.cli --scenario research regenerate <thread_id>
  uv run python -m app.cli --scenario research state <thread_id>
  uv run python -m app.cli --scenario research dream      # 马上整理一次长期记忆（平时每天按时整理，见 graph.research.memory）

  运行时逐字打印思考和正文、每一步工具。
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


class _Printer:
    """逐字打印：思考和正文按片段接着写，工具步骤、标题各占一行。一个事件调一次。"""

    def __init__(self) -> None:
        self.current = None

    def __call__(self, ev: dict) -> None:
        name, data = ev["event"], ev["data"]
        if name == "delta":
            key = (data["id"], data["kind"])
            if key != self.current:
                sys.stdout.write("\n[思考] " if data["kind"] == "thinking" else "\n[正文] ")
                self.current = key
            sys.stdout.write(data["text"])
            sys.stdout.flush()
            return
        self.current = None
        if name == "step" and not data["step"]["pending"]:
            s = data["step"]
            print(f"\n[{s['kind']}] {s['text']}  {'✓' if s['ok'] else '✗ ' + s['message']}")
        elif name == "title":
            print(f"[标题] {data['title']}")


def research(args) -> None:
    from app.api.research_runner import ResearchRunner

    from app import costs

    costs.open_ledger()  # 命令行调的模型也记成本（见 app.costs），退出时写完
    r = ResearchRunner(fake=args.fake)
    if args.cmd == "dream":
        print(json.dumps(r.dream(), ensure_ascii=False, indent=2))
        books = r.memory.by_owner()
        print("\n".join(f"{owner or '（登录以前共用的）'}\n" + "\n".join(f"  [{m['id']}] {m['text']}" for m in ms) for owner, ms in books.items()) or "（没有记忆）")
        return
    tid = getattr(args, "thread_id", None) or str(uuid.uuid4())
    if args.cmd in ("new", "say"):
        r.accept(tid, args.text)
    elif args.cmd == "retry":
        r.retry_input(tid)
    elif args.cmd == "regenerate":
        r.regenerate_input(tid)
    else:
        v = r.state_view(tid)
        print(json.dumps({k: v.get(k) for k in ("status", "error", "title")}, ensure_ascii=False, indent=2))
        return
    print(f"thread_id: {tid}")
    r.run_turn(tid, None, printer := _Printer())
    while (ask := r.state_view(tid)["ask"]) is not None:  # 模型停下来问了：在这里回答（选项的编号，或者自己写；直接回车是跳过），接着跑
        print(f"\n[问] {ask['question']}" + "".join(f"\n  {i}. {o}" for i, o in enumerate(ask["options"], 1)))
        said = input("回答：").strip()
        text = ask["options"][int(said) - 1] if said.isdigit() and 0 < int(said) <= len(ask["options"]) else said
        r.run_turn(tid, r.answer_input(tid, ask["id"], text, skip=not text), printer)
    print()
    v = r.state_view(tid)
    error = r.failure_detail(tid) or v.get("error")  # 命令行给开发看：异常原文
    print(f"== {v['status']}" + (f"：{error}" if error else ""))


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
    p_res = sub.add_parser("resume")  # 海报场景
    p_res.add_argument("thread_id")
    p_res.add_argument("payload")
    p_retry = sub.add_parser("retry")
    p_retry.add_argument("thread_id")
    p_regen = sub.add_parser("regenerate")
    p_regen.add_argument("thread_id")
    p_state = sub.add_parser("state")
    p_state.add_argument("thread_id")
    sub.add_parser("dream")  # 对话场景
    args = ap.parse_args()
    (research if args.scenario == "research" else poster)(args)


if __name__ == "__main__":
    main()
