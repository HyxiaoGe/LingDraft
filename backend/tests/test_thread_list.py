"""线程列表：每行从检查点算出来的那几项记在库里，不用每次把每段对话的检查点读一遍（见 ResearchRunner._alist_threads）。
这里看的是：什么时候不读、什么时候重算，记着的不会和检查点对不上。只用假模型。
别的测试里取列表时也都把记着的和现算的对了一遍（见 conftest 的 _check_summaries）；这里要数读了几次检查点，把它关掉。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import aio
from app.api import main as api_main
from app.api import research_runner
from app.api.research_runner import SUMMARY_V, ResearchRunner
from app.graph.research.graph import latest_checkpoints, memory_checkpointer
from app.identity import Identity
from app.research_llm import FakeResearchPlanner
from app.tools.gateway import GatewayAuthError
from tests.auth_helpers import USER, bearer, user_token
from tests.research_helpers import events, hold, hold_model, last, new, say, state, wait_until

ME = Identity("u1", "user")


def _client(runner: ResearchRunner) -> TestClient:
    api_main.set_research_runner(runner)
    return TestClient(api_main.app, headers=USER)


def _rows(http: TestClient) -> dict[str, dict]:
    return {r["thread_id"]: r for r in http.get("/api/research/threads").json()["threads"]}


def _reads(runner: ResearchRunner, monkeypatch) -> list[str]:
    """记下之后每次读了哪段对话的检查点（列表、状态、跑一轮都算），并关掉“记着的和现算的对一遍”（它每行都读）。"""
    monkeypatch.setattr(ResearchRunner, "check_summaries", False)
    read: list[str] = []
    real = runner.graph.aget_state

    async def counting(cfg, *a, **kw):
        read.append(cfg["configurable"]["thread_id"])
        return await real(cfg, *a, **kw)
    monkeypatch.setattr(runner.graph, "aget_state", counting)
    return read


def _listed(http: TestClient, read: list[str]) -> tuple[dict[str, dict], list[str]]:
    """取一次列表：各行，和这一次读了哪几段对话的检查点。"""
    del read[:]
    rows = _rows(http)
    return rows, sorted(read)


def _same_as_state(http: TestClient, rows: dict[str, dict]) -> None:
    for tid, row in rows.items():
        st = state(http, tid)
        assert (row["title"], row["status"], row["work"], row["updated_at"]) == (st["title"], st["status"], st["work"], st["updated_at"]), tid


def test_list_reads_only_the_conversations_that_changed(monkeypatch):
    """第一次取列表每段读一次、记下；之后没变的不读。说了一句、改了名的那段重算；置顶、标已读不碰检查点，也不读。
    服务重启以后记着的还在。"""
    rr = ResearchRunner(fake=True)
    http = _client(rr)
    a, b, c = (new(http, f"直接答：{x}")["thread_id"] for x in "甲乙丙")
    read = _reads(rr, monkeypatch)
    first, got = _listed(http, read)
    assert got == sorted([a, b, c]) and set(first) == {a, b, c}
    _same_as_state(http, first)
    again, got = _listed(http, read)
    assert got == [] and again == first

    last(events(say(http, b, "直接答：再说一句")), "state")
    rows, got = _listed(http, read)
    assert got == [b] and rows[b]["updated_at"] > first[b]["updated_at"] and list(rows)[0] == b  # 最近有动静的在前
    assert rows[a] == first[a] and rows[c] == first[c]
    _same_as_state(http, rows)

    assert http.patch(f"/api/research/threads/{c}", json={"title": "改过的名字"}).status_code == 200
    rows, got = _listed(http, read)
    assert got == [c] and rows[c]["title"] == "改过的名字"

    pinned = http.post(f"/api/research/threads/{a}/pin").json()["pinned_at"]
    assert http.post(f"/api/research/threads/{a}/read").status_code == 200
    rows, got = _listed(http, read)
    assert got == [] and rows[a]["pinned_at"] == pinned
    assert _listed(http, read)[1] == []

    fresh = ResearchRunner(fake=True)  # 像服务重启：记着的在库里
    read = _reads(fresh, monkeypatch)
    after, got = _listed(_client(fresh), read)
    assert got == [] and after == rows


def test_running_conversation_is_read_and_not_kept(monkeypatch):
    """正在跑的那段每次都读检查点、不记（检查点和它上面记的东西正在变）；跑完第一次取列表时重算、记下。"""
    rr = ResearchRunner(fake=True)
    http = _client(rr)
    tid = new(http, "直接答：好")["thread_id"]
    read = _reads(rr, monkeypatch)
    assert _listed(http, read)[1] == [tid] and _listed(http, read)[1] == []
    gate = hold(rr.planner, "suggest")  # 推荐问题起得慢：这一轮已经答完、退了占位，推荐问题还没写进检查点
    try:
        ev = []
        with http.stream("POST", f"/api/research/threads/{tid}/messages", json={"text": "直接答：再来"}) as resp:
            assert wait_until(lambda: state(http, tid)["suggesting"])
            before, got = _listed(http, read)
            assert got == [tid] and before[tid]["status"] == "idle"
            assert _listed(http, read)[1] == []  # 答完了、记下了；推荐问题还没写
            gate.set()
            ev = [line for line in resp.iter_lines()]
        assert ev
    finally:
        gate.set()
    assert wait_until(lambda: not state(http, tid)["suggesting"])
    rows, got = _listed(http, read)
    # 推荐问题是这一轮结束以后、不占位写进检查点的：有了新的检查点，记着的对不上，重算
    assert got == [tid] and rows[tid]["updated_at"] == state(http, tid)["updated_at"] > before[tid]["updated_at"]
    assert _listed(http, read)[1] == []


def test_status_that_changes_on_the_same_checkpoint_is_recomputed(monkeypatch):
    """要了当前位置停下来等（asking）；给了位置接着跑，天气服务的密钥出错，这一轮没跑完（failed）。
    两头是同一个检查点：接着跑的那一步没做完就不出新的，变的只是它上面记的问题和出错。光看检查点的编号看不出变了，
    靠跑一轮开头把记着的作废（见 _run_turn）。"""
    p = FakeResearchPlanner()
    aio.run(p.connect_tools())

    async def down(**kw) -> str:
        raise GatewayAuthError("调 /weather/mcp 失败：HTTP 401", kind="http_401")
    p._mcp.weather.tool = p._mcp.weather.tool.model_copy(update={"coroutine": down})
    rr = ResearchRunner(planner=p)
    http = _client(rr)
    st = new(http, "当前位置天气")
    tid = st["thread_id"]
    assert st["status"] == "asking"
    at = aio.run(latest_checkpoints(rr.graph.checkpointer, [tid]))
    assert _rows(http)[tid]["status"] == "asking"  # 记下了
    kept = aio.run(rr.run_store.rows_of("u1"))[0]
    assert kept["summary"]["status"] == "asking" and kept["summary"]["checkpoint"] == at[tid]

    here = {"longitude": 114.057868, "latitude": 22.543099, "accuracy_m": 35}
    end = last(events(http.post(f"/api/research/threads/{tid}/answer", json={"id": st["ask"]["id"], "location": here})), "state")
    assert end["status"] == "failed"
    assert aio.run(latest_checkpoints(rr.graph.checkpointer, [tid])) == at  # 检查点没换
    monkeypatch.setattr(ResearchRunner, "check_summaries", False)  # 不靠对一遍兜着：记着的就该已经作废了
    row = aio.run(rr.run_store.rows_of("u1"))[0]
    assert row["summary"] is None and row["rev"] == kept["rev"] + 1
    assert _rows(http)[tid]["status"] == "failed"
    assert aio.run(rr.run_store.rows_of("u1"))[0]["summary"]["status"] == "failed"


def test_kept_rows_from_an_older_version_are_recomputed(monkeypatch):
    """算法改了就把 SUMMARY_V 加一：记着的全部重算一遍。"""
    rr = ResearchRunner(fake=True)
    http = _client(rr)
    a, b = (new(http, f"直接答：{x}")["thread_id"] for x in "甲乙")
    read = _reads(rr, monkeypatch)
    assert _listed(http, read)[1] == sorted([a, b]) and _listed(http, read)[1] == []
    monkeypatch.setattr(research_runner, "SUMMARY_V", SUMMARY_V + 1)
    rows, got = _listed(http, read)
    assert got == sorted([a, b]) and set(rows) == {a, b}
    assert _listed(http, read)[1] == []
    assert {r["summary"]["v"] for r in aio.run(rr.run_store.rows_of("u1"))} == {SUMMARY_V + 1}


def test_what_is_kept_for_a_row():
    """记下的就是这几项。这里对不上了（多了一项、哪一项的算法变了），把 research_runner.SUMMARY_V 加一再改这条：
    不加的话，库里按旧算法记着的会一直用下去，直到那段对话有新的检查点。"""
    rr = ResearchRunner(fake=True)
    http = _client(rr)
    done = new(http, "查一下局部修改")
    tid = done["thread_id"]
    _rows(http)
    kept = aio.run(rr.run_store.rows_of("u1"))[0]["summary"]
    assert kept == {"v": 1, "checkpoint": aio.run(latest_checkpoints(rr.graph.checkpointer, [tid]))[tid], "title": done["title"], "fallback": None,
                    "status": "idle", "work": False, "scheduled": False, "updated_at": state(http, tid)["updated_at"]}
    assert SUMMARY_V == 1


def test_claimed_thread_without_a_checkpoint_is_not_listed(monkeypatch):
    """归了人、第一句话却没写进检查点的（写的时候出了错）：不存在，不在列表里，也不用每次去读。"""
    rr = ResearchRunner(fake=True)
    http = _client(rr)
    tid = new(http, "直接答：好")["thread_id"]
    aio.run(rr.run_store.claim("ghost", "u1"))
    read = _reads(rr, monkeypatch)
    rows, got = _listed(http, read)
    assert set(rows) == {tid} and got == [tid]
    assert _listed(http, read) == (rows, [])
    assert rr.list_threads(Identity("u2", "user")) == []


def test_latest_checkpoints():
    saver = memory_checkpointer()
    assert aio.run(latest_checkpoints(saver, [])) == {} and aio.run(latest_checkpoints(saver, ["nope"])) == {}
    assert "nope" not in saver.storage  # 查的时候不在存储里建出空的线程
    rr = ResearchRunner(fake=True)  # 存储见 conftest 的 _storage：设了测试库时是 Postgres
    rr.accept("t1", "直接答：你好", owner="u1")
    one = aio.run(latest_checkpoints(rr.graph.checkpointer, ["t1", "nope"]))
    assert list(one) == ["t1"] and one["t1"] == rr.graph.get_state({"configurable": {"thread_id": "t1"}}).config["configurable"]["checkpoint_id"]
    rr.run_turn("t1")
    two = aio.run(latest_checkpoints(rr.graph.checkpointer, ["t1"]))
    assert two["t1"] > one["t1"] and two["t1"] == rr.graph.get_state({"configurable": {"thread_id": "t1"}}).config["configurable"]["checkpoint_id"]
    assert [r["status"] for r in rr.list_threads(ME)] == ["idle"]


def test_keeping_fails_quietly(monkeypatch):
    """算好的没记上、要跑时没作废成（库一时连不上）：列表照常给、这一轮照常跑，只是下次再算。"""
    rr = ResearchRunner(fake=True)
    http = _client(rr)

    async def broken(*a, **kw):
        raise RuntimeError("库连不上")
    monkeypatch.setattr(rr.run_store, "put_summary", broken)
    monkeypatch.setattr(rr.run_store, "touch", broken)
    tid = new(http, "直接答：好")["thread_id"]
    assert _rows(http)[tid]["status"] == "idle" and _rows(http)[tid]["status"] == "idle"
    assert aio.run(rr.run_store.rows_of("u1"))[0]["summary"] is None


@pytest.mark.parametrize("held", ["title", "suggest"])
def test_list_while_a_turn_is_running(held, monkeypatch):
    """跑着的时候取列表：状态是在跑；跑完再取是答完的样子，和打开对话看到的一样。"""
    rr = ResearchRunner(fake=True)
    http = _client(rr)
    gate = hold(rr.planner, held)
    try:
        with http.stream("POST", "/api/research/threads", json={"text": "查一下局部修改"}) as resp:
            assert wait_until(lambda: len(_rows(http)) == 1)
            tid = next(iter(_rows(http)))
            if held == "title":
                assert _rows(http)[tid]["status"] == "running" and _rows(http)[tid]["running_since"]
            gate.set()
            assert [line for line in resp.iter_lines()]
    finally:
        gate.set()
    assert wait_until(lambda: not state(http, tid)["suggesting"])
    rows = _rows(http)
    assert rows[tid]["status"] == "idle" and rows[tid]["running_since"] is None
    _same_as_state(http, rows)


def test_list_page():
    """页面先取最近的几段、往下滚再多取：limit 是最近有动静的多少段；置顶的、有未读的、在跑的、current 那段不管排在哪都带上；
    more 是还有没给的。不给 limit 是全部。"""
    rr = ResearchRunner(fake=True)
    http = _client(rr)
    ids = [new(http, f"直接答：{x}")["thread_id"] for x in "甲乙丙丁戊己"]  # 越后面的越新
    newest = ids[::-1]

    def page(**q) -> tuple[list[str], bool]:
        got = http.get("/api/research/threads", params=q).json()
        return [r["thread_id"] for r in got["threads"]], got["more"]

    assert page() == (newest, False)
    assert page(limit=2) == (newest[:2], True)
    assert page(limit=6) == (newest, False) and page(limit=50) == (newest, False)
    assert page(limit=5) == (newest[:5], True)
    assert http.get("/api/research/threads", params={"limit": 0}).status_code == 422
    # 开着的那段：排在后面也带上；别人的、没有的不带
    assert page(limit=2, current=ids[0]) == (newest[:2] + [ids[0]], True)
    assert page(limit=2, current=ids[5]) == (newest[:2], True) and page(limit=2, current="nope") == (newest[:2], True)
    # 置顶的、有未读的
    assert http.post(f"/api/research/threads/{ids[1]}/pin").status_code == 200
    aio.run(rr.run_store.notify(ids[2], {"title": "早报", "text": "", "ok": True}))
    assert page(limit=2) == (newest[:2] + [ids[2], ids[1]], True)
    assert page(limit=2, current=ids[0]) == (newest[:2] + [ids[2], ids[1], ids[0]], True)
    assert page(limit=3) == (newest[:3] + [ids[2], ids[1]], True) and page(limit=4) == (newest[:4] + [ids[1]], True)
    http.post(f"/api/research/threads/{ids[2]}/read")
    assert page(limit=2) == (newest[:2] + [ids[1]], True)
    # 在跑的：它开跑以后别的对话又有了动静，排到后面了也带上
    gate = hold_model(rr.planner)
    try:
        with http.stream("POST", f"/api/research/threads/{ids[0]}/messages", json={"text": "直接答：再来"}) as resp:
            assert wait_until(lambda: _rows(http)[ids[0]]["status"] == "running")
            assert http.patch(f"/api/research/threads/{ids[5]}", json={"title": "改个名"}).status_code == 200  # 这段排到最前
            assert page(limit=1) == ([ids[5], ids[0], ids[1]], True)
            gate.set()
            assert [line for line in resp.iter_lines()]
    finally:
        gate.set()
    assert wait_until(lambda: not state(http, ids[0])["suggesting"])
    assert page(limit=1) == ([ids[0], ids[1]], True)  # 答完了：它是最新的那段，另带上置顶的
    http2 = TestClient(api_main.app, headers=bearer(user_token("u2")))  # 别人：一段都没有；拿别人的 id 当 current 也拿不到
    assert http2.get("/api/research/threads", params={"limit": 2, "current": ids[0]}).json() == {"threads": [], "more": False}
