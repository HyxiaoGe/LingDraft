"""线程生命周期：并行执行、运行占位、事件日志与断线续读、运行状态、失败后重试、停止和重新生成。只用假模型。"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.constants import START

from app.api import main as api_main
from app.api import runner as runner_mod
from app.api.research_runner import ResearchRunner
from app.api.runner import LocalEventLog, LocalRunRegistry
from tests.research_helpers import SIMPLE, after_tool, events, flaky_runner, gated_runner, hold, last, new, run_async, say, state, wait_until


@pytest.fixture
def rr(tmp_path):
    return ResearchRunner(fake=True, data_dir=tmp_path / "data")


@pytest.fixture
def client(rr):
    return _client(rr)


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


def _stop(http: TestClient, tid: str):
    return http.post(f"/api/research/threads/{tid}/stop")


def _regenerate(http: TestClient, tid: str):
    return http.post(f"/api/research/threads/{tid}/regenerate")


def _steps(st: dict, turn: int = -1) -> list[str]:
    return [i["kind"] for i in st["turns"][turn]["items"] if i["type"] == "step"]


# ---- 并行执行与运行占位 ----


def test_event_log_wait_wakes_on_append():
    log = LocalEventLog()
    run = log.open("k")

    async def go():
        assert await log.wait(run, 0, 0.05) is False  # 没有新事件，等到超时
        t0 = time.monotonic()
        threading.Timer(0.05, log.append, (run, {"event": "x", "data": {}})).start()  # 像工作线程写进来
        assert await log.wait(run, 0, 5) is True and time.monotonic() - t0 < 1  # 一写进来就醒，不等到超时
        assert await log.wait(run, 0, 5) is True  # 已经有序号大于 0 的事件，马上返回
        threading.Timer(0.05, log.close, (run,)).start()
        assert await log.wait(run, 1, 5) is True and log.read(run, 1)[1]  # 结束也叫醒
        assert await log.wait("nope", 0, 5) is True  # 没有这次执行（日志丢了），不等

    run_async(go())
    assert not log._runs[run]["waiters"]


def test_research_threads_run_in_parallel(tmp_path):
    two = ResearchRunner(fake=True, data_dir=tmp_path / "data", workers=2)
    tids = ("par00001", "par00002")

    async def go():
        gate = hold(two.planner, "title")
        streams = [two.start(t, SIMPLE) for t in tids]
        try:
            # 两个都开跑了（停在同一道闸门上），不是一个跑、一个排队
            assert wait_until(lambda: all(two.state_view(t)["status"] == "running" for t in tids), 5)
        finally:
            gate.set()
        for s in streams:
            await _drain(s)
        assert all(two.state_view(t)["status"] == "idle" for t in tids)

    run_async(go())


def test_failed_submit_gives_back_slots(tmp_path):
    one = ResearchRunner(fake=True, data_dir=tmp_path / "data", workers=1)
    real = one.executor._pool
    one.executor._pool = type("Down", (), {"submit": lambda self, fn: (_ for _ in ()).throw(RuntimeError("线程池已关闭"))})()
    with pytest.raises(RuntimeError):
        one.start("sub00001", SIMPLE)
    assert one.runs.get(one._run_key("sub00001")) is None  # 线程的占位退了
    one.executor._pool = real
    # 话已经落盘了：这时线程算没跑完，只能重试
    ev = run_async(_drain(one.retry("sub00001")))
    assert ev[0][2]["queued"] is False  # 执行器的名额也退了，不会把下一次误报成排队
    assert ev[-2][2]["status"] == "idle"


def test_nothing_else_while_running(tmp_path):
    """跑着的时候不能接着说、重新生成、重试；跑完照常。"""
    rr, gate = gated_runner(tmp_path, after_tool("read_page"))  # 停在写正文之前
    client = _client(rr)
    tid = new(client, "直接答：先说一句")["thread_id"]
    first: dict = {}
    t = threading.Thread(target=lambda: first.setdefault("resp", say(client, tid, SIMPLE)))
    t.start()
    try:
        assert wait_until(lambda: state(client, tid)["status"] == "running")
        assert say(client, tid, "插一句").status_code == 409
        assert _regenerate(client, tid).status_code == 409
        assert _retry(client, tid).status_code == 409
    finally:
        gate.set()
        t.join(30)
    events(first["resp"])
    end = state(client, tid)
    assert end["status"] == "idle" and len(end["turns"]) == 2 and end["turns"][1]["record"]["outcome"] == "ok"


def test_disconnect_does_not_release_or_cancel_the_run(rr):
    async def go():
        gate = hold(rr.planner, "title")
        try:
            # 浏览器在响应开始前就断了：请求已被接受，后台照样跑
            gone = rr.start("drop0001", SIMPLE)
            await gone.aclose()
            # 读到 thread 事件后断开
            half = rr.start("drop0002", SIMPLE)
            assert json.loads((await half.__anext__()).split("data: ", 1)[1])["thread_id"] == "drop0002"
            await half.aclose()
            for tid in ("drop0001", "drop0002"):
                assert rr.state_view(tid)["status"] in ("queued", "running"), tid
        finally:
            gate.set()
        assert wait_until(lambda: all(rr.state_view(t)["status"] == "idle" for t in ("drop0001", "drop0002")), 20)
        assert all(rr.state_view(t)["turns"][0]["record"]["outcome"] == "ok" for t in ("drop0001", "drop0002"))

    run_async(go())


def test_queued_new_thread_is_visible_at_once(tmp_path):
    rr = ResearchRunner(fake=True, data_dir=tmp_path / "data", workers=1)

    async def go():
        gate = hold(rr.planner, "title")
        a = rr.start("queue001", SIMPLE)  # 占住唯一的执行线程
        try:
            ev = await asyncio.wait_for(a.__anext__(), 5)
            assert '"queued": false' in ev
            b = rr.start("queue002", SIMPLE)
            ev = await asyncio.wait_for(b.__anext__(), 2)  # 排队中也要马上拿到 id
            assert "event: thread" in ev and '"queued": true' in ev
            v = rr.state_view("queue002")
            assert v["exists"] and v["status"] == "queued" and v["title"] == SIMPLE and v["turns"][0]["user"] == SIMPLE
        finally:
            gate.set()
        async for _ in a:
            pass
        async for _ in b:
            pass
        assert rr.state_view("queue002")["status"] == "idle"

    run_async(go())


# ---- 事件日志 ----


def test_reconnect_reads_only_what_was_missed(rr):
    async def go():
        s = rr.start("evt00001", SIMPLE)
        first = _block(await s.__anext__())
        await s.aclose()  # 收到第一个事件就断线
        assert first[1] == "thread"
        assert wait_until(lambda: rr.state_view("evt00001")["status"] == "idle")
        rest = await _drain(rr.follow("evt00001", first[0]))
        run = first[0].rsplit("-", 1)[0]
        assert all(eid.startswith(run + "-") for eid, _, _ in rest)
        assert [_seq(eid) for eid, _, _ in rest] == list(range(_seq(first[0]) + 1, _seq(first[0]) + 1 + len(rest)))  # 紧接断点，不重不漏
        kinds = [e for _, e, _ in rest]
        assert kinds[0] == "started" and "step" in kinds and "delta" in kinds and kinds[-2:] == ["state", "end"]
        assert rest[-2][2]["status"] == "idle" and rest[-2][2]["turns"][0]["record"]["outcome"] == "ok"
        # 已经收全的页面、刷新后的页面（没有 id）、拿着别的执行的 id：执行已结束，都只拿当前状态
        for after in (rest[-1][0], None, "zzzzzzzz-3"):
            again = await _drain(rr.follow("evt00001", after))
            assert [e for _, e, _ in again] == ["state", "end"] and again[0][2]["status"] == "idle", after

    run_async(go())


def test_refresh_mid_run_replays_progress_so_far(tmp_path):
    rr, gate = gated_runner(tmp_path, after_tool("read_page"))  # 停在写正文之前：搜索、读页都发过了

    async def go():
        await rr.start("evt00002", SIMPLE).aclose()
        try:
            assert wait_until(lambda: any(e["event"] == "node" and e["data"]["node"] == "model" for e in _logged(rr, "evt00002"))
                              and len([e for e in _logged(rr, "evt00002") if e["event"] == "step"]) == 4)
            v = rr.state_view("evt00002")
            assert v["status"] == "running" and [(i["kind"], i["pending"]) for i in v["turns"][0]["items"] if i["type"] == "step"] == \
                [("search", False), ("read", False)]
            live = asyncio.ensure_future(_drain(rr.follow("evt00002", None)))  # 刷新后的页面：手里没有事件 id
            await asyncio.sleep(0.3)
        finally:
            gate.set()
        got = await asyncio.wait_for(live, 10)
        kinds = [e for _, e, _ in got]
        assert kinds[:2] == ["thread", "started"] and "step" in kinds and kinds[-2:] == ["state", "end"]
        assert [_seq(eid) for eid, _, _ in got] == list(range(1, len(got) + 1))

    run_async(go())


def test_follow_after_restart_sends_current_state(rr, tmp_path):
    run_async(_drain(rr.start("evt00003", SIMPLE)))
    fresh = ResearchRunner(fake=True, data_dir=tmp_path / "data")
    fresh.events, fresh.runs = LocalEventLog(), LocalRunRegistry()  # 重启后进程里什么都没有，只剩检查点
    got = run_async(_drain(fresh.follow("evt00003", "abcdef12-9")))
    assert [e for _, e, _ in got] == ["state", "end"] and got[0][2]["status"] == "idle"


def test_quiet_stream_sends_heartbeat(rr, monkeypatch):
    monkeypatch.setattr(runner_mod, "HEARTBEAT_S", 0.3)

    async def go():
        gate = hold(rr.planner, "title")
        s = rr.start("beat0001", SIMPLE)
        try:
            chunks = [await asyncio.wait_for(s.__anext__(), 3) for _ in range(4)]
        finally:
            gate.set()
        assert any(c.startswith(":") for c in chunks)  # 长时间没事件时发注释行，代理不会当成空闲连接掐掉
        await _drain(s)

    run_async(go())


# ---- 运行状态与失败出口 ----


def test_failed_turn_keeps_content_and_can_retry(tmp_path):
    """第二轮的模型调用出错：来源、第一轮的对话都在；不能接着说，只能重试；重试后这一轮做完。"""
    calls = {"turn2": False}
    rr = flaky_runner(tmp_path, lambda messages: calls["turn2"])
    http = _client(rr)
    st = new(http, SIMPLE)
    assert st["status"] == "idle"
    calls["turn2"] = True
    failed = last(events(say(http, st["thread_id"], "直接答：为什么")), "state")
    assert failed["status"] == "failed" and "网络断了" in failed["error"]
    assert failed["sources"] == st["sources"] and failed["turns"][0] == st["turns"][0]
    assert len(failed["turns"]) == 2 and failed["turns"][1]["user"] == "直接答：为什么"  # 用户的话留着
    assert state(http, st["thread_id"])["status"] == "failed"  # 刷新回来也一样
    assert say(http, st["thread_id"], "接着说").status_code == 409
    end = last(events(_retry(http, st["thread_id"])), "state")
    assert end["status"] == "idle" and len(end["turns"]) == 2 and end["turns"][1]["record"]["outcome"] == "ok"


def test_retry_does_not_redo_searches(tmp_path):
    rr = flaky_runner(tmp_path, after_tool("read_page"))  # 搜过、读过，下一次模型调用出错
    http = _client(rr)
    ev = events(http.post("/api/research/threads", json={"text": SIMPLE}))
    st = last(ev, "state")
    assert st["status"] == "failed" and _steps(st) == ["search", "read"]
    assert any(e == "error" for e, _ in ev) and rr.runs.get(rr._run_key(st["thread_id"])) is None  # 占位已释放
    ev = events(_retry(http, st["thread_id"]))
    assert [e for e, _ in ev if e == "step"] == []  # 搜索、读页不重来
    end = last(ev, "state")
    assert end["status"] == "idle" and _steps(end) == ["search", "read"] and len(end["sources"]) == 3


def test_accepted_thread_survives_restart_before_running(tmp_path):
    one = ResearchRunner(fake=True, data_dir=tmp_path / "data", workers=1)
    one.executor._pool = type("Stuck", (), {"submit": lambda self, fn: None})()  # 收下了但一直没轮到，进程就停了

    async def go():
        s = one.start("acc00001", SIMPLE)
        assert _block(await s.__anext__())[1] == "thread"  # 页面已经拿到线程 id
        await s.aclose()

    run_async(go())
    assert one.state_view("acc00001")["status"] == "queued"
    fresh = ResearchRunner(fake=True, data_dir=tmp_path / "data")
    fresh.events, fresh.runs = LocalEventLog(), LocalRunRegistry()
    v = fresh.state_view("acc00001")
    assert v["exists"] and v["status"] == "failed" and v["error"] == "上次运行没跑完就停了" and v["turns"][0]["user"] == SIMPLE
    end = last([(e, d) for _, e, d in run_async(_drain(fresh.retry("acc00001")))], "state")
    assert end["status"] == "idle" and end["turns"][0]["record"]["outcome"] == "ok"


def test_run_cut_short_is_failed_and_retryable(client, rr):
    # 进程在两步之间停了（比如重启）：检查点没走完，也没有记下错误
    cfg = {"configurable": {"thread_id": "cut00001"}}
    rr.graph.update_state(cfg, {"messages": [HumanMessage(SIMPLE)]}, as_node=START)
    rr.graph.invoke(None, cfg, interrupt_before=["tools"])
    st = state(client, "cut00001")
    assert st["status"] == "failed" and "没跑完" in st["error"]
    end = last(events(_retry(client, "cut00001")), "state")
    assert end["status"] == "idle" and end["turns"][0]["record"]["outcome"] == "ok"


def test_retry_rejected_unless_failed(client):
    st = new(client, SIMPLE)
    assert _retry(client, st["thread_id"]).status_code == 409  # 这一轮跑完了
    ev = events(client.get(f"/api/research/threads/{st['thread_id']}/events"))
    assert [e for e, _ in ev] == ["state", "end"] and ev[0][1]["status"] == "idle"
    assert _retry(client, "nope0000").status_code == 404
    assert client.get("/api/research/threads/nope0000/events").status_code == 404
    assert client.get("/api/research/threads/%2e%2e/state").status_code == 404


def test_tool_results_carry_retrieval_time(client, rr):
    st = new(client, SIMPLE)
    msgs = rr.graph.get_state({"configurable": {"thread_id": st["thread_id"]}}).values["messages"]
    search = next(m for m in msgs if isinstance(m, ToolMessage) and m.name == "search_web")
    assert search.artifact["retrieved_at"] and all(s["retrieved_at"] for s in st["sources"].values())


# ---- 停止和重新生成 ----


def _ended(http: TestClient, tid: str, n: int) -> bool:
    v = state(http, tid)
    return v["status"] == "idle" and len(v["turns"]) == n and bool(v["turns"][-1]["record"])


def test_turn_ends_before_suggestions(client, rr):
    """正文写完这一轮就结束：推荐问题还在起的时候线程已经空闲，没有可停的，能改名；页面先拿到这一轮的最终状态，
    再收到“正在起推荐问题”的阶段，推荐问题起好了写进这一轮的记录，再发一次状态。"""
    tid = new(client, "直接答：先说一句")["thread_id"]
    gate = hold(rr.planner, "suggest")
    first: dict = {}
    t = threading.Thread(target=lambda: first.setdefault("resp", say(client, tid, SIMPLE)))
    t.start()
    try:
        assert wait_until(lambda: _ended(client, tid, 2))
        rec = state(client, tid)["turns"][1]["record"]
        assert rec["outcome"] == "ok" and rec["ended_at"] and rec["suggestions"] == []
        assert _stop(client, tid).status_code == 409
        assert client.patch(f"/api/research/threads/{tid}", json={"title": "改过"}).status_code == 200
    finally:
        gate.set()
        t.join(30)
    ev = events(first["resp"])
    assert [e for e, _ in ev][-4:] == ["state", "phase", "state", "end"] and not any(e == "error" for e, _ in ev)
    assert [d for e, d in ev if e == "phase"][-1] == {"phase": "suggest"}
    done, more = [d for e, d in ev if e == "state"]
    assert done["status"] == "idle" and done["turns"][1]["record"]["suggestions"] == []
    assert len(more["turns"][1]["record"]["suggestions"]) == 3 and more["title"] == "改过"  # 改名和写推荐问题都在
    assert state(client, tid)["turns"][1]["record"]["suggestions"] == more["turns"][1]["record"]["suggestions"]


def test_saying_more_drops_pending_suggestions(client, rr):
    """推荐问题还没起好时用户又说了话：新的一轮马上开始，不等它；上一轮的推荐问题不要了，起好了也不写。"""
    tid = new(client, "直接答：先说一句")["thread_id"]
    gate = hold(rr.planner, "suggest")
    resp: dict = {}
    talk = [threading.Thread(target=lambda k=k, text=text: resp.setdefault(k, say(client, tid, text))) for k, text in ((1, SIMPLE), (2, "直接答：好"))]
    talk[0].start()
    try:
        assert wait_until(lambda: _ended(client, tid, 2))
        talk[1].start()
        assert wait_until(lambda: _ended(client, tid, 3))  # 上一轮的推荐问题还卡在闸门上
    finally:
        gate.set()
        for t in talk:
            t.join(30)
    assert resp[2].status_code == 200 and [e for e, _ in events(resp[1])].count("state") == 1
    end = state(client, tid)
    assert end["turns"][1]["record"]["suggestions"] == [] and len(end["turns"][2]["record"]["suggestions"]) == 3


def test_delete_drops_pending_suggestions(client, rr):
    """推荐问题还没起好时删掉对话：起好了也不写，对话不会被写回来。"""
    tid = new(client, "直接答：先说一句")["thread_id"]
    gate = hold(rr.planner, "suggest")
    t = threading.Thread(target=lambda: say(client, tid, SIMPLE))
    t.start()
    try:
        assert wait_until(lambda: _ended(client, tid, 2))
        assert client.delete(f"/api/research/threads/{tid}").json() == {"ok": True}
    finally:
        gate.set()
        t.join(30)
    assert client.get(f"/api/research/threads/{tid}/state").status_code == 404


def test_stop_before_the_model_answers(client, rr):
    gate = hold(rr.planner, "title")
    first: dict = {}
    t = threading.Thread(target=lambda: first.setdefault("resp", client.post("/api/research/threads", json={"text": SIMPLE})))
    t.start()
    try:
        assert wait_until(lambda: any(r["status"] == "running" for r in client.get("/api/research/threads").json()["threads"]))
        tid = client.get("/api/research/threads").json()["threads"][0]["thread_id"]
        assert _stop(client, tid).status_code == 200
    finally:
        gate.set()
        t.join(30)
    end = last(events(first["resp"]), "state")
    (turn,) = end["turns"]
    assert turn["items"] == [] and turn["record"]["outcome"] == "stopped" and end["status"] == "idle"


def test_stop_queued_thread(tmp_path):
    one = ResearchRunner(fake=True, data_dir=tmp_path / "data", workers=1)

    async def go():
        gate = hold(one.planner, "title")
        a = one.start("stopq001", SIMPLE)
        try:
            await asyncio.wait_for(a.__anext__(), 5)
            b = one.start("stopq002", SIMPLE)
            await asyncio.wait_for(b.__anext__(), 2)
            assert one.state_view("stopq002")["status"] == "queued" and one.stop("stopq002")
        finally:
            gate.set()
        await _drain(a)
        got = await _drain(b)
        assert [e for _, e, _ in got if e in ("delta", "step", "title")] == []  # 没跑
        v = got[-2][2]
        assert v["status"] == "idle" and v["turns"][0]["record"]["outcome"] == "stopped" and v["turns"][0]["items"] == []
        assert one.state_view("stopq001")["turns"][0]["record"]["outcome"] == "ok"  # 别的线程不受影响

    run_async(go())


def test_regenerate_over_http(client):
    st = new(client, SIMPLE)
    tid = st["thread_id"]
    ev = events(_regenerate(client, tid))
    names = [e for e, _ in ev]
    assert names[:2] == ["thread", "started"] and "delta" in names and "title" not in names  # 标题不重起
    end = last(ev, "state")
    assert len(end["turns"]) == 1 and end["turns"][0]["id"] == st["turns"][0]["id"] and end["turns"][0]["record"]["outcome"] == "ok"
    assert end["title"] == st["title"]
    assert _regenerate(client, "nope0000").status_code == 404


def test_edit_rename_delete_over_http(client):
    st = new(client, SIMPLE)
    tid = st["thread_id"]
    end = last(events(client.post(f"/api/research/threads/{tid}/edit", json={"text": "直接答：换个问法"})), "state")
    assert len(end["turns"]) == 1 and end["turns"][0]["id"] == st["turns"][0]["id"] and end["turns"][0]["user"] == "直接答：换个问法"
    assert client.post(f"/api/research/threads/{tid}/edit", json={"text": "  "}).status_code == 400
    r = client.patch(f"/api/research/threads/{tid}", json={"title": "  改过的\n标题 "})
    assert r.status_code == 200 and r.json()["title"] == "改过的 标题" and state(client, tid)["title"] == "改过的 标题"
    assert client.patch(f"/api/research/threads/{tid}", json={"title": " "}).status_code == 400
    assert client.patch(f"/api/research/threads/{tid}", json={"title": "长" * 41}).status_code == 400
    assert client.delete(f"/api/research/threads/{tid}").json() == {"ok": True}
    assert client.get(f"/api/research/threads/{tid}/state").status_code == 404
    assert tid not in [t["thread_id"] for t in client.get("/api/research/threads").json()["threads"]]
    for r in (client.delete(f"/api/research/threads/{tid}"), client.patch(f"/api/research/threads/{tid}", json={"title": "x"}),
              client.post(f"/api/research/threads/{tid}/edit", json={"text": "x"})):
        assert r.status_code == 404


def test_rename_and_delete_refused_while_running(rr):
    http = _client(rr)
    gate = hold(rr.planner, "title")
    out: dict = {}
    t = threading.Thread(target=lambda: out.update(st=new(http, SIMPLE)))
    t.start()
    assert wait_until(lambda: http.get("/api/research/threads").json()["threads"])
    tid = http.get("/api/research/threads").json()["threads"][0]["thread_id"]
    try:
        assert http.patch(f"/api/research/threads/{tid}", json={"title": "x"}).status_code == 409
        assert http.delete(f"/api/research/threads/{tid}").status_code == 409
    finally:
        gate.set()
        t.join(10)
    assert out["st"]["status"] == "idle" and out["st"]["title"] != "x"


def test_regenerate_rejected_when_failed(tmp_path):
    http = _client(flaky_runner(tmp_path, lambda messages: True))
    st = last(events(http.post("/api/research/threads", json={"text": SIMPLE})), "state")
    assert st["status"] == "failed"
    r = _regenerate(http, st["thread_id"])
    assert r.status_code == 409 and "重试" in r.json()["detail"]
