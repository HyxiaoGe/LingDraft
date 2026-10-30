"""对话回放评测：用真实模型按用例跑对话（evals/research/cases.yaml），每轮做几项自动检查，导出对话记录。

用法（在 backend 目录）：uv run python -m app.evals.research [--case id ...] [--repeat N] [--jobs N] [--no-sources]
每个用例用独立的内存检查点，不碰本机的服务和 sqlite；走和页面同一个运行器（ResearchRunner），
所以顺带检查流式片段的消息 id 和存下来的对得上。结果写到 data/evals/research-<时间>.json，对话记录写到同名的 .md。

每轮的检查：跑完了、有回复（这一轮的结果是 ok，最后一条模型消息有正文）、正文里引用的来源都存在；
expect 里写了的才检查：搜没搜、读到几篇。judge 写了就由裁判模型按标准看这一轮的正文（回答质量）。
正文里带来源角标的说法交给裁判模型逐条拿 agent 当时看到的材料核对（来源对不对，--no-sources 不做）。
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.graph.research import history
from app.graph.research.graph import memory_checkpointer
from app.graph.research.sources import SOURCE_ID, cited, url_key
from app.prompting import PromptFile
from app.tools.gateway import GatewayError

BACKEND = Path(__file__).resolve().parents[2]
CASES_FILE = BACKEND / "evals" / "research" / "cases.yaml"
RESULTS_DIR = BACKEND / "data" / "evals"
PROMPTS = PromptFile(Path(__file__).with_name("prompts.toml"))
EVIDENCE_CHARS = 8000  # 每个来源给裁判看的材料上限（读页本身截到 6000 字）


# ---- 用例 ----


class Expect(BaseModel):
    """没写的项不检查。"""

    model_config = ConfigDict(extra="forbid")
    searched: bool | None = None
    min_reads: int | None = None


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


# ---- 记录 agent 看到的材料 ----


class Recorder:
    """包一层网关客户端：记下每次搜索和读页的参数和结果，裁判按 agent 当时看到的材料核对引用。"""

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

    def mark(self) -> tuple[int, int]:
        with self._lock:
            return len(self.searches), len(self.reads)


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

    def rubric(self, said: str, rubric: str, reply: str) -> RubricJudgement: ...


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

    def rubric(self, said: str, rubric: str, reply: str) -> RubricJudgement:
        return self._ask(RubricJudgement, "rubric", said=said, rubric=rubric, reply=reply, today=date.today().isoformat())


# ---- 跑一轮 ----


def _check(name: str, passed: bool, detail: str = "") -> dict:
    return {"name": name, "passed": bool(passed), "detail": detail}


def play(runner, tid: str, turn: CaseTurn, rec: Recorder) -> dict:
    """说一句话，跑到这一轮结束。返回这一轮的记录。"""
    searches0, reads0 = rec.mark()
    runner.accept(tid, turn.say)
    events = list(runner._graph_events(tid, None))
    view = runner.state_view(tid)
    msgs = runner.graph.get_state(runner._cfg(tid)).values["messages"]
    t = view["turns"][-1]
    rec_turn = t["record"] or {}
    streamed = {e["data"]["id"] for e in events if e["event"] == "delta"}
    stored = {i["id"] for i in t["items"] if i["type"] in ("thinking", "text")}
    return {"say": turn.say, "events": len(events), "status": view["status"], "error": view.get("error"),
            "reply": history.final_text(msgs[history.last_human(msgs) + 1:]), "items": t["items"],
            "suggestions": rec_turn.get("suggestions", []), "usage": rec_turn.get("usage"), "outcome": rec_turn.get("outcome"),
            "searches": rec.searches[searches0:], "reads": rec.reads[reads0:],
            "stream_ids_match": streamed == stored, "view": view}


def turn_checks(turn: CaseTurn, played: dict) -> list[dict]:
    e = turn.expect
    ids = cited(played["reply"])
    unknown = [i for i in ids if i not in played["view"]["sources"]]
    # 这条对话里的来源 id 出现在 [id] 以外的写法里（[a, b]、(a)、“见 a”），页面上不会变成角标，会原样露出来
    loose = sorted(i for i in played["view"]["sources"] if i in SOURCE_ID.sub("", played["reply"]))
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
    return out


def judge_citations(judge: Judge, question: str, played: dict, rec: Recorder) -> list[dict]:
    """正文里带角标的说法逐条拿引用的来源材料核对。正文没有引用就不核对。"""
    ids = cited(played["reply"])
    srcs = played["view"]["sources"]
    if not ids:
        return []
    sources = [{"id": i, "url": srcs[i]["url"], "text": rec.text_for(srcs[i]["url"])} for i in ids if i in srcs]
    return [v.model_dump() for v in judge.citations(question, played["reply"], sources).verdicts]


# ---- 跑一个用例 ----


def run_case(case: Case, make_planner: Callable[[Recorder], Any], make_gateway: Callable[[], Any], judge: Judge,
             run: int = 1, check_sources: bool = True) -> dict:
    from app.api.research_runner import ResearchRunner

    started = time.time()
    rec = Recorder(make_gateway())
    result: dict[str, Any] = {"id": case.id, "run": run, "turns": []}
    try:
        runner = ResearchRunner(planner=make_planner(rec), checkpointer=memory_checkpointer())
        tid = f"eval-{case.id}-{run}"
        for turn in case.turns:
            played = play(runner, tid, turn, rec)
            checks = turn_checks(turn, played)
            citations: list[dict] = []
            if played["status"] == "idle" and played["reply"]:
                if turn.judge:
                    v = judge.rubric(turn.say, turn.judge, played["reply"])
                    checks.append(_check("judge", v.passed, v.reason))
                if check_sources:
                    citations = judge_citations(judge, turn.say, played, rec)
                    bad = [c["statement"][:40] for c in citations if c["verdict"] == "unsupported"]
                    if citations:
                        checks.append(_check("sources_supported", not bad, "；".join(bad) or f"{len(citations)} 条都有依据"))
            result["turns"].append({**{k: v for k, v in played.items() if k != "view"}, "checks": checks, "citations": citations})
            if played["status"] != "idle":
                break
        if rec.failed_searches and not rec.searches:  # 测的是搜索服务，不是对话链路
            raise RuntimeError(f"搜索全部失败：{rec.failed_searches[0]['message']}")
        all_checks = [c for t in result["turns"] for c in t["checks"]]
        result["passed"] = all(c["passed"] for c in all_checks) and len(result["turns"]) == len(case.turns)
        result["usage"] = _total([t["usage"] for t in result["turns"] if t["usage"]])
    except Exception as e:  # noqa: BLE001 —— 一个用例出错不影响其他用例，记为 error
        result.update({"passed": False, "error": f"{type(e).__name__}: {e}"})
    result["duration_s"] = round(time.time() - started, 1)
    return result


def _total(usages: list[dict]) -> dict:
    keys = ("model_calls", "input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens", "searches", "reads")
    return {k: sum(u.get(k) or 0 for u in usages) for k in keys}


# ---- 输出 ----


def summary_line(r: dict) -> str:
    if "error" in r:
        return f"{r['id']:<16} 出错  {r['duration_s']:>4.0f}s  {r['error'][:160]}"
    u = r["usage"]
    failed = [f"第{i}轮 {c['name']}（{c['detail'][:60]}）" for i, t in enumerate(r["turns"], 1) for c in t["checks"] if not c["passed"]]
    return (f"{r['id']:<16} {'通过' if r['passed'] else '失败'}  {r['duration_s']:>4.0f}s  {len(r['turns'])} 轮  "
            f"调用 {u['model_calls']}  输入 {u['input_tokens'] / 1000:.0f}k（缓存 {u['cached_tokens'] / 1000:.0f}k）  输出 {u['output_tokens'] / 1000:.0f}k"
            + (f"  ✗ {'；'.join(failed)}" if failed else ""))


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


def main(argv: list[str] | None = None) -> int:
    from app import config
    from app.research_llm import LiveResearchPlanner
    from app.tools.gateway import GatewayClient

    ap = argparse.ArgumentParser(description="对话回放评测（真实模型）")
    ap.add_argument("--case", action="append", help="只跑这些用例（可以写多次）")
    ap.add_argument("--repeat", type=int, default=1, help="每个用例跑几遍")
    ap.add_argument("--jobs", type=int, default=3, help="同时跑几个")
    ap.add_argument("--no-sources", action="store_true", help="不核对正文里的引用")
    ap.add_argument("--label", default="", help="写进结果文件名，方便对比")
    args = ap.parse_args(argv)

    cases = [c for c in load_cases() if not args.case or c.id in args.case]
    if args.case and len(cases) != len(set(args.case)):
        ap.error(f"没有这些用例：{sorted(set(args.case) - {c.id for c in cases})}")
    judge = LiveJudge()

    def gateway():
        return GatewayClient(config.AGENT_TOOLS_URL, config.AGENT_TOOLS_API_KEY, scene="research_eval")  # 网关记账时和真实对话分开

    jobs = [(c, i + 1) for c in cases for i in range(args.repeat)]
    results = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(run_case, c, lambda rec: LiveResearchPlanner(gateway=rec), gateway, judge, n, not args.no_sources) for c, n in jobs]
        for f in as_completed(futures):
            results.append(f.result())
            print(summary_line(results[-1]), flush=True)
    results.sort(key=lambda r: ([c.id for c in cases].index(r["id"]), r["run"]))
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    name = f"research-{datetime.now():%Y%m%d-%H%M%S}" + (f"-{args.label}" if args.label else "")
    model = config.RESEARCH_MODEL or (config.DEEPSEEK_MODEL if config.RESEARCH_PROVIDER == "deepseek" else config.ARK_MODEL)
    (RESULTS_DIR / f"{name}.json").write_text(json.dumps({"provider": config.RESEARCH_PROVIDER, "model": model, "cases": results},
                                                         ensure_ascii=False, indent=1), encoding="utf-8")
    (RESULTS_DIR / f"{name}.md").write_text(transcript(results), encoding="utf-8")
    total = _total([r["usage"] for r in results if "usage" in r])
    passed = sum(r["passed"] for r in results)
    print(f"\n通过 {passed}/{len(results)}；合计调用 {total['model_calls']} 次，输入 {total['input_tokens'] / 1e6:.2f}M"
          f"（缓存 {total['cached_tokens'] / 1e6:.2f}M），输出 {total['output_tokens'] / 1e6:.2f}M")
    print(f"结果：{RESULTS_DIR / name}.json / .md")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
