"""回放评测的离线部分：用例文件能解析，轨迹检查和打分在假模型上跑得通。真实模型的评测见 evals/README.md。"""

from __future__ import annotations

import httpx

from app.evals.research import (
    Case,
    ClaimJudgement,
    ClaimVerdict,
    Recorder,
    RubricJudgement,
    load_cases,
    run_case,
)
from app.research_llm import FakeResearchPlanner, fake_gateway_client
from app.tools.gateway import GatewayClient


class FakeJudge:
    def __init__(self, verdict: str = "supported") -> None:
        self.verdict = verdict
        self.seen: list[dict] = []

    def claims(self, question, claims, sources, answers):
        self.seen, self.answers = sources, answers
        return ClaimJudgement(verdicts=[ClaimVerdict(claim_id=c["id"], verdict=self.verdict, reason="假裁判") for c in claims])

    def rubric(self, question, rubric, report):
        return RubricJudgement(passed=True, reason="假裁判")


def _run(case: dict, judge=None) -> dict:
    return run_case(Case.model_validate({"source": "测试", **case}), lambda rec: FakeResearchPlanner(gateway=rec),
                    fake_gateway_client, judge or FakeJudge())


def test_cases_file_parses():
    cases = load_cases()
    assert cases and all(c.source and c.request for c in cases)


def test_passing_case_records_trajectory_and_evidence():
    judge = FakeJudge()
    r = _run({"id": "pick", "request": "向量库选型", "expect": {"asked": False, "skill": "comparison", "min_reads": 1,
                                                          "max_searches": 2, "recency": False}, "judge": "给了推荐"}, judge)
    assert r["passed"], r
    assert {c["name"] for c in r["checks"]} >= {"reached_review", "answered", "skill", "min_reads", "claims_supported", "judge"}
    assert r["metrics"]["skills"] == ["comparison"] and r["metrics"]["reads"] == 1 and r["searches"][0]["urls"]
    assert judge.seen and all(s["text"] != "（没有记录到材料）" for s in judge.seen)  # 裁判拿到的是 agent 看到的材料


def test_failing_checks_and_unsupported_claims_fail_the_case():
    r = _run({"id": "x", "request": "最近新加坡的天气情况如何？", "expect": {"skill": "comparison", "asked": True}},
             FakeJudge("unsupported"))
    failed = {c["name"] for c in r["checks"] if not c["passed"]}
    assert not r["passed"] and failed == {"skill", "asked", "claims_supported"}


def test_scope_and_ask_cards_answered_from_case():
    judge = FakeJudge()
    r = _run({"id": "cards", "request": "AI 海报工具在局部修改上的现状", "scope_answers": ["改文字"], "ask_answers": ["包含"],
              "expect": {"scope_card": True, "asked": True}}, judge)
    assert r["passed"], r
    assert r["asks"] == ["范围是否包含 PPT 工具？"] and r["metrics"]["scope_cards"] == 1
    assert [a["answer"] for a in judge.answers] == ["改文字", "包含"]  # 用户答过的话交给裁判当前提


def test_search_service_down_is_an_error_not_a_failure():
    """搜索一次都没成功：测的是搜索服务，记为出错，不算调研链路没通过。"""
    def down():
        return GatewayClient("http://gw", "k", transport=httpx.MockTransport(lambda req: httpx.Response(400, text="bad header")))

    r = run_case(Case(id="x", source="测试", request="最近新加坡的天气情况如何？"), lambda rec: FakeResearchPlanner(gateway=rec),
                 down, FakeJudge())
    assert not r["passed"] and r["error"].startswith("RuntimeError: 搜索全部失败")


def test_recorder_keeps_text_per_url():
    rec = Recorder(fake_gateway_client())
    hits = rec.search("q", count=3, freshness="pm", domains=["example.test"])
    rec.read(hits[0].url)
    assert rec.searches[0]["freshness"] == "pm" and rec.searches[0]["domains"] == ["example.test"]
    assert "正文" in rec.text_for(hits[0].url) and "长摘要" in rec.text_for(hits[0].url)
    assert rec.text_for("https://nowhere.test") == "（没有记录到材料）"
