"""回放评测的离线部分：用例文件能解析，多轮驱动、每轮的检查和引用核对在假模型上跑得通。真实模型的评测见 evals/README.md。"""

from __future__ import annotations

import json
from datetime import datetime

from fastmcp import FastMCP
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, ToolMessage

from app.evals.research import (
    Case,
    CaseTurn,
    CitationJudgement,
    CitationVerdict,
    RubricJudgement,
    load_cases,
    material,
    rejudge_case,
    run_case,
    stability,
    summary_line,
    text_for,
    transcript,
    turn_checks,
)
from app import aio
from app.evals.tape import Tape
from app.research_llm import FakeResearchPlanner
from app.tools.research_tools import CHINA_TZ
from tests.test_research import failing_search


class FakeJudge:
    def __init__(self, verdict: str = "supported", uncited: str | None = None) -> None:
        self.verdict = verdict
        self.uncited = uncited  # 再列一句没带角标的（真裁判有时会这样）
        self.sources: list[dict] = []
        self.rubrics: list[tuple] = []

    def citations(self, question, reply, sources, today):
        self.sources = sources
        self.today = today
        extra = [CitationVerdict(statement="没角标的话", source_ids=[], verdict=self.uncited, reason="假裁判")] if self.uncited else []
        return CitationJudgement(verdicts=[CitationVerdict(statement=reply[:10], source_ids=[s["id"]], verdict=self.verdict, reason="假裁判")
                                           for s in sources] + extra)

    def rubric(self, said, rubric, reply, today):
        self.rubrics.append((said, rubric, reply, today))
        return RubricJudgement(passed=True, reason="假裁判")


def _run(turns: list[dict], judge=None, planner=FakeResearchPlanner) -> dict:
    return run_case(Case.model_validate({"id": "x", "source": "测试", "turns": turns}), planner, judge or FakeJudge())


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
    said, rubric, reply, today = judge.rubrics[0]
    assert said == "查一下向量库选型" and rubric == "给了推荐" and reply.startswith("结论")
    assert today == datetime.now(CHINA_TZ).date().isoformat() == r["turns"][0]["today"]  # 没回放：就是今天
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
    r = _run([{"say": "查一下天气"}], FakeJudge(uncited="unsupported"))  # 没带角标的句子不算引用，不核对
    assert r["passed"], _failed(r)
    assert r["turns"][0]["citations"] and all(c["source_ids"] for c in r["turns"][0]["citations"])


def test_reply_is_the_last_model_message():
    """有回复看的是最后一条模型消息：中途带着工具调用说的话不算这一轮的回复。"""
    r = _run([{"say": "坏JSON 一直坏"}])
    assert r["turns"][0]["reply"] == "" and r["turns"][0]["outcome"] == "no_reply" and _failed(r) == [(1, "replied")]


def test_search_service_down_is_an_error_not_a_failure():
    """搜索一次都没成功：测的是搜索服务，记为出错，不算对话链路没通过。"""
    async def down(query: str) -> str:
        raise RuntimeError("Client failed to connect")

    r = _run([{"say": "比较两款笔记软件"}], planner=lambda **_: failing_search(down))
    assert not r["passed"] and r["error"].startswith("RuntimeError: 搜索全部失败（unavailable）")


def _tool(name: str, cid: str, body, status: str = "success", artifact=None) -> ToolMessage:
    return ToolMessage(content=json.dumps(body, ensure_ascii=False), name=name, tool_call_id=cid, status=status, artifact=artifact)


def test_material_keeps_what_the_agent_saw_per_url():
    """搜了什么、读了什么、每个网址看到的文字，都从消息里取：新旧两种搜索结果都认，没做成的记下错误码。"""
    calls = AIMessage(content="", tool_calls=[
        {"name": "search_web", "args": {"query": "q", "recency_days": 30, "domains": ["example.test"]}, "id": "c1"},
        {"name": "search_web", "args": {"query": "old"}, "id": "c2"},
        {"name": "search_web", "args": {"query": "down"}, "id": "c3"},
        {"name": "read_page", "args": {"url": "https://a.test/1"}, "id": "c4"},
        {"name": "read_page", "args": {"url": "https://a.test/3"}, "id": "c5"}])
    msgs = [calls,
            _tool("search_web", "c1", {"ok": True, "query": "q", "results": [{"id": "s1", "url": "https://a.test/1", "title": "A", "snippet": "摘要一"}]}),
            _tool("search_web", "c2", [{"id": "s2", "url": "https://a.test/2", "title": "B", "description": "短", "content": "旧的长摘要"}]),
            ToolMessage(content="No search was run.", name="search_web", tool_call_id="c3", status="error",
                        artifact={"error": {"code": "unavailable", "retryable": True}}),
            _tool("read_page", "c4", {"id": "s1", "ok": True, "url": "https://a.test/1", "title": "A", "content": "正文一"}),
            ToolMessage(content="The page does not exist.", name="read_page", tool_call_id="c5", status="error",
                        artifact={"error": {"code": "page_not_found", "retryable": False, "status": 404}})]
    m = material(msgs)
    assert m["searches"] == [{"query": "q", "recency_days": 30, "domains": ["example.test"], "urls": ["https://a.test/1"]},
                             {"query": "old", "recency_days": None, "domains": [], "urls": ["https://a.test/2"]}]
    assert [(f["query"], f["code"]) for f in m["failed_searches"]] == [("down", "unavailable")]
    assert m["reads"] == [{"url": "https://a.test/1", "ok": True, "code": None}, {"url": "https://a.test/3", "ok": False, "code": "page_not_found"}]
    assert text_for(m["evidence"], "https://A.test/1#top") == "摘要一\n\n正文一" and text_for(m["evidence"], "https://a.test/2") == "旧的长摘要"
    assert text_for(m["evidence"], "https://nowhere.test") == "（没有记录到材料）"


def test_documentation_snippets_are_evidence_for_their_page():
    """查文档查到的每段按它的出处记成那个网址的材料，裁判按它核对引用。"""
    docs = ToolMessage(content="### T\n\nSource: https://docs.test/a\nSource id: s1\n\nBody.", name="query-docs", tool_call_id="c1",
                       artifact={"snippets": [{"id": "s1", "url": "https://docs.test/a", "title": "Hooks", "content": "Use before_model."},
                                              {"id": "s1", "url": "https://docs.test/a", "title": "More", "content": "Return None."}]})
    m = material([AIMessage(content="", tool_calls=[{"name": "query-docs", "args": {"libraryId": "/x/y", "query": "q"}, "id": "c1"}]), docs])
    assert text_for(m["evidence"], "https://docs.test/a") == "Hooks\nUse before_model.\n\nMore\nReturn None."
    assert m["searches"] == [] and m["reads"] == []


def test_subagent_searches_count_as_searched():
    """主 agent 只派了子任务：子 agent 的搜索、读页也算这一轮的材料。"""
    r = _run([{"say": "子任务查一下向量库", "expect": {"searched": True, "min_reads": 1}}])
    assert ("searched", True) in [(c["name"], c["passed"]) for c in r["turns"][0]["checks"]]
    assert ("min_reads", True) in [(c["name"], c["passed"]) for c in r["turns"][0]["checks"]]
    assert r["turns"][0]["lookups"] == ["task"]


def test_citation_outside_brackets_fails_cite_format():
    played = {"status": "idle", "outcome": "ok", "stream_ids_match": True, "searches": [], "reads": [],
              "reply": "定价 2 美元 [s3ae980]。[s3ae980 之外，见 sa63a73] 另有说法 [sa63a73][s1b2c3d]",
              "sources": {"s3ae980": "https://a", "sa63a73": "https://b", "s1b2c3d": "https://c"}}
    checks = {c["name"]: c for c in turn_checks(CaseTurn(say="x"), played)}
    assert not checks["cite_format"]["passed"] and checks["cite_format"]["detail"] == "没按 [id] 写的引用：s3ae980、sa63a73"
    played["reply"] = "定价 2 美元 [s3ae980]，另有说法 [sa63a73][s1b2c3d]。"
    assert {c["name"]: c for c in turn_checks(CaseTurn(say="x"), played)}["cite_format"]["passed"]


# ---- 工具录像、重新打分、汇总 ----


def _case(turns: list[dict], cid: str = "x") -> Case:
    return Case.model_validate({"id": cid, "source": "测试", "turns": turns})


def _broken_search():
    """调了就出错的搜索服务（进程内的 MCP）：回放时搜索对上了录像，就不该调到它。"""
    srv = FastMCP("broken-search")

    @srv.tool(name="search_web")
    async def search_web(query: str, count: int = 5) -> dict:
        raise RuntimeError("搜索服务坏了")

    return srv


def test_replay_gives_recorded_tool_results_without_calling_the_service(tmp_path):
    """录一遍：工具都真调、录下来；回放：同样的调用给录好的，搜索服务这时坏了也不影响，回答和录的时候一样。"""
    path = tmp_path / "x.json"
    case = _case([{"say": "查一下向量库选型", "expect": {"searched": True, "min_reads": 1}}, {"say": "直接答：为什么"}])
    rec = run_case(case, FakeResearchPlanner, FakeJudge(), tape=Tape(path, fresh=True))
    assert rec["passed"], (_failed(rec), rec.get("error"))
    assert rec["tape"]["hits"] == 0 and rec["tape"]["live"] > 0
    assert rec["turns"][0]["tape"] == rec["tape"] and rec["turns"][1]["tape"] == {"hits": 0, "live": 0}
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert len(saved["calls"]) == rec["tape"]["live"] and {c["tool"] for c in saved["calls"]} == {"search_web", "read_page"}

    again = run_case(case, lambda **kw: FakeResearchPlanner(search=_broken_search, **kw), FakeJudge(), tape=Tape(path))
    assert again["passed"], (_failed(again), again.get("error"))
    assert again["tape"] == {"hits": rec["tape"]["live"], "live": 0}
    assert [t["reply"] for t in again["turns"]] == [t["reply"] for t in rec["turns"]]
    assert again["turns"][0]["sources"] == rec["turns"][0]["sources"]
    assert json.loads(path.read_text(encoding="utf-8")) == saved  # 全对上了，录像不动


def test_replay_calls_live_when_not_recorded_and_adds_it(tmp_path):
    """换了说法、搜索词不一样：没对上的照常真调，补录进去；已经录的不动。"""
    path = tmp_path / "x.json"
    run_case(_case([{"say": "查一下向量库选型"}]), FakeResearchPlanner, FakeJudge(), tape=Tape(path, fresh=True))
    before = json.loads(path.read_text(encoding="utf-8"))
    r = run_case(_case([{"say": "查一下笔记软件"}]), FakeResearchPlanner, FakeJudge(), tape=Tape(path))
    assert r["tape"]["live"] > 0
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["recorded_at"] == before["recorded_at"] and after["calls"][:len(before["calls"])] == before["calls"]
    assert len(after["calls"]) == len(before["calls"]) + r["tape"]["live"]


def test_replay_moves_the_clock_to_when_it_was_recorded(tmp_path):
    """回放时这个用例的时钟挪到录的时候：取当前时间、这一轮的日期、裁判看的日期都是录的那天。"""
    path = tmp_path / "x.json"
    path.write_text(json.dumps({"recorded_at": "2026-01-01T02:00:00+00:00", "calls": []}), encoding="utf-8")
    made: list = []

    prompts: list[str] = []

    class Seen(BaseCallbackHandler):
        def on_chat_model_start(self, serialized, messages, **kw):
            prompts.append(messages[0][0].content)

    def make(**kw):
        made.append(FakeResearchPlanner(**kw))
        made[-1]._model.callbacks = [Seen()]
        return made[-1]

    judge = FakeJudge()
    r = run_case(_case([{"say": "现在几点 直接答", "judge": "说了几点"}]), make, judge, tape=Tape(path))
    assert r["turns"][0]["today"] == "2026-01-01" and judge.rubrics[0][3] == "2026-01-01"
    assert prompts and all("[Current date] 2026-01-01 (Thursday)" in p for p in prompts)  # 系统提示词里的日期
    assert json.loads(made[0]._tools[0].invoke({}))["now"].startswith("2026-01-01 10:0")  # 北京时间
    assert datetime.now(CHINA_TZ).date().isoformat() != "2026-01-01"


def test_tape_gives_repeated_calls_in_order_and_skips_errors(tmp_path):
    """同样的调用录了几次按顺序给，用完了一直给最后一次；出错的不录，回放时照常真调。"""
    from langchain_core.tools import StructuredTool

    answers = iter(["一", "二", RuntimeError("连不上"), "三"])

    async def live(query: str):
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return [{"type": "text", "text": a}], None

    def tool():
        return StructuredTool.from_function(coroutine=live, name="search_web", description="d", response_format="content_and_artifact")

    path = tmp_path / "t.json"
    rec = Tape(path, fresh=True)
    t = rec.wrap(tool())
    call = {"name": "search_web", "args": {"query": "q"}, "id": "c", "type": "tool_call"}
    assert [aio.run(t.ainvoke(call)).content[0]["text"] for _ in range(2)] == ["一", "二"]
    try:
        aio.run(t.ainvoke(call))
    except Exception:  # noqa: BLE001 —— 出错照常抛给中间件
        pass
    assert rec.counts() == {"hits": 0, "live": 2}
    rec.save()
    play = Tape(path)
    t = play.wrap(tool())
    assert [aio.run(t.ainvoke(call)).content[0]["text"] for _ in range(3)] == ["一", "二", "二"]
    assert play.counts() == {"hits": 3, "live": 0}


def test_rejudge_scores_saved_replies_with_the_current_case():
    """重新打分：agent 不重跑，按现在的 expect、judge 标准和裁判重新检查；对话改过了、旧结果文件没存材料的记为出错。"""
    case = _case([{"say": "查一下向量库选型", "expect": {"searched": True}}])
    r = run_case(case, FakeResearchPlanner, FakeJudge())
    assert r["passed"]
    stricter = _case([{"say": "查一下向量库选型", "expect": {"searched": False}, "judge": "给了推荐"}])
    judge = FakeJudge("unsupported")
    again = rejudge_case(json.loads(json.dumps(r)), stricter, judge, run=1)  # 像从结果文件里读回来的
    assert not again["passed"] and _failed(again) == [(1, "searched"), (1, "sources_supported")]
    assert again["usage"] == r["usage"] and again["rejudged_from"] == 1
    assert "重新打分（第 1 遍的回答）" in summary_line(again) and "调用" not in summary_line(again)
    assert judge.rubrics[0][3] == r["turns"][0]["today"] and len(judge.sources) == 2
    assert "对不上" in rejudge_case(r, _case([{"say": "别的话"}]), FakeJudge(), run=1)["error"]
    old = {**r, "turns": [{k: v for k, v in t.items() if k != "cited_sources"} for t in r["turns"]]}
    assert "以前跑的" in rejudge_case(old, case, FakeJudge(), run=1)["error"]


def test_stability_separates_flaky_from_always_failing():
    def res(run: int, judge: bool, searched: bool = False) -> dict:
        checks = [{"name": "finished", "passed": True}, {"name": "judge", "passed": judge}, {"name": "searched", "passed": searched}]
        return {"id": "a", "run": run, "passed": judge and searched, "turns": [{"checks": checks}]}

    text = stability([res(1, True), res(2, False), res(3, True), {"id": "b", "run": 1, "error": "x"}])
    assert "用例通过：a 0/3" in text and "出错（不算进检查）：b 1 遍" in text
    assert "会翻的检查 1 项：\n  a 第1轮 judge：2/3" in text and "一直不过的 1 项：\n  a 第1轮 searched：0/3" in text
    assert text.endswith("一直过的 1 项")


def test_citation_judge_gets_the_same_dates_as_the_agent():
    # 正文里“明天（11月6日，星期五）”是助理按日期推的：核对引用的裁判也要知道今天、明天是哪天（2026-11-05 决策点回放查出来的）
    from datetime import date

    from app.evals.research import PROMPTS
    from app.graph.research.agent import date_anchors

    text = PROMPTS.render("citations.user", question="北京明天限行尾号", reply="明天（11月6日，星期五）限行 4 和 9 [s1]。",
                          sources=[{"id": "s1", "url": "https://example.test", "text": "星期五限行尾号为4和9"}], **date_anchors(date(2026, 11, 5)))
    assert "今天是 2026-11-05 (Thursday)" in text and "明天是 2026-11-06 (Friday)" in text
    judge = FakeJudge()
    r = _run([{"say": "查一下向量库选型"}], judge)
    assert judge.today == r["turns"][0]["today"]
