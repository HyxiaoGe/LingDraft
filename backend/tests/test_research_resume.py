"""提问卡回传的安全性：文件路径、严格载荷、中断 id、运行占位、断线与排队。只用假模型。"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import BaseMessage, ToolMessage

from app.api import main as api_main
from app.api.research_runner import ResearchRunner
from app.api.runner import GraphRunner
from app.research_llm import FakeResearchPlanner, FakeToolModel

ASKS = "问一下 调研局部修改"  # 先问一次，答完接着查、写报告
SIMPLE = "调研一下局部修改"  # 不问，直接查完写第 1 版


@pytest.fixture
def rr(tmp_path):
    return ResearchRunner(fake=True, data_dir=tmp_path / "data", out_dir=tmp_path / "out" / "research")


@pytest.fixture
def client(rr, tmp_path):
    api_main.set_research_runner(rr)
    api_main.set_runner(GraphRunner(fake=True, data_dir=tmp_path / "pdata", out_dir=tmp_path / "out" / "poster"))
    return TestClient(api_main.app)


def flaky_runner(tmp_path, fail) -> ResearchRunner:
    """模型在 fail(messages) 为真的那次调用上抛错（只抛一次）。agent 建好后换不了模型，所以在建运行器之前装上。"""
    state = {"armed": True}

    class Flaky(FakeToolModel):
        def _message(self, messages: list[BaseMessage]):
            if state["armed"] and fail(messages):
                state["armed"] = False
                raise RuntimeError("假模型：网络断了")
            return super()._message(messages)

    p = FakeResearchPlanner()
    p._model = Flaky()
    return ResearchRunner(planner=p, data_dir=tmp_path / "data", out_dir=tmp_path / "out" / "research")


def after_tool(name: str):
    return lambda messages: isinstance(messages[-1], ToolMessage) and messages[-1].name == name


def _events(resp) -> list[tuple[str, dict]]:
    assert resp.status_code == 200, resp.text
    out, ev = [], None
    for line in resp.text.splitlines():
        if line.startswith("event: "):
            ev = line[7:]
        elif line.startswith("data: "):
            out.append((ev, json.loads(line[6:])))
    return out


def _last(events, name):
    return [d for e, d in events if e == name][-1]


def _state(client, tid: str) -> dict:
    return client.get(f"/api/research/threads/{tid}/state").json()


def _answer(client, st: dict, payload: dict):
    """按页面的做法回传：带上当前这张卡的中断 id。"""
    return client.post(f"/api/research/threads/{st['thread_id']}/resume",
                       json={"interrupt_id": st["interrupt"]["id"], "payload": payload})


def _new(client, text: str) -> dict:
    return _last(_events(client.post("/api/research/threads", json={"text": text})), "state")


def _to_ask(client) -> dict:
    st = _new(client, ASKS)
    assert st["status"] == "waiting" and st["interrupt"]["type"] == "research.ask"
    return st


def _asks(st: dict) -> list[tuple[str, str | None]]:
    return [(i["question"], i["answer"]) for t in st["turns"] for i in t["items"] if i["type"] == "ask" and not i["pending"]]


def _hold(planner, name: str) -> threading.Event:
    """让 planner 的这一步停在闸门上，模拟慢模型。10 秒兜底，修复前的代码不会把测试永远卡住。"""
    gate = threading.Event()
    orig = getattr(planner, name)

    def slow(*a, **kw):
        gate.wait(10)
        return orig(*a, **kw)

    setattr(planner, name, slow)
    return gate


def _run_async(coro):
    """在新线程里跑事件循环：同一进程里的海报测试会在主线程留下 Playwright 的循环，主线程不能再 asyncio.run。"""
    with ThreadPoolExecutor(1) as ex:
        return ex.submit(asyncio.run, coro).result(60)


def _wait_until(fn, timeout: float = 10) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.05)
    return False


# ---- 文件路径 ----


def test_thread_id_cannot_escape_output_dir(client, rr, tmp_path):
    out = tmp_path / "out"
    for d in ("research", "poster"):
        (out / d).mkdir(parents=True, exist_ok=True)  # 目录在了，“..”才能真的往上跳
    (out / "secret.txt").write_text("SECRET")
    for url in ["/api/research/threads/%2e%2e/state", "/api/threads/%2e%2e/files/secret.txt", "/api/threads/%2e%2e/export"]:
        r = client.get(url)
        assert r.status_code in (400, 404), url
        assert "SECRET" not in r.text, url
    with pytest.raises(ValueError):
        rr.thread_dir("..")


# ---- 严格载荷 ----


@pytest.mark.parametrize("bad", [{"action": "approve"}, {}, {"answer": "包含", "skip": True}, {"answer": "   "}, {"skip": False}])
def test_ask_rejects_malformed_answer(client, bad):
    st = _to_ask(client)
    r = _answer(client, st, bad)
    assert r.status_code == 400, r.text
    after = _state(client, st["thread_id"])
    assert after["interrupt"]["id"] == st["interrupt"]["id"] and _asks(after) == [] and after["status"] == "waiting"
    ev = _events(_answer(client, st, {"answer": "包含"}))  # 被拒以后同一张卡照样能答
    assert not any(e == "error" for e, _ in ev) and _last(ev, "state")["status"] == "idle"


# ---- 中断 id ----


def test_interrupt_events_carry_the_state_id_once(client):
    ev = _events(client.post("/api/research/threads", json={"text": ASKS}))
    ints = [d for e, d in ev if e == "interrupt"]
    st = _last(ev, "state")
    assert len(ints) == 1 and ints[0]["id"] == st["interrupt"]["id"] and ints[0]["title"] == "范围包含 PPT 工具吗？"
    ev = _events(_answer(client, st, {"answer": "包含"}))
    assert not [d for e, d in ev if e == "interrupt"] and _last(ev, "state")["interrupt"] is None


def test_stale_interrupt_id_conflicts_and_leaves_state(client):
    st = _to_ask(client)
    r = client.post(f"/api/research/threads/{st['thread_id']}/resume", json={"interrupt_id": "old", "payload": {"answer": "包含"}})
    assert r.status_code == 409, r.text
    after = _state(client, st["thread_id"])
    assert after["interrupt"]["id"] == st["interrupt"]["id"] and _asks(after) == []
    # 不带 id 的请求不接受
    r = client.post(f"/api/research/threads/{st['thread_id']}/resume", json={"payload": {"answer": "包含"}})
    assert r.status_code == 422


def test_resume_finished_thread_conflicts(client):
    st = _to_ask(client)
    _events(_answer(client, st, {"answer": "包含"}))
    assert _answer(client, st, {"answer": "包含"}).status_code == 409


# ---- 运行占位 ----


def test_concurrent_duplicate_resume_only_one_applies(client, rr):
    st = _to_ask(client)
    tid = st["thread_id"]
    gate = _hold(rr.planner, "consolidate")
    first: dict = {}
    t = threading.Thread(target=lambda: first.setdefault("resp", _answer(client, st, {"answer": "包含"})))
    t.start()
    try:
        assert _wait_until(lambda: _state(client, tid)["status"] == "running")
        assert _answer(client, st, {"answer": "包含"}).status_code == 409
        r = client.post(f"/api/research/threads/{tid}/messages", json={"text": "插一句"})
        assert r.status_code == 409  # 跑着的时候不能接着说
    finally:
        gate.set()
        t.join(30)
    _events(first["resp"])
    end = _state(client, tid)
    assert _asks(end) == [("范围包含 PPT 工具吗？", "包含")] and end["status"] == "idle" and end["report"]["version"] == 1


def test_disconnect_does_not_release_or_cancel_the_run(rr):
    async def go():
        gate = _hold(rr.planner, "title")
        try:
            # 浏览器在响应开始前就断了：请求已被接受，后台照样跑
            gone = rr.start("drop0001", SIMPLE)
            await gone.aclose()
            # 读到 thread 事件后断开
            half = rr.start("drop0002", ASKS)
            assert json.loads((await half.__anext__()).split("data: ", 1)[1])["thread_id"] == "drop0002"
            await half.aclose()
            for tid in ("drop0001", "drop0002"):
                assert rr.state_view(tid)["status"] in ("queued", "running"), tid
        finally:
            gate.set()
        for _ in range(400):
            if rr.state_view("drop0001")["status"] == "idle" and rr.state_view("drop0002")["status"] == "waiting":
                break
            await asyncio.sleep(0.05)
        assert rr.state_view("drop0001")["report"]["version"] == 1
        assert rr.state_view("drop0002")["interrupt"]["type"] == "research.ask"

    _run_async(go())


def test_queued_new_thread_is_visible_at_once(tmp_path):
    rr = ResearchRunner(fake=True, data_dir=tmp_path / "data", out_dir=tmp_path / "out", workers=1)

    async def go():
        gate = _hold(rr.planner, "title")
        a = rr.start("queue001", SIMPLE)  # 占住唯一的执行线程
        try:
            ev = await asyncio.wait_for(a.__anext__(), 5)
            assert '"queued": false' in ev
            b = rr.start("queue002", SIMPLE)
            ev = await asyncio.wait_for(b.__anext__(), 2)  # 排队中也要马上拿到 id
            assert "event: thread" in ev and '"queued": true' in ev
            v = rr.state_view("queue002")
            assert v["exists"] and v["status"] == "queued" and v["title"] == SIMPLE and v["turns"][0]["user"]["text"] == SIMPLE
            assert v["interrupt"] is None
        finally:
            gate.set()
        async for _ in a:
            pass
        async for _ in b:
            pass
        assert rr.state_view("queue002")["status"] == "idle"

    _run_async(go())


def test_failed_run_releases_slot(tmp_path):
    rr = flaky_runner(tmp_path, after_tool("ask_user"))
    api_main.set_research_runner(rr)
    client = TestClient(api_main.app)
    st = _to_ask(client)
    ev = _events(_answer(client, st, {"answer": "包含"}))
    assert any(e == "error" for e, _ in ev)
    after = _last(ev, "state")
    assert after["status"] == "failed" and "网络断了" in after["error"]
    assert rr.runs.get(rr._run_key(st["thread_id"])) is None  # 占位已释放
    end = _last(_events(client.post(f"/api/research/threads/{st['thread_id']}/retry")), "state")
    assert end["status"] == "idle" and _asks(end) == [("范围包含 PPT 工具吗？", "包含")]
