"""决策点回放：从录好的那遍里挑一次调模型（第几轮第几次），把那之前的状态原样拿出来，只重跑这一次调模型，看模型这一下做了什么。

用法（在 backend 目录）：uv run python -m app.evals.decisions [--decision id ...] [--samples N] [--jobs N] [--no-sources] [--label X]
  [--no-smith] | --compare 结果文件A 结果文件B。决策点写在 evals/research/decisions.yaml，各项的意思见 evals/README.md。

- 状态从哪来：录像（research --record）时检查点每一步都存（runner.durability = "sync"；页面照旧一轮结束才写），
  每次调模型之前的那个检查点存进 data/evals/tapes/<用例>.snapshots.json（不进仓库：里面有搜到、读到的内容）。
- 怎么重跑：那个检查点原样放进一个新的内存存储（只放检查点，不带它之后的写入，所以这一步一定重跑，不会拿录的结果顶上），
  从它接着跑，到执行工具之前停（interrupt_before=["tools"]），工具一个都不执行。
  模型出了工具调用就看调了什么；没调工具就是这一轮的正文，照用例那样让裁判看、核对引用。
- 用的是现在的代码：系统提示词、压缩、工具说明（从服务现取）都是新的；对话、工具结果、这一轮的日期是录的那时的（都在检查点里）。
  前面的步骤是固定的：改动要是会让模型前面搜别的词，这里看不出来，最后还要整段回放确认（research --replay）。
  工具结果的整理（来源编号、天气整理）在检查点里已经整理好了，改这部分代码这里也测不出来。
- 每个决策点跑几次（--samples），每项检查算过了几次；两份结果比（--compare），差得够大才算变了（双侧 Fisher 精确检验，p < 0.05）。
- 配了 LANGSMITH_API_KEY 的话同时传到 LangSmith 成一个实验，每重跑一次是一行（app.evals.smith），--no-smith 不传。
"""

from __future__ import annotations

import argparse
import base64
import json
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from datetime import datetime
from math import comb
from pathlib import Path
from typing import Any, Literal

import yaml
from langchain_core.messages import AIMessage, BaseMessage
from pydantic import BaseModel, ConfigDict, Field

from app.evals import smith
from app.graph.research import history
from app.graph.research.agent import CHINA_TZ, LOOP_ENTRY, ChatContext, turn_start
from app.graph.research.graph import memory_checkpointer
from app.graph.research.sources import collect_sources
from app.tools.research_tools import TIME_TOOL

BACKEND = Path(__file__).resolve().parents[2]
DECISIONS_FILE = BACKEND / "evals" / "research" / "decisions.yaml"
SIGNIFICANT = 0.05
FORK_TITLE = "决策点回放"  # 给运行的上下文一个标题：第一轮第一次调模型的检查点里还没有标题，不另外调模型起


# ---- 决策点 ----


class DecisionExpect(BaseModel):
    """这一下的工具调用。没写的项不检查。"""

    model_config = ConfigDict(extra="forbid")
    searched: bool | None = None
    looked_up: bool | None = None  # 搜索、读页、天气、地图、航班火车、子任务、查文档任一（同 research.LOOKUP_TOOLS）
    checked_time: bool | None = None
    replied: bool | None = None  # 这一下直接写了正文（没调工具）


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    case: str = Field(description="从这个用例录的那遍里取（evals/research/cases.yaml 的 id）")
    turn: int = Field(default=1, ge=1)
    call: int | Literal["last"] = Field(default=1, description="这一轮第几次调模型；last 是最后一次（录的那遍写正文的那一下）")
    source: str = Field(description="为什么有这个决策点：要回答什么问题")
    expect: DecisionExpect = Field(default_factory=DecisionExpect)
    judge: str | None = Field(default=None, description="这一下写了正文时，裁判按这个标准看（同用例的 judge）")


def load_decisions(path: Path = DECISIONS_FILE) -> list[Decision]:
    from app.evals.research import load_cases

    decisions = [Decision.model_validate(d) for d in yaml.safe_load(path.read_text(encoding="utf-8"))]
    ids = [d.id for d in decisions]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{path.name} 里有重复的决策点 id")
    cases = {c.id for c in load_cases()}
    if bad := sorted({d.case for d in decisions} - cases):
        raise ValueError(f"{path.name} 里的这些用例不存在：{bad}")
    return decisions


# ---- 录的时候存下每次调模型之前的检查点 ----


def snapshot_path(tape_path: Path) -> Path:
    """和工具录像放在一起：<用例>.json 旁边的 <用例>.snapshots.json。"""
    return tape_path.with_name(f"{tape_path.stem}.snapshots.json")


def _dump(serde, obj: Any) -> dict:
    kind, data = serde.dumps_typed(obj)
    return {"type": kind, "data": base64.b64encode(data).decode()}


def _load(serde, d: dict) -> Any:
    return serde.loads_typed((d["type"], base64.b64decode(d["data"])))


def snapshots(graph, config: dict) -> list[dict]:
    """这条线程里每次调模型之前的检查点，按先后，带上第几轮第几次（同一轮里出错重发、插话以后再调的都接着数）。"""
    saver = graph.checkpointer
    out = []
    for s in reversed(list(graph.get_state_history(config))):
        if s.next != (LOOP_ENTRY,):
            continue
        msgs = s.values["messages"]
        tup = saver.get_tuple(s.config)
        out.append({"turn": len(s.values.get("turns", [])),
                    "call": sum(isinstance(m, AIMessage) for m in msgs[history.last_human(msgs) + 1:]) + 1,
                    "checkpoint": _dump(saver.serde, tup.checkpoint), "metadata": _dump(saver.serde, tup.metadata)})
    return out


def save_snapshots(path: Path, graph, config: dict, recorded_at: datetime | None) -> None:
    data = {"recorded_at": recorded_at.isoformat() if recorded_at else None, "snapshots": snapshots(graph, config)}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def pick(snaps: list[dict], d: Decision) -> dict:
    mine = [s for s in snaps if s["turn"] == d.turn]
    if not mine:
        raise ValueError(f"{d.id}：录的那遍没有第 {d.turn} 轮")
    if d.call == "last":
        return max(mine, key=lambda s: s["call"])
    if not (got := [s for s in mine if s["call"] == d.call]):
        raise ValueError(f"{d.id}：录的那遍第 {d.turn} 轮只调了 {max(s['call'] for s in mine)} 次模型")
    return got[0]


# ---- 从检查点接着跑这一下 ----


async def afork(graph, snap: dict, thread_id: str) -> tuple[dict, list[BaseMessage]]:
    """检查点放进 graph 的存储（新的线程），接着跑到执行工具之前。返回 (检查点里的状态, 跑完的消息)。
    stream_mode=messages 让模型照页面那样流式地调。"""
    saver = graph.checkpointer
    checkpoint, metadata = _load(saver.serde, snap["checkpoint"]), _load(saver.serde, snap["metadata"])
    before = dict(checkpoint["channel_values"])
    cfg = saver.put({"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}, checkpoint, metadata, checkpoint["channel_versions"])
    async for _ in graph.astream(None, cfg, context=ChatContext(title=FORK_TITLE), interrupt_before=["tools"], stream_mode="messages",
                                 durability="exit"):
        pass
    state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    return before, state.values["messages"]


def _check(name: str, passed: bool, detail: str = "") -> dict:
    return {"name": name, "passed": bool(passed), "detail": detail}


def sample(runner, judge, d: Decision, snap: dict, n: int, check_sources: bool = True) -> dict:
    """跑一次这个决策点，做检查。"""
    from app import aio
    from app.evals.research import LOOKUP_TOOLS, cited, cited_sources, judge_citations, material

    started = time.time()
    out: dict[str, Any] = {"decision": d.id, "sample": n}
    try:
        state, msgs = aio.run(afork(runner.graph, snap, f"fork-{d.id}-{n}"))
        before = state["messages"]
        new = [m for m in msgs[len(before):] if isinstance(m, AIMessage)]
        if not new:
            raise RuntimeError("这一下没有模型消息")
        m = new[-1]
        names = [tc["name"] for tc in m.tool_calls]
        reply = history.final_text(msgs)
        i = history.last_human(before)
        today = turn_start(before, state).astimezone(CHINA_TZ).date().isoformat()  # 这一轮开始的那天（录的时候）
        u = m.usage_metadata or {}
        out.update({"say": history.text_of(before[i]), "today": today, "tool_calls": [{"name": tc["name"], "args": tc["args"]} for tc in m.tool_calls],
                    "reply": reply, "thinking": history.thinking_of(m),
                    "usage": {"input": u.get("input_tokens", 0), "cached": (u.get("input_token_details") or {}).get("cache_read", 0),
                              "output": u.get("output_tokens", 0)}})
        e, checks = d.expect, []
        if e.searched is not None:
            checks.append(_check("searched", ("search_web" in names) == e.searched, "、".join(names) or "没调工具"))
        if e.looked_up is not None:
            looked = [x for x in names if x in LOOKUP_TOOLS]
            checks.append(_check("looked_up", bool(looked) == e.looked_up, "、".join(looked) or "没查"))
        if e.checked_time is not None:
            checks.append(_check("checked_time", (TIME_TOOL in names) == e.checked_time, "、".join(names) or "没调工具"))
        if e.replied is not None:
            checks.append(_check("replied", bool(reply) == e.replied, "写了正文" if reply else "调了 " + "、".join(names)))
        citations: list[dict] = []
        if reply:
            sources = {s.id: s.url for s in collect_sources(msgs)}
            ids = cited(reply)
            unknown = [x for x in ids if x not in sources]
            checks.append(_check("cited_known", not unknown, f"不存在的来源：{'、'.join(unknown)}" if unknown else f"引用了 {len(ids)} 个来源"))
            if d.judge:
                v = judge.rubric(out["say"], d.judge, reply, today)
                checks.append(_check("judge", v.passed, v.reason))
            if check_sources:
                played = {"reply": reply, "sources": sources, "evidence": material(msgs)["evidence"]}
                citations = judge_citations(judge, out["say"], reply, cited_sources(played), today)
                bad = [c["statement"][:40] for c in citations if c["verdict"] == "unsupported"]
                if citations:
                    checks.append(_check("sources_supported", not bad, "；".join(bad) or f"{len(citations)} 条都有依据"))
        out.update({"checks": checks, "citations": citations})
    except Exception as e:  # noqa: BLE001 —— 一次出错不影响其他的，记为 error
        out["error"] = f"{type(e).__name__}: {e}"
    out["duration_s"] = round(time.time() - started, 1)
    return out


# ---- 汇总、比较 ----


def tally(results: list[dict]) -> dict[str, dict[str, list[bool]]]:
    """决策点 → 检查 → 每次过没过。出错的那次没有检查结果，不算进去。"""
    out: dict[str, dict[str, list[bool]]] = {}
    for r in results:
        for c in r.get("checks", []):
            out.setdefault(r["decision"], {}).setdefault(c["name"], []).append(c["passed"])
    return out


def fisher_p(a: int, n1: int, b: int, n2: int) -> float:
    """两边各跑 n1、n2 次，过了 a、b 次：要是其实一样，碰巧差这么多或更多的概率（双侧 Fisher 精确检验）。"""
    k, n = a + b, n1 + n2

    def p(x: int) -> float:
        return comb(n1, x) * comb(n2, k - x) / comb(n, k)

    obs = p(a)
    return min(1.0, sum(q for x in range(max(0, k - n2), min(k, n1) + 1) if (q := p(x)) <= obs * (1 + 1e-9)))


def summary(results: list[dict], order: list[str]) -> str:
    t = tally(results)
    out = []
    for did in order:
        rs = [r for r in results if r["decision"] == did]
        errors = sum("error" in r for r in rs)
        used = [r["usage"] for r in rs if "usage" in r]
        per = (f"  平均每次输入 {sum(u['input'] for u in used) / len(used) / 1000:.0f}k（缓存 {sum(u['cached'] for u in used) / len(used) / 1000:.0f}k）"
               f"、输出 {sum(u['output'] for u in used) / len(used) / 1000:.1f}k") if used else ""
        checks = "  ".join(f"{name} {sum(v)}/{len(v)}" for name, v in t.get(did, {}).items())
        out.append(f"{did:<24} {len(rs)} 次  {checks}" + (f"  出错 {errors} 次" if errors else "") + per)
    return "\n".join(out)


def compare(a: list[dict], b: list[dict]) -> str:
    """同一个决策点同一项检查，A、B 各过了几次，差得够不够大。"""
    ta, tb = tally(a), tally(b)
    out = []
    for did in [d for d in ta if d in tb]:
        for name in [x for x in ta[did] if x in tb[did]]:
            va, vb = ta[did][name], tb[did][name]
            p = fisher_p(sum(va), len(va), sum(vb), len(vb))
            out.append(f"{did} {name}：{sum(va)}/{len(va)} → {sum(vb)}/{len(vb)}  p={p:.2f}  " + ("变了" if p < SIGNIFICANT else "看不出变化"))
    return "\n".join(out) or "两份结果里没有相同的决策点和检查"


def transcript(results: list[dict]) -> str:
    out = []
    for r in results:
        out.append(f"# {r['decision']}（第 {r['sample']} 次）\n")
        if "error" in r:
            out.append(f"出错：{r['error']}\n")
            continue
        if r["thinking"]:
            out.append(f"> 思考：{r['thinking'][:800]}" + ("……" if len(r["thinking"]) > 800 else "") + "\n")
        if r["tool_calls"]:
            out.append("调了：" + "；".join(f"{tc['name']} {json.dumps(tc['args'], ensure_ascii=False)}" for tc in r["tool_calls"]) + "\n")
        if r["reply"]:
            out.append(f"**正文**：\n\n{r['reply']}\n")
        out.append("检查：" + "；".join(f"{c['name']} {'✓' if c['passed'] else '✗'} {c['detail']}" for c in r["checks"]) + "\n")
        if r["citations"]:
            out.append("引用核对：\n\n" + "\n".join(f"- {c['verdict']} {'、'.join(c['source_ids'])}：{c['statement']}（{c['reason']}）"
                                                for c in r["citations"]) + "\n")
    return "\n".join(out)


# ---- 传到 LangSmith（见 app.evals.smith）----


def decision_inputs(d: Decision) -> dict:
    """数据集里一个样例的输入：哪个用例第几轮第几次调模型、这一下该怎么做。"""
    return d.model_dump(mode="json", exclude_none=True, exclude={"id", "source"})


def sample_view(r: dict) -> dict:
    """实验里这一行的输出：这一下调了什么、写了什么。"""
    if "error" in r:
        return {"error": r["error"]}
    return {"tool_calls": r["tool_calls"], "reply": r["reply"]}


def sample_scores(r: dict) -> list[dict]:
    checks = r.get("checks", [])
    return smith.check_scores([checks], all(c["passed"] for c in checks), r.get("error"))


def run(decisions: list[Decision], make_runner: Callable[[], Any], judge, samples: int, jobs: int, check_sources: bool,
        snaps_for: Callable[[Decision], list[dict]], exp: smith.Experiment | None = None) -> list[dict]:
    """每个决策点跑 samples 次。所有决策点共用一个运行器（每次是检查点存储里一条新的线程）。给了 exp 就每次在一条 trace 里跑、记分数。"""
    runner = make_runner()
    picked = {d.id: pick(snaps_for(d), d) for d in decisions}

    def one(d: Decision, n: int) -> dict:
        def go() -> dict:
            return sample(runner, judge, d, picked[d.id], n, check_sources)

        return exp.run(d.id, decision_inputs(d), go, sample_scores, sample_view) if exp else go()

    results = []
    with smith.trimmed() if exp else nullcontext(), ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [pool.submit(one, d, n + 1) for d in decisions for n in range(samples)]
        for f in as_completed(futures):
            results.append(r := f.result())
            print(f"{r['decision']:<24} 第 {r['sample']} 次  " + (f"出错 {r['error'][:120]}" if "error" in r else
                  "  ".join(f"{c['name']} {'✓' if c['passed'] else '✗'}" for c in r["checks"])), flush=True)
    order = [d.id for d in decisions]
    results.sort(key=lambda r: (order.index(r["decision"]), r["sample"]))
    return results


def main(argv: list[str] | None = None) -> int:
    from app import config
    from app.api.research_runner import ResearchRunner
    from app.evals.research import RESULTS_DIR, TAPES_DIR, LiveJudge
    from app.research_llm import LiveResearchPlanner

    ap = argparse.ArgumentParser(description="决策点回放（真实模型）")
    ap.add_argument("--decision", action="append", help="只跑这些决策点（可以写多次）")
    ap.add_argument("--samples", type=int, default=10, help="每个决策点跑几次")
    ap.add_argument("--jobs", type=int, default=3, help="同时跑几个")
    ap.add_argument("--no-sources", action="store_true", help="不核对正文里的引用")
    ap.add_argument("--label", default="", help="写进结果文件名，方便对比")
    ap.add_argument("--no-smith", action="store_true", help="不传 LangSmith（配了 LANGSMITH_API_KEY 时默认传）")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"), help="不跑：比较两份结果，每项检查差得够不够大")
    args = ap.parse_args(argv)

    if args.compare:
        a, b = (json.loads(Path(f).read_text(encoding="utf-8"))["results"] for f in args.compare)
        print(compare(a, b))
        return 0
    decisions = [d for d in load_decisions() if not args.decision or d.id in args.decision]
    if args.decision and len(decisions) != len(set(args.decision)):
        ap.error(f"没有这些决策点：{sorted(set(args.decision) - {d.id for d in decisions})}")
    if missing := sorted({d.case for d in decisions if not (TAPES_DIR / f"{d.case}.snapshots.json").exists()}):
        ap.error(f"这些用例还没有存检查点的录像，先 research --record --case …：{missing}")

    def snaps_for(d: Decision) -> list[dict]:
        return json.loads((TAPES_DIR / f"{d.case}.snapshots.json").read_text(encoding="utf-8"))["snapshots"]

    name = "-".join(p for p in (f"decisions-{datetime.now():%Y%m%d-%H%M%S}", args.label) if p)
    model = config.RESEARCH_MODEL or (config.DEEPSEEK_MODEL if config.RESEARCH_PROVIDER == "deepseek" else config.ARK_MODEL)
    head: dict[str, Any] = {"provider": config.RESEARCH_PROVIDER, "model": model, "samples": args.samples}
    exp = None
    if not args.no_smith and decisions and args.samples > 0:
        exp = smith.start(smith.DECISIONS_DATASET, "灵稿的决策点：从录好的对话里挑一次调模型重跑，正本是仓库里的 backend/evals/research/decisions.yaml",
                          [smith.Example(d.id, decision_inputs(d), {"source": d.source}) for d in load_decisions()], name,
                          {**head, "label": args.label}, used=len(decisions), repetitions=args.samples)
    if exp:
        head["langsmith"] = exp.url
    # 工具不执行（停在执行工具之前），planner 只用来拿模型和工具说明；调 agent-tools 记在 research_eval 场景
    results = run(decisions, lambda: ResearchRunner(planner=LiveResearchPlanner(scene="research_eval"), checkpointer=memory_checkpointer()),
                  LiveJudge(), args.samples, args.jobs, not args.no_sources, snaps_for, exp)
    if exp:
        exp.close()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    head["decisions"] = [d.model_dump() for d in decisions]
    (RESULTS_DIR / f"{name}.json").write_text(json.dumps({**head, "results": results}, ensure_ascii=False, indent=1), encoding="utf-8")
    (RESULTS_DIR / f"{name}.md").write_text(transcript(results), encoding="utf-8")
    print("\n" + summary(results, [d.id for d in decisions]))
    print(f"结果：{RESULTS_DIR / name}.json / .md")
    if exp:
        print(f"LangSmith：{exp.url}" + (f"（{exp.feedback_errors} 个分数没传上去，本地结果里有）" if exp.feedback_errors else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
