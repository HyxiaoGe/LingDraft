"""回答进行中又说的话：排着，这一轮答完或停下就接着发；出错的留着，页面放回输入框。只用假模型。"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.api import main as api_main
from app.api.research_runner import ResearchRunner
from app.api.run_store import QUEUE
from app.research_llm import FakeResearchPlanner, FakeToolModel
from tests.research_helpers import events, hold_model, last, new, say, state, wait_until


@pytest.fixture
def rr(tmp_path):
    return ResearchRunner(fake=True, data_dir=tmp_path / "data")


@pytest.fixture
def client(rr):
    return _client(rr)


def _client(runner: ResearchRunner) -> TestClient:
    api_main.set_research_runner(runner)
    return TestClient(api_main.app)


def _queue(http: TestClient, tid: str, text: str):
    return http.post(f"/api/research/threads/{tid}/queue", json={"text": text})


def _unqueue(http: TestClient, tid: str):
    return http.delete(f"/api/research/threads/{tid}/queue")


def _kinds(http: TestClient, tid: str) -> list[str]:
    return [r["kind"] for r in http.get(f"/api/research/threads/{tid}/runs").json()["runs"]]


def _settled(http: TestClient, tid: str, turns: int) -> dict:
    assert wait_until(lambda: (s := state(http, tid))["status"] != "running" and s["status"] != "queued" and len(s["turns"]) == turns)
    return state(http, tid)


class _Running:
    """在后台说一句话，模型停在闸门上（hold_model），等这一轮跑起来；退出时放开闸门、收完这次执行的事件。"""

    def __init__(self, http: TestClient, rr: ResearchRunner, tid: str, text: str, gate: threading.Event | None = None):
        self.http, self.tid, self.text = http, tid, text
        self.gate = gate or hold_model(rr.planner)
        self.resp: dict = {}
        self.evs: list = []

    def __enter__(self):
        self.t = threading.Thread(target=lambda: self.resp.setdefault("r", say(self.http, self.tid, self.text)))
        self.t.start()
        assert wait_until(lambda: state(self.http, self.tid)["status"] == "running")
        return self

    def __exit__(self, *exc):
        self.gate.set()
        self.t.join(30)
        if exc[0] is None:
            self.evs = events(self.resp["r"])


def test_said_while_running_goes_after_the_turn(client, rr):
    """答着的时候又说了两次：合成一句排着；这一轮答完自动发成新的一轮。排着话的这一轮不起推荐问题。"""
    tid = new(client, "直接答：先说一句")["thread_id"]
    with _Running(client, rr, tid, "直接答：第二句") as run:
        one = _queue(client, tid, "直接答：第三句")
        assert one.status_code == 200, one.text
        first = one.json()["queued"]
        assert first["text"] == "直接答：第三句"
        merged = _queue(client, tid, "  再补一句  ").json()["queued"]
        assert merged["text"] == "直接答：第三句\n再补一句" and merged["created_at"] == first["created_at"]
        assert state(client, tid)["queued_message"]["text"] == merged["text"]
    # 这次执行最后的状态里，排着的话已经发出去了：线程又在跑新的一轮
    end_of_run = last(run.evs, "state")
    assert end_of_run["queued_message"] is None and len(end_of_run["turns"]) == 3
    assert end_of_run["turns"][2]["user"] == merged["text"]
    assert ("phase", {"phase": "suggest"}) not in run.evs
    end = _settled(client, tid, 3)
    assert end["status"] == "idle" and end["queued_message"] is None
    assert [t["record"]["outcome"] for t in end["turns"]] == ["ok", "ok", "ok"]
    assert not end["turns"][1]["record"]["suggestions"]  # 排着话的那一轮没有推荐问题
    assert wait_until(lambda: state(client, tid)["turns"][2]["record"]["suggestions"])  # 最后一轮照常有
    assert _kinds(client, tid) == ["say", "say", "new"]


def test_stop_still_sends_the_queued_message(client, rr):
    """排着话的时候点了停止：这一轮停下，排着的话接着发。"""
    tid = new(client, "直接答：先说一句")["thread_id"]
    with _Running(client, rr, tid, "直接答：第二句"):
        assert _queue(client, tid, "直接答：停了也发").json()["queued"]["text"] == "直接答：停了也发"
        assert client.post(f"/api/research/threads/{tid}/stop").status_code == 200
    end = _settled(client, tid, 3)
    assert end["status"] == "idle" and not end["stopping"]
    assert [t["record"]["outcome"] for t in end["turns"]] == ["ok", "stopped", "ok"]
    assert end["turns"][2]["user"] == "直接答：停了也发"


def test_failed_turn_keeps_the_queued_message(tmp_path):
    """这一轮出错：排着的话不发，留着等页面取回输入框；取一次就没了。失败的线程不能再排话，先重试。"""
    gate, armed = threading.Event(), {"on": False}

    class Failing(FakeToolModel):
        def _message(self, messages):
            if armed["on"]:
                armed["on"] = False
                gate.wait(10)
                raise RuntimeError("假模型：网络断了")
            return super()._message(messages)

    p = FakeResearchPlanner()
    p._model = Failing()
    rr = ResearchRunner(planner=p, data_dir=tmp_path / "data")
    http = _client(rr)
    tid = new(http, "直接答：先说一句")["thread_id"]
    armed["on"] = True
    with _Running(http, rr, tid, "直接答：第二句", gate) as run:
        _queue(http, tid, "直接答：等会儿再说")
    failed = last(run.evs, "state")
    assert failed["status"] == "failed" and failed["queued_message"]["text"] == "直接答：等会儿再说"
    assert _kinds(http, tid) == ["say", "new"]  # 没有自动发
    r = _queue(http, tid, "直接答：再排一句")
    assert r.status_code == 409 and "重试" in r.json()["detail"]
    assert _unqueue(http, tid).json() == {"text": "直接答：等会儿再说"}
    assert _unqueue(http, tid).json() == {"text": None}
    assert state(http, tid)["queued_message"] is None


def test_withdrawn_message_is_not_sent(client, rr):
    tid = new(client, "直接答：先说一句")["thread_id"]
    with _Running(client, rr, tid, "直接答：第二句"):
        _queue(client, tid, "直接答：算了")
        assert _unqueue(client, tid).json() == {"text": "直接答：算了"}
    end = _settled(client, tid, 2)
    assert end["status"] == "idle" and end["queued_message"] is None and _kinds(client, tid) == ["say", "new"]


def test_queue_when_not_running_sends_at_once(client):
    """页面以为还在答、话到的时候这一轮刚好答完了：直接发，页面去接事件流。"""
    assert _queue(client, "nope0000", "直接答：你好").status_code == 404
    tid = new(client, "直接答：先说一句")["thread_id"]
    assert _queue(client, tid, "   ").status_code == 400
    r = _queue(client, tid, "直接答：刚好答完了")
    assert r.status_code == 200 and r.json() == {"queued": None}
    end = _settled(client, tid, 2)
    assert end["turns"][1]["user"] == "直接答：刚好答完了" and end["turns"][1]["record"]["outcome"] == "ok"
    assert _kinds(client, tid) == ["say", "new"]


def test_queued_message_is_put_back_when_it_cannot_be_sent(client, rr, monkeypatch):
    """这一轮答完了，排着的话却没发出去（写检查点出错）：话放回去留着，线程空闲，页面会把它放回输入框。"""
    tid = new(client, "直接答：先说一句")["thread_id"]
    with _Running(client, rr, tid, "直接答：第二句") as run:
        _queue(client, tid, "直接答：第三句")

        def broken(thread_id, text):
            raise RuntimeError("库挂了")

        monkeypatch.setattr(rr, "accept", broken)
    end = last(run.evs, "state")
    assert end["status"] == "idle" and len(end["turns"]) == 2 and end["queued_message"]["text"] == "直接答：第三句"


def test_queued_message_survives_restart(tmp_path):
    one = ResearchRunner(fake=True, data_dir=tmp_path / "data")
    tid = new(_client(one), "直接答：先说一句")["thread_id"]
    one.run_store.put_pending(tid, QUEUE, "重启前排着的")
    two = ResearchRunner(fake=True, data_dir=tmp_path / "data")
    assert two.state_view(tid)["queued_message"]["text"] == "重启前排着的"


def test_delete_thread_drops_its_queued_message(client, rr):
    tid = new(client, "直接答：先说一句")["thread_id"]
    rr.run_store.put_pending(tid, QUEUE, "还排着")
    assert client.delete(f"/api/research/threads/{tid}").status_code == 200
    assert rr.run_store.get_pending(tid, QUEUE) is None
