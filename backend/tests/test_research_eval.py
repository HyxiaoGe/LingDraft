"""回放评测的离线部分：用例文件能解析，多轮驱动、每轮的检查和论断核对在假模型上跑得通。真实模型的评测见 evals/README.md。"""

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
    summary_line,
    transcript,
)
from app.research_llm import FakeResearchPlanner, fake_gateway_client
from app.tools.gateway import GatewayClient


class FakeJudge:
    def __init__(self, verdict: str = "supported") -> None:
        self.verdict = verdict
        self.sources: list[dict] = []
        self.answers: list[dict] = []
        self.rubrics: list[tuple] = []

    def claims(self, question, claims, sources, answers):
        self.sources, self.answers = sources, answers
        return ClaimJudgement(verdicts=[ClaimVerdict(claim_id=c["id"], verdict=self.verdict, reason="假裁判") for c in claims])

    def rubric(self, said, rubric, reply, report):
        self.rubrics.append((said, rubric, reply, report))
        return RubricJudgement(passed=True, reason="假裁判")


def _run(turns: list[dict], judge=None, gateway=fake_gateway_client) -> dict:
    return run_case(Case.model_validate({"id": "x", "source": "测试", "turns": turns}), lambda rec: FakeResearchPlanner(gateway=rec),
                    gateway, judge or FakeJudge())


def _failed(r: dict) -> list[tuple[int, str]]:
    return [(i, c["name"]) for i, t in enumerate(r["turns"], 1) for c in t["checks"] if not c["passed"]]


def test_cases_file_parses():
    cases = load_cases()
    assert cases and all(c.source and c.turns and c.turns[0].say for c in cases)


def test_turn_that_needs_a_report_stops_the_case_when_there_is_none():
    r = _run([{"say": "直接答：为什么", "expect": {"version": False}}, {"say": "这里说得太绝对了", "quote_sentence": 1}])
    assert "error" not in r and not r["passed"] and len(r["turns"]) == 1
    assert "第 2 轮" in r["stopped"] and "停在" in summary_line(r)


def test_multi_turn_case_passes_and_records_each_turn():
    judge = FakeJudge()
    r = _run([
        {"say": "调研向量库选型", "expect": {"version": True, "asked": False, "skill": "comparison", "min_reads": 1, "max_searches": 2,
                                        "recency": False, "searched": True}, "judge": "给了推荐"},
        {"say": "直接答：为什么", "expect": {"version": False, "searched": False, "skill": None}},
        {"say": "这条不靠谱", "exclude": "1", "expect": {"version": True}},
        {"say": "重写", "rewrite_section": 1, "expect": {"version": True, "only_section": 1}},
    ], judge)
    assert r["passed"], (_failed(r), r.get("error"))
    assert [t["version"] for t in r["turns"]] == [1, None, 2, 3]
    assert r["turns"][2]["action"]["type"] == "exclude_source" and r["turns"][3]["action"]["type"] == "rewrite_section"
    assert all(t["stream_ids_match"] for t in r["turns"])
    assert r["usage"]["model_calls"] == sum(t["usage"]["model_calls"] for t in r["turns"])
    # 裁判看的是这一轮的正文和当时的报告；论断按 agent 当时看到的材料核对
    said, rubric, reply, report = judge.rubrics[0]
    assert said == "调研向量库选型" and rubric == "给了推荐" and reply.startswith("结论") and report.startswith("# ")
    assert r["claims"] and r["claims_check"]["passed"]
    assert judge.sources and all(s["text"] != "（没有记录到材料）" for s in judge.sources)
    text = transcript([r])
    assert "## 第 4 轮" in text and "第 3 版报告" in text and "论断核对" in text
    assert summary_line(r).startswith("x") and "通过" in summary_line(r)


def test_failed_checks_and_unsupported_claims_fail_the_case():
    r = _run([{"say": "调研一下天气", "expect": {"skill": "comparison", "asked": True, "version": False}}], FakeJudge("unsupported"))
    assert not r["passed"] and _failed(r) == [(1, "version"), (1, "asked"), (1, "skill")]
    assert not r["claims_check"]["passed"]
    assert "✗" in summary_line(r)


def test_only_section_catches_edits_elsewhere():
    """选中一句说“太绝对”，假模型把整份报告重写了：别的节也变了，检查要抓出来。"""
    r = _run([{"say": "调研局部修改"},
              {"say": "这里说得太绝对 调研", "quote_sentence": 1, "expect": {"only_section": "quoted"}}])
    assert _failed(r) == [(2, "only_section")]
    assert r["turns"][1]["quote"]["claim_ids"]


def test_asks_answered_from_case_and_kept_for_the_judge():
    judge = FakeJudge()
    r = _run([{"say": "问一下 调研局部修改", "answers": ["包含"], "expect": {"asked": True, "version": True}}], judge)
    assert r["passed"], _failed(r)
    assert r["turns"][0]["asks"] == ["范围包含 PPT 工具吗？"]
    assert judge.answers == [{"question": "范围包含 PPT 工具吗？", "answer": "包含"}]
    # 没写回答的提问跳过
    r = _run([{"say": "问一下 直接答", "expect": {"asked": True}}])
    assert r["passed"] and r["turns"][0]["answers"] == []


def test_search_service_down_is_an_error_not_a_failure():
    """搜索一次都没成功：测的是搜索服务，记为出错，不算调研链路没通过。"""
    def down():
        return GatewayClient("http://gw", "k", transport=httpx.MockTransport(lambda req: httpx.Response(400, text="bad header")))

    r = _run([{"say": "比较两款笔记软件"}], gateway=down)
    assert not r["passed"] and r["error"].startswith("RuntimeError: 搜索全部失败")


def test_recorder_keeps_text_per_url():
    rec = Recorder(fake_gateway_client())
    hits = rec.search("q", count=3, freshness="pm", domains=["example.test"])
    rec.read(hits[0].url)
    assert rec.searches[0]["freshness"] == "pm" and rec.searches[0]["domains"] == ["example.test"]
    assert "正文" in rec.text_for(hits[0].url) and "长摘要" in rec.text_for(hits[0].url)
    assert rec.text_for("https://nowhere.test") == "（没有记录到材料）"
