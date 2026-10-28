"""ask_user 自然触发探针：跳过 intake，直接让调研 agent 面对一个“选型取决于用户内部约束”的任务。

三个版本：缺约束（期望途中问）、缺约束但用户的回答不同（期望推荐随之变）、补上约束（期望不问、直接推荐）。
提示里不写“请提问”，不点名工具。

  uv run python scripts/probe_ask.py [--reps 2] [--out ../docs/probe/ask/run1.json]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from langgraph.types import Command

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.cli import pending_interrupt, research_resume_payload
from app.graph.research.agent import ask_view, is_ask_interrupt
from app.graph.research.graph import build_graph, memory_checkpointer
from app.graph.research.state import Scope
from app.research_llm import IntakeResult, LiveResearchPlanner

TASK = ("我们公司要给内部员工上一个基于大模型的知识库问答。帮我在「直接调 DeepSeek 官方 API」和「私有化部署开源 DeepSeek 模型」"
        "之间选一个，给出明确推荐和理由。")
CONSTRAINT = "补充：内部文档涉及客户合同，按公司规定不能传到任何外部服务；我们有 3 人的运维团队和几台 A100。"
VARIANTS = {
    "missing_A": (TASK, "可以，没有合规限制，文档都是公开资料"),
    "missing_B": (TASK, "不能，内部文档涉及客户合同，公司规定不能出网"),
    "missing_skip": (TASK, None),
    "given": (TASK + "\n" + CONSTRAINT, "（不应该问）"),
}


class NoIntakePlanner(LiveResearchPlanner):
    def intake(self, request: str) -> IntakeResult:
        return IntakeResult(scope=Scope(question=request), questions=[])


def run(variant: str, rep: int, out_dir: Path) -> dict:
    request, reply = VARIANTS[variant]
    g = build_graph(NoIntakePlanner(), memory_checkpointer(), out_dir)
    cfg = {"configurable": {"thread_id": f"{variant}_{rep}"}}
    t0 = time.time()
    r = g.invoke({"request": request}, cfg)
    asks = []
    while True:
        ints = r.get("__interrupt__") or []
        if not ints:
            break
        val = ints[0].value
        if not is_ask_interrupt(val):
            break  # 终审卡
        view = ask_view(val)
        asks.append(view)
        print(f"  [{variant}#{rep}] ask: {view['title']} {view['options']} | {view['why']}", flush=True)
        payload = {"skip": True} if reply is None else {"answer": reply}
        r = g.invoke(Command(resume=research_resume_payload(pending_interrupt(g, cfg), payload)), cfg)
    v = g.get_state(cfg).values
    rec = {"variant": variant, "rep": rep, "elapsed": round(time.time() - t0, 1), "asks": asks, "asks_used": v.get("asks_used"),
           "answer_how": v.get("answer_how"), "sources": len(v.get("sources", [])), "answer": v.get("answer"),
           "assumptions": v.get("assumptions", []), "checks_failed": [c.name for c in v.get("check_results", []) if not c.passed]}
    print(f"  [{variant}#{rep}] {rec['elapsed']}s asks={len(asks)} how={rec['answer_how']} 结论: {(v.get('answer') or '')[:160]!r}", flush=True)
    return rec


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--variants", nargs="*", default=list(VARIANTS))
    ap.add_argument("--out", default="../docs/probe/ask/run1.json")
    a = ap.parse_args()
    out_dir = Path(__file__).resolve().parents[1] / "output" / "probe_ask"
    results = []
    for variant in a.variants:
        for rep in range(a.reps):
            try:
                results.append(run(variant, rep, out_dir))
            except Exception as e:  # noqa: BLE001
                print(f"  [{variant}#{rep}] ERROR {type(e).__name__}: {e}", flush=True)
                results.append({"variant": variant, "rep": rep, "error": f"{type(e).__name__}: {e}"})
            Path(a.out).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
