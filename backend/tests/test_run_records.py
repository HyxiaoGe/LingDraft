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


def _client(runner: ResearchRunner) -> TestClient:
    api_main.set_research_runner(runner)
    return TestClient(api_main.app)


@pytest.fixture
def rr(tmp_path):
    return ResearchRunner(fake=True, data_dir=tmp_path / "data")


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
    store.create("a", "t1", "new", None, None)
    store.create("b", "t2", "new", None, None)
    store.delete_thread("t1")
    assert store.for_thread("t1") == [] and [r["id"] for r in store.for_thread("t2")] == ["b"]


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
    assert store.take_pending("t1", QUEUE) == "放前面\n第一句\n第二句" and store.take_pending("t1", QUEUE) is None
    assert store.get_pending("t1", "guide")["text"] == "另一种"
    store.delete_thread("t1")
    assert store.get_pending("t1", "guide") is None and store.get_pending("t2", QUEUE)["text"] == "别的对话"


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
    assert [r["id"] for r in http.get("/api/research/runs?status=error").json()["runs"]] == [first["id"]]


def test_stopped_run_is_recorded(client, rr):
    gate = hold_model(rr.planner)
    first: dict = {}
    t = threading.Thread(target=lambda: first.setdefault("resp", client.post("/api/research/threads", json={"text": SIMPLE})))
    t.start()
    try:
        assert wait_until(lambda: any(r["status"] == "running" for r in client.get("/api/research/runs").json()["runs"]))
        (running,) = client.get("/api/research/runs").json()["runs"]
        assert running["started_at"] and running["ended_at"] is None
        assert client.post(f"/api/research/threads/{running['thread_id']}/stop").status_code == 200
    finally:
        gate.set()
        t.join(30)
    events(first["resp"])
    assert _runs(client, running["thread_id"])[0]["status"] == "stopped"


def test_records_survive_restart_and_unfinished_ones_become_lost(tmp_path):
    """进程重启：运行记录还在；上次没跑完的，服务启动时记成 lost。再建一个运行器像重启（存储见 conftest 的 _storage）。"""
    one = ResearchRunner(fake=True, data_dir=tmp_path / "data")
    st = new(_client(one), SIMPLE)
    aio.run(one.run_store.create("cut", st["thread_id"], "say", None, None))  # 像是重启前正在跑的一次
    aio.run(one.run_store.started("cut"))
    two = ResearchRunner(fake=True, data_dir=tmp_path / "data")
    two.recover()
    runs = {r["id"]: r["status"] for r in two.thread_runs(st["thread_id"])}
    assert runs.pop("cut") == LOST and list(runs.values()) == ["success"]


def test_delete_thread_deletes_its_runs(client, rr):
    a, b = new(client, SIMPLE), new(client, SIMPLE)
    assert client.delete(f"/api/research/threads/{a['thread_id']}").status_code == 200
    assert aio.run(rr.run_store.for_thread(a["thread_id"])) == [] and len(_runs(client, b["thread_id"])) == 1


def test_runs_api_checks_its_input(client):
    assert client.get("/api/research/threads/nope0000/runs").status_code == 404
    assert client.get("/api/research/runs?status=weird").status_code == 400
    assert client.get("/api/research/runs?limit=0").status_code == 400
    assert client.get("/api/research/runs?limit=201").status_code == 400
    assert client.get("/api/research/runs").json() == {"runs": []}


def test_record_write_failure_does_not_break_the_run(tmp_path, caplog):
    class Broken(MemoryRunStore):
        async def create(self, *a, **kw):
            raise RuntimeError("库挂了")

        started = finish = create

    rr = ResearchRunner(fake=True, data_dir=tmp_path / "data", run_store=Broken())
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
    rr = ResearchRunner(fake=True, data_dir=tmp_path / "data", checkpointer=memory_checkpointer())
    assert isinstance(rr.run_store, MemoryRunStore) and isinstance(rr.memory.store, InMemoryStore)


def test_no_database_fails_at_startup(monkeypatch):
    """没配库：服务启动时就报错，起不来，不会悄悄存到别处。"""
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(config, "USE_FAKE_PLANNER", True)  # CI 没有 .env：真实模型缺密钥会先报错
    monkeypatch.setattr(research_runner_mod, "open_storage", REAL_OPEN_STORAGE)  # conftest 换掉的换回来
    monkeypatch.setattr(api_main, "_research", None)
    with pytest.raises(RuntimeError, match="没配 LINGDRAFT_DATABASE_URL"), TestClient(api_main.app):
        pass
    assert api_main._research is None
