"""调研回放评测：用真实模型按场景跑多轮对话（evals/research/cases.yaml），每轮做几项自动检查，导出对话记录。

用法（在 backend 目录）：uv run python -m app.evals.research [--case id ...] [--repeat N] [--jobs N] [--no-claims]
每个场景用独立的内存检查点和临时目录，不碰本机的服务和 sqlite；走和页面同一个运行器（ResearchRunner），
所以顺带检查流式片段的消息 id 和存下来的对得上。结果写到 data/evals/research-<时间>.json，对话记录写到同名的 .md。

每轮的检查（expect 里写了的才检查）：出没出新版本、问没问、搜没搜、用了哪个调研方法、读了几篇、搜了几次、有没有限定时间、
只改了指定的那一节、那一节有没有变短；另外每轮都要求有正文。judge 写了就由裁判模型按标准看这一轮的正文和报告。
最后一版报告的论断交给裁判模型逐条拿 agent 当时看到的材料核对（--no-claims 不做）。
"""

from __future__ import annotations

import argparse
import json
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.graph.research.graph import memory_checkpointer
from app.graph.research.sources import url_key
from app.prompting import PromptFile
from app.tools.gateway import GatewayError

BACKEND = Path(__file__).resolve().parents[2]
CASES_FILE = BACKEND / "evals" / "research" / "cases.yaml"
RESULTS_DIR = BACKEND / "data" / "evals"
PROMPTS = PromptFile(Path(__file__).with_name("prompts.toml"))
EVIDENCE_CHARS = 8000  # 每个来源给裁判看的材料上限（读页本身截到 6000 字）
MAX_ASKS = 4  # 一轮最多应答几张提问卡，防止卡在循环里


# ---- 用例 ----


class Expect(BaseModel):
    """没写的项不检查。skill 写 null 表示这一轮不该读调研方法。"""

    model_config = ConfigDict(extra="forbid")
    version: bool | None = None
    asked: bool | None = None
    searched: bool | None = None
    skill: str | None = None
    min_reads: int | None = None
    max_searches: int | None = None
    recency: bool | None = None
    cites_domain: str | None = None
    only_section: int | Literal["quoted"] | None = Field(default=None, description="只有这一节（上一版里的第几节）能变；quoted 是选中的句子所在的节")
    shorter: int | None = Field(default=None, description="上一版的第几节要变短")


class CaseTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    say: str = ""
    quote_sentence: int | None = Field(default=None, description="先选中上一版报告里第几句带来源的句子")
    exclude: str | None = Field(default=None, description="在来源卡上点“排除”：上一版报告里编号为这个的来源")
    rewrite_section: int | None = Field(default=None, description="点上一版报告第几节的“重写这一节”")
    answers: list[str | None] = Field(default_factory=list, description="这一轮的提问按顺序怎么答；没写的跳过")
    expect: Expect = Field(default_factory=Expect)
    judge: str | None = Field(default=None, description="写成“正文 / 报告做到了 X，且没有 Y”")


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    source: str = Field(description="哪次探针、事故、改动或交互约定里的哪个场景")
    turns: list[CaseTurn] = Field(min_length=1)


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

    def mark(self) -> tuple[int, int]:
        with self._lock:
            return len(self.searches), len(self.reads)


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

    def rubric(self, said: str, rubric: str, reply: str, report: str | None) -> RubricJudgement: ...


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

    def rubric(self, said: str, rubric: str, reply: str, report: str | None) -> RubricJudgement:
        return self._ask(RubricJudgement, "rubric", said=said, rubric=rubric, reply=reply, report=report)


# ---- 跑一轮 ----


def _check(name: str, passed: bool, detail: str = "") -> dict:
    return {"name": name, "passed": bool(passed), "detail": detail}


def _section_texts(report: dict | None) -> list[str]:
    """报告每一节的正文（按节的先后）。"""
    if not report:
        return []
    return ["".join(s["text"] for s in report["sections"][n["id"]]["sentences"]) for n in report["outline"] if n["id"] in report["sections"]]


def _resolve(turn: CaseTurn, report: dict | None) -> tuple[dict | None, dict | None, str | None]:
    """用例里按位置写的选中、按钮 → 接口要的 quote / action（对着上一版报告）。另返回选中的句子所在的节 id。"""
    blocks = (report or {}).get("blocks") or []
    quote = action = quoted = None
    if turn.quote_sentence is not None:
        sent = [b for b in blocks if b["type"] == "sentence" and b["claim_id"]][turn.quote_sentence - 1]
        claim = next(c for c in report["claims"] if c["id"] == sent["claim_id"])
        quoted = claim["outline_id"]
        quote = {"text": sent["text"], "claim_ids": [claim["id"]], "section_ids": [quoted]}
    if turn.exclude is not None:
        src = next(b for b in blocks if b["type"] == "source" and b["label"] == turn.exclude)
        action = {"type": "exclude_source", "source_id": src["source_id"]}
    if turn.rewrite_section is not None:
        action = {"type": "rewrite_section", "section_id": report["outline"][turn.rewrite_section - 1]["id"]}
    return quote, action, quoted


def play(runner, tid: str, turn: CaseTurn, before: dict | None, rec: Recorder) -> dict:
    """说一句话，跑到这一轮结束（提问按用例答，没写的跳过）。返回这一轮的记录。"""
    report_before = (before or {}).get("report")
    quote, action, quoted = _resolve(turn, report_before)
    searches0, reads0 = rec.mark()
    runner.accept(tid, turn.say, quote, action)
    events = list(runner._graph_events(tid, None))
    answers, asks, user_answers = iter(turn.answers), [], []
    for _ in range(MAX_ASKS):
        it = runner.pending(tid)
        if it is None:
            break
        card = runner._interrupt_view(tid, it)
        answer = next(answers, None)
        asks.append(card["title"])
        if answer:
            user_answers.append({"question": card["title"], "answer": answer})
        events += list(runner._graph_events(tid, runner.resume_input(tid, {"answer": answer} if answer else {"skip": True}, it.id)))
    view = runner.state_view(tid)
    t = view["turns"][-1]
    rec_turn = t["record"] or {}
    reply = next((i["text"] for i in reversed(t["items"]) if i["type"] == "text"), "")
    streamed = {e["data"]["id"] for e in events if e["event"] == "delta"}
    stored = {i["id"] for i in t["items"] if i["type"] in ("thinking", "text")}
    return {"say": turn.say, "quote": quote, "action": action, "quoted_section": quoted, "events": len(events),
            "status": view["status"], "error": view.get("error"), "reply": reply, "items": t["items"], "asks": asks,
            "answers": user_answers, "version": rec_turn.get("version"), "suggestions": rec_turn.get("suggestions", []),
            "usage": rec_turn.get("usage"), "outcome": rec_turn.get("outcome"),
            "searches": rec.searches[searches0:], "reads": rec.reads[reads0:],
            "stream_ids_match": streamed == stored, "view": view}


def turn_checks(turn: CaseTurn, played: dict, before: dict | None) -> list[dict]:
    e = turn.expect
    view, prev = played["view"], (before or {}).get("report")
    steps = [i for i in played["items"] if i["type"] == "step"]
    skills = [s["text"] for s in steps if s["kind"] == "skill" and s["ok"]]
    out = [_check("finished", played["status"] == "idle", played.get("error") or played["status"]),
           _check("replied", bool(played["reply"]), played.get("outcome") or ""),
           _check("stream_ids_match", played["stream_ids_match"])]
    if e.version is not None:
        out.append(_check("version", (played["version"] is not None) == e.version, f"第 {played['version']} 版" if played["version"] else "没出新版本"))
    if e.asked is not None:
        out.append(_check("asked", bool(played["asks"]) == e.asked, "；".join(played["asks"]) or "没问"))
    if e.searched is not None:
        out.append(_check("searched", bool(played["searches"]) == e.searched, f"搜了 {len(played['searches'])} 次"))
    if "skill" in e.model_fields_set:
        from app.graph.research import skills as sk

        want = [sk.catalog()[e.skill].title] if e.skill else []
        out.append(_check("skill", skills == want, "、".join(skills) or "没用"))
    if e.min_reads is not None:
        n = sum(1 for r in played["reads"] if r["ok"])
        out.append(_check("min_reads", n >= e.min_reads, f"读到 {n} 篇"))
    if e.max_searches is not None:
        out.append(_check("max_searches", len(played["searches"]) <= e.max_searches, f"搜了 {len(played['searches'])} 次"))
    if e.recency is not None:
        used = [s["freshness"] for s in played["searches"] if s["freshness"]]
        out.append(_check("recency", bool(used) == e.recency, "、".join(used) or "没限定"))
    if e.cites_domain:
        hosts = sorted({urlsplit(s["url"]).hostname or "" for s in view["sources"].values() if s["status"] == "adopted"})
        hit = any(h == e.cites_domain or h.endswith("." + e.cites_domain) for h in hosts)
        out.append(_check("cites_domain", hit, "、".join(hosts)))
    if e.only_section is not None and prev and view.get("report"):
        # 上一版的节按 id 对到这一版：只有指定的那一节能变，别的节一字不差
        target = played["quoted_section"] if e.only_section == "quoted" else prev["outline"][e.only_section - 1]["id"]
        now = {n["id"]: t for n, t in zip(view["report"]["outline"], _section_texts(view["report"]))}
        old = {n["id"]: t for n, t in zip(prev["outline"], _section_texts(prev))}
        changed = [sid for sid in old if now.get(sid) != old[sid]]
        out.append(_check("only_section", changed == [target], f"变了：{'、'.join(changed) or '没有'}；应该只变 {target}"))
    if e.shorter is not None and prev and view.get("report"):
        sid = prev["outline"][e.shorter - 1]["id"]
        old = dict(zip([n["id"] for n in prev["outline"]], _section_texts(prev))).get(sid, "")
        new = dict(zip([n["id"] for n in view["report"]["outline"]], _section_texts(view["report"]))).get(sid, "")
        out.append(_check("shorter", 0 < len(new) < len(old), f"{len(old)} → {len(new)} 字"))
    return out


# ---- 跑一个场景 ----


def judge_claims(judge: Judge, question: str, view: dict, rec: Recorder, answers: list[dict]) -> list[dict]:
    report = view.get("report")
    claims = (report or {}).get("claims") or []
    if not claims:
        return []
    cited = {sid for c in claims for sid in c["source_ids"]}
    sources = [{"id": s["id"], "url": s["url"], "text": rec.text_for(s["url"])} for s in view["sources"].values() if s["id"] in cited]
    verdicts = {v.claim_id: v for v in judge.claims(question, claims, sources, answers).verdicts}
    return [{"id": c["id"], "kind": c["kind"], "statement": c["statement"], "source_ids": c["source_ids"],
             "verdict": verdicts[c["id"]].verdict if c["id"] in verdicts else "unjudged",
             "reason": verdicts[c["id"]].reason if c["id"] in verdicts else "裁判没有给结论"}
            for c in claims]


def _report_text(view: dict) -> str | None:
    from app.graph.research.report import to_markdown

    return to_markdown(view["report"]["blocks"]) if view.get("report") else None


def run_case(case: Case, make_planner: Callable[[Recorder], Any], make_gateway: Callable[[], Any], judge: Judge,
             run: int = 1, claims: bool = True) -> dict:
    from app.api.research_runner import ResearchRunner

    started = time.time()
    rec = Recorder(make_gateway())
    result: dict[str, Any] = {"id": case.id, "run": run, "turns": []}
    try:
        with tempfile.TemporaryDirectory() as out:
            runner = ResearchRunner(planner=make_planner(rec), checkpointer=memory_checkpointer(), out_dir=Path(out))
            tid = f"eval-{case.id}-{run}"
            before: dict | None = None
            answers: list[dict] = []
            for turn in case.turns:
                if (turn.quote_sentence, turn.exclude, turn.rewrite_section) != (None, None, None) and not (before or {}).get("report"):
                    result["stopped"] = f"第 {len(result['turns']) + 1} 轮要对着上一版报告选中或点按钮，但还没有报告"
                    break
                played = play(runner, tid, turn, before, rec)
                checks = turn_checks(turn, played, before)
                answers += played["answers"]
                if turn.judge and played["status"] == "idle":
                    said = turn.say + (f"\n（选中的文字：「{played['quote']['text']}」）" if played["quote"] else "") + \
                        (f"\n（按钮：{json.dumps(played['action'], ensure_ascii=False)}）" if played["action"] else "")
                    v = judge.rubric(said, turn.judge, played["reply"], _report_text(played["view"]))
                    checks.append(_check("judge", v.passed, v.reason))
                before = played["view"]
                result["turns"].append({**{k: v for k, v in played.items() if k != "view"}, "checks": checks,
                                        "report": _report_text(before), "report_version": (before.get("report") or {}).get("version")})
                if played["status"] != "idle":
                    break
        if rec.failed_searches and not rec.searches:  # 测的是搜索服务，不是调研链路
            raise RuntimeError(f"搜索全部失败：{rec.failed_searches[0]['message']}")
        final: list[dict] = []
        if claims and before and before.get("report"):
            final = judge_claims(judge, case.turns[0].say, before, rec, answers)
            bad = [c["id"] for c in final if c["verdict"] in ("unsupported", "unjudged")]
            result["claims_check"] = _check("claims_supported", not bad, "、".join(bad) or f"{len(final)} 条")
        result["claims"] = final
        all_checks = [c for t in result["turns"] for c in t["checks"]] + ([result["claims_check"]] if "claims_check" in result else [])
        result["passed"] = all(c["passed"] for c in all_checks) and len(result["turns"]) == len(case.turns)
        result["usage"] = _total([t["usage"] for t in result["turns"] if t["usage"]])
    except Exception as e:  # noqa: BLE001 —— 一个场景出错不影响其他场景，记为 error
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
    if "claims_check" in r and not r["claims_check"]["passed"]:
        failed.append(f"论断（{r['claims_check']['detail'][:60]}）")
    return (f"{r['id']:<16} {'通过' if r['passed'] else '失败'}  {r['duration_s']:>4.0f}s  {len(r['turns'])} 轮  "
            f"调用 {u['model_calls']}  输入 {u['input_tokens'] / 1000:.0f}k（缓存 {u['cached_tokens'] / 1000:.0f}k）  输出 {u['output_tokens'] / 1000:.0f}k"
            + (f"  ✗ {'；'.join(failed)}" if failed else "") + (f"  停在：{r['stopped']}" if r.get("stopped") else ""))


def transcript(results: list[dict]) -> str:
    """对话记录：每轮用户说了什么、模型做了什么、正文、出没出版本、检查结果。人看的。"""
    out = []
    for r in results:
        out.append(f"# {r['id']}（第 {r['run']} 遍）{'通过' if r.get('passed') else '失败'}\n")
        if "error" in r:
            out.append(f"出错：{r['error']}\n")
        if r.get("stopped"):
            out.append(f"没跑完：{r['stopped']}\n")
        for i, t in enumerate(r.get("turns", []), 1):
            out.append(f"## 第 {i} 轮\n\n**用户**：{t['say']}" + (f"（选中：「{t['quote']['text']}」）" if t["quote"] else "")
                       + (f"（按钮：{t['action']['type']}）" if t["action"] else "") + "\n")
            for item in t["items"]:
                if item["type"] == "thinking":
                    out.append(f"> 思考（{(item.get('ms') or 0) / 1000:.0f} 秒）：{item['text'][:600]}" + ("……" if len(item["text"]) > 600 else ""))
                elif item["type"] == "step":
                    out.append(f"- [{item['kind']}] {item['text']} " + ("✓" if item["ok"] else "✗ " + item["message"]) + (f" {item['message']}" if item["ok"] and item["message"] else ""))
                elif item["type"] == "ask":
                    out.append(f"- [问] {item['question']} → {item['answer'] or '跳过'}")
                elif item["type"] == "text" and item["text"] != t["reply"]:
                    out.append(f"- [话] {item['text']}")
            out.append(f"\n**正文**：\n\n{t['reply']}\n")
            out.append(f"版本：{'第 ' + str(t['version']) + ' 版' if t['version'] else '没出新版本'}；推荐：{' / '.join(t['suggestions'])}；用量：{t['usage']}\n")
            out.append("检查：" + "；".join(f"{c['name']} {'✓' if c['passed'] else '✗'} {c['detail']}" for c in t["checks"]) + "\n")
            if t["version"] and t["report"]:
                out.append(f"<details><summary>第 {t['version']} 版报告</summary>\n\n{t['report']}\n</details>\n")
        if r.get("claims"):
            out.append("## 论断核对\n\n" + "\n".join(f"- {c['id']} {c['verdict']}：{c['statement']}（{c['reason']}）" for c in r["claims"]) + "\n")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    from app import config
    from app.research_llm import LiveResearchPlanner
    from app.tools.gateway import GatewayClient

    ap = argparse.ArgumentParser(description="调研回放评测（真实模型，多轮）")
    ap.add_argument("--case", action="append", help="只跑这些用例（可以写多次）")
    ap.add_argument("--repeat", type=int, default=1, help="每个用例跑几遍")
    ap.add_argument("--jobs", type=int, default=3, help="同时跑几个")
    ap.add_argument("--no-claims", action="store_true", help="不核对最后一版报告的论断")
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
        futures = [pool.submit(run_case, c, lambda rec: LiveResearchPlanner(gateway=rec), gateway, judge, n, not args.no_claims) for c, n in jobs]
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
