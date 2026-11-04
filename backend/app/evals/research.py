"""对话回放评测：用真实模型按用例跑对话（evals/research/cases.yaml），每轮做几项自动检查，导出对话记录。

用法（在 backend 目录）：uv run python -m app.evals.research [--case id ...] [--repeat N] [--jobs N] [--no-sources]
  [--record | --replay | --rejudge 结果文件 | --stats 结果文件 ...]，各项的意思见 evals/README.md。
每个用例用独立的内存检查点，不碰本机的服务和配置的库；走和页面同一个运行器（ResearchRunner），
所以顺带检查流式片段的消息 id 和存下来的对得上。结果写到 data/evals/research-<时间>.json，对话记录写到同名的 .md。
工具结果可以录下来回放（app.evals.tape）；结果文件里存了重新打分要的东西（来源、裁判看的材料、那一轮的日期），
改了检查、标准或裁判，可以拿旧的回答重新打分，不用重跑 agent。

每轮的检查：跑完了、有回复（这一轮的结果是 ok，最后一条模型消息有正文）、正文里引用的来源都存在；
expect 里写了的才检查：搜没搜、读到几篇、取没取当前时间、查没查（搜索、读页、天气、子任务任一）。judge 写了就由裁判模型按标准看这一轮的正文（回答质量）。
正文里带来源角标的说法交给裁判模型逐条拿 agent 当时看到的材料核对（来源对不对，--no-sources 不做）。
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from pydantic import BaseModel, ConfigDict, Field

from app.clock import Clock
from app.evals.tape import Tape
from app.graph.research import history
from app.graph.research.graph import memory_checkpointer
from app.graph.research.sources import DOCS_TOOL, SOURCE_ID, SUBAGENT_TOOL, cited, doc_snippets, flatten, hit_text, parse, search_hits, url_key
from app.prompting import PromptFile
from app.tools.docs_mcp import DOCS_TOOLS
from app.tools.flyai_mcp import FLIGHT_TOOL, TRAIN_TOOL
from app.tools.maps_mcp import PLACE_TOOL, ROUTE_TOOL
from app.tools.research_tools import CHINA_TZ, TIME_TOOL
from app.tools.weather_mcp import WEATHER_TOOL

BACKEND = Path(__file__).resolve().parents[2]
CASES_FILE = BACKEND / "evals" / "research" / "cases.yaml"
RESULTS_DIR = BACKEND / "data" / "evals"
TAPES_DIR = RESULTS_DIR / "tapes"  # 工具录像，每个用例一份（见 app.evals.tape）
PROMPTS = PromptFile(Path(__file__).with_name("prompts.toml"))
# 去外面查东西的工具：主 agent 调了其中任一个就算这一轮查过（取当前时间、recall 不算）
LOOKUP_TOOLS = {"search_web", "read_page", WEATHER_TOOL, PLACE_TOOL, ROUTE_TOOL, FLIGHT_TOOL, TRAIN_TOOL, SUBAGENT_TOOL, *DOCS_TOOLS}
EVIDENCE_CHARS = 10000  # 每个来源给裁判看的材料上限（读页本身截到 8000 字，再加上搜索摘要）


# ---- 用例 ----


class Expect(BaseModel):
    """没写的项不检查。"""

    model_config = ConfigDict(extra="forbid")
    searched: bool | None = None
    min_reads: int | None = None
    checked_time: bool | None = None
    looked_up: bool | None = None  # 这一轮查没查：搜索、读页、天气、子任务任一（Fusion 11-04 没调工具就编了天气）


class CaseTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    say: str
    expect: Expect = Field(default_factory=Expect)
    judge: str | None = Field(default=None, description="写成“正文做到了 X，且没有 Y”")


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    source: str = Field(description="为什么有这个用例：哪次讨论、事故或改动")
    turns: list[CaseTurn] = Field(min_length=1)


def load_cases(path: Path = CASES_FILE) -> list[Case]:
    cases = [Case.model_validate(c) for c in yaml.safe_load(path.read_text(encoding="utf-8"))]
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{path.name} 里有重复的用例 id")
    return cases


# ---- agent 看到的材料 ----


def _code(m: ToolMessage, data: Any) -> str | None:
    if err := history.tool_error(m):
        return err["code"]
    return data.get("kind") if isinstance(data, dict) else None  # 11-05 以前的样子，和内容审核去掉的


def material(messages: list[BaseMessage]) -> dict:
    """从对话消息里取出 agent 看到的材料，子 agent 查到的也算：搜了什么（成功的、没成的）、读了什么，
    每个网址看到的文字（搜索摘要、读到的正文、查文档查到的那几段），裁判按它核对引用。"""
    flat = flatten(messages)
    args = {tc["id"]: tc.get("args") or {} for m in flat if isinstance(m, AIMessage) for tc in m.tool_calls}
    out: dict[str, Any] = {"searches": [], "failed_searches": [], "reads": [], "evidence": {}}

    def keep(url: str, text: str) -> None:
        if url and text and text not in (seen := out["evidence"].setdefault(url_key(url), [])):
            seen.append(text)

    for m in flat:
        if isinstance(m, ToolMessage) and m.name == DOCS_TOOL and not history.not_run(m):
            for s in doc_snippets(m):
                keep(s["url"], f"{s.get('title') or ''}\n{s.get('content') or ''}".strip())
            continue
        if not isinstance(m, ToolMessage) or m.name not in ("search_web", "read_page") or history.not_run(m):
            continue
        a, data = args.get(m.tool_call_id, {}), parse(m)
        if m.name == "search_web":
            call = {"query": a.get("query"), "recency_days": a.get("recency_days"), "domains": a.get("domains") or []}
            hits = search_hits(data) if m.status != "error" else None
            if hits is None:
                out["failed_searches"].append({**call, "code": _code(m, data), "message": history.text_of(m)[:200]})
                continue
            out["searches"].append({**call, "urls": [h.get("url") for h in hits]})
            for h in hits:
                keep(str(h.get("url") or ""), hit_text(h))
        else:
            ok = m.status != "error" and isinstance(data, dict) and data.get("ok") is True
            out["reads"].append({"url": a.get("url"), "ok": ok, "code": None if ok else _code(m, data)})
            if ok:
                keep(str(data.get("url") or a.get("url") or ""), str(data.get("content") or ""))
    return out


def text_for(evidence: dict[str, list[str]], url: str) -> str:
    return "\n\n".join(evidence.get(url_key(url), []))[:EVIDENCE_CHARS] or "（没有记录到材料）"


# ---- 裁判 ----


class CitationVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    statement: str
    source_ids: list[str]
    verdict: Literal["supported", "partial", "unsupported"]
    reason: str


class CitationJudgement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdicts: list[CitationVerdict]


class RubricJudgement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    passed: bool
    reason: str


class Judge(Protocol):
    def citations(self, question: str, reply: str, sources: list[dict]) -> CitationJudgement: ...

    def rubric(self, said: str, rubric: str, reply: str, today: str) -> RubricJudgement: ...  # today 是那一轮的日期（回放时是录的那天）


class LiveJudge:
    """对话用的同一个提供方，不开思考，结构化输出。"""

    def __init__(self) -> None:
        from app import config
        from app.research_llm import make_chat_model

        self._llm = make_chat_model(config.RESEARCH_PROVIDER, config.RESEARCH_MODEL, thinking=False, timeout=180)

    def _ask(self, schema, key: str, **values):
        chain = self._llm.with_structured_output(schema, method="json_schema", strict=True)
        return chain.invoke([("system", PROMPTS.render(f"{key}.system")), ("user", PROMPTS.render(f"{key}.user", **values))])

    def citations(self, question: str, reply: str, sources: list[dict]) -> CitationJudgement:
        return self._ask(CitationJudgement, "citations", question=question, reply=reply, sources=sources)

    def rubric(self, said: str, rubric: str, reply: str, today: str) -> RubricJudgement:
        return self._ask(RubricJudgement, "rubric", said=said, rubric=rubric, reply=reply, today=today)


# ---- 跑一轮 ----


def _check(name: str, passed: bool, detail: str = "") -> dict:
    return {"name": name, "passed": bool(passed), "detail": detail}


def play(runner, tid: str, turn: CaseTurn) -> dict:
    """说一句话，跑到这一轮结束。返回这一轮的记录；view（页面看到的）、evidence（整段对话的材料）不进结果文件。"""
    runner.accept(tid, turn.say)
    events: list[dict] = []
    runner.run_turn(tid, None, events.append)
    runner.suggest(tid)  # 这一轮结束之后起推荐问题
    view = runner.state_view(tid)
    msgs = runner.graph.get_state(runner._cfg(tid)).values["messages"]
    t = view["turns"][-1]
    rec_turn = t["record"] or {}
    streamed = {e["data"]["id"] for e in events if e["event"] == "delta"}
    stored = {i["id"] for i in t["items"] if i["type"] in ("thinking", "text")}
    this = msgs[history.last_human(msgs) + 1:]
    ais = [m for m in this if m.type == "ai"]
    seen = material(this)
    # 每次调模型的输入和其中命中缓存的，看前缀缓存接没接上（上一轮发过的这一轮第一次调用就该命中）
    calls = [{"input": m.usage_metadata["input_tokens"], "cached": (m.usage_metadata.get("input_token_details") or {}).get("cache_read", 0)}
             for m in ais if m.usage_metadata]
    return {"say": turn.say, "events": len(events), "status": view["status"], "error": view.get("error"),
            "reply": history.final_text(msgs[history.last_human(msgs) + 1:]), "items": t["items"],
            "suggestions": rec_turn.get("suggestions", []), "usage": rec_turn.get("usage"), "outcome": rec_turn.get("outcome"),
            "searches": seen["searches"], "failed_searches": seen["failed_searches"], "reads": seen["reads"],
            "time_checks": sum(1 for m in ais for tc in m.tool_calls if tc["name"] == TIME_TOOL), "calls": calls,
            "lookups": [tc["name"] for m in ais for tc in m.tool_calls if tc["name"] in LOOKUP_TOOLS],
            "stream_ids_match": streamed == stored, "sources": {i: s["url"] for i, s in view["sources"].items()},
            "view": view, "evidence": material(msgs)["evidence"]}


def turn_checks(turn: CaseTurn, played: dict) -> list[dict]:
    e = turn.expect
    ids = cited(played["reply"])
    unknown = [i for i in ids if i not in played["sources"]]
    # 这条对话里的来源 id 出现在 [id] 以外的写法里（[a, b]、(a)、“见 a”），页面上不会变成角标，会原样露出来
    loose = sorted(i for i in played["sources"] if i in SOURCE_ID.sub("", played["reply"]))
    out = [_check("finished", played["status"] == "idle", played.get("error") or played["status"]),
           _check("replied", played["outcome"] == "ok" and bool(played["reply"]), played.get("outcome") or ""),
           _check("stream_ids_match", played["stream_ids_match"]),
           _check("cited_known", not unknown, f"不存在的来源：{'、'.join(unknown)}" if unknown else f"引用了 {len(ids)} 个来源"),
           _check("cite_format", not loose, f"没按 [id] 写的引用：{'、'.join(loose)}" if loose else "")]
    if e.searched is not None:
        out.append(_check("searched", bool(played["searches"]) == e.searched, f"搜了 {len(played['searches'])} 次"))
    if e.min_reads is not None:
        n = sum(1 for r in played["reads"] if r["ok"])
        out.append(_check("min_reads", n >= e.min_reads, f"读到 {n} 篇"))
    if e.checked_time is not None:
        out.append(_check("checked_time", bool(played["time_checks"]) == e.checked_time, f"取了 {played['time_checks']} 次时间"))
    if e.looked_up is not None:
        looked = played["lookups"]
        out.append(_check("looked_up", bool(looked) == e.looked_up, "、".join(looked) if looked else "没查"))
    return out


def cited_sources(played: dict) -> list[dict]:
    """正文里引用的来源，带上 agent 当时看到的材料：裁判按它核对，存进结果文件，重新打分时直接用。"""
    srcs = played["sources"]
    return [{"id": i, "url": srcs[i], "text": text_for(played["evidence"], srcs[i])} for i in cited(played["reply"]) if i in srcs]


def judge_citations(judge: Judge, question: str, reply: str, sources: list[dict]) -> list[dict]:
    """正文里带角标的说法逐条拿引用的来源材料核对。正文没有引用（或引用的都不存在，cited_known 已经不过）就不核对。"""
    if not sources:
        return []
    verdicts = judge.citations(question, reply, sources).verdicts
    # 裁判有时把没带角标的句子也列出来（source_ids 为空）判 unsupported，比如天气工具给的预报（不是来源，本来就不标）。不归这一项管
    return [v.model_dump() for v in verdicts if v.source_ids]


def score(turn: CaseTurn, played: dict, judge: Judge, check_sources: bool) -> tuple[list[dict], list[dict]]:
    """一轮的检查和引用核对。played 是 play 的结果，或者结果文件里存下的一轮（重新打分）。"""
    checks = turn_checks(turn, played)
    citations: list[dict] = []
    if played["status"] == "idle" and played["reply"]:
        if turn.judge:
            v = judge.rubric(turn.say, turn.judge, played["reply"], played["today"])
            checks.append(_check("judge", v.passed, v.reason))
        if check_sources:
            citations = judge_citations(judge, turn.say, played["reply"], played["cited_sources"])
            bad = [c["statement"][:40] for c in citations if c["verdict"] == "unsupported"]
            if citations:
                checks.append(_check("sources_supported", not bad, "；".join(bad) or f"{len(citations)} 条都有依据"))
    return checks, citations


def _diff(after: dict[str, int], before: dict[str, int]) -> dict[str, int]:
    return {k: after[k] - before.get(k, 0) for k in after}


# ---- 跑一个用例 ----


def run_case(case: Case, make_planner: Callable[..., Any], judge: Judge, run: int = 1, check_sources: bool = True,
             tape: Tape | None = None) -> dict:
    """make_planner(clock=, wrap_tool=) 造这个用例的 planner。tape 给了就录像或回放（见 app.evals.tape），时钟跟着它挪。"""
    from app.api.research_runner import ResearchRunner

    started = time.time()
    result: dict[str, Any] = {"id": case.id, "run": run, "turns": []}
    clock = Clock()
    try:
        if tape:
            tape.start(clock)
        runner = ResearchRunner(planner=make_planner(clock=clock, wrap_tool=tape.wrap if tape else None), checkpointer=memory_checkpointer())
        tid = f"eval-{case.id}-{run}"
        for turn in case.turns:
            before = tape.counts() if tape else {}
            today = clock.now(CHINA_TZ).date().isoformat()
            played = {**play(runner, tid, turn), "today": today}
            played["cited_sources"] = cited_sources(played)
            if tape:
                played["tape"] = _diff(tape.counts(), before)
            checks, citations = score(turn, played, judge, check_sources)
            result["turns"].append({**{k: v for k, v in played.items() if k not in ("view", "evidence")}, "checks": checks, "citations": citations})
            if played["status"] != "idle":
                break
        failed = [s for t in result["turns"] for s in t["failed_searches"]]
        if failed and not any(t["searches"] for t in result["turns"]):  # 测的是搜索服务，不是对话链路
            raise RuntimeError(f"搜索全部失败（{failed[0]['code']}）：{failed[0]['message']}")
        all_checks = [c for t in result["turns"] for c in t["checks"]]
        result["passed"] = all(c["passed"] for c in all_checks) and len(result["turns"]) == len(case.turns)
        result["usage"] = _total([t["usage"] for t in result["turns"] if t["usage"]])
    except Exception as e:  # noqa: BLE001 —— 一个用例出错不影响其他用例，记为 error
        result.update({"passed": False, "error": f"{type(e).__name__}: {e}"})
    finally:
        if tape:
            result["tape"] = tape.counts()
            tape.save()  # 出错了也存：已经真调过的不用再花一遍钱
    result["duration_s"] = round(time.time() - started, 1)
    return result


def rejudge_case(r: dict, case: Case, judge: Judge, run: int, check_sources: bool = True) -> dict:
    """拿结果文件里一个用例的回答，按现在的用例（expect、judge 标准）和裁判重新检查打分，agent 不重跑。"""
    started = time.time()
    out: dict[str, Any] = {"id": r["id"], "run": run, "rejudged_from": r["run"], "turns": []}
    try:
        if "error" in r:
            raise RuntimeError(f"原来那遍就出错了：{r['error']}")
        if len(r["turns"]) > len(case.turns) or any(t["say"] != c.say for t, c in zip(r["turns"], case.turns)):
            raise ValueError("用例里的对话改过了，和结果文件里的对不上")
        if any(k not in t for t in r["turns"] for k in ("sources", "cited_sources", "today")):
            raise ValueError("结果文件是加上重新打分以前跑的，没存来源和裁判看的材料")
        for t, turn in zip(r["turns"], case.turns):
            checks, citations = score(turn, t, judge, check_sources)
            out["turns"].append({**t, "checks": checks, "citations": citations})
        out["passed"] = all(c["passed"] for t in out["turns"] for c in t["checks"]) and len(out["turns"]) == len(case.turns)
        out["usage"] = r["usage"]  # agent 没重跑，这是原来那遍的用量
    except Exception as e:  # noqa: BLE001
        out.update({"passed": False, "error": f"{type(e).__name__}: {e}"})
    out["duration_s"] = round(time.time() - started, 1)
    return out


def _total(usages: list[dict]) -> dict:
    keys = ("model_calls", "input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens", "searches", "reads")
    return {k: sum(u.get(k) or 0 for u in usages) for k in keys}


# ---- 输出 ----


def summary_line(r: dict) -> str:
    if "error" in r:
        return f"{r['id']:<16} 出错  {r['duration_s']:>4.0f}s  {r['error'][:160]}"
    u = r["usage"]
    failed = [f"第{i}轮 {c['name']}（{c['detail'][:60]}）" for i, t in enumerate(r["turns"], 1) for c in t["checks"] if not c["passed"]]
    tape = f"  录像对上 {r['tape']['hits']}、真调 {r['tape']['live']}" if r.get("tape") else ""
    return (f"{r['id']:<16} {'通过' if r['passed'] else '失败'}  {r['duration_s']:>4.0f}s  {len(r['turns'])} 轮  "
            f"调用 {u['model_calls']}  输入 {u['input_tokens'] / 1000:.0f}k（缓存 {u['cached_tokens'] / 1000:.0f}k）  输出 {u['output_tokens'] / 1000:.0f}k{tape}"
            + (f"  ✗ {'；'.join(failed)}" if failed else ""))


def stability(results: list[dict]) -> str:
    """几遍下来每个用例每轮每项检查过了几次：会翻的、一直不过的分开列。出错的那遍没有检查结果，单独数。"""
    tally: dict[tuple[str, int, str], list[bool]] = {}
    passed: dict[str, list[bool]] = {}
    errors: Counter[str] = Counter()
    for r in results:
        if "error" in r:
            errors[r["id"]] += 1
            continue
        passed.setdefault(r["id"], []).append(r["passed"])
        for i, t in enumerate(r["turns"], 1):
            for c in t["checks"]:
                tally.setdefault((r["id"], i, c["name"]), []).append(c["passed"])

    def row(k: tuple[str, int, str], v: list[bool]) -> str:
        return f"  {k[0]} 第{k[1]}轮 {k[2]}：{sum(v)}/{len(v)}"

    flaky = [row(k, v) for k, v in tally.items() if 0 < sum(v) < len(v)]
    never = [row(k, v) for k, v in tally.items() if not any(v)]
    out = ["用例通过：" + "，".join(f"{i} {sum(v)}/{len(v)}" for i, v in passed.items())]
    if errors:
        out.append("出错（不算进检查）：" + "，".join(f"{i} {n} 遍" for i, n in errors.items()))
    out.append(f"会翻的检查 {len(flaky)} 项" + ("：" if flaky else ""))
    out += flaky
    out.append(f"一直不过的 {len(never)} 项" + ("：" if never else ""))
    out += never
    out.append(f"一直过的 {len(tally) - len(flaky) - len(never)} 项")
    return "\n".join(out)


def transcript(results: list[dict]) -> str:
    """对话记录：每轮用户说了什么、模型做了什么、正文、检查结果、引用核对。人看的。"""
    out = []
    for r in results:
        out.append(f"# {r['id']}（第 {r['run']} 遍）{'通过' if r.get('passed') else '失败'}\n")
        if "error" in r:
            out.append(f"出错：{r['error']}\n")
        for i, t in enumerate(r.get("turns", []), 1):
            out.append(f"## 第 {i} 轮\n\n**用户**：{t['say']}\n")
            for item in t["items"]:
                if item["type"] == "thinking":
                    out.append(f"> 思考（{(item.get('ms') or 0) / 1000:.0f} 秒）：{item['text'][:600]}" + ("……" if len(item["text"]) > 600 else ""))
                elif item["type"] == "step":
                    out.append(f"- [{item['kind']}] {item['text']} " + ("✓" if item["ok"] else "✗ " + item["message"]) + (f" {item['message']}" if item["ok"] and item["message"] else ""))
                elif item["type"] == "text" and item["text"] != t["reply"]:
                    out.append(f"- [话] {item['text']}")
            out.append(f"\n**正文**：\n\n{t['reply']}\n")
            out.append(f"推荐：{' / '.join(t['suggestions'])}；用量：{t['usage']}\n")
            out.append("检查：" + "；".join(f"{c['name']} {'✓' if c['passed'] else '✗'} {c['detail']}" for c in t["checks"]) + "\n")
            if t["citations"]:
                out.append("引用核对：\n\n" + "\n".join(f"- {c['verdict']} {'、'.join(c['source_ids'])}：{c['statement']}（{c['reason']}）"
                                                    for c in t["citations"]) + "\n")
    return "\n".join(out)


def _load_results(path: str) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))["cases"]


def main(argv: list[str] | None = None) -> int:
    from app import config
    from app.research_llm import LiveResearchPlanner

    ap = argparse.ArgumentParser(description="对话回放评测（真实模型）")
    ap.add_argument("--case", action="append", help="只跑这些用例（可以写多次）")
    ap.add_argument("--repeat", type=int, default=1, help="每个用例跑几遍（--rejudge 时是每个回答判几遍）")
    ap.add_argument("--jobs", type=int, default=3, help="同时跑几个")
    ap.add_argument("--no-sources", action="store_true", help="不核对正文里的引用")
    ap.add_argument("--label", default="", help="写进结果文件名，方便对比")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--record", action="store_true", help="真跑，把工具结果录下来（整份换掉这些用例原来的录像）")
    mode.add_argument("--replay", action="store_true", help="用录像回放工具结果，没对上的真调并补录；时钟挪到录的时候")
    mode.add_argument("--rejudge", metavar="结果文件", help="不跑 agent：拿这个结果文件里的回答，按现在的用例和裁判重新检查打分")
    mode.add_argument("--stats", metavar="结果文件", nargs="+", help="不跑：汇总这些结果文件，每项检查过了几次")
    args = ap.parse_args(argv)

    cases = [c for c in load_cases() if not args.case or c.id in args.case]
    if args.case and len(cases) != len(set(args.case)):
        ap.error(f"没有这些用例：{sorted(set(args.case) - {c.id for c in cases})}")
    order = [c.id for c in cases]
    if args.stats:
        print(stability([r for f in args.stats for r in _load_results(f) if r["id"] in order]))
        return 0
    if args.record and args.repeat > 1:
        ap.error("录一遍就够了：先 --record，再 --replay --repeat N")
    if args.replay and (missing := [c.id for c in cases if not (TAPES_DIR / f"{c.id}.json").exists()]):
        ap.error(f"这些用例还没有录像，先 --record：{missing}")
    judge = LiveJudge()

    results = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        if args.rejudge:
            by_id = {c.id: c for c in cases}
            old = [r for r in _load_results(args.rejudge) if r["id"] in by_id]
            futures = [pool.submit(rejudge_case, r, by_id[r["id"]], judge, n * args.repeat + k + 1, not args.no_sources)
                       for n, r in enumerate(old) for k in range(args.repeat)]
        else:
            # 调 agent-tools 用 research_eval 场景：网关记账时和真实对话分开
            def make(**kw):
                return LiveResearchPlanner(scene="research_eval", **kw)

            def tape(c: Case) -> Tape | None:
                return Tape(TAPES_DIR / f"{c.id}.json", fresh=args.record) if args.record or args.replay else None

            futures = [pool.submit(run_case, c, make, judge, i + 1, not args.no_sources, tape(c)) for c in cases for i in range(args.repeat)]
        for f in as_completed(futures):
            results.append(f.result())
            print(summary_line(results[-1]), flush=True)
    results.sort(key=lambda r: (order.index(r["id"]), r["run"]))
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    how = "record" if args.record else "replay" if args.replay else "rejudge" if args.rejudge else None
    name = "-".join(p for p in (f"research-{datetime.now():%Y%m%d-%H%M%S}", how, args.label) if p)
    model = config.RESEARCH_MODEL or (config.DEEPSEEK_MODEL if config.RESEARCH_PROVIDER == "deepseek" else config.ARK_MODEL)
    head = {"provider": config.RESEARCH_PROVIDER, "model": model, "mode": how, **({"rejudged": args.rejudge} if args.rejudge else {})}
    (RESULTS_DIR / f"{name}.json").write_text(json.dumps({**head, "cases": results}, ensure_ascii=False, indent=1), encoding="utf-8")
    (RESULTS_DIR / f"{name}.md").write_text(transcript(results), encoding="utf-8")
    passed = sum(r["passed"] for r in results)
    if args.rejudge:
        print(f"\n通过 {passed}/{len(results)}（agent 没重跑，只重新打分）")
    else:
        total = _total([r["usage"] for r in results if "usage" in r])
        hits, live = (sum((r.get("tape") or {}).get(k, 0) for r in results) for k in ("hits", "live"))
        print(f"\n通过 {passed}/{len(results)}；合计调用 {total['model_calls']} 次，输入 {total['input_tokens'] / 1e6:.2f}M"
              f"（缓存 {total['cached_tokens'] / 1e6:.2f}M），输出 {total['output_tokens'] / 1e6:.2f}M"
              + (f"；工具录像对上 {hits} 次、真调 {live} 次" if how else ""))
    if args.repeat > 1:
        print("\n" + stability(results))
    print(f"结果：{RESULTS_DIR / name}.json / .md")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
