"""确认卡回传的安全性：文件路径、严格载荷、中断 id、运行占位、断线与排队。只用假模型。"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from langgraph.types import Command

from app.api import main as api_main
from app.api.research_runner import ResearchRunner
from app.api.runner import GraphRunner
from app.graph.research.graph import build_graph, memory_checkpointer
from app.research_llm import FakeResearchPlanner

AMBIGUOUS = "AI 海报工具在局部修改上的现状"  # 会弹澄清卡，答完再问一次
SIMPLE = "最近新加坡的天气情况如何？"  # 不弹卡，直接到终审


@pytest.fixture
def rr(tmp_path):
    return ResearchRunner(fake=True, data_dir=tmp_path / "data", out_dir=tmp_path / "out" / "research")


@pytest.fixture
def client(rr, tmp_path):
    api_main.set_research_runner(rr)
    api_main.set_runner(GraphRunner(fake=True, data_dir=tmp_path / "pdata", out_dir=tmp_path / "out" / "poster"))
    return TestClient(api_main.app)


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


def _new(client, request: str) -> dict:
    return _last(_events(client.post("/api/research/threads", json={"request": request})), "state")


def _to_ask(client) -> dict:
    st = _new(client, AMBIGUOUS)
    st = _last(_events(_answer(client, st, {"answers": {"q1": "改文字"}})), "state")
    assert st["interrupt"]["type"] == "research.ask"
    return st


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


@pytest.mark.parametrize("bad", [
    {"action": "approve"},
    {},
    {"answer": "包含", "skip": True},
    {"answer": "   "},
    {"skip": False},
])
def test_ask_rejects_malformed_answer(client, bad):
    st = _to_ask(client)
    r = _answer(client, st, bad)
    assert r.status_code == 400, r.text
    after = _state(client, st["thread_id"])
    assert after["interrupt"]["id"] == st["interrupt"]["id"] and after["asks"] == [] and after["next"] == st["next"]


def test_rejected_scope_answer_does_not_block_correct_one(client):
    st = _new(client, AMBIGUOUS)
    for bad in ({"answers": {"nope": "x"}}, {"answers": {"q1": "  "}}, {"answers": {"q1": "改文字"}, "extra": 1}):
        r = _answer(client, st, bad)
        assert r.status_code == 400, r.text
    ev = _events(_answer(client, st, {"answers": {"q1": "改文字"}}))
    assert not any(e == "error" for e, _ in ev)
    assert _last(ev, "state")["interrupt"]["type"] == "research.ask"


def test_rejected_review_decision_does_not_block_approve(client):
    st = _new(client, SIMPLE)
    assert st["interrupt"]["type"] == "research.review"
    for bad in ({"action": "edit_claim", "target": st["claims"][0]["id"]},
                {"action": "edit_claim", "target": "k999", "content": "x"},
                {"action": "exclude_source", "target": "s999"},
                {"action": "rewrite_section", "target": "c9", "instruction": "短一点"},
                {"action": "approve", "content": "x"},
                {"action": "publish"}):
        r = _answer(client, st, bad)
        assert r.status_code == 400, (bad, r.text)
    ev = _events(_answer(client, st, {"action": "approve"}))
    assert not any(e == "error" for e, _ in ev)
    end = _last(ev, "state")
    assert end["next"] == [] and end["interrupt"] is None


def test_node_schema_rejects_without_poisoning(tmp_path):
    """绕过接口直接把坏回答交给图（节点的兜底校验）：被拒后，同一张卡仍能用正确的回答继续。"""
    g = build_graph(FakeResearchPlanner(), memory_checkpointer(), tmp_path / "out")

    def pending(cfg):
        return next(it for t in g.get_state(cfg).tasks for it in t.interrupts)

    cfg = {"configurable": {"thread_id": "scope"}}
    g.invoke({"request": AMBIGUOUS}, cfg)
    it = pending(cfg)
    with pytest.raises(ValueError):
        g.invoke(Command(resume={it.id: {"answers": {"nope": "x"}}}), cfg)
    g.invoke(Command(resume={it.id: {"answers": {"q1": "改文字"}}}), cfg)
    assert "action_requests" in pending(cfg).value

    cfg = {"configurable": {"thread_id": "review"}}
    g.invoke({"request": SIMPLE}, cfg)
    it = pending(cfg)
    with pytest.raises(ValueError):
        g.invoke(Command(resume={it.id: {"action": "edit_claim", "target": "k1"}}), cfg)
    g.invoke(Command(resume={it.id: {"action": "approve"}}), cfg)
    assert g.get_state(cfg).next == ()


# ---- 中断 id ----


def test_interrupt_events_carry_the_state_id_once(client):
    ev = _events(client.post("/api/research/threads", json={"request": AMBIGUOUS}))
    ints = [d for e, d in ev if e == "interrupt"]
    st = _last(ev, "state")
    assert len(ints) == 1 and ints[0]["id"] == st["interrupt"]["id"]
    ev = _events(_answer(client, st, {"answers": {"q1": "改文字"}}))
    ints = [d for e, d in ev if e == "interrupt"]
    assert len(ints) == 1 and ints[0]["type"] == "research.ask" and ints[0]["id"] == _last(ev, "state")["interrupt"]["id"]


def test_stale_interrupt_id_conflicts_and_leaves_state(client):
    scope = _new(client, AMBIGUOUS)
    ask = _last(_events(_answer(client, scope, {"answers": {"q1": "改文字"}})), "state")
    # 页面拿着旧的澄清卡又提交了一次（比如断线后重发）：不能落到新的提问卡上
    r = _answer(client, scope, {"answer": "包含"})
    assert r.status_code == 409, r.text
    after = _state(client, ask["thread_id"])
    assert after["interrupt"]["id"] == ask["interrupt"]["id"] and after["asks"] == []
    # 不带 id 的请求不接受
    r = client.post(f"/api/research/threads/{ask['thread_id']}/resume", json={"payload": {"answer": "包含"}})
    assert r.status_code == 422


def test_resume_finished_thread_conflicts(client):
    st = _new(client, SIMPLE)
    _events(_answer(client, st, {"action": "approve"}))
    assert _answer(client, st, {"action": "approve"}).status_code == 409


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
        r = _answer(client, st, {"answer": "包含"})
        assert r.status_code == 409, r.text
    finally:
        gate.set()
        t.join(30)
    _events(first["resp"])
    end = _state(client, tid)
    assert [(a["question"], a["answer"]) for a in end["asks"]] == [("范围是否包含 PPT 工具？", "包含")]
    assert end["interrupt"]["type"] == "research.review" and end["status"] == "waiting"


def test_disconnect_does_not_release_or_cancel_the_run(rr):
    async def go():
        gate = _hold(rr.planner, "intake")
        try:
            # 浏览器在响应开始前就断了：请求已被接受，后台照样跑
            gone = rr.start("drop0001", SIMPLE)
            await gone.aclose()
            # 读到 thread 事件后断开
            half = rr.start("drop0002", AMBIGUOUS)
            assert json.loads((await half.__anext__()).split("data: ", 1)[1])["thread_id"] == "drop0002"
            await half.aclose()
            for tid in ("drop0001", "drop0002"):
                assert rr.state_view(tid)["status"] in ("queued", "running"), tid
        finally:
            gate.set()
        for _ in range(400):
            if all(rr.state_view(t)["status"] == "waiting" for t in ("drop0001", "drop0002")):
                break
            await asyncio.sleep(0.05)
        assert rr.state_view("drop0001")["interrupt"]["type"] == "research.review"
        assert rr.state_view("drop0002")["interrupt"]["type"] == "research.scope"


    _run_async(go())


def test_queued_new_thread_is_visible_at_once(tmp_path):
    rr = ResearchRunner(fake=True, data_dir=tmp_path / "data", out_dir=tmp_path / "out", workers=1)

    async def go():
        gate = _hold(rr.planner, "intake")
        a = rr.start("queue001", SIMPLE)  # 占住唯一的执行线程
        try:
            ev = await asyncio.wait_for(a.__anext__(), 5)
            assert '"queued": false' in ev
            b = rr.start("queue002", SIMPLE)
            ev = await asyncio.wait_for(b.__anext__(), 2)  # 排队中也要马上拿到 id
            assert "event: thread" in ev and '"queued": true' in ev
            v = rr.state_view("queue002")
            assert v["exists"] and v["status"] == "queued" and v["request"] == SIMPLE and v["interrupt"] is None
        finally:
            gate.set()
        async for _ in a:
            pass
        async for _ in b:
            pass
        v = rr.state_view("queue002")
        assert v["status"] == "waiting" and v["interrupt"]["type"] == "research.review"

    _run_async(go())


def test_failed_run_releases_slot(client, rr):
    st = _to_ask(client)
    orig = rr.planner.agent_model

    def boom(*a, **kw):
        raise RuntimeError("假模型：网络断了")

    rr.planner.agent_model = boom  # 恢复时 research 节点重建 agent 就失败，异常冒出图
    ev = _events(_answer(client, st, {"answer": "包含"}))
    assert any(e == "error" for e, _ in ev)
    # 回答没被用上：还是这张卡、在等用户（不算失败，不用重试），占位已释放，重新回答就能接着走
    after = _last(ev, "state")
    assert after["status"] == "waiting" and after["interrupt"]["id"] == st["interrupt"]["id"] and after["asks"] == []
    rr.planner.agent_model = orig
    end = _last(_events(_answer(client, after, {"answer": "包含"})), "state")
    assert end["interrupt"]["type"] == "research.review" and [a["answer"] for a in end["asks"]] == ["包含"]
