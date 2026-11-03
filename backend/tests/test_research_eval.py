"""回放评测的离线部分：用例文件能解析，多轮驱动、每轮的检查和引用核对在假模型上跑得通。真实模型的评测见 evals/README.md。"""

from __future__ import annotations

import httpx

from app.evals.research import (
    Case,
    CaseTurn,
    CitationJudgement,
    CitationVerdict,
    Recorder,
    RubricJudgement,
    load_cases,
    run_case,
    summary_line,
    transcript,
    turn_checks,
)
from app.research_llm import FakeResearchPlanner, fake_gateway_client
from app.tools.gateway import GatewayClient
from tests.research_helpers import blocking


class FakeJudge:
    def __init__(self, verdict: str = "supported") -> None:
        self.verdict = verdict
        self.sources: list[dict] = []
        self.rubrics: list[tuple] = []

    def citations(self, question, reply, sources):
        self.sources = sources
        return CitationJudgement(verdicts=[CitationVerdict(statement=reply[:10], source_ids=[s["id"]], verdict=self.verdict, reason="假裁判")
                                           for s in sources])

    def rubric(self, said, rubric, reply):
        self.rubrics.append((said, rubric, reply))
        return RubricJudgement(passed=True, reason="假裁判")


def _run(turns: list[dict], judge=None, gateway=fake_gateway_client) -> dict:
    return run_case(Case.model_validate({"id": "x", "source": "测试", "turns": turns}), lambda rec: FakeResearchPlanner(gateway=rec),
                    gateway, judge or FakeJudge())


def _failed(r: dict) -> list[tuple[int, str]]:
    return [(i, c["name"]) for i, t in enumerate(r["turns"], 1) for c in t["checks"] if not c["passed"]]


def test_cases_file_parses():
    cases = load_cases()
    assert cases and all(c.source and c.turns and c.turns[0].say for c in cases)


def test_multi_turn_case_passes_and_records_each_turn():
    judge = FakeJudge()
    r = _run([
        {"say": "查一下向量库选型", "expect": {"searched": True, "min_reads": 1}, "judge": "给了推荐"},
        {"say": "直接答：为什么", "expect": {"searched": False}},
    ], judge)
    assert r["passed"], (_failed(r), r.get("error"))
    assert all(t["stream_ids_match"] for t in r["turns"])
    assert r["usage"]["model_calls"] == sum(t["usage"]["model_calls"] for t in r["turns"])
    assert [c["name"] for c in r["turns"][0]["checks"]] == ["finished", "replied", "stream_ids_match", "cited_known", "cite_format", "searched",
                                                           "min_reads",
                                                           "judge", "sources_supported"]
    # 裁判看的是这一轮的正文；引用按 agent 当时看到的材料核对；没有引用的那轮不核对
    said, rubric, reply = judge.rubrics[0]
    assert said == "查一下向量库选型" and rubric == "给了推荐" and reply.startswith("结论")
    assert len(r["turns"][0]["citations"]) == 2 and r["turns"][1]["citations"] == []
    assert len(judge.sources) == 2 and all(s["text"] != "（没有记录到材料）" for s in judge.sources)
    text = transcript([r])
    assert "## 第 2 轮" in text and "引用核对" in text
    assert summary_line(r).startswith("x") and "通过" in summary_line(r)


def test_checked_time_counts_calls_and_each_call_is_recorded():
    r = _run([{"say": "现在几点 直接答", "expect": {"checked_time": True, "searched": False}},
              {"say": "直接答 谢谢", "expect": {"checked_time": True}}])
    assert _failed(r) == [(2, "checked_time")]
    assert [t["time_checks"] for t in r["turns"]] == [1, 0]
    assert [len(t["calls"]) for t in r["turns"]] == [2, 1] and all("cached" in c for c in r["turns"][0]["calls"])


def test_failed_checks_and_unsupported_citations_fail_the_case():
    r = _run([{"say": "直接答：天气", "expect": {"searched": True}}, {"say": "查一下 不回复"}])
    assert not r["passed"] and _failed(r) == [(1, "searched"), (2, "replied")]
    r = _run([{"say": "查一下天气"}], FakeJudge("unsupported"))
    assert not r["passed"] and _failed(r) == [(1, "sources_supported")]
    assert "✗" in summary_line(r)


def test_reply_is_the_last_model_message():
    """有回复看的是最后一条模型消息：中途带着工具调用说的话不算这一轮的回复。"""
    r = _run([{"say": "坏JSON 一直坏"}])
    assert r["turns"][0]["reply"] == "" and r["turns"][0]["outcome"] == "no_reply" and _failed(r) == [(1, "replied")]


def test_search_service_down_is_an_error_not_a_failure():
    """搜索一次都没成功：测的是搜索服务，记为出错，不算对话链路没通过。"""
    def down():
        return GatewayClient("http://gw", "k", transport=httpx.MockTransport(lambda req: httpx.Response(400, text="bad header")))

    r = _run([{"say": "比较两款笔记软件"}], gateway=down)
    assert not r["passed"] and r["error"].startswith("RuntimeError: 搜索全部失败")


def test_recorder_keeps_text_per_url():
    rec = blocking(Recorder(fake_gateway_client()))
    hits = rec.search("q", count=3, freshness="pm", domains=["example.test"])
    rec.read(hits[0].url)
    assert rec.searches[0]["freshness"] == "pm" and rec.searches[0]["domains"] == ["example.test"]
    assert "正文" in rec.text_for(hits[0].url) and "长摘要" in rec.text_for(hits[0].url)
    assert rec.text_for("https://nowhere.test") == "（没有记录到材料）"


def test_citation_outside_brackets_fails_cite_format():
    played = {"status": "idle", "outcome": "ok", "stream_ids_match": True, "searches": [], "reads": [],
              "reply": "定价 2 美元 [s3ae980]。[s3ae980 之外，见 sa63a73] 另有说法 [sa63a73][s1b2c3d]",
              "view": {"sources": {"s3ae980": {}, "sa63a73": {}, "s1b2c3d": {}}}}
    checks = {c["name"]: c for c in turn_checks(CaseTurn(say="x"), played)}
    assert not checks["cite_format"]["passed"] and checks["cite_format"]["detail"] == "没按 [id] 写的引用：s3ae980、sa63a73"
    played["reply"] = "定价 2 美元 [s3ae980]，另有说法 [sa63a73][s1b2c3d]。"
    assert {c["name"]: c for c in turn_checks(CaseTurn(say="x"), played)}["cite_format"]["passed"]
