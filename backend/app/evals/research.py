"""调研回放评测：用真实模型跑 evals/research/cases.yaml 里的用例，按轨迹检查和裁判模型打分。

用法（在 backend 目录）：uv run python -m app.evals.research [--case id ...] [--repeat N] [--jobs N]
每个用例用独立的内存检查点，不碰本机的服务和 sqlite；结果写到 data/evals/research-<时间>.json。

一个用例通过要同时满足：
- 轨迹检查（expect 里写了的才检查）：弹没弹范围卡、agent 问没问、用了哪个调研方法、搜了几次、读了几篇、
  有没有限定时间、引用里有没有某个站点；另外都要求出了报告、整理判断问题已回答；
- 论断支撑：裁判模型拿 agent 当时看到的材料（搜索摘要、读到的正文）和用户途中的回答逐条核对，不能有 unsupported；
- judge 标准：裁判模型按用例写的标准判断报告。
"""

from __future__ import annotations

import argparse
import json
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.cli import interrupt_view, research_resume
from app.graph.research.agent import is_ask_interrupt, url_key
from app.graph.research.graph import build_graph, memory_checkpointer
from app.prompting import PromptFile
from app.tools.gateway import GatewayError

BACKEND = Path(__file__).resolve().parents[2]
CASES_FILE = BACKEND / "evals" / "research" / "cases.yaml"
RESULTS_DIR = BACKEND / "data" / "evals"
PROMPTS = PromptFile(Path(__file__).with_name("prompts.toml"))
EVIDENCE_CHARS = 8000  # 每个来源给裁判看的材料上限（读页本身截到 6000 字）
MAX_CARDS = 6  # 一个用例最多应答几张卡，防止卡在循环里


# ---- 用例 ----


class Expect(BaseModel):
    """没写的项不检查。skill 写 null 表示不该用调研方法。"""

    model_config = ConfigDict(extra="forbid")
    scope_card: bool | None = None
    asked: bool | None = None
    skill: str | None = None
    min_reads: int | None = None
    max_searches: int | None = None
    recency: bool | None = None
    cites_domain: str | None = None


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    source: str = Field(description="哪次探针、事故或改动发现的问题")
    request: str
    scope_answers: list[str | None] = Field(default_factory=list, description="范围卡上的问题按顺序怎么答；没写的跳过")
    ask_answers: list[str | None] = Field(default_factory=list, description="agent 途中提问按顺序怎么答；没写的跳过")
    expect: Expect = Field(default_factory=Expect)
    judge: str | None = Field(default=None, description="写成“报告做到了 X，且没有 Y”")


def load_cases(path: Path = CASES_FILE) -> list[Case]:
    cases = [Case.model_validate(c) for c in yaml.safe_load(path.read_text(encoding="utf-8"))]
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{path.name} 里有重复的用例 id")
    return cases


# ---- 记录 agent 看到的材料 ----


class Recorder:
    """包一层网关客户端：记下每次搜索和读页的参数和结果，裁判按 agent 当时看到的材料核对论断。"""

    def __init__(self, client) -> None:
        self._client = client
        self._lock = threading.Lock()  # 同一步的多个工具调用可能并行执行
        self.searches: list[dict] = []  # 成功的搜索
        self.failed_searches: list[dict] = []
        self.reads: list[dict] = []
        self.evidence: dict[str, list[str]] = {}

    def _keep(self, url: str, text: str) -> None:
        if text and text not in self.evidence.setdefault(url_key(url), []):
            self.evidence[url_key(url)].append(text)

    def search(self, query: str, **kw):
        call = {"query": query, "freshness": kw.get("freshness"), "domains": kw.get("domains") or []}
        try:
            hits = self._client.search(query, **kw)
        except GatewayError as e:  # 记下来照常抛给工具
            with self._lock:
                self.failed_searches.append({**call, "kind": e.kind, "message": str(e)[:200]})
            raise
        with self._lock:
            self.searches.append({**call, "urls": [h.url for h in hits]})
            for h in hits:
                self._keep(h.url, h.content)
        return hits

    def read(self, url: str, **kw):
        res = self._client.read(url, **kw)
        with self._lock:
            self.reads.append({"url": url, "ok": res.ok, "kind": res.kind})
            if res.ok:
                self._keep(url, res.content)
        return res

    def text_for(self, url: str) -> str:
        return "\n\n".join(self.evidence.get(url_key(url), []))[:EVIDENCE_CHARS] or "（没有记录到材料）"


# ---- 裁判 ----


class ClaimVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_id: str
    verdict: Literal["supported", "partial", "unsupported"]
    reason: str


class ClaimJudgement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdicts: list[ClaimVerdict]


class RubricJudgement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    passed: bool
    reason: str


class Judge(Protocol):
    def claims(self, question: str, claims: list[dict], sources: list[dict], answers: list[dict]) -> ClaimJudgement: ...

    def rubric(self, question: str, rubric: str, report: str) -> RubricJudgement: ...


class LiveJudge:
    """调研用的同一个提供方，不开思考，结构化输出。"""

    def __init__(self) -> None:
        from app import config
        from app.research_llm import make_chat_model

        self._llm = make_chat_model(config.RESEARCH_PROVIDER, config.RESEARCH_MODEL, thinking=False, timeout=180)

    def _ask(self, schema, key: str, **values):
        chain = self._llm.with_structured_output(schema, method="json_schema", strict=True)
        return chain.invoke([("system", PROMPTS.render(f"{key}.system")), ("user", PROMPTS.render(f"{key}.user", **values))])

    def claims(self, question: str, claims: list[dict], sources: list[dict], answers: list[dict]) -> ClaimJudgement:
        return self._ask(ClaimJudgement, "claims", question=question, claims=claims, sources=sources, answers=answers)

    def rubric(self, question: str, rubric: str, report: str) -> RubricJudgement:
        return self._ask(RubricJudgement, "rubric", question=question, rubric=rubric, report=report)


# ---- 跑一个用例 ----


@dataclass
class Trace:
    scope_cards: int = 0
    asks: list[str] = field(default_factory=list)
    answers: list[dict] = field(default_factory=list)  # 用户答了的问题：{"question", "answer"}，给裁判当已确认的前提
    review: dict | None = None


def _interrupt(result: dict) -> dict | None:
    ints = result.get("__interrupt__") or []
    return ints[0].value if ints else None


def drive(graph, config: dict, case: Case) -> Trace:
    """像用户那样一路应答到终审卡：范围卡、途中提问按用例写的答，没写的跳过。"""
    trace = Trace()
    asks = iter(case.ask_answers)
    payload = _interrupt(graph.invoke({"request": case.request}, config))
    for _ in range(MAX_CARDS):
        if payload is None or payload.get("type") == "research.review":
            trace.review = payload
            return trace
        if payload.get("type") == "research.scope":
            trace.scope_cards += 1
            answers = case.scope_answers
            reply = {"answers": {q["id"]: answers[i] if i < len(answers) else None for i, q in enumerate(payload["questions"])}}
            trace.answers += [{"question": q["title"], "answer": reply["answers"][q["id"]]}
                              for q in payload["questions"] if reply["answers"][q["id"]]]
        elif is_ask_interrupt(payload):
            trace.asks.append(interrupt_view(payload)["title"])
            answer = next(asks, None)
            reply = {"answer": answer} if answer else {"skip": True}
            if answer:
                trace.answers.append({"question": trace.asks[-1], "answer": answer})
        else:
            raise RuntimeError(f"没见过的卡片：{payload.get('type')}")
        payload = _interrupt(graph.invoke(research_resume(graph, config, reply), config))
    raise RuntimeError(f"应答了 {MAX_CARDS} 张卡还没到终审")


def _check(name: str, passed: bool, detail: str = "") -> dict:
    return {"name": name, "passed": bool(passed), "detail": detail}


def trajectory_checks(case: Case, trace: Trace, values: dict, rec: Recorder) -> list[dict]:
    e = case.expect
    picked = values.get("skills") or []
    reads = sum(1 for r in rec.reads if r["ok"])
    out = [
        _check("reached_review", trace.review is not None),
        _check("answered", values.get("answered") is True, values.get("open_gap") or ""),
    ]
    if e.scope_card is not None:
        out.append(_check("scope_card", (trace.scope_cards > 0) == e.scope_card, f"弹了 {trace.scope_cards} 张"))
    if e.asked is not None:
        out.append(_check("asked", bool(trace.asks) == e.asked, "；".join(trace.asks) or "没问"))
    if "skill" in e.model_fields_set:
        out.append(_check("skill", picked == ([e.skill] if e.skill else []), "、".join(picked) or "没用"))
    if e.min_reads is not None:
        out.append(_check("min_reads", reads >= e.min_reads, f"读到 {reads} 篇"))
    if e.max_searches is not None:
        out.append(_check("max_searches", len(rec.searches) <= e.max_searches, f"搜了 {len(rec.searches)} 次"))
    if e.recency is not None:
        used = [s["freshness"] for s in rec.searches if s["freshness"]]
        out.append(_check("recency", bool(used) == e.recency, "、".join(used) or "没限定"))
    if e.cites_domain:
        cited = {sid for c in values.get("claims", []) if not c.orphaned for sid in c.source_ids}
        hosts = sorted({urlsplit(s.url).hostname or "" for s in values.get("sources", []) if s.id in cited})
        hit = any(h == e.cites_domain or h.endswith("." + e.cites_domain) for h in hosts)
        out.append(_check("cites_domain", hit, "、".join(hosts)))
    return out


def judge_claims(judge: Judge, question: str, values: dict, rec: Recorder, answers: list[dict]) -> list[dict]:
    claims = [c for c in values.get("claims", []) if not c.orphaned]
    if not claims:
        return []
    cited = {sid for c in claims for sid in c.source_ids}
    sources = [{"id": s.id, "url": s.url, "text": rec.text_for(s.url)} for s in values.get("sources", []) if s.id in cited]
    verdicts = {v.claim_id: v for v in judge.claims(question, [c.model_dump() for c in claims], sources, answers).verdicts}
    return [{"id": c.id, "kind": c.kind, "statement": c.statement, "source_ids": c.source_ids,
             "verdict": verdicts[c.id].verdict if c.id in verdicts else "unjudged",
             "reason": verdicts[c.id].reason if c.id in verdicts else "裁判没有给结论"}
            for c in claims]


def run_case(case: Case, make_planner: Callable[[Recorder], Any], make_gateway: Callable[[], Any], judge: Judge,
             run: int = 1) -> dict:
    started = time.time()
    rec = Recorder(make_gateway())
    result: dict[str, Any] = {"id": case.id, "run": run, "request": case.request}
    try:
        with tempfile.TemporaryDirectory() as out:
            graph = build_graph(make_planner(rec), memory_checkpointer(), Path(out))
            config = {"configurable": {"thread_id": f"eval-{case.id}-{run}"}}
            trace = drive(graph, config, case)
            values = graph.get_state(config).values
        if rec.failed_searches and not rec.searches:  # 测的是搜索服务，不是调研链路
            raise RuntimeError(f"搜索全部失败：{rec.failed_searches[0]['message']}")
        checks = trajectory_checks(case, trace, values, rec)
        claims = judge_claims(judge, case.request, values, rec, trace.answers)
        bad = [c["id"] for c in claims if c["verdict"] in ("unsupported", "unjudged")]
        checks.append(_check("claims_supported", not bad and bool(claims), "、".join(bad) or f"{len(claims)} 条"))
        if case.judge and trace.review:
            verdict = judge.rubric(case.request, case.judge, trace.review["report"])
            checks.append(_check("judge", verdict.passed, verdict.reason))
        result.update({
            "passed": all(c["passed"] for c in checks), "checks": checks, "claims": claims,
            "metrics": {"searches": len(rec.searches), "reads": sum(1 for r in rec.reads if r["ok"]), "asks": len(trace.asks),
                        "scope_cards": trace.scope_cards, "skills": values.get("skills") or [],
                        "claims": len(claims), "partial": sum(c["verdict"] == "partial" for c in claims),
                        "unsupported": sum(c["verdict"] == "unsupported" for c in claims)},
            "searches": rec.searches, "failed_searches": rec.failed_searches, "reads": rec.reads, "asks": trace.asks,
            "answers": trace.answers, "notes": values.get("notes", []),
            "report": trace.review["report"] if trace.review else None,
        })
    except Exception as e:  # noqa: BLE001 —— 一个用例出错不影响其他用例，记为 error
        result.update({"passed": False, "error": f"{type(e).__name__}: {e}"})
    result["duration_s"] = round(time.time() - started, 1)
    return result


# ---- 命令行 ----


def summary_line(r: dict) -> str:
    if "error" in r:
        return f"{r['id']:<14} 出错  {r['duration_s']:>4.0f}s  {r['error'][:120]}"
    m = r["metrics"]
    failed = [f"{c['name']}（{c['detail'][:60]}）" for c in r["checks"] if not c["passed"]]
    return (f"{r['id']:<14} {'通过' if r['passed'] else '失败'}  {r['duration_s']:>4.0f}s  搜 {m['searches']} 读 {m['reads']} 问 {m['asks']}"
            f"  方法 {'、'.join(m['skills']) or '-'}  论断 {m['claims']}（部分 {m['partial']}，不支持 {m['unsupported']}）"
            + (f"  ✗ {'；'.join(failed)}" if failed else ""))


def main(argv: list[str] | None = None) -> int:
    from app import config
    from app.research_llm import LiveResearchPlanner
    from app.tools.gateway import GatewayClient

    ap = argparse.ArgumentParser(description="调研回放评测（真实模型）")
    ap.add_argument("--case", action="append", help="只跑这些用例（可以写多次）")
    ap.add_argument("--repeat", type=int, default=1, help="每个用例跑几遍")
    ap.add_argument("--jobs", type=int, default=3, help="同时跑几个")
    ap.add_argument("--label", default="", help="写进结果文件名，方便对比")
    args = ap.parse_args(argv)

    cases = [c for c in load_cases() if not args.case or c.id in args.case]
    if args.case and len(cases) != len(set(args.case)):
        ap.error(f"没有这些用例：{sorted(set(args.case) - {c.id for c in cases})}")
    judge = LiveJudge()

    def gateway():
        return GatewayClient(config.AGENT_TOOLS_URL, config.AGENT_TOOLS_API_KEY, scene="research_eval")  # 网关记账时和真实调研分开

    jobs = [(c, i + 1) for c in cases for i in range(args.repeat)]
    results = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(run_case, c, lambda rec: LiveResearchPlanner(gateway=rec), gateway, judge, n) for c, n in jobs]
        for f in as_completed(futures):
            results.append(f.result())
            print(summary_line(results[-1]), flush=True)
    results.sort(key=lambda r: ([c.id for c in cases].index(r["id"]), r["run"]))
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    name = f"research-{datetime.now():%Y%m%d-%H%M%S}" + (f"-{args.label}" if args.label else "") + ".json"
    path = RESULTS_DIR / name
    model = config.RESEARCH_MODEL or (config.DEEPSEEK_MODEL if config.RESEARCH_PROVIDER == "deepseek" else config.ARK_MODEL)
    path.write_text(json.dumps({"provider": config.RESEARCH_PROVIDER, "model": model, "cases": results}, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    passed = sum(r["passed"] for r in results)
    print(f"\n通过 {passed}/{len(results)}，结果：{path}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
