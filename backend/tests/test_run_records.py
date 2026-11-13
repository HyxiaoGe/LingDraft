"""运行记录：每次执行一行，存进库里（见 app.api.run_store）。只用假模型。"""

from __future__ import annotations

import logging
import threading

import pytest
from fastapi.testclient import TestClient

from app import aio, config
from app.api import main as api_main
from app.api import research_runner as research_runner_mod
from app.api.research_runner import ResearchRunner
from app.api.research_runner import open_storage as REAL_OPEN_STORAGE  # 导入时还没被 conftest 换掉
from app.api.run_store import LOST, QUEUE, MemoryRunStore, PostgresRunStore
from app.graph.research.graph import postgres_pool
from tests.research_helpers import SIMPLE, events, flaky_runner, hold_model, last, new, say, wait_until
from tests.auth_helpers import ADMIN, USER  # 跨所有人的运行记录只有管理员能看


def _client(runner: ResearchRunner) -> TestClient:
    api_main.set_research_runner(runner)
    return TestClient(api_main.app, headers=USER)


@pytest.fixture
def rr(tmp_path):
    return ResearchRunner(fake=True)


@pytest.fixture
def client(rr):
    return _client(rr)


def _runs(client, tid: str) -> list[dict]:
    resp = client.get(f"/api/research/threads/{tid}/runs")
    assert resp.status_code == 200, resp.text
    return resp.json()["runs"]


# ---- 存储本身：内存，设了 LINGDRAFT_TEST_DATABASE_URL 时还有 Postgres ----

class _Sync:
    """存储的方法都是协程（在对话的事件循环里调）：测试里同步地调，转到那个循环里做。"""

    def __init__(self, store) -> None:
        self._store = store

    def __getattr__(self, name: str):
        method = getattr(self._store, name)
        return lambda *a, **kw: aio.run(method(*a, **kw))


@pytest.fixture(params=["memory", "postgres"])
def store(request):
    if request.param == "memory":
        yield _Sync(MemoryRunStore())
        return
    if not config.DATABASE_URL:
        pytest.skip("没设 LINGDRAFT_TEST_DATABASE_URL")
    pool = aio.run(postgres_pool(config.DATABASE_URL))
    try:
        s = PostgresRunStore(pool)
        aio.run(s.setup())
        yield _Sync(s)
    finally:
        aio.run(pool.close())


def test_store_keeps_each_run(store):
    store.create("r1", "t1", "new", "h1", "abc1234")
    store.started("r1")
    store.finish("r1", "success")
    store.create("r2", "t1", "say", "h2", None)
    store.started("r2")
    store.finish("r2", "error", "RuntimeError", "网络断了", "Traceback ...")
    store.create("r3", "t2", "new", "h3", None)
    one, two = store.for_thread("t1")
    assert (one["id"], two["id"]) == ("r2", "r1")  # 新的在前
    assert two["status"] == "success" and two["kind"] == "new" and two["turn_id"] == "h1" and two["version"] == "abc1234"
    assert two["error"] is None and two["created_at"] <= two["started_at"] <= two["ended_at"]
    assert two["created_at"].endswith("+00:00")
    assert (one["status"], one["error_type"], one["error"], one["traceback"]) == ("error", "RuntimeError", "网络断了", "Traceback ...")
    assert [r["id"] for r in store.recent(None, 10)] == ["r3", "r2", "r1"]
    assert [r["id"] for r in store.recent(None, 1)] == ["r3"]
    assert [r["id"] for r in store.recent("error", 10)] == ["r2"]
    assert store.recent("queued", 10)[0]["started_at"] is None


def test_store_sweep_marks_unfinished_runs_lost(store):
    store.create("q", "t1", "new", None, None)
    store.create("r", "t2", "new", None, None)
    store.started("r")
    store.create("done", "t3", "new", None, None)
    store.finish("done", "stopped")
    assert store.sweep() == 2
    got = {r["id"]: r for r in store.recent(None, 10)}
    assert got["q"]["status"] == got["r"]["status"] == LOST and got["q"]["ended_at"] and got["done"]["status"] == "stopped"
    assert store.sweep() == 0


def test_store_delete_thread(store):
    """删对话分两步：运行记录、排着的话先删，归谁那一行最后删（ResearchRunner._adelete 在检查点删掉以后才调 disown）。
    中间出错的话对话还归这个人，可以再删一次。"""
    store.create("a", "t1", "new", None, None)
    store.create("b", "t2", "new", None, None)
    store.claim("t1", "u9")
    store.claim("t2", "u9")
    store.delete_thread("t1")
    assert store.for_thread("t1") == [] and [r["id"] for r in store.for_thread("t2")] == ["b"]
    assert store.owner_of("t1") == "u9"  # 归谁还在
    store.disown("t1")
    assert store.owner_of("t1") is None and store.owner_of("t2") == "u9"
    store.disown("t1")  # 再删一次也没事
    store.delete_thread("t2")
    store.disown("t2")


def test_store_summaries(store):
    """线程列表里算好的那几项记在归谁那张表上：作废一次 rev 加一，按旧的 rev 记不上；跟着对话并进账号，删对话一起没；没归谁的不记。"""
    store.claim("t1", "guest:a")
    store.claim("t2", "u9")
    assert store.rows_of("guest:a") == [{"thread_id": "t1", "pinned_at": None, "summary": None, "rev": 0}]
    one = {"v": 1, "checkpoint": "c1", "title": "标题", "fallback": None, "status": "idle", "work": False, "scheduled": True,
           "updated_at": "2026-11-13T01:02:03.456789+00:00"}
    store.put_summary("t1", 0, one)
    store.put_summary("nobody", 0, one)
    pinned = store.pin("t1", True)
    assert store.rows_of("guest:a") == [{"thread_id": "t1", "pinned_at": pinned, "summary": one, "rev": 0}]
    assert store.rows_of("u9") == [{"thread_id": "t2", "pinned_at": None, "summary": None, "rev": 0}] and store.rows_of("nobody") == []
    store.touch("t1")
    store.touch("nobody")
    assert store.rows_of("guest:a")[0] | {"pinned_at": None} == {"thread_id": "t1", "pinned_at": None, "summary": None, "rev": 1}
    store.put_summary("t1", 0, one)  # 读检查点的时候这段对话开跑了：开跑前算的不留
    assert store.rows_of("guest:a")[0]["summary"] is None
    two = one | {"checkpoint": "c2", "status": "asking"}
    store.put_summary("t1", 1, two)
    assert store.rows_of("guest:a")[0]["summary"] == two
    assert store.transfer("guest:a", "u9") == 1
    assert {r["thread_id"]: (r["summary"], r["rev"]) for r in store.rows_of("u9")} == {"t1": (two, 1), "t2": (None, 0)}
    store.notify("t1", {"title": "早报", "text": "", "ok": True})  # 未读、置顶不动记着的
    store.mark_read("t1")
    store.pin("t1", False)
    assert {r["thread_id"]: (r["summary"], r["rev"]) for r in store.rows_of("u9")}["t1"] == (two, 1)
    store.disown("t1")
    assert [r["thread_id"] for r in store.rows_of("u9")] == ["t2"]
    store.claim("t1", "u9")  # 同一个 id 再建：从头来
    assert {r["thread_id"]: (r["summary"], r["rev"]) for r in store.rows_of("u9")}["t1"] == (None, 0)
    store.disown("t1")
    store.disown("t2")


def test_store_unread(store):
    """未读记在归谁那张表上：每次加一、通知换成最近那一次；看过了清零；没归谁的不记；跟着对话并进账号，删对话一起没。"""
    store.claim("t1", "guest:a")
    store.claim("t2", "u9")
    assert store.unread_of("guest:a") == {}
    store.notify("t1", {"title": "早报", "text": "第一次", "ok": True})
    store.notify("t1", {"title": "早报", "text": "", "ok": False})
    store.notify("nobody", {"title": "x", "text": "", "ok": True})
    got = store.unread_of("guest:a")
    assert list(got) == ["t1"] and got["t1"]["unread"] == 2
    n = got["t1"]["notice"]
    assert (n["title"], n["text"], n["ok"]) == ("早报", "", False) and n["at"].endswith("+00:00")
    assert store.unread_of("u9") == {}
    assert store.transfer("guest:a", "u9") == 1 and store.unread_of("u9")["t1"]["unread"] == 2
    store.mark_read("t1")
    store.mark_read("t2")  # 没有未读的：什么都不动
    assert store.unread_of("u9") == {}
    store.notify("t1", {"title": "早报", "text": "又一次", "ok": True})
    assert store.unread_of("u9")["t1"]["unread"] == 1
    store.disown("t1")
    assert store.unread_of("u9") == {}
    store.disown("t2")


def test_store_pending_messages(store):
    """排着的话：每个对话、每种方式一句；又说的接在后面（front 放到前面），取走就没了；删掉对话一起删。"""
    assert store.get_pending("t1", QUEUE) is None and store.take_pending("t1", QUEUE) is None
    a = store.put_pending("t1", QUEUE, "第一句")
    b = store.put_pending("t1", QUEUE, "第二句")
    assert a["text"] == "第一句" and b["text"] == "第一句\n第二句"
    assert b["created_at"] == a["created_at"] <= b["updated_at"] and b["created_at"].endswith("+00:00")
    store.put_pending("t1", QUEUE, "放前面", front=True)
    store.put_pending("t1", "guide", "另一种")
    store.put_pending("t2", QUEUE, "别的对话")
    assert store.get_pending("t1", QUEUE)["text"] == "放前面\n第一句\n第二句"
    assert store.take_pending("t1", QUEUE) == ("放前面\n第一句\n第二句", None) and store.take_pending("t1", QUEUE) is None
    assert store.get_pending("t1", "guide")["text"] == "另一种"
    store.delete_thread("t1")
    assert store.get_pending("t1", "guide") is None and store.get_pending("t2", QUEUE)["text"] == "别的对话"


def test_store_pending_thinking(store):
    """排着的话记着思考开关：合成一句时按后发的那句，后发的没给就留着原来的；放回前面的（比排着的早）不盖过排着的。"""
    assert store.put_pending("t1", QUEUE, "一", thinking=False)["thinking"] is False
    assert store.put_pending("t1", QUEUE, "二")["thinking"] is False  # 没给开关的不改
    assert store.put_pending("t1", QUEUE, "三", thinking=True)["thinking"] is True
    assert store.put_pending("t1", QUEUE, "早先的", front=True, thinking=False)["thinking"] is True
    assert store.get_pending("t1", QUEUE)["thinking"] is True
    assert store.take_pending("t1", QUEUE) == ("早先的\n一\n二\n三", True)
    store.put_pending("t1", QUEUE, "没给")
    assert store.put_pending("t1", QUEUE, "放回来的", front=True, thinking=False)["thinking"] is False  # 排着的没给，用放回来的
    assert store.take_pending("t1", QUEUE).thinking is False


# ---- 经过运行器和接口 ----

def test_each_run_is_recorded(client, rr):
    st = new(client, SIMPLE)
    tid = st["thread_id"]
    last(events(say(client, tid, "再说说")), "state")
    events(client.post(f"/api/research/threads/{tid}/regenerate"))
    end = last(events(client.post(f"/api/research/threads/{tid}/edit", json={"text": "换个问法"})), "state")
    runs = _runs(client, tid)
    assert [r["kind"] for r in runs] == ["edit", "regenerate", "say", "new"]
    assert all(r["status"] == "success" and r["thread_id"] == tid and r["ended_at"] for r in runs)
    assert all("traceback" not in r for r in runs)  # 堆栈不经接口给出去
    turns = [t["id"] for t in end["turns"]]
    assert [r["turn_id"] for r in runs] == [turns[1], turns[1], turns[1], turns[0]]
    # 运行记录的 id 就是事件 id 里的那次执行
    ev_ids = [line[4:] for line in client.post(f"/api/research/threads/{tid}/regenerate").text.splitlines() if line.startswith("id: ")]
    assert {i.rpartition("-")[0] for i in ev_ids} == {_runs(client, tid)[0]["id"]}


def test_version_is_recorded(client, rr, monkeypatch):
    monkeypatch.setattr(config, "VERSION", "aca72b2")
    st = new(client, SIMPLE)
    assert _runs(client, st["thread_id"])[0]["version"] == "aca72b2"


def test_failed_run_keeps_its_error_after_retry(tmp_path, caplog):
    """出错的那次运行连同原因留着：重试成功以后，检查点里的错误没了，运行记录里还在。堆栈进库、进日志，不进接口。"""
    rr = flaky_runner(tmp_path, lambda messages: True)
    http = _client(rr)
    with caplog.at_level(logging.ERROR, logger="app.api.runner"):
        failed = last(events(http.post("/api/research/threads", json={"text": SIMPLE})), "state")
    tid = failed["thread_id"]
    assert failed["status"] == "failed"
    assert any("执行出错" in r.getMessage() and r.exc_info for r in caplog.records)
    (run,) = _runs(http, tid)
    assert (run["status"], run["error_type"], run["error"]) == ("error", "RuntimeError", "假模型：网络断了")
    assert "Traceback" in aio.run(rr.run_store.for_thread(tid))[0]["traceback"]
    end = last(events(http.post(f"/api/research/threads/{tid}/retry")), "state")
    assert end["status"] == "idle" and end["error"] is None
    retry, first = _runs(http, tid)
    assert retry["kind"] == "retry" and retry["status"] == "success" and retry["turn_id"] == first["turn_id"]
    assert first["status"] == "error" and first["error"] == "假模型：网络断了"
    assert [r["id"] for r in http.get("/api/research/runs?status=error", headers=ADMIN).json()["runs"]] == [first["id"]]


def test_stopped_run_is_recorded(client, rr):
    gate = hold_model(rr.planner)
    first: dict = {}
    t = threading.Thread(target=lambda: first.setdefault("resp", client.post("/api/research/threads", json={"text": SIMPLE})))
    t.start()
    try:
        assert wait_until(lambda: any(r["status"] == "running" for r in client.get("/api/research/runs", headers=ADMIN).json()["runs"]))
        (running,) = client.get("/api/research/runs", headers=ADMIN).json()["runs"]
        assert running["started_at"] and running["ended_at"] is None
        assert client.post(f"/api/research/threads/{running['thread_id']}/stop").status_code == 200
    finally:
        gate.set()
        t.join(30)
    events(first["resp"])
    assert _runs(client, running["thread_id"])[0]["status"] == "stopped"


def test_records_survive_restart_and_unfinished_ones_become_lost(tmp_path):
    """进程重启：运行记录还在；上次没跑完的，服务启动时记成 lost。再建一个运行器像重启（存储见 conftest 的 _storage）。"""
    one = ResearchRunner(fake=True)
    st = new(_client(one), SIMPLE)
    aio.run(one.run_store.create("cut", st["thread_id"], "say", None, None))  # 像是重启前正在跑的一次
    aio.run(one.run_store.started("cut"))
    two = ResearchRunner(fake=True)
    two.recover()
    runs = {r["id"]: r["status"] for r in two.thread_runs(st["thread_id"])}
    assert runs.pop("cut") == LOST and list(runs.values()) == ["success"]


def test_delete_thread_deletes_its_runs(client, rr):
    a, b = new(client, SIMPLE), new(client, SIMPLE)
    assert client.delete(f"/api/research/threads/{a['thread_id']}").status_code == 200
    assert aio.run(rr.run_store.for_thread(a["thread_id"])) == [] and len(_runs(client, b["thread_id"])) == 1


@pytest.mark.parametrize("step", ["memory.aforget_thread", "files.delete_workspace", "graph.checkpointer.adelete_thread"])
def test_delete_that_fails_halfway_can_be_done_again(client, rr, monkeypatch, step):
    """删到一半出错（这一步的库断了）：对话还在、还归这个人，再删一次就删干净了。
    以前归谁那一行在第一步就删了：出错以后对话接口一律 404，剩下的检查点、文件没人删得到。"""
    tid = new(client, SIMPLE)["thread_id"]
    holder = rr
    *path, name = step.split(".")
    for part in path:
        holder = getattr(holder, part)
    real, calls = getattr(holder, name), []

    async def down_once(thread_id):
        calls.append(thread_id)
        if len(calls) == 1:
            raise RuntimeError("库断了")
        await real(thread_id)
    monkeypatch.setattr(holder, name, down_once)
    with pytest.raises(RuntimeError, match="库断了"):
        client.delete(f"/api/research/threads/{tid}")
    assert aio.run(rr.run_store.owner_of(tid)) is not None
    assert client.get(f"/api/research/threads/{tid}/state").status_code == 200  # 还看得到，还能再删
    assert [t["thread_id"] for t in client.get("/api/research/threads").json()["threads"]] == [tid]
    assert client.delete(f"/api/research/threads/{tid}").status_code == 200 and calls == [tid, tid]
    assert aio.run(rr.run_store.owner_of(tid)) is None
    assert client.get(f"/api/research/threads/{tid}/state").status_code == 404
    assert client.get("/api/research/threads").json()["threads"] == []
    assert aio.run(rr.files.store.of_workspace(tid)) == []


def test_runs_api_checks_its_input(client):
    assert client.get("/api/research/threads/nope0000/runs").status_code == 404
    assert client.get("/api/research/runs?status=weird", headers=ADMIN).status_code == 400
    assert client.get("/api/research/runs?limit=0", headers=ADMIN).status_code == 400
    assert client.get("/api/research/runs?limit=201", headers=ADMIN).status_code == 400
    assert client.get("/api/research/runs", headers=ADMIN).json() == {"runs": []}


def test_record_write_failure_does_not_break_the_run(tmp_path, caplog):
    class Broken(MemoryRunStore):
        async def create(self, *a, **kw):
            raise RuntimeError("库挂了")

        started = finish = create

    rr = ResearchRunner(fake=True, run_store=Broken())
    with caplog.at_level(logging.WARNING, logger="app.api.runner"):
        st = new(_client(rr), SIMPLE)
    assert st["status"] == "idle" and st["turns"][0]["record"]["outcome"] == "ok"
    assert sum("运行记录没写上" in r.getMessage() for r in caplog.records) == 3


def test_in_memory_checkpointer_means_in_memory_records(tmp_path, monkeypatch):
    """测试、评测传内存的检查点：运行记录、长期记忆也在内存里，不碰配置的库。"""
    from langgraph.store.memory import InMemoryStore

    from app.api import research_runner
    from app.graph.research.graph import memory_checkpointer

    monkeypatch.setattr(research_runner, "open_storage", None)  # 一调就报错
    rr = ResearchRunner(fake=True, checkpointer=memory_checkpointer())
    assert isinstance(rr.run_store, MemoryRunStore) and isinstance(rr.memory.store, InMemoryStore)


def test_no_database_fails_at_startup(monkeypatch):
    """没配库：服务启动时就报错，起不来，不会悄悄存到别处。"""
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(config, "USE_FAKE_PLANNER", True)  # CI 没有 .env：真实模型缺密钥会先报错
    monkeypatch.setattr(research_runner_mod, "open_storage", REAL_OPEN_STORAGE)  # conftest 换掉的换回来
    monkeypatch.setattr(api_main, "_research", None)
    with pytest.raises(RuntimeError, match="没配 LINGDRAFT_DATABASE_URL"), TestClient(api_main.app, headers=USER):
        pass
    assert api_main._research is None
