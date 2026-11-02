"""实验 06 第二步：端到端。主 agent 用 Pro、子 agent 用 Flash，同样两个问题，比较三种：现在的（不设预算）、每个子 agent 搜 5 读 5、搜 8 读 8。
看的是用户最后看到的回答：bench.py 里少掉的细节，经过主 agent 汇总以后还在不在。真实模型，一次约 0.07～0.12 美元。

用法（在 backend 下，一次跑一个，好在中间看搜索额度）：
  PYTHONPATH=. LINGDRAFT_DATABASE_URL= uv run python "../docs/LangGraph研究/实验/06-subagent-budget/e2e.py" b8 deep
做法：base（ba1eeea 的提示词，不设预算）、b5、b8。问题：deep（Rust/Go/Zig）、cities（三城购房政策）。
结果写到 backend/output/subagent-budget/e2e/<做法>-<问题>.json 和 .answer.md。不碰 8000、不碰本地库：内存检查点，数据目录在 output 下。

第 06 篇的数是 2c456a3 的代码跑的。b5、b8 直接用产品代码，base 只是去掉预算、换回提示词，所以以后产品代码一改，三种都会跟着变。
要复现那次的结果，先 git worktree add 到 2c456a3（.env 拷过去），在那边跑。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from app.graph.research import prompts, subagent

OUT = Path("output/subagent-budget/e2e")  # 相对 backend
PRICE = {"main": (0.022, 0.66, 1.98), "sub": (0.003, 0.15, 0.6)}  # 闲时价：缓存命中、未命中、输出（美元/百万 token）
Q = {
    "deep": "帮我分别深入调研一下 Rust、Go、Zig 这三门语言 2026 年下半年各自的版本更新和主要变化",
    "cities": "帮我对比一下杭州、成都、武汉这三个城市 2026 年出台的购房支持政策",
}
# ba1eeea 的 [subagent].process（Anthropic 研究子 agent 的预算说法），上限的数直接写进去
BASE_PROCESS = """[Research process]
1. Plan first: review what the task asks for and set a research budget, roughly how many tool calls this task needs. Adapt it to the task: a simple fact needs under 5 tool calls, a medium task about 5, a hard task about 10, and a very difficult or multi-part task up to 15. Stick to this budget to remain efficient; going over it hits your limit: after 12 responses in all, tools are turned off and you must report with what you have.
2. Research loop: after each result, consider what you have found, what is still missing, and which query or page would fill the gap best. Issue independent searches in parallel in one response. Never repeat the same query; if an approach is not working, try another angle.
3. As soon as you have what the task needs, or new results stop adding relevant information, stop using tools and write the report."""
VARIANTS = {"base": None, "b5": 5, "b8": 8}


def setup(variant: str) -> None:
    n = VARIANTS[variant]
    if n is None:  # 不设预算：不挂 ToolBudget、不加提醒，提示词换回去
        subagent.TOOL_BUDGET = {}
        prompts.texts()["subagent.process"] = BASE_PROCESS
        prompts.PROMPTS._template.cache_clear()
    else:
        subagent.TOOL_BUDGET = {"search_web": n, "read_page": n}


def cost(u: dict, who: str) -> float:
    hit, miss, out = PRICE[who]
    c = u["cached_tokens"]
    return (c * hit + (u["input_tokens"] - c) * miss + u["output_tokens"] * out) / 1e6


def main() -> None:
    variant, q = sys.argv[1], sys.argv[2]
    setup(variant)  # 先改，再建图：ToolBudget 在建子 agent 时挂上
    from app.api.research_runner import ResearchRunner
    from app.api.runner import LocalRunRegistry
    from app.graph.research.graph import memory_checkpointer
    from app.graph.research.sources import cited

    OUT.mkdir(parents=True, exist_ok=True)
    r = ResearchRunner(fake="--fake" in sys.argv, data_dir=OUT / "data", checkpointer=memory_checkpointer())
    r.runs = LocalRunRegistry()
    tid = f"{variant}-{q}"
    r.accept(tid, Q[q])
    key = r._run_key(tid)
    r.runs.admit(key)
    r.runs.started(key)
    t0 = time.time()
    try:
        for _ in r._graph_events(tid, None):
            pass
    finally:
        r.runs.release(key)
    secs = time.time() - t0
    v = r.state_view(tid)
    turn = v["turns"][-1]
    rec = turn["record"]
    tasks = [it for it in turn["items"] if it["type"] == "step" and it["kind"] == "task"]
    answer = next((it["text"] for it in reversed(turn["items"]) if it["type"] == "text"), "")
    u, su = rec["usage"], rec["subagent_usage"]
    summary = {
        "variant": variant, "q": q, "secs": round(secs), "outcome": rec["outcome"], "answer_chars": len(answer),
        "answer_citations": len(set(cited(answer))), "main_cost": round(cost(u, "main"), 4), "sub_cost": round(cost(su, "sub"), 4),
        "main_searches": u["searches"], "main_reads": u["reads"], "subagents": u["subagents"],
        "tasks": [{"title": t["text"], "ok": t["ok"],
                   "model_calls": sum(s["type"] == "thinking" for s in t.get("items", [])),
                   "searches": sum(s["type"] == "step" and s["kind"] == "search" for s in t.get("items", [])),
                   "reads": sum(s["type"] == "step" and s["kind"] == "read" for s in t.get("items", []))} for t in tasks],
    }
    (OUT / f"{tid}.json").write_text(json.dumps({"summary": summary, "view": v}, ensure_ascii=False, indent=1, default=str))
    (OUT / f"{tid}.answer.md").write_text(answer)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
