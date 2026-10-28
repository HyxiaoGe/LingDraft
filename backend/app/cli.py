"""命令行驱动。

  uv run python -m app.cli new "需求" [--fake] [--image 路径] [--scenario poster|research]
  uv run python -m app.cli resume <thread_id> '<json>' [--fake] [--scenario ...]
  uv run python -m app.cli state <thread_id> [--scenario ...]

每次运行到 interrupt 时打印确认点载荷；用 resume 回传答案继续。

调研场景的回传格式（与接口层同一份校验，不合法的不会交给图）：
  澄清问题   {"answers": {"q1": "..."}}（null 或不传的记为假设）
  动态提问   {"answer": "..."} 或 {"skip": true}，二选一
  终审       每一版报告写出来就是交付物（下载包在 package/）；这里是可选的修改，改完出新一版
             {"action": "approve"}（不用改了，结束） | {"action": "exclude_source", "target": "s2"}
             | {"action": "edit_claim", "target": "k1", "content": "..."}
             | {"action": "rewrite_section", "target": "c1", "instruction": "..."}
             | {"action": "instruct", "text": "用户原话"}（交给模型理解成上面几种操作）

海报终审除了按钮的几种操作，也可以 {"action": "instruct", "text": "用户原话"}，由模型理解成改文案 / 改事实 / 换风格 / 导出。
"""

from __future__ import annotations

import argparse
import json
import uuid

from langgraph.types import Command

from app import config
from app.graph.poster.graph import build_graph as build_poster
from app.graph.poster.graph import open_sqlite_checkpointer as open_poster_saver
from app.graph.research.agent import ask_view, is_ask_interrupt
from app.graph.research.answers import InvalidAnswer, resume_value
from app.graph.research.graph import build_graph as build_research
from app.graph.research.graph import open_sqlite_checkpointer as open_research_saver
from app.llm import ArkPlanner, FakePlanner
from app.research_llm import FakeResearchPlanner, LiveResearchPlanner


def _graph(scenario: str, fake: bool):
    if scenario == "research":
        planner = FakeResearchPlanner() if fake else LiveResearchPlanner()
        saver = open_research_saver(config.DATA_DIR / "checkpoints_research.sqlite")
        return build_research(planner, saver, config.OUTPUT_DIR / "research")
    planner = FakePlanner() if fake else ArkPlanner()
    saver = open_poster_saver(config.DATA_DIR / "checkpoints.sqlite")
    return build_poster(planner, saver, config.OUTPUT_DIR)


def pending(graph, cfg: dict):
    """当前挂起的中断（带 id 和 value）；没有就是 None。"""
    for task in graph.get_state(cfg).tasks:
        for it in task.interrupts:
            return it
    return None


def research_resume(graph, cfg: dict, payload: dict) -> Command:
    """校验回答并按中断 id 恢复；校验不过抛 InvalidAnswer，图看不到这个回答。"""
    it = pending(graph, cfg)
    if it is None:
        raise InvalidAnswer("这个线程没有在等回答")
    return Command(resume={it.id: resume_value(it.value, payload)})


def interrupt_view(value: dict) -> dict:
    return ask_view(value) if is_ask_interrupt(value) else value


def _report(result: dict, thread_id: str) -> None:
    print(f"thread_id: {thread_id}")
    interrupts = result.get("__interrupt__") or []
    if interrupts:
        view = dict(interrupt_view(interrupts[0].value))
        report = view.pop("report", None)
        print("== 等待你的输入 ==")
        print(json.dumps(view, ensure_ascii=False, indent=2))
        if report:
            print("---- 报告 ----")
            print(report)
    elif result.get("exported"):
        print("== 已导出 ==")
        print(json.dumps(result["exported"], ensure_ascii=False, indent=2))
    else:
        print("== 运行结束，无待确认项 ==")


def _state_view(scenario: str, snap) -> dict:
    v = snap.values
    if scenario == "research":
        return {
            "next": snap.next,
            "scope": v.get("scope").model_dump() if v.get("scope") else None,
            "outline": [n.model_dump() for n in v.get("outline", [])],
            "answer": v.get("answer"),
            "answer_how": v.get("answer_how"),
            "sources": [s.model_dump(exclude={"excerpt"}) for s in v.get("sources", [])],
            "claims": [c.model_dump() for c in v.get("claims", [])],
            "assumptions": v.get("assumptions", []),
            "asks_used": v.get("asks_used", 0),
            "versions": [x.model_dump() for x in v.get("versions", [])],
            "checks": [c.model_dump() for c in v.get("check_results", [])],
            "notes": v.get("notes", []),
        }
    return {
        "next": snap.next,
        "facts": {k: f.model_dump() for k, f in v.get("facts", {}).items()},
        "style": v.get("style").model_dump() if v.get("style") else None,
        "elements": [e.model_dump() for e in v.get("elements", [])],
        "versions": [{"n": x.n, "note": x.note, "png": x.png_path} for x in v.get("versions", [])],
        "notes": v.get("notes", []),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", choices=["poster", "research"], default="poster")
    ap.add_argument("--fake", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_new = sub.add_parser("new")
    p_new.add_argument("request")
    p_new.add_argument("--image", default=None)
    p_res = sub.add_parser("resume")
    p_res.add_argument("thread_id")
    p_res.add_argument("payload")
    p_state = sub.add_parser("state")
    p_state.add_argument("thread_id")
    args = ap.parse_args()

    graph = _graph(args.scenario, args.fake)
    if args.cmd == "new":
        thread_id = uuid.uuid4().hex[:8]
        cfg = {"configurable": {"thread_id": thread_id}}
        inp = {"request": args.request} if args.scenario == "research" else {"request": args.request, "product_image": args.image}
        _report(graph.invoke(inp, cfg), thread_id)
    elif args.cmd == "resume":
        cfg = {"configurable": {"thread_id": args.thread_id}}
        payload = json.loads(args.payload)
        if args.scenario == "research":
            try:
                cmd = research_resume(graph, cfg, payload)
            except InvalidAnswer as e:
                raise SystemExit(f"回答不合法：{e}") from None
        else:
            cmd = Command(resume=payload)
        _report(graph.invoke(cmd, cfg), args.thread_id)
    else:
        cfg = {"configurable": {"thread_id": args.thread_id}}
        print(json.dumps(_state_view(args.scenario, graph.get_state(cfg)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
