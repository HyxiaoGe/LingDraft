"""决策点回放的离线部分：录的时候存下每次调模型之前的检查点，从那儿只重跑这一下、工具不执行，检查和比较在假模型上跑得通。"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage

from app import aio
from app.api.research_runner import ResearchRunner
from app.evals.decisions import Decision, afork, compare, fisher_p, load_decisions, pick, sample, snapshot_path, summary
from app.evals.research import Case, run_case
from app.evals.tape import Tape
from app.graph.research.graph import memory_checkpointer
from app.research_llm import FakeResearchPlanner
from tests.test_research_eval import FakeJudge


def _record(tmp_path, turns: list[dict]) -> list[dict]:
    """假模型录一遍（重新录），返回存下的检查点。"""
    case = Case.model_validate({"id": "x", "source": "测试", "turns": turns})
    tape = Tape(tmp_path / "x.json", fresh=True)
    r = run_case(case, FakeResearchPlanner, FakeJudge(), tape=tape)
    assert r["passed"], r
    return json.loads(snapshot_path(tape.path).read_text(encoding="utf-8"))["snapshots"]


class CountingTools:
    """数一数 MCP 工具真执行了几次。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def wrap(self, tool):
        live = tool.coroutine

        async def call(**args):
            self.calls.append(tool.name)
            return await live(**args)

        tool.coroutine = call
        return tool


def _runner(counter: CountingTools | None = None) -> ResearchRunner:
    return ResearchRunner(planner=FakeResearchPlanner(wrap_tool=counter.wrap if counter else None), checkpointer=memory_checkpointer())


def _decision(**kw) -> Decision:
    return Decision.model_validate({"id": "d", "case": "x", "source": "测试", **kw})


def test_decisions_file_parses():
    assert load_decisions()


def test_record_saves_the_state_before_each_model_call(tmp_path):
    # 假模型：天气 → 搜索 → 读页 → 写正文，一轮调 4 次模型；第二轮直接答，调 1 次
    snaps = _record(tmp_path, [{"say": "广州天气怎么样"}, {"say": "直接答：为什么"}])
    assert [(s["turn"], s["call"]) for s in snaps] == [(1, 1), (1, 2), (1, 3), (1, 4), (2, 1)]


def test_replay_without_record_saves_no_snapshots(tmp_path):
    _record(tmp_path, [{"say": "广州天气怎么样"}])
    path = snapshot_path(tmp_path / "x.json")
    path.unlink()
    case = Case.model_validate({"id": "x", "source": "测试", "turns": [{"say": "广州天气怎么样"}]})
    run_case(case, FakeResearchPlanner, FakeJudge(), tape=Tape(tmp_path / "x.json"))
    assert not path.exists()


def test_fork_reruns_only_that_model_call_and_runs_no_tools(tmp_path):
    snaps = _record(tmp_path, [{"say": "广州天气怎么样"}])
    counter = CountingTools()
    runner = _runner(counter)
    state, msgs = aio.run(afork(runner.graph, pick(snaps, _decision(call=1)), "t1"))
    # 停在执行工具之前：只多了这一下的模型消息，带着工具调用，工具一个都没执行
    assert len(msgs) == len(state["messages"]) + 1
    assert isinstance(msgs[-1], AIMessage)
    assert [tc["name"] for tc in msgs[-1].tool_calls] == ["get_weather"]
    assert counter.calls == []
    # 是重新调的模型，不是拿录的那条顶上：录的那遍这一下的消息在下一个检查点里，id 不一样
    later, _ = aio.run(afork(_runner().graph, pick(snaps, _decision(call=2)), "t2"))
    assert later["messages"][1].tool_calls[0]["name"] == "get_weather"
    assert later["messages"][1].id != msgs[-1].id


def test_fork_can_switch_the_model(tmp_path):
    """--model：这一下换成别的模型（这一轮的记录换掉），不给就照录的那遍用的。"""
    from app import model_catalog

    snaps = _record(tmp_path, [{"say": "广州天气怎么样"}])
    plan = model_catalog.load().plan("doubao-seed-2-1-pro", False, lambda ref: True)
    runner = _runner()
    runner.planner.model_uses.clear()
    aio.run(afork(runner.graph, pick(snaps, _decision(call=1)), "t1", plan))
    assert runner.planner.model_uses == [(plan.agent, False)]
    runner.planner.model_uses.clear()
    aio.run(afork(runner.graph, pick(snaps, _decision(call=1)), "t2"))
    auto = model_catalog.load().plan(None, None, lambda ref: True).agent.ref  # 录的那遍是 Auto（免费的 Pro 还能用时先走它）
    assert [u.ref for u, _ in runner.planner.model_uses] == [auto]


def test_sample_checks_the_tool_decision(tmp_path):
    snaps = _record(tmp_path, [{"say": "广州天气怎么样"}])
    runner = _runner()
    ok = sample(runner, FakeJudge(), _decision(call=1, expect={"looked_up": True, "replied": False}), pick(snaps, _decision(call=1)), 1)
    assert [(c["name"], c["passed"]) for c in ok["checks"]] == [("looked_up", True), ("replied", True)]
    assert ok["tool_calls"][0]["name"] == "get_weather" and not ok["reply"] and ok["say"] == "广州天气怎么样"
    bad = sample(runner, FakeJudge(), _decision(call=1, expect={"looked_up": False}), pick(snaps, _decision(call=1)), 2)
    assert [(c["name"], c["passed"]) for c in bad["checks"]] == [("looked_up", False)]


def test_sample_of_the_last_call_scores_the_reply_against_the_recorded_material(tmp_path):
    snaps = _record(tmp_path, [{"say": "广州天气怎么样"}])
    judge = FakeJudge()
    d = _decision(call="last", expect={"replied": True}, judge="给了结论")
    r = sample(_runner(), judge, d, pick(snaps, d), 1)
    assert r["reply"].startswith("结论") and not r["tool_calls"]
    assert [c["name"] for c in r["checks"]] == ["replied", "cited_known", "judge", "sources_supported"]
    assert all(c["passed"] for c in r["checks"]), r["checks"]
    # 裁判看到的是录的那遍读到的材料；日期是这一轮开始的那天
    assert judge.sources and all(s["text"] != "（没有记录到材料）" for s in judge.sources)
    assert judge.rubrics[0][0] == "广州天气怎么样" and judge.rubrics[0][3] == r["today"]


def test_recording_through_a_question_keeps_the_call_after_the_answer(tmp_path):
    """录的那遍里模型问了用户（用例的 answers 回答）：回答之后那次调模型之前的状态也存下了，能从那儿重跑“回答以后怎么做”这一下。"""
    snaps = _record(tmp_path, [{"say": "问我 对比两种向量库", "work": True, "answers": ["选型"], "expect": {"asked": True}}])
    assert [(s["turn"], s["call"]) for s in snaps] == [(1, 1), (1, 2)]
    d = _decision(call=2, expect={"asked": False, "replied": True})
    r = sample(_runner(), FakeJudge(), d, pick(snaps, d), 1)
    assert r["reply"] == "收到回答：选型。" and all(c["passed"] for c in r["checks"]), r


def test_decision_from_a_first_message_needs_no_recording():
    """只写一句话的决策点：这句话开一段新对话，只调第一次模型，工具（包括问用户）都不执行。"""
    counter = CountingTools()
    runner = _runner(counter)
    d = Decision.model_validate({"id": "d", "say": "问我 对比两种向量库", "work": True, "source": "测试",
                                 "expect": {"asked": True, "replied": False}})
    r = sample(runner, FakeJudge(), d, None, 1)
    assert [(c["name"], c["passed"]) for c in r["checks"]] == [("replied", True), ("asked", True)], r
    assert r["say"] == "问我 对比两种向量库" and [tc["name"] for tc in r["tool_calls"]] == ["ask_user"]
    assert r["checks"][1]["detail"] == "这份对比是用来选型，还是做分享？"
    # 停在执行工具之前：没有真停下来问（线程不在等回答），只有这句话和模型这一下
    view = runner.state_view("fork-d-1")
    assert view["ask"] is None and counter.calls == []
    assert len(runner.graph.get_state(runner._cfg("fork-d-1")).values["messages"]) == 2
    quick = Decision.model_validate({"id": "q", "say": "直接答：为什么", "source": "测试", "expect": {"asked": False, "replied": True}})
    r = sample(runner, FakeJudge(), quick, None, 1)
    assert all(c["passed"] for c in r["checks"]) and r["reply"] == "直接回答：这个问题不用查。"
    searched = Decision.model_validate({"id": "s", "say": "查一下向量库选型", "source": "测试", "expect": {"asked": True}})
    r = sample(runner, FakeJudge(), searched, None, 1)
    assert not r["checks"][0]["passed"] and r["checks"][0]["detail"] == "没问，调了 search_web" and counter.calls == []


def test_first_message_decision_uses_the_given_model():
    from app import model_catalog

    plan = model_catalog.load().plan("doubao-seed-2-1-pro", None, lambda ref: True)  # 思考开关按选项的默认（main 也是这样定的）
    runner = _runner()
    runner.planner.model_uses.clear()
    d = Decision.model_validate({"id": "d", "say": "直接答：为什么", "source": "测试"})
    assert "error" not in (r := sample(runner, FakeJudge(), d, None, 1, plan=plan)), r
    assert runner.planner.model_uses == [(plan.agent, False)]  # 对话定成这个选项：不另外判断用哪个模型
    # --no-thinking：这句话关着思考发（页面上关掉深度思考的样子）；不给的按选项的默认，Flash 默认开
    flash = model_catalog.load().plan("deepseek-flash", None, lambda ref: True)
    for n, (thinking, on) in enumerate([(None, True), (False, False)], 2):
        runner.planner.model_uses.clear()
        assert "error" not in (r := sample(runner, FakeJudge(), d, None, n, plan=flash, thinking=thinking)), r
        assert [u.thinking for u, _ in runner.planner.model_uses] == [on]


def test_decision_is_from_a_case_or_a_first_message_not_both():
    with pytest.raises(ValueError, match="case 和 say 写一个"):
        Decision.model_validate({"id": "d", "source": "测试"})
    with pytest.raises(ValueError, match="case 和 say 写一个"):
        _decision(say="你好")
    with pytest.raises(ValueError, match="不写 turn、call"):
        Decision.model_validate({"id": "d", "say": "你好", "call": 2, "source": "测试"})
    with pytest.raises(ValueError, match="work 只给只写一句话的"):
        _decision(work=True)


def test_pick_says_what_was_recorded(tmp_path):
    snaps = _record(tmp_path, [{"say": "广州天气怎么样"}])
    assert pick(snaps, _decision(call="last"))["call"] == 4
    with pytest.raises(ValueError, match="只调了 4 次"):
        pick(snaps, _decision(call=9))
    with pytest.raises(ValueError, match="没有第 2 轮"):
        pick(snaps, _decision(turn=2))


def _results(did: str, passed: list[bool]) -> list[dict]:
    return [{"decision": did, "sample": i + 1, "checks": [{"name": "looked_up", "passed": p, "detail": ""}],
             "usage": {"input": 16000, "cached": 15000, "output": 300}} for i, p in enumerate(passed)]


def test_compare_needs_a_big_enough_difference():
    assert fisher_p(2, 10, 8, 10) < 0.05
    assert fisher_p(4, 10, 6, 10) > 0.5
    assert fisher_p(5, 10, 5, 10) == pytest.approx(1)
    a = _results("d", [True] * 2 + [False] * 8)
    assert "2/10 → 8/10" in compare(a, _results("d", [True] * 8 + [False] * 2)) and "变了" in compare(a, _results("d", [True] * 8 + [False] * 2))
    assert "看不出变化" in compare(a, _results("d", [True] * 4 + [False] * 6))
    assert "looked_up 2/10" in summary(a, ["d"])
