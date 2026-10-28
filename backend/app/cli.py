"""命令行驱动。

  uv run python -m app.cli new "需求" [--fake] [--image 路径] [--scenario poster|research]
  uv run python -m app.cli resume <thread_id> '<json>' [--fake] [--scenario ...]
  uv run python -m app.cli state <thread_id> [--scenario ...]

每次运行到 interrupt 时打印确认点载荷；用 resume 回传答案继续。

调研场景的回传格式：
  澄清问题   {"answers": {"q1": "..."}}（未回答的记为假设）
  动态提问   {"answer": "..."} 或 {"skip": true}
  终审       报告校验过就已经导出；这里是可选的修改，改完会重新导出覆盖
             {"action": "approve"}（不用改了，结束） | {"action": "exclude_source", "target": "s2"}
             | {"action": "edit_claim", "target": "k1", "content": "..."}
             | {"action": "rewrite_section", "target": "c1", "instruction": "..."}
"""

from __future__ import annotations

import argparse
import json
import uuid

from langgraph.types import Command

from app import config
from app.graph.poster.graph import build_graph as build_poster
from app.graph.poster.graph import open_sqlite_checkpointer as open_poster_saver
from app.graph.research.agent import ask_resume, ask_view, is_ask_interrupt
from app.graph.research.graph import build_graph as build_research
from app.graph.research.graph import open_sqlite_checkpointer as open_research_saver
from app.graph.research.state import AskAnswer
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


def pending_interrupt(graph, cfg: dict) -> dict | None:
    for task in graph.get_state(cfg).tasks:
        for it in task.interrupts:
            return it.value
    return None


def research_resume_payload(pending: dict | None, payload: dict) -> dict:
    """把统一的回答格式转成图需要的 resume 值；agent 的动态提问要转成中间件的 decisions 格式。"""
    if is_ask_interrupt(pending):
        a = AskAnswer.model_validate(payload)
        return ask_resume(a.answer, a.skip, pending)
    return payload


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
            "exported": v.get("exported"),
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
            payload = research_resume_payload(pending_interrupt(graph, cfg), payload)
        _report(graph.invoke(Command(resume=payload), cfg), args.thread_id)
    else:
        cfg = {"configurable": {"thread_id": args.thread_id}}
        print(json.dumps(_state_view(args.scenario, graph.get_state(cfg)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
