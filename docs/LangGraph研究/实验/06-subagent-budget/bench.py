"""实验 06：子 agent 查个没完。拿 2026-11-01 两次真实运行里 Pro 写好的 6 份交代（tasks.json）当固定输入，直接跑子 agent（不经过 Pro），
比较几种让它收住的做法：每个子 agent 调了几次模型、搜了几次、读了几页、花了多少钱、报告多长。真实模型，要花钱（一种做法约 0.1～0.17 美元）。

做法（VARIANTS）：
- base：现在的提示词（Anthropic 研究子 agent 的预算说法，写明 12 次上限）。
- odr：换成 open_deep_research 研究员的写法：搜索 2～3 / 最多 5 次、具体的停止条件，不说代码上限。
- cap6：提示词不动，代码上限 12 → 6。
- odr2：odr 再把读页也算进预算。
- tracker：odr2 + BATS（arXiv 2511.17006）的 Budget Tracker：每次工具结果回来，请求末尾加一条各工具用了几次、还剩几次（不存进记录）。

用法（在 backend 下）：
  PYTHONPATH=. LINGDRAFT_DATABASE_URL= uv run python "../docs/LangGraph研究/实验/06-subagent-budget/bench.py" tracker [--only deep|cities] [--fake]
  PYTHONPATH=. LINGDRAFT_DATABASE_URL= uv run python "../docs/LangGraph研究/实验/06-subagent-budget/judge.py" odr cap6 odr2 tracker
结果写到 backend/output/subagent-budget/<做法>/（不进仓库）。judge.py 拿每种做法的报告和 base 的成对比，Flash 当评委。

要在 ba1eeea 上跑：第 06 篇的数是 0d24e84 的产品代码跑的。2c456a3 起产品默认挂上 8/8 预算和每步提醒，在之后的代码上跑，
每种做法都会带上 8/8，base 的提示词也已经换成了 odr2 那样的写法；tracker 自己的 5/5 提醒还会被 SubagentPrompt 先加的那条挡掉
（它看到的最后一条不再是工具结果），比的就不是原来那几样了。所以在现在的代码上脚本直接退出。复现（在仓库根目录）：
  git worktree add ../lingdraft-ba1eeea ba1eeea && cp backend/.env ../lingdraft-ba1eeea/backend/
再到 ../lingdraft-ba1eeea/backend 下按上面的用法跑。
"""
from __future__ import annotations

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.graph.research import history, prompts, subagent
from app.graph.research.agent import ChatContext
from app.graph.research.sources import cited
from app.research_llm import FakeResearchPlanner, LiveResearchPlanner

HERE = Path(__file__).parent
OUT = Path("output/subagent-budget")  # 相对 backend
PRICE = (0.003, 0.15, 0.6)  # Flash 闲时价：缓存命中、未命中、输出（美元/百万 token）
STARTED = "2026-11-01T20:57:03+08:00"

# 粗略的关键事实（从基线报告里来，只是看紧预算会不会丢掉主干，评测脚本里用正则没关系）
FACTS = {
    "Rust": [r"1\.97\.0", r"1\.98\.0", r"1\.99\.0", r"1\.97\.1", r"1\.98\.1", r"1\.99\.1", r"1\.100"],
    "Go": [r"1\.27(\.0)?\b", r"1\.27\.1", r"1\.27\.2", r"1\.27\.3", r"(2026-)?0?8-19|8\s*月\s*19"],
    "Zig": [r"0\.17\.0", r"(2026[-‑])?10[-‑]0?2|10\s*月\s*2\s*日", r"0\.18"],
    "杭州": [r"180\s*万", r"15\s*%", r"现房"],
    "成都": [r"80\s*万", r"120\s*万", r"15\s*%", r"现房"],
    "武汉": [r"150\s*万", r"15\s*%", r"现房", r"1\.5\s*%"],
}


def tasks() -> list[dict]:
    return json.loads((HERE / "tasks.json").read_text())


# open_deep_research 研究员提示词（prompts.py:138-185）的 Instructions + Hard Limits + Show Your Thinking，
# tavily_search 换成 search_web；它的 think_tool 换成“在思考里评估”（Flash 每步都有思考）；不告诉模型代码上限（ODR 也不告诉）
ODR_PROCESS = """[Research process]
Think like a human researcher with limited time. Follow these steps:
1. Read the task carefully: what specific information does it need?
2. Start with broader searches: use broad, comprehensive queries first.
3. After each search, pause and assess: do I have enough to answer? What's still missing?
4. Execute narrower searches as you gather information, to fill in the gaps.
5. Stop when you can answer confidently. Don't keep searching for perfection.

[Hard limits]
Tool call budgets (prevent excessive searching):
- Simple tasks: use 2-3 search_web calls maximum.
- Complex tasks: use up to 5 search_web calls maximum.
- Always stop after 5 search_web calls if you cannot find the right sources.
Stop immediately when:
- You can complete the task comprehensively.
- You have 3+ relevant sources for the question.
- Your last 2 searches returned similar information.

[Show your thinking]
After each round of results, assess in your reasoning: What key information did I find? What's missing? Do I have enough to complete the task comprehensively? Should I search more or write the report?"""

# odr2：预算把读页也管上（odr 里它把省下的搜索换成了读页）
ODR2_PROCESS = ODR_PROCESS.replace("""- Simple tasks: use 2-3 search_web calls maximum.
- Complex tasks: use up to 5 search_web calls maximum.""", """- Simple tasks: use 2-3 search_web calls and 1-2 read_page calls maximum.
- Complex tasks: use up to 5 search_web calls and up to 5 read_page calls maximum.""")
assert ODR2_PROCESS != ODR_PROCESS

BUDGET = {"search_web": 5, "read_page": 5}


class Tracker(subagent.SubagentPrompt):
    """BATS（arXiv 2511.17006 §3.1）的 Budget Tracker：每次工具结果回来，请求末尾加一条各工具用了几次、还剩几次（不存进记录）。"""

    def wrap_model_call(self, request, handler):
        def with_budget(req):
            if req.messages and isinstance(req.messages[-1], ToolMessage) and req.tool_choice != "none":
                calls = [tc["name"] for m in req.messages if isinstance(m, AIMessage) for tc in m.tool_calls]
                lines = [f"{t} Budget Used: {calls.count(t)}, {t} Budget Remaining: {max(0, n - calls.count(t))}" for t, n in BUDGET.items()]
                note = HumanMessage("<budget>\n" + "\n".join(lines) + "\nMake the best use of the available resources.\n</budget>")
                req = req.override(messages=[*req.messages, note])
            return handler(req)
        return super().wrap_model_call(request, with_budget)


VARIANTS: dict[str, dict] = {
    "base": {},  # 现在提交了的（0d24e84）
    "odr": {"prompts": {"subagent.process": ODR_PROCESS}},
    "cap6": {"max_calls": 6},
    "odr2": {"prompts": {"subagent.process": ODR2_PROCESS}},
    "tracker": {"prompts": {"subagent.process": ODR2_PROCESS}, "middleware": Tracker},
}


def apply(variant: dict) -> None:
    texts = prompts.texts()
    for k, v in variant.get("prompts", {}).items():
        texts[k] = v
    prompts.PROMPTS._template.cache_clear()
    if "max_calls" in variant:
        subagent.MAX_MODEL_CALLS = variant["max_calls"]
    if "middleware" in variant:  # 换掉 SubagentPrompt：子类里改请求
        subagent.SubagentPrompt = variant["middleware"]
    if "order" in variant:
        subagent.SYSTEM_PROMPT = variant["order"]


def cost(u) -> float:
    hit, miss, out = PRICE
    return (u.cached_tokens * hit + (u.input_tokens - u.cached_tokens) * miss + u.output_tokens * out) / 1e6


def run_one(worker, t: dict, variant: dict) -> dict:
    prompt = t["prompt"] + variant.get("task_suffix", "")
    t0 = time.time()
    out = worker.invoke({"messages": [HumanMessage(prompt)], "started_at": STARTED, "parent_sources": {}},
                        context=ChatContext(images=None, check_stop=lambda: None))
    secs = time.time() - t0
    msgs = out["messages"]
    ais = [m for m in msgs if isinstance(m, AIMessage)]
    calls = [tc["name"] for m in ais for tc in m.tool_calls]
    report = history.final_text(msgs) or ""
    u = history.usage_of(msgs)
    facts = [p for p in FACTS[t["key"]] if re.search(p, report)]
    return {"key": t["key"], "set": t["set"], "secs": round(secs), "model_calls": len(ais),
            "tool_rounds": sum(bool(m.tool_calls) for m in ais), "searches": calls.count("search_web"),
            "reads": calls.count("read_page"), "report_chars": len(report), "citations": len(cited(report)),
            "facts": f"{len(facts)}/{len(FACTS[t['key']])}", "missing": [p for p in FACTS[t["key"]] if p not in facts],
            "cost": round(cost(u), 4), "hit_cap": len(ais) >= subagent.MAX_MODEL_CALLS,
            "report": report, "messages": [m.model_dump(mode="json") for m in msgs if not isinstance(m, ToolMessage)]}


def main() -> None:
    if getattr(subagent, "TOOL_BUDGET", None):  # 产品代码已经带预算了：见开头“要在 ba1eeea 上跑”
        sys.exit("bench.py 要在 ba1eeea 上跑，现在的产品代码默认带 8/8 预算，比的就不是原来那几样了。复现方法见脚本开头。")
    name = sys.argv[1]
    only =sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
    variant = VARIANTS[name]
    apply(variant)
    planner = FakeResearchPlanner() if "--fake" in sys.argv else LiveResearchPlanner()
    worker = subagent.build_subagent(planner)
    ts = [t for t in tasks() if only in (None, t["set"])]
    d = OUT / name
    d.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda t: run_one(worker, t, variant), ts))
    for r in results:
        (d / f"{r['key']}.report.md").write_text(r["report"])
        (d / f"{r['key']}.json").write_text(json.dumps(r, ensure_ascii=False, indent=1))
    print(f"== {name}  ({time.time() - t0:.0f}s，{len(results)} 个任务)")
    for r in results:
        print(f"  {r['key']:4} {r['secs']:4d}s 调模型 {r['model_calls']:2d}{'(到顶)' if r['hit_cap'] else '     '} 搜 {r['searches']:2d} 读 {r['reads']:2d}"
              f"  报告 {r['report_chars']:5d}字 引用 {r['citations']:3d}  事实 {r['facts']} 缺 {r['missing']}  ${r['cost']:.3f}")
    print(f"  合计 ${sum(r['cost'] for r in results):.3f}，搜 {sum(r['searches'] for r in results)} 读 {sum(r['reads'] for r in results)}")


if __name__ == "__main__":
    main()
