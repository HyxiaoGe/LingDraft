"""线程生命周期：并行执行、事件日志与断线续读、运行状态、失败后重试。只用假模型。"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import ToolMessage
from langgraph.constants import START

from app.api import main as api_main
from app.api import runner as runner_mod
from app.api.research_runner import ResearchRunner
from app.api.runner import LocalEventLog, LocalRunRegistry
from app.graph.research.answers import user_message
from tests.test_research_resume import (
    ASKS,
    SIMPLE,
    _answer,
    _asks,
    _events,
    _hold,
    _last,
    _new,
    _run_async,
    _state,
    _to_ask,
    _wait_until,
    after_tool,
    flaky_runner,
)


@pytest.fixture
def rr(tmp_path):
    return ResearchRunner(fake=True, data_dir=tmp_path / "data", out_dir=tmp_path / "out" / "research")


@pytest.fixture
def client(rr):
    api_main.set_research_runner(rr)
    return TestClient(api_main.app)


def _client(runner: ResearchRunner) -> TestClient:
    api_main.set_research_runner(runner)
    return TestClient(api_main.app)


def _block(chunk: str) -> tuple[str | None, str | None, dict | None]:
    """一个 SSE 块 → (id, event, data)；保活的注释行三项都是 None。"""
    eid = ev = data = None
    for line in chunk.strip().splitlines():
        if line.startswith("id: "):
            eid = line[4:]
        elif line.startswith("event: "):
            ev = line[7:]
        elif line.startswith("data: "):
            data = json.loads(line[6:])
    return eid, ev, data


async def _drain(stream) -> list[tuple]:
    return [b for b in [_block(c) async for c in stream] if b[1]]


def _seq(eid: str) -> int:
    return int(eid.rsplit("-", 1)[1])


def _logged(runner: ResearchRunner, tid: str) -> list[dict]:
    run = runner.events.current(runner._run_key(tid))
    return [ev for _, ev in runner.events.read(run, 0)[0]] if run else []


def _retry(http: TestClient, tid: str):
    return http.post(f"/api/research/threads/{tid}/retry")


def _say(http: TestClient, tid: str, text: str = "", **kw):
    return http.post(f"/api/research/threads/{tid}/messages", json={"text": text, **kw})


def _steps(st: dict, turn: int = -1) -> list[str]:
    return [i["kind"] for i in st["turns"][turn]["items"] if i["type"] == "step"]


# ---- 并行执行 ----


def test_research_threads_run_in_parallel(tmp_path):
    two = ResearchRunner(fake=True, data_dir=tmp_path / "data", out_dir=tmp_path / "out", workers=2)
    tids = ("par00001", "par00002")

    async def go():
        gate = _hold(two.planner, "title")
        streams = [two.start(t, SIMPLE) for t in tids]
        try:
            # 两个都开跑了（停在同一道闸门上），不是一个跑、一个排队
            assert _wait_until(lambda: all(two.state_view(t)["status"] == "running" for t in tids), 5)
        finally:
            gate.set()
        for s in streams:
            await _drain(s)
        assert all(two.state_view(t)["status"] == "idle" for t in tids)

    _run_async(go())


def test_failed_submit_gives_back_slots(tmp_path):
    one = ResearchRunner(fake=True, data_dir=tmp_path / "data", out_dir=tmp_path / "out", workers=1)
    real = one.executor._pool
    one.executor._pool = type("Down", (), {"submit": lambda self, fn: (_ for _ in ()).throw(RuntimeError("线程池已关闭"))})()
    with pytest.raises(RuntimeError):
        one.start("sub00001", SIMPLE)
    assert one.runs.get(one._run_key("sub00001")) is None  # 线程的占位退了
    one.executor._pool = real
    # 话已经落盘了：这时线程算没跑完，只能重试
    ev = _run_async(_drain(one.retry("sub00001")))
    assert ev[0][2]["queued"] is False  # 执行器的名额也退了，不会把下一次误报成排队
    assert ev[-2][2]["status"] == "idle"


# ---- 事件日志 ----


def test_reconnect_reads_only_what_was_missed(rr):
    async def go():
        s = rr.start("evt00001", SIMPLE)
        first = _block(await s.__anext__())
        await s.aclose()  # 收到第一个事件就断线
        assert first[1] == "thread"
        assert _wait_until(lambda: rr.state_view("evt00001")["status"] == "idle")
        rest = await _drain(rr.follow("evt00001", first[0]))
        run = first[0].rsplit("-", 1)[0]
        assert all(eid.startswith(run + "-") for eid, _, _ in rest)
        assert [_seq(eid) for eid, _, _ in rest] == list(range(_seq(first[0]) + 1, _seq(first[0]) + 1 + len(rest)))  # 紧接断点，不重不漏
        kinds = [e for _, e, _ in rest]
        assert kinds[0] == "started" and "step" in kinds and "delta" in kinds and kinds[-2:] == ["state", "end"]
        assert rest[-2][2]["status"] == "idle" and rest[-2][2]["report"]["version"] == 1
        # 已经收全的页面、刷新后的页面（没有 id）、拿着别的执行的 id：执行已结束，都只拿当前状态
        for after in (rest[-1][0], None, "zzzzzzzz-3"):
            again = await _drain(rr.follow("evt00001", after))
            assert [e for _, e, _ in again] == ["state", "end"] and again[0][2]["status"] == "idle", after

    _run_async(go())


def test_refresh_mid_run_replays_progress_so_far(rr):
    async def go():
        gate = _hold(rr.planner, "consolidate")  # 停在写报告里：搜索、读页的进度已经发过了
        await rr.start("evt00002", SIMPLE).aclose()
        try:
            assert _wait_until(lambda: any(e["event"] == "step" and e["data"]["step"]["kind"] == "report" for e in _logged(rr, "evt00002")))
            v = rr.state_view("evt00002")
            steps = [(i["kind"], i["pending"]) for i in v["turns"][0]["items"] if i["type"] == "step"]
            assert v["status"] == "running" and steps == [("search", False), ("read", False), ("report", True)]  # 正在写的那步是 pending
            live = asyncio.ensure_future(_drain(rr.follow("evt00002", None)))  # 刷新后的页面：手里没有事件 id
            await asyncio.sleep(0.3)
        finally:
            gate.set()
        got = await asyncio.wait_for(live, 10)
        kinds = [e for _, e, _ in got]
        assert kinds[:2] == ["thread", "started"] and "step" in kinds and kinds[-2:] == ["state", "end"]
        assert [_seq(eid) for eid, _, _ in got] == list(range(1, len(got) + 1))

    _run_async(go())


def test_follow_after_restart_sends_current_state(rr, tmp_path):
    _run_async(_drain(rr.start("evt00003", ASKS)))
    fresh = ResearchRunner(fake=True, data_dir=tmp_path / "data", out_dir=tmp_path / "out" / "research")
    fresh.events, fresh.runs = LocalEventLog(), LocalRunRegistry()  # 重启后进程里什么都没有，只剩检查点
    got = _run_async(_drain(fresh.follow("evt00003", "abcdef12-9")))
    assert [e for _, e, _ in got] == ["state", "end"] and got[0][2]["interrupt"]["type"] == "research.ask"


def test_quiet_stream_sends_heartbeat(rr, monkeypatch):
    monkeypatch.setattr(runner_mod, "HEARTBEAT_S", 0.3)

    async def go():
        gate = _hold(rr.planner, "title")
        s = rr.start("beat0001", SIMPLE)
        try:
            chunks = [await asyncio.wait_for(s.__anext__(), 3) for _ in range(4)]
        finally:
            gate.set()
        assert any(c.startswith(":") for c in chunks)  # 长时间没事件时发注释行，代理不会当成空闲连接掐掉
        await _drain(s)

    _run_async(go())


# ---- 运行状态与失败出口 ----


def test_failed_turn_keeps_content_and_can_retry(tmp_path):
    """第二轮的模型调用出错：第 1 版、来源、第一轮的对话都在；不能接着说，只能重试；重试后这一轮做完。"""
    calls = {"turn2": False}

    def second_turn(messages):
        return calls["turn2"]

    rr = flaky_runner(tmp_path, second_turn)
    http = _client(rr)
    st = _new(http, SIMPLE)
    assert st["status"] == "idle" and st["report"]["version"] == 1
    calls["turn2"] = True
    sec = st["report"]["outline"][0]["id"]
    ev = _events(_say(http, st["thread_id"], "重写", action={"type": "rewrite_section", "section_id": sec}))
    failed = _last(ev, "state")
    assert failed["status"] == "failed" and "网络断了" in failed["error"] and failed["interrupt"] is None
    assert failed["report"]["version"] == 1 and failed["sources"] == st["sources"] and failed["turns"][0] == st["turns"][0]
    assert len(failed["turns"]) == 2 and failed["turns"][1]["user"]["action"]["section_id"] == sec  # 用户的话留着
    assert _state(http, st["thread_id"])["status"] == "failed"  # 刷新回来也一样
    assert _say(http, st["thread_id"], "接着说").status_code == 409
    end = _last(_events(_retry(http, st["thread_id"])), "state")
    assert end["status"] == "idle" and end["report"]["version"] == 2 and len(end["turns"]) == 2


def test_retry_does_not_redo_searches(tmp_path):
    rr = flaky_runner(tmp_path, after_tool("read_page"))  # 搜过、读过，下一次模型调用出错
    http = _client(rr)
    ev = _events(http.post("/api/research/threads", json={"text": SIMPLE}))
    st = _last(ev, "state")
    assert st["status"] == "failed" and _steps(st) == ["search", "read"]
    ev = _events(_retry(http, st["thread_id"]))
    assert [e["step"]["kind"] for n, e in ev if n == "step"] == ["report", "report"]  # 只有写报告的开始和完成
    end = _last(ev, "state")
    assert end["status"] == "idle" and _steps(end) == ["search", "read", "report"] and len(end["sources"]) == 3


def test_answer_used_then_error_is_failed_and_retry_keeps_it(tmp_path):
    rr = flaky_runner(tmp_path, after_tool("ask_user"))
    http = _client(rr)
    st = _to_ask(http)
    after = _last(_events(_answer(http, st, {"answer": "包含 PPT"})), "state")
    # agent 已经拿到回答、下一步才出错：不能再把这张卡当成在等人，否则改了的回答会被悄悄丢掉
    assert after["status"] == "failed" and after["interrupt"] is None
    assert _answer(http, st, {"answer": "不包含 PPT"}).status_code == 409
    end = _last(_events(_retry(http, st["thread_id"])), "state")
    assert end["status"] == "idle" and _asks(end) == [("范围包含 PPT 工具吗？", "包含 PPT")]


def test_accepted_thread_survives_restart_before_running(tmp_path):
    one = ResearchRunner(fake=True, data_dir=tmp_path / "data", out_dir=tmp_path / "out", workers=1)
    one.executor._pool = type("Stuck", (), {"submit": lambda self, fn: None})()  # 收下了但一直没轮到，进程就停了

    async def go():
        s = one.start("acc00001", SIMPLE)
        assert _block(await s.__anext__())[1] == "thread"  # 页面已经拿到线程 id
        await s.aclose()

    _run_async(go())
    assert one.state_view("acc00001")["status"] == "queued"
    fresh = ResearchRunner(fake=True, data_dir=tmp_path / "data", out_dir=tmp_path / "out")
    fresh.events, fresh.runs = LocalEventLog(), LocalRunRegistry()
    v = fresh.state_view("acc00001")
    assert v["exists"] and v["status"] == "failed" and v["turns"][0]["user"]["text"] == SIMPLE  # 重启后查得到，可以重试
    end = _last([(e, d) for _, e, d in _run_async(_drain(fresh.retry("acc00001")))], "state")
    assert end["status"] == "idle" and end["report"]["version"] == 1


def test_run_cut_short_is_failed_and_retryable(client, rr):
    # 进程在两步之间停了（比如重启）：检查点没走完，也没有记下错误
    cfg = {"configurable": {"thread_id": "cut00001"}}
    rr.graph.update_state(cfg, {"messages": [user_message({}, SIMPLE)]}, as_node=START)
    rr.graph.invoke(None, cfg, interrupt_before=["tools"])
    st = _state(client, "cut00001")
    assert st["status"] == "failed" and "没跑完" in st["error"] and st["interrupt"] is None
    end = _last(_events(_retry(client, "cut00001")), "state")
    assert end["status"] == "idle" and end["report"]["version"] == 1


def test_retry_rejected_unless_failed(client):
    st = _to_ask(client)
    assert _retry(client, st["thread_id"]).status_code == 409  # 在等用户
    _events(_answer(client, st, {"answer": "包含"}))
    assert _retry(client, st["thread_id"]).status_code == 409  # 这一轮跑完了
    ev = _events(client.get(f"/api/research/threads/{st['thread_id']}/events"))
    assert [e for e, _ in ev] == ["state", "end"] and ev[0][1]["status"] == "idle"
    assert _retry(client, "nope0000").status_code == 404
    assert client.get("/api/research/threads/nope0000/events").status_code == 404


def test_tool_results_carry_retrieval_time(client, rr):
    st = _new(client, SIMPLE)
    msgs = rr.graph.get_state({"configurable": {"thread_id": st["thread_id"]}}).values["messages"]
    search = next(m for m in msgs if isinstance(m, ToolMessage) and m.name == "search_web")
    assert search.artifact["retrieved_at"] and all(s["retrieved_at"] for s in st["sources"].values())
