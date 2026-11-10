"""对话回放评测：用真实模型按用例跑对话（evals/research/cases.yaml），每轮做几项自动检查，导出对话记录。

用法（在 backend 目录）：uv run python -m app.evals.research [--case id ...] [--repeat N] [--jobs N] [--no-sources] [--no-smith]
  [--record | --replay | --rejudge 结果文件 | --stats 结果文件 ...]，各项的意思见 evals/README.md。
每个用例用独立的内存检查点，不碰本机的服务和配置的库；走和页面同一个运行器（ResearchRunner），
所以顺带检查流式片段的消息 id 和存下来的对得上。结果写到 data/evals/research-<时间>.json，对话记录写到同名的 .md。
工具结果可以录下来回放（app.evals.tape）；结果文件里存了重新打分要的东西（来源、裁判看的材料、那一轮的日期），
改了检查、标准或裁判，可以拿旧的回答重新打分，不用重跑 agent。
配了 LANGSMITH_API_KEY 的话每次评测（重新打分也算）同时传到 LangSmith 成一个实验（app.evals.smith），--no-smith 不传。

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
from contextlib import nullcontext
from datetime import date, datetime
from functools import partial
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.clock import Clock
from app import costs
from app.evals import smith
from app.evals.tape import Tape
from app.graph.research import history
from app.graph.research.agent import date_anchors
from app.graph.research.graph import memory_checkpointer
from app.graph.research.sources import (
    DOCS_TOOL,
    SOURCE_ID,
    SUBAGENT_TOOL,
    cited,
    doc_snippets,
    flatten,
    hit_text,
    parse,
    search_hits,
    url_key,
)
from app.model_catalog import AUTO
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
    asked: bool | None = None  # 这一轮有没有停下来问用户（ask_user 真问出来了的，见 app.tools.ask_user）


class CaseTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    say: str
    work: bool = Field(default=False, description="这段对话是工作任务（新建时选的“工作”）：模型才能停下来问用户。整段不变，每一句都要写成一样的")
    answers: list[str] = Field(default_factory=list, description="模型这一轮停下来问用户时，按先后这样回答；没写的（问的比写的多）都跳过")
    mute: bool = Field(default=False, description="这一轮第一次被问时点“别再问了”（跳过，并且这段对话之后不再问）；这时不写 answers")
    expect: Expect = Field(default_factory=Expect)
    judge: str | None = Field(default=None, description="写成“正文做到了 X，且没有 Y”")


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    source: str = Field(description="为什么有这个用例：哪次讨论、事故或改动")
    turns: list[CaseTurn] = Field(min_length=1)

    @model_validator(mode="after")
    def _one_mode(self) -> Case:
        if len({t.work for t in self.turns}) > 1:  # 对话 / 工作新建时定、整段不变（2026-11-11 Sean 定）：后面的话换不了
            raise ValueError(f"{self.id}：work 每一句都要一样（对话还是工作任务新建时定，中途不能换）")
        return self


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
    def citations(self, question: str, reply: str, sources: list[dict], today: str) -> CitationJudgement: ...

    def rubric(self, said: str, rubric: str, reply: str, today: str) -> RubricJudgement: ...  # today 是那一轮的日期（回放时是录的那天）


class LiveJudge:
    """models.toml 里 roles.judge 那个模型，不开思考，结构化输出。换被测的模型（--model）时裁判不变，分数才比得了。"""

    def __init__(self) -> None:
        from app import model_catalog
        from app.research_llm import RoleModel, usable

        self._llm = RoleModel(model_catalog.load(), "judge", usable, timeout=180)  # 先走同一个模型的免费额度，见 RoleModel

    def _ask(self, schema, key: str, **values):
        return self._llm.invoke([("system", PROMPTS.render(f"{key}.system")), ("user", PROMPTS.render(f"{key}.user", **values))],
                                shape=lambda m: m.with_structured_output(schema, method="function_calling", strict=True))

    def citations(self, question: str, reply: str, sources: list[dict], today: str) -> CitationJudgement:
        # 给裁判和助理一样的日期（今天、明天、这周末……）：正文里“明天（11月6日，星期五）”是助理按日期推的，材料里本来就没有
        #（2026-11-05 决策点回放查出来：没给日期时裁判把它判成没依据，restriction 用例因此时过时不过）
        dates = date_anchors(date.fromisoformat(today))
        return self._ask(CitationJudgement, "citations", question=question, reply=reply, sources=sources, **dates)

    def rubric(self, said: str, rubric: str, reply: str, today: str) -> RubricJudgement:
        return self._ask(RubricJudgement, "rubric", said=said, rubric=rubric, reply=reply, today=today)


# ---- 跑一轮 ----


def _check(name: str, passed: bool, detail: str = "") -> dict:
    return {"name": name, "passed": bool(passed), "detail": detail}


def play(runner, tid: str, turn: CaseTurn, option: str | None = None, thinking: bool | None = None) -> dict:
    """说一句话，跑到这一轮结束。返回这一轮的记录；view（页面看到的）、evidence（整段对话的材料）不进结果文件。
    option 是第一句话时给对话选的模型（models.toml 里选项的 id），没给是 Auto；thinking 是思考开关（--no-thinking 是 False，没给按选项的默认）。
    模型停下来问用户的（ask_user）：按用例写的 answers 依次回答，没写的跳过，同一轮接着跑；问了什么、怎么答的记在 asks 里。
    用例写了 mute 的：第一次被问就点“别再问了”。"""
    runner.accept(tid, turn.say, thinking, option=option, work=turn.work or None)
    events: list[dict] = []
    runner.run_turn(tid, None, events.append)
    asks: list[dict] = []
    answered: set[str] = set()
    # 答过的那个问题还在等（接着跑的那次没跑起来）就不再答：不然会一直重跑下去。这一轮停在 asking，finished 不过
    while (ask := runner.state_view(tid)["ask"]) is not None and ask["id"] not in answered:
        answered.add(ask["id"])
        text = turn.answers[len(asks)] if len(asks) < len(turn.answers) else ""
        mute = turn.mute and not asks
        asks.append({"question": ask["question"], "options": ask["options"], "answer": text, **({"muted": True} if mute else {})})
        runner.run_turn(tid, runner.answer_input(tid, ask["id"], text, skip=not text, mute=mute), events.append)
    runner.suggest(tid)  # 这一轮结束之后起推荐问题
    view = runner.state_view(tid)
    msgs = runner.graph.get_state(runner._cfg(tid)).values["messages"]
    t = view["turns"][-1]
    rec_turn = t["record"] or {}
    # 只看主 agent 的片段：子 agent 的片段（带 task）进的是它的子任务卡，不是这一轮的思考、正文
    streamed = {e["data"]["id"] for e in events if e["event"] == "delta" and not e["data"].get("task")}
    stored = {i["id"] for i in t["items"] if i["type"] in ("thinking", "text")}
    this = msgs[history.last_human(msgs) + 1:]
    ais = [m for m in this if m.type == "ai"]
    seen = material(this)
    # 每次调模型的输入和其中命中缓存的，看前缀缓存接没接上（上一轮发过的这一轮第一次调用就该命中）
    calls = [{"input": m.usage_metadata["input_tokens"], "cached": (m.usage_metadata.get("input_token_details") or {}).get("cache_read", 0)}
             for m in ais if m.usage_metadata]
    return {"say": turn.say, "events": len(events), "status": view["status"], "error": runner.failure_detail(tid) or view.get("error"),
            "reply": history.final_text(msgs[history.last_human(msgs) + 1:]), "items": t["items"],
            "suggestions": rec_turn.get("suggestions", []), "usage": rec_turn.get("usage"), "outcome": rec_turn.get("outcome"),
            "model": rec_turn.get("model"),  # 这一轮实际用的（Auto 按第一句话定）
            "searches": seen["searches"], "failed_searches": seen["failed_searches"], "reads": seen["reads"],
            "time_checks": sum(1 for m in ais for tc in m.tool_calls if tc["name"] == TIME_TOOL), "calls": calls,
            "lookups": [tc["name"] for m in ais for tc in m.tool_calls if tc["name"] in LOOKUP_TOOLS], "asks": asks,
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
    if e.asked is not None:
        asks = played.get("asks") or []  # 加这一项以前的结果文件里没有
        out.append(_check("asked", bool(asks) == e.asked, "；".join(a["question"] for a in asks) or "没问"))
    return out


def cited_sources(played: dict) -> list[dict]:
    """正文里引用的来源，带上 agent 当时看到的材料：裁判按它核对，存进结果文件，重新打分时直接用。"""
    srcs = played["sources"]
    return [{"id": i, "url": srcs[i], "text": text_for(played["evidence"], srcs[i])} for i in cited(played["reply"]) if i in srcs]


def judge_citations(judge: Judge, question: str, reply: str, sources: list[dict], today: str) -> list[dict]:
    """正文里带角标的说法逐条拿引用的来源材料核对。正文没有引用（或引用的都不存在，cited_known 已经不过）就不核对。
    today 是那一轮的日期（回放时是录的那天）。"""
    if not sources:
        return []
    verdicts = judge.citations(question, reply, sources, today).verdicts
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
            citations = judge_citations(judge, turn.say, played["reply"], played["cited_sources"], played["today"])
            bad = [c["statement"][:40] for c in citations if c["verdict"] == "unsupported"]
            if citations:
                checks.append(_check("sources_supported", not bad, "；".join(bad) or f"{len(citations)} 条都有依据"))
    return checks, citations


def _diff(after: dict[str, int], before: dict[str, int]) -> dict[str, int]:
    return {k: after[k] - before.get(k, 0) for k in after}


# ---- 跑一个用例 ----


def run_case(case: Case, make_planner: Callable[..., Any], judge: Judge, run: int = 1, check_sources: bool = True,
             tape: Tape | None = None, option: str | None = None, thinking: bool | None = None) -> dict:
    """make_planner(clock=, wrap_tool=) 造这个用例的 planner，option 是对话选的模型（没给是 Auto），thinking 是每句话的思考开关（见 play）。
    tape 给了就录像或回放（见 app.evals.tape），时钟跟着它挪；
    重新录的时候每一步都存检查点，跑完把每次调模型之前的那个存下来，给决策点回放用（见 app.evals.decisions）。"""
    from app.api.research_runner import ResearchRunner
    from app.evals.decisions import save_snapshots, snapshot_path

    started = time.time()
    result: dict[str, Any] = {"id": case.id, "run": run, "turns": []}
    clock = Clock()
    runner = None
    tid = f"eval-{case.id}-{run}"
    try:
        if tape:
            tape.start(clock)
        runner = ResearchRunner(planner=make_planner(clock=clock, wrap_tool=tape.wrap if tape else None), checkpointer=memory_checkpointer())
        if tape and tape.fresh:
            runner.durability = "sync"
        for i, turn in enumerate(case.turns):
            before = tape.counts() if tape else {}
            today = clock.now(CHINA_TZ).date().isoformat()
            played = {**play(runner, tid, turn, option if i == 0 else None, thinking), "today": today}
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
            if tape.fresh and runner is not None:
                try:
                    save_snapshots(snapshot_path(tape.path), runner.graph, runner._cfg(tid), tape.recorded_at)
                except Exception as e:  # noqa: BLE001 —— 存不下来也不打断别的用例，记成这个用例出错
                    result.update({"passed": False, "error": result.get("error") or f"存检查点出错：{type(e).__name__}: {e}"})
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


# ---- 传到 LangSmith（见 app.evals.smith）----


def case_inputs(c: Case) -> dict:
    """数据集里一个样例的输入：这个用例的几轮（说什么、查什么、裁判看什么）。"""
    # 对话的（没写 work）不带这一项：加这个字段之前传上去的样例不跟着变
    return {"turns": [t.model_dump(mode="json", exclude_none=True, exclude=set() if t.work else {"work"}) for t in c.turns]}


def case_view(r: dict) -> dict:
    """实验里这一行的输出：每轮说了什么、正文，过没过。完整的过程在这一行的 trace 里。"""
    return {"passed": r.get("passed"), **({"error": r["error"]} if "error" in r else {}),
            **({"rejudged_from": r["rejudged_from"]} if "rejudged_from" in r else {}),
            "turns": [{"say": t["say"], "status": t["status"], "reply": t["reply"]} for t in r.get("turns", [])]}


def case_scores(r: dict) -> list[dict]:
    return smith.check_scores([t["checks"] for t in r.get("turns", [])], r.get("passed", False), r.get("error"))


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
    # 重新打分的 agent 没重跑，结果里的用量是原来那遍的，不打出来，免得看成又花了一遍
    used = (f"调用 {u['model_calls']}  输入 {u['input_tokens'] / 1000:.0f}k（缓存 {u['cached_tokens'] / 1000:.0f}k）  输出 {u['output_tokens'] / 1000:.0f}k"
            if "rejudged_from" not in r else f"重新打分（第 {r['rejudged_from']} 遍的回答）")
    return (f"{r['id']:<16} {'通过' if r['passed'] else '失败'}  {r['duration_s']:>4.0f}s  {len(r['turns'])} 轮  {used}{tape}"
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
                elif item["type"] == "ask":
                    said = item["answer"] or ("跳过了" if item["skipped"] else "没有回答")
                    out.append(f"- [问] {item['question']}（{' / '.join(item['options'])}）→ {said}")
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


def model_head(option: str) -> dict[str, str]:
    """结果文件开头记的模型：选项和它这时对应的主 agent、子 agent 的模型（Auto 的策略以后会变，记下这次实际用的）。
    选项写错了、停用了、没配 key 直接报错：不然悄悄换成 Auto，测的就不是想测的模型了。
    Auto 不查 key：它就是换过去的那个，没配 key 的话建 planner 时就报错。"""
    from app import model_catalog
    from app.research_llm import usable

    catalog = model_catalog.load()
    o = catalog.option(option)
    if o is None or o.status:
        raise SystemExit(f"没有这个模型可选（看 app/models.toml）：{option}")
    if option != model_catalog.AUTO and not all(usable(r) for r in o.refs()):
        raise SystemExit(f"这个模型没配 key：{option}")
    if option == model_catalog.AUTO:  # 2026-11-08 起按第一句话判断难易定下模型（见 Catalog.pick），每轮实际用的记在各轮的 model
        return {"option": option, "model": "按第一句话定", "subagent_model": "按第一句话定"}
    plan = catalog.plan(option, None, usable)
    return {"option": option, "model": plan.agent.ref, "subagent_model": plan.subagent.ref}


def main(argv: list[str] | None = None) -> int:
    from app.research_llm import LiveResearchPlanner

    ap = argparse.ArgumentParser(description="对话回放评测（真实模型）")
    ap.add_argument("--case", action="append", help="只跑这些用例（可以写多次）")
    ap.add_argument("--repeat", type=int, default=1, help="每个用例跑几遍（--rejudge 时是每个回答判几遍）")
    ap.add_argument("--jobs", type=int, default=3, help="同时跑几个")
    ap.add_argument("--no-sources", action="store_true", help="不核对正文里的引用")
    ap.add_argument("--label", default="", help="写进结果文件名，方便对比")
    ap.add_argument("--no-smith", action="store_true", help="不传 LangSmith（配了 LANGSMITH_API_KEY 时默认传）")
    ap.add_argument("--model", default=AUTO, help="对话选哪个模型（app/models.toml 里选项的 id），默认 Auto；裁判不跟着换")
    ap.add_argument("--no-thinking", action="store_true", help="关着深度思考跑；不给按选项的默认，能开关的默认开")
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
    costs.open_ledger()  # 评测花的钱记进本机的库（配了的话），对账时和 dev 的加起来（见 app.costs）
    judge = LiveJudge()

    # 要跑的每一行：(用例, 跑它)。重新打分是原来那份结果里的每个回答各判 repeat 遍
    by_id = {c.id: c for c in cases}
    if args.rejudge:
        old = [r for r in _load_results(args.rejudge) if r["id"] in by_id]
        jobs = [(by_id[r["id"]], partial(rejudge_case, r, by_id[r["id"]], judge, n * args.repeat + k + 1, not args.no_sources))
                for n, r in enumerate(old) for k in range(args.repeat)]
    else:
        # 调 agent-tools 用 research_eval 场景：网关记账时和真实对话分开
        def make(**kw):
            return LiveResearchPlanner(scene="research_eval", **kw)

        def tape(c: Case) -> Tape | None:
            return Tape(TAPES_DIR / f"{c.id}.json", fresh=args.record) if args.record or args.replay else None

        jobs = [(c, partial(run_case, c, make, judge, i + 1, not args.no_sources, tape(c), args.model, False if args.no_thinking else None))
                for c in cases for i in range(args.repeat)]
    how = "record" if args.record else "replay" if args.replay else "rejudge" if args.rejudge else None
    name = "-".join(p for p in (f"research-{datetime.now():%Y%m%d-%H%M%S}", how, args.model if args.model != AUTO else "",
                                "nothink" if args.no_thinking else "", args.label) if p)
    # 重新打分不跑 agent，模型看原来那份结果文件
    head = {**({"rejudged": args.rejudge} if args.rejudge else {**model_head(args.model), "thinking": "off" if args.no_thinking else "default"}),
            "mode": how}
    exp = None
    if not args.no_smith and jobs:
        exp = smith.start(smith.RESEARCH_DATASET, "灵稿对话评测的用例，正本是仓库里的 backend/evals/research/cases.yaml",
                          [smith.Example(c.id, case_inputs(c), {"source": c.source}) for c in load_cases()], name,
                          {**head, "label": args.label}, used=len({c.id for c, _ in jobs}), repetitions=max(Counter(c.id for c, _ in jobs).values()))
    if exp:
        head["langsmith"] = exp.url

    def row(c: Case, fn: Callable[[], dict]) -> dict:
        return exp.run(c.id, case_inputs(c), fn, case_scores, case_view) if exp else fn()

    results = []
    with smith.trimmed() if exp else nullcontext(), ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for f in as_completed([pool.submit(row, c, fn) for c, fn in jobs]):
            results.append(f.result())
            print(summary_line(results[-1]), flush=True)
    if exp:
        exp.close()
    results.sort(key=lambda r: (order.index(r["id"]), r["run"]))
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
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
    if exp:
        print(f"LangSmith：{exp.url}" + (f"（{exp.feedback_errors} 个分数没传上去，本地结果里有）" if exp.feedback_errors else ""))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
