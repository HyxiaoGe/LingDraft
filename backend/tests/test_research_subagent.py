"""子 agent：主 agent 用 task 派活，子 agent 在自己的上下文里搜、读，交回报告。只用假模型。

假模型的路径（见 FakeToolModel）：用户的话里有“子任务”派一个，“分头”派两个，“派N个”派 N 个；子 agent 照常搜 → 读 → 回复。
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest
from langchain_core.messages import AIMessageChunk, BaseMessage, HumanMessage, ToolMessage

from app import aio
from app.api.research_runner import StreamBuffer
from app.graph.research import history, prompts, subagent
from app.graph.research.graph import open_sqlite_checkpointer
from app.graph.research.sources import EXCERPT_CHARS, cited, subagent_messages
from app.research_llm import FakeResearchPlanner, FakeToolModel
from tests.test_research import Chat, _answered, _kinds

SUB_ITEMS = ["thinking", "step:search", "thinking", "step:read", "thinking", "text"]


def _tasks(turn: dict) -> list[dict]:
    return [i for i in turn["items"] if i["type"] == "step" and i["kind"] == "task"]


def _task_results(c: Chat) -> list[ToolMessage]:
    return [m for m in c.values()["messages"] if isinstance(m, ToolMessage) and m.name == "task"]


def _handed_over(m: ToolMessage) -> list[dict] | None:
    """停下的子任务补上的结果里列给主 agent 的来源；没列的（还没查到东西）是 None。"""
    text, head = history.text_of(m), prompts.render("tools.task.stopped", sources="").strip()
    return json.loads(text[len(head):]) if text.startswith(head) else None


def _first_sub_step_done(e: dict) -> bool:
    return e["event"] == "step" and "task" in e["data"] and not e["data"]["step"]["pending"]


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


def test_subagent_steps_carry_their_sources():
    """执行中页面从事件拿来源：子 agent 的搜索、读页做完带着来源，主 agent 那边子任务那一步做完带着子 agent 查到的全部来源。"""
    c = Chat()
    c.say("分头查一下杭州美食")
    v = c.view()
    sub = [e["data"] for e in c.events if e["event"] == "step" and "task" in e["data"] and not e["data"]["step"]["pending"]]
    assert sub and all(d["sources"] and list(d["sources"]) == d["step"]["source_ids"] for d in sub)
    tasks = [e["data"] for e in c.events if e["event"] == "step" and "task" not in e["data"]
             and e["data"]["step"]["kind"] == "task" and not e["data"]["step"]["pending"]]
    assert len(tasks) == 2
    for t in tasks:
        assert t["sources"] and all(src == v["sources"][sid] for sid, src in t["sources"].items())
    assert {sid for t in tasks for sid in t["sources"]} == {sid for d in sub for sid in d["sources"]}


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

        async def ainvoke(self, *a, **kw):
            with mu:
                seen["now"] += 1
                seen["max"] = max(seen["max"], seen["now"])
            try:
                await asyncio.sleep(0.1)
                return await self.worker.ainvoke(*a, **kw)
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


def test_budget_blocks_extra_calls_and_reminds_each_step(monkeypatch):
    """搜索预算 3，模型不管预算、每次并行搜两个：第二次只执行一个，另一个不执行，模型收到“不要再调”。
    每次工具结果回来，请求末尾提醒用了几次、还剩几次；提醒说用完了，它就写报告。
    没执行的那个页面上不列、用量不算；提醒只在请求里，不存进记录。"""
    monkeypatch.setattr(subagent, "TOOL_BUDGET", {"search_web": 3, "read_page": 2})
    notes: list[str] = []
    decide = FakeToolModel._decide

    def spy(self, messages):
        if str(messages[-1].content).startswith("<budget>"):
            notes.append(str(messages[-1].content))
        return decide(self, messages)

    monkeypatch.setattr(FakeToolModel, "_decide", spy)
    c = Chat()
    v = c.say("子任务一直搜杭州美食")
    (task,) = _tasks(v["turns"][0])
    assert task["ok"] and _kinds(task) == ["thinking", "step:search", "step:search", "thinking", "step:search", "thinking", "text"]
    assert task["items"][-1]["text"].startswith("预算用完了")
    assert [n.splitlines()[1:3] for n in notes] == [
        ["search_web Budget Used: 2, search_web Budget Remaining: 1", "read_page Budget Used: 0, read_page Budget Remaining: 2"],
        ["search_web Budget Used: 3, search_web Budget Remaining: 0", "read_page Budget Used: 0, read_page Budget Remaining: 2"]]
    sub = subagent_messages(_task_results(c)[0])
    blocked = [m for m in sub if isinstance(m, ToolMessage) and history.not_run(m)]
    assert len(blocked) == 1 and blocked[0].status == "error" and "Do not call 'search_web' again" in history.text_of(blocked[0])
    assert sum(isinstance(m, HumanMessage) for m in sub) == 1
    assert v["turns"][0]["record"]["subagent_usage"]["searches"] == 3


def test_stop_inside_a_subagent_keeps_what_it_did():
    """子 agent 搜完一次就点停止：两个子 agent 都马上停下，子任务显示停止了，卡片里留着已经做完的搜索，来源也在；
    还没做完的那一步显示成停止了，不会一直在转；下一轮能接着说。"""
    c = Chat()
    v = c.say("分头查一下杭州美食", stop_on=_first_sub_step_done)
    (turn,) = v["turns"]
    assert turn["record"]["outcome"] == "stopped" and v["status"] == "idle"
    tasks = _tasks(turn)
    assert [t["ok"] for t in tasks] == [False, False] and {t["message"] for t in tasks} == {"停止了，没有做完"}
    # 另一个子 agent 的搜索可能还没回来：那一步停了，做到哪儿算哪儿
    steps = [s for t in tasks for s in t["items"] if s["type"] == "step"]
    assert all(t["items"] for t in tasks) and not any(s["pending"] for s in steps)
    assert any(s["kind"] == "search" and s["ok"] for s in steps)
    assert all(s["ok"] or s["message"] == "停止了，没有做完" for s in steps)
    done = [s for s in steps if s["ok"]]
    assert {sid for s in done for sid in s["source_ids"]} <= v["sources"].keys()
    assert _answered(c.values()["messages"])
    assert c.say("直接答：好")["turns"][-1]["record"]["outcome"] == "ok"


def test_stopped_subagent_hands_what_it_found_to_the_main_agent():
    """两个子 agent 都搜完一次就点停止：补上的结果里给主 agent 列出它已经查到的来源，和子任务卡上的一样，只有来源卡上的那些；
    下一轮主 agent 的模型看得到。页面收到搜索做完马上就点：那一步可能还没存进子 agent 的检查点，也要算上。"""
    seen: list[list[BaseMessage]] = []
    searched: set[str] = set()

    def both_searched(e: dict) -> bool:
        if _first_sub_step_done(e):
            searched.add(e["data"]["task"])
        return len(searched) == 2

    class Spy(FakeToolModel):
        def _message(self, messages: list[BaseMessage]):
            seen.append(list(messages))
            return super()._message(messages)

    p = FakeResearchPlanner()
    p._model = Spy()
    c = Chat(planner=p)
    v = c.say("分头查一下杭州美食", stop_on=both_searched)
    results = {m.tool_call_id: m for m in _task_results(c)}
    assert len(results) == 2
    for t in _tasks(v["turns"][0]):
        found = _handed_over(results[t["id"]])
        on_card = {sid for s in t["items"] if s["type"] == "step" and s["ok"] for sid in s["source_ids"]}
        assert found and {s["id"] for s in found} == on_card
        for s in found:
            assert set(s) <= {"id", "title", "url", "published_at", "excerpt_from", "excerpt"}
            assert s["url"] == v["sources"][s["id"]]["url"] and 0 < len(s["excerpt"]) <= EXCERPT_CHARS
    seen.clear()
    c.say("直接答：好")
    sent = {m.tool_call_id: history.text_of(m) for m in seen[0] if isinstance(m, ToolMessage) and m.name == "task"}
    assert sent == {k: history.text_of(m) for k, m in results.items()}


def test_subagent_stopped_before_it_found_anything_says_only_that_it_stopped():
    """子 agent 还在等第一次模型回复时点停止：还没查到东西，补上的结果只说停止了、没做完。"""
    p = FakeResearchPlanner()
    gate = threading.Event()
    p._sub_model.gates.append(gate)

    def stop_on(e: dict) -> bool:
        if e["event"] == "phase" and "task" in e["data"]:
            threading.Timer(0.3, gate.set).start()  # 停止生效之后才放子 agent 的模型回复
            return True
        return False

    c = Chat(planner=p)
    v = c.say("分头查一下杭州美食", stop_on=stop_on)
    assert v["turns"][0]["record"]["outcome"] == "stopped"
    results = _task_results(c)
    assert len(results) == 2 and {history.text_of(m) for m in results} == {prompts.render("agent.stopped_call")}


def test_retry_continues_the_subagent_where_it_failed():
    """子 agent 搜完以后调模型出错，这一轮失败；重试时它接着自己的检查点跑：不重搜，任务说明也不会多一份，预算接着算。"""
    armed, notes = [True], []

    class Flaky(FakeToolModel):
        def _message(self, messages: list[BaseMessage]):
            if str(messages[-1].content).startswith("<budget>"):
                notes.append(str(messages[-1].content).splitlines()[1])
            if armed[0] and any(isinstance(m, ToolMessage) for m in messages):
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
    budget = subagent.TOOL_BUDGET["search_web"]
    assert notes[:2] == [f"search_web Budget Used: 1, search_web Budget Remaining: {budget - 1}"] * 2  # 出错那次、重试那次


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


def test_edit_and_resend_drops_the_old_subagent_work_and_its_sources():
    """改了最后那句话再问：旧的子任务卡、结果、用量和它们查到的来源都去掉，子 agent 按改过的话重新查。"""
    c = Chat()
    old = {s["url"] for s in c.say("子任务查一下杭州美食")["sources"].values()}
    c.r.regenerate_input(c.tid, "子任务查一下成都美食")
    v = c.run()
    (turn,) = v["turns"]
    (task,) = _tasks(turn)
    assert task["ok"] and task["text"] == "查子任务查一下成都美食1" and _kinds(task) == SUB_ITEMS
    assert len(_task_results(c)) == 1
    assert turn["record"]["usage"]["subagents"] == 1 and turn["record"]["subagent_usage"]["searches"] == 1
    new = {s["url"] for s in v["sources"].values()}
    assert old and new and not old & new and all("成都" in u for u in new)
    assert set(task["source_ids"]) <= v["sources"].keys()


def test_regenerate_after_stopping_a_subagent_starts_it_over():
    """子 agent 做到一半点了停止，再重新回答：重新派的子 agent 从头查，不接着停下的那个的检查点；停下的卡不留，用量只算这一次。"""
    c = Chat()
    c.say("分头查一下杭州美食", stop_on=_first_sub_step_done)
    v = c.regenerate()
    (turn,) = v["turns"]
    assert turn["record"]["outcome"] == "ok" and v["status"] == "idle"
    tasks = _tasks(turn)
    assert [t["ok"] for t in tasks] == [True, True] and all(_kinds(t) == SUB_ITEMS for t in tasks)
    assert len(_task_results(c)) == 2
    searched = [e for e in c.events if e["event"] == "step" and "task" in e["data"]
                and e["data"]["step"]["kind"] == "search" and not e["data"]["step"]["pending"]]
    assert {e["data"]["task"] for e in searched} == {t["id"] for t in tasks} and len(searched) == 2  # 这一次两个都重新搜了
    assert turn["record"]["subagent_usage"]["searches"] == 2
    assert _answered(c.values()["messages"])


def test_sqlite_round_trip_keeps_the_subagent_card(tmp_path):
    db = tmp_path / "data" / "r.sqlite"
    before = Chat(saver=aio.run(open_sqlite_checkpointer(db)))
    before.say("分头查一下杭州美食")
    after = Chat(saver=aio.run(open_sqlite_checkpointer(db)))  # 像服务重启
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
    ms = deltas[1].pop("ms")  # 甲想完了：用时记在甲思考那一段上；乙还在想，没有
    assert isinstance(ms, int) and ms >= 0
    assert deltas == [{"id": "m", "kind": "text", "text": "主"}, {"id": "x", "kind": "thinking", "text": "甲想再想", "task": "A"},
                      {"id": "x", "kind": "thinking", "text": "乙想", "task": "B"}, {"id": "x", "kind": "text", "text": "甲写", "task": "A"}]
    main, a, b = buf.partial(), buf.partial("A"), buf.partial("B")
    assert main and a and b and main.content == "主" and a.content == "甲写" and b.additional_kwargs == {"reasoning_content": "乙想"}
    assert buf.partial("C") is None


def test_task_prompts_render():
    assert "5" in prompts.render("tools.task.description", max_per_turn=subagent.MAX_PER_TURN)
    text = subagent.system_prompt(subagent.datetime(2026, 11, 1, tzinfo=subagent.CHINA_TZ), subagent.SYSTEM_PROMPT,
                                  search_budget=7, read_budget=4)
    assert text.startswith("[Your role]") and "2026-11-01 (Sunday)" in text and "[Your report]" in text
    assert "up to 7 search_web calls and up to 4 read_page calls" in text
    note = prompts.render("subagent.budget", tools=[("search_web", 3, 4), ("read_page", 0, 4)])
    assert note.splitlines() == ["<budget>", "search_web Budget Used: 3, search_web Budget Remaining: 4",
                                 "read_page Budget Used: 0, read_page Budget Remaining: 4", "Make the best use of the available resources.", "</budget>"]
