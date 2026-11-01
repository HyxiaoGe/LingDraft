"""子 agent：主 agent 用 task 派活，子 agent 在自己的上下文里搜、读，交回报告。只用假模型。

假模型的路径（见 FakeToolModel）：用户的话里有“子任务”派一个，“分头”派两个，“派N个”派 N 个；子 agent 照常搜 → 读 → 回复。
"""

from __future__ import annotations

import threading
import time

import pytest
from langchain_core.messages import AIMessageChunk, BaseMessage, HumanMessage, ToolMessage

from app.api.research_runner import StreamBuffer
from app.graph.research import history, prompts, subagent
from app.graph.research.graph import open_sqlite_checkpointer
from app.graph.research.sources import cited, subagent_messages
from app.research_llm import FakeResearchPlanner, FakeToolModel
from tests.test_research import Chat, _answered, _kinds

SUB_ITEMS = ["thinking", "step:search", "thinking", "step:read", "thinking", "text"]


def _tasks(turn: dict) -> list[dict]:
    return [i for i in turn["items"] if i["type"] == "step" and i["kind"] == "task"]


def _task_results(c: Chat) -> list[ToolMessage]:
    return [m for m in c.values()["messages"] if isinstance(m, ToolMessage) and m.name == "task"]


def test_parallel_subagents_report_back_with_their_work():
    """一次派两个：每个子任务那一步带着它的子 agent 做过的事；来源并进这一轮，主回答引用的是子 agent 查到的；用量分开记。"""
    c = Chat()
    v = c.say("分头查一下杭州美食")
    (turn,) = v["turns"]
    assert _kinds(turn) == ["thinking", "step:task", "step:task", "thinking", "text"]
    a, b = _tasks(turn)
    assert a["ok"] and b["ok"] and a["text"] == "查分头查一下杭州美食1" and a["prompt"] == "查一查 分头查一下杭州美食 第1方面"
    for t in (a, b):
        assert _kinds(t) == SUB_ITEMS and t["items"][-1]["text"].startswith("结论：")
        assert t["source_ids"] and set(t["source_ids"]) <= v["sources"].keys()
    answer = turn["items"][-1]["text"]
    assert answer.startswith("汇总：") and cited(answer) and set(cited(answer)) <= v["sources"].keys()
    rec = turn["record"]
    assert rec["usage"]["subagents"] == 2 and rec["usage"]["searches"] == 0 and rec["usage"]["model_calls"] == 2
    assert rec["subagent_usage"]["model_calls"] == 6 and rec["subagent_usage"]["searches"] == 2 and rec["subagent_usage"]["reads"] == 2
    # 主 agent 的模型只看到报告：子 agent 的消息在 artifact 里
    for m in _task_results(c):
        assert history.text_of(m).startswith("结论：") and [type(x).__name__ for x in subagent_messages(m)][:2] == ["HumanMessage", "AIMessage"]


def test_subagent_stream_is_tagged_and_kept_out_of_the_answer():
    """子 agent 的片段、进度、阶段带着派它的 task 调用 id；主回答的片段里没有子 agent 的字；子 agent 的节点做完不发 node。"""
    c = Chat()
    c.say("分头查一下杭州美食")
    calls = {t["id"] for t in _tasks(c.view()["turns"][0])}
    sub = [e for e in c.events if e["event"] in ("delta", "step", "phase") and "task" in e["data"]]
    assert {e["data"]["task"] for e in sub} == calls
    assert {e["data"]["step"]["kind"] for e in sub if e["event"] == "step"} == {"search", "read"}
    main_text = "".join(e["data"]["text"] for e in c.events if e["event"] == "delta" and e["data"]["kind"] == "text" and "task" not in e["data"])
    assert main_text == c.view()["turns"][0]["items"][-1]["text"]
    assert [e["data"]["node"] for e in c.events if e["event"] == "node"].count("tools") == 2  # 主 agent 的两个工具任务
    assert not any(e["event"] == "subagent" for e in c.events)  # 运行器自己用的，不发给页面


def test_subagents_run_in_parallel():
    """两个子 agent 同时在跑：第一次调模型时互相等一下，不是并行就会等不到。"""
    barrier = threading.Barrier(2, timeout=5)

    class Meet(FakeToolModel):
        def _message(self, messages: list[BaseMessage]):
            if len(messages) == 1:
                barrier.wait()
            return super()._message(messages)

    p = FakeResearchPlanner()
    p._sub_model = Meet(prefix="fake-sub")
    v = Chat(planner=p).say("分头查一下杭州美食")
    assert [t["ok"] for t in _tasks(v["turns"][0])] == [True, True]


def test_at_most_three_subagents_run_at_once(monkeypatch):
    """一次派 5 个：同时最多跑 3 个，另外两个排队，卡片上说在排队；5 个都做完。"""
    real = subagent.build_subagent
    seen, mu = {"now": 0, "max": 0}, threading.Lock()

    class Counted:
        def __init__(self, worker) -> None:
            self.worker = worker

        def invoke(self, *a, **kw):
            with mu:
                seen["now"] += 1
                seen["max"] = max(seen["max"], seen["now"])
            try:
                time.sleep(0.1)
                return self.worker.invoke(*a, **kw)
            finally:
                with mu:
                    seen["now"] -= 1

    monkeypatch.setattr(subagent, "build_subagent", lambda planner: Counted(real(planner)))
    c = Chat()
    v = c.say("派5个查一下杭州美食")
    assert [t["ok"] for t in _tasks(v["turns"][0])] == [True] * 5
    assert seen["max"] == subagent.MAX_RUNNING == 3
    queued = {e["data"]["task"] for e in c.events if e["event"] == "phase" and e["data"]["phase"] == "queued"}
    assert len(queued) == 2


def test_more_than_five_in_a_turn_are_refused():
    v = Chat().say("派6个查一下杭州美食")
    (turn,) = v["turns"]
    tasks = _tasks(turn)
    assert [t["ok"] for t in tasks] == [True] * 5 + [False]
    assert tasks[-1]["message"] == history.TASK_FAILURES["limit"] and tasks[-1]["items"] == []
    assert turn["record"]["usage"]["subagents"] == 5


def test_last_model_call_writes_the_report_without_tools(monkeypatch):
    """子 agent 调模型到了上限：最后一次不许调工具，用查到的写报告。"""
    monkeypatch.setattr(subagent, "MAX_MODEL_CALLS", 2)
    c = Chat()
    v = c.say("子任务查一下杭州美食")
    (task,) = _tasks(v["turns"][0])
    assert task["ok"] and _kinds(task) == ["thinking", "step:search", "thinking", "text"]
    assert task["items"][-1]["text"].startswith("不能再查了")
    sub = subagent_messages(_task_results(c)[0])
    assert sum(isinstance(m, HumanMessage) for m in sub) == 1  # 最后那条说明只在请求里，不存进记录


def test_stop_inside_a_subagent_keeps_what_it_did():
    """子 agent 搜完一次就点停止：两个子 agent 都停下，子任务显示停止了，卡片里留着它们已经做的搜索，来源也在；下一轮能接着说。"""
    c = Chat()
    v = c.say("分头查一下杭州美食", stop_on=lambda e: e["event"] == "step" and "task" in e["data"] and not e["data"]["step"]["pending"])
    (turn,) = v["turns"]
    assert turn["record"]["outcome"] == "stopped" and v["status"] == "idle"
    tasks = _tasks(turn)
    assert [t["ok"] for t in tasks] == [False, False] and {t["message"] for t in tasks} == {"停止了，没有做完"}
    # 停止在两个线程里先后生效：另一个可能已经接着读了一页，做到哪儿算哪儿
    for t in tasks:
        assert any(s["type"] == "step" and s["kind"] == "search" and s["ok"] for s in t["items"])
    done = [s for t in tasks for s in t["items"] if s["type"] == "step" and s["ok"]]
    assert {sid for s in done for sid in s["source_ids"]} <= v["sources"].keys()
    assert _answered(c.values()["messages"])
    assert c.say("直接答：好")["turns"][-1]["record"]["outcome"] == "ok"


def test_retry_continues_the_subagent_where_it_failed():
    """子 agent 搜完以后调模型出错，这一轮失败；重试时它接着自己的检查点跑：不重搜，任务说明也不会多一份。"""
    armed = [True]

    class Flaky(FakeToolModel):
        def _message(self, messages: list[BaseMessage]):
            if armed[0] and isinstance(messages[-1], ToolMessage):
                armed[0] = False
                raise RuntimeError("假模型：网络断了")
            return super()._message(messages)

    p = FakeResearchPlanner()
    p._sub_model = Flaky(prefix="fake-sub")
    c = Chat(planner=p)
    with pytest.raises(RuntimeError):
        c.say("子任务查一下杭州美食")
    assert c.view()["status"] == "failed"
    c.r.retry_input(c.tid)
    v = c.run()
    (task,) = _tasks(v["turns"][0])
    assert task["ok"] and _kinds(task) == SUB_ITEMS
    (result,) = _task_results(c)
    assert sum(isinstance(m, HumanMessage) for m in subagent_messages(result)) == 1


def test_moderation_stop_in_a_subagent_is_reported_and_the_turn_goes_on():
    """子 agent 查到的资料没通过内容审核、它停下了：子任务显示没做成，主 agent 照常回答。"""
    v = Chat().say("子任务查完就拦")
    (turn,) = v["turns"]
    (task,) = _tasks(turn)
    assert not task["ok"] and task["message"] == history.TASK_FAILURES["blocked"]
    assert task["items"][-1]["type"] == "blocked"
    assert turn["record"]["outcome"] == "ok" and turn["items"][-1]["text"] == "汇总：子任务没查到。"


def test_regenerate_drops_the_old_subagent_work():
    c = Chat()
    c.say("子任务查一下杭州美食")
    v = c.regenerate()
    assert len(_tasks(v["turns"][0])) == 1 and len(_task_results(c)) == 1


def test_sqlite_round_trip_keeps_the_subagent_card(tmp_path):
    db = tmp_path / "data" / "r.sqlite"
    before = Chat(saver=open_sqlite_checkpointer(db))
    before.say("分头查一下杭州美食")
    after = Chat(saver=open_sqlite_checkpointer(db))  # 像服务重启
    assert after.view()["turns"] == before.view()["turns"] and after.view()["sources"] == before.view()["sources"]


def test_stream_buffer_keeps_subagents_apart():
    """并行的子 agent 的片段交错着来：按（子任务，消息，种类）合并；停止时各自的半截分开记。"""
    buf = StreamBuffer()
    buf.add(AIMessageChunk(content="主", id="m"))
    buf.add(AIMessageChunk(content="", id="x", additional_kwargs={"reasoning_content": "甲想"}), "A")
    buf.add(AIMessageChunk(content="", id="x", additional_kwargs={"reasoning_content": "乙想"}), "B")
    buf.add(AIMessageChunk(content="", id="x", additional_kwargs={"reasoning_content": "再想"}), "A")
    buf.add(AIMessageChunk(content="甲写", id="x"), "A")
    deltas = [e["data"] for e in buf.flush()]
    assert deltas == [{"id": "m", "kind": "text", "text": "主"}, {"id": "x", "kind": "thinking", "text": "甲想再想", "task": "A"},
                      {"id": "x", "kind": "thinking", "text": "乙想", "task": "B"}, {"id": "x", "kind": "text", "text": "甲写", "task": "A"}]
    main, a, b = buf.partial(), buf.partial("A"), buf.partial("B")
    assert main and a and b and main.content == "主" and a.content == "甲写" and b.additional_kwargs == {"reasoning_content": "乙想"}
    assert buf.partial("C") is None


def test_task_prompts_render():
    assert "5" in prompts.render("tools.task.description", max_per_turn=subagent.MAX_PER_TURN)
    text = subagent.system_prompt(subagent.datetime(2026, 11, 1, tzinfo=subagent.CHINA_TZ), subagent.SYSTEM_PROMPT,
                                  max_calls=subagent.MAX_MODEL_CALLS)
    assert text.startswith("[Your role]") and "2026-11-01 (Sunday)" in text and "[Your report]" in text
    assert f"after {subagent.MAX_MODEL_CALLS} responses in all" in text
