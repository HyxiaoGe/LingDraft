"""回答进行中又说的话：排着（最多一句），这一轮答完或停下就接着发；出错的留着，页面放回输入框。
排着的话也可以插进正在跑的这一轮：主 agent 下一次调模型之前放进对话；这一轮没再调模型就结束了，照常排着发。只用假模型。"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.api import main as api_main
from app.api.research_runner import ResearchRunner
from app.api.run_store import GUIDE, QUEUE
from app.graph.research import history
from app.research_llm import FAKE_BLOCKED, FakeResearchPlanner, FakeToolModel
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


def _guide(http: TestClient, tid: str):
    return http.post(f"/api/research/threads/{tid}/guide")


def _unguide(http: TestClient, tid: str):
    return http.delete(f"/api/research/threads/{tid}/guide")


class _Gate(threading.Event):
    """主 agent 的假模型第 at 次调用时停在这里（之前的直接过）；停下时 waiting 置上。放开以后再调都不停。"""

    def __init__(self, at: int = 1):
        super().__init__()
        self.at, self.calls, self.waiting = at, 0, threading.Event()

    def wait(self, timeout=None):
        self.calls += 1
        if self.calls < self.at:
            return True
        self.waiting.set()
        return super().wait(timeout)


def _gate(planner, at: int = 1) -> _Gate:
    g = _Gate(at)
    planner._model.gates.append(g)
    return g


def _types(turn: dict) -> list[str]:
    return [i["kind"] if i["type"] == "step" else i["type"] for i in turn["items"]]


def _reply(turn: dict) -> str:
    return "".join(i["text"] for i in turn["items"] if i["type"] == "text")


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
    """答着的时候又说了一句：排着；再说一句不收（没发出去的话最多一句）。这一轮答完自动发成新的一轮。排着话的这一轮不起推荐问题。"""
    tid = new(client, "直接答：先说一句")["thread_id"]
    with _Running(client, rr, tid, "直接答：第二句") as run:
        one = _queue(client, tid, "  直接答：第三句  ")
        assert one.status_code == 200, one.text
        assert one.json()["queued"]["text"] == "直接答：第三句"
        two = _queue(client, tid, "再补一句")
        assert two.status_code == 409 and "还没发出去" in two.json()["detail"]
        assert state(client, tid)["queued_message"]["text"] == "直接答：第三句"
    # 这次执行最后的状态里，排着的话已经发出去了：线程又在跑新的一轮
    end_of_run = last(run.evs, "state")
    assert end_of_run["queued_message"] is None and len(end_of_run["turns"]) == 3
    assert end_of_run["turns"][2]["user"] == "直接答：第三句"
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


# ---- 插进正在跑的这一轮 ----

def test_guided_message_goes_into_the_running_turn(client, rr):
    """搜着的时候把排着的话插进这一轮：搜完、下一次调模型之前放进对话，模型看得到；还是同一轮，插的话排在过程里它起作用的地方。
    重新回答从这一轮用户的话重来，插的话不要了。"""
    tid = new(client, "直接答：先说一句")["thread_id"]
    gate = _gate(rr.planner)
    with _Running(client, rr, tid, "搜一下猫", gate) as run:
        assert gate.waiting.wait(10)  # 第一次调模型已经开始：插的话等下一次
        assert _queue(client, tid, "只看橘猫").json()["queued"]["text"] == "只看橘猫"
        r = _guide(client, tid)
        assert r.status_code == 200 and r.json()["guided"]["text"] == "只看橘猫"
        s = state(client, tid)
        assert s["queued_message"] is None and s["guided_message"]["text"] == "只看橘猫"
    (ev,) = [d for e, d in run.evs if e == "guide"]
    end = last(run.evs, "state")
    assert end["status"] == "idle" and end["queued_message"] is None and end["guided_message"] is None
    assert len(end["turns"]) == 2 and _kinds(client, tid) == ["say", "new"]
    turn = end["turns"][1]
    assert turn["user"] == "搜一下猫" and turn["record"]["outcome"] == "ok"
    assert _types(turn) == ["thinking", "search", "guide", "thinking", "read", "thinking", "text"]
    item = turn["items"][2]
    assert (item["id"], item["text"]) == (ev["id"], ev["text"]) == (ev["id"], "只看橘猫") and item["sent_at"] == ev["sent_at"] != ""
    assert _reply(turn).startswith("收到：只看橘猫。")
    assert wait_until(lambda: state(client, tid)["turns"][1]["record"]["suggestions"])  # 推荐问题照常按这一轮用户的话起
    again = last(events(client.post(f"/api/research/threads/{tid}/regenerate")), "state")
    assert "guide" not in _types(again["turns"][1]) and not _reply(again["turns"][1]).startswith("收到")


def test_guided_message_is_queued_when_the_turn_ends_first(client, rr):
    """插的话还没等到下一次调模型，这一轮就答完了：照常排着发成新的一轮。等着插的时候也算有一句没发出去，再说的不收。"""
    tid = new(client, "直接答：先说一句")["thread_id"]
    gate = _gate(rr.planner)
    with _Running(client, rr, tid, "直接答：第二句", gate):
        assert gate.waiting.wait(10)
        _queue(client, tid, "直接答：插不进去了")
        assert _guide(client, tid).json()["guided"]["text"] == "直接答：插不进去了"
        assert _queue(client, tid, "后来又说的").status_code == 409
        s = state(client, tid)
        assert s["guided_message"]["text"] == "直接答：插不进去了" and s["queued_message"] is None
    end = _settled(client, tid, 3)
    assert "guide" not in _types(end["turns"][1])
    assert end["turns"][2]["user"] == "直接答：插不进去了"
    assert end["queued_message"] is None and end["guided_message"] is None and _kinds(client, tid) == ["say", "say", "new"]


def test_stop_sends_the_guided_message_that_did_not_go_in(client, rr):
    """插的话还没放进去就点了停止：和排着的一样，接着发成新的一轮。"""
    tid = new(client, "直接答：先说一句")["thread_id"]
    gate = _gate(rr.planner)
    with _Running(client, rr, tid, "直接答：第二句", gate):
        assert gate.waiting.wait(10)
        _queue(client, tid, "直接答：停了也发")
        _guide(client, tid)
        assert client.post(f"/api/research/threads/{tid}/stop").status_code == 200
    end = _settled(client, tid, 3)
    assert [t["record"]["outcome"] for t in end["turns"]] == ["ok", "stopped", "ok"]
    assert end["turns"][2]["user"] == "直接答：停了也发"


def test_stop_after_the_guided_message_went_in_keeps_it(client, rr):
    """插的话已经放进去了，接着调模型时点了停止：插的话留在停下的这一轮里，不再发一遍。"""
    tid = new(client, "直接答：先说一句")["thread_id"]
    first, second = _gate(rr.planner, 1), _gate(rr.planner, 2)
    with _Running(client, rr, tid, "搜一下猫", second) as run:
        assert first.waiting.wait(10)
        _queue(client, tid, "只看橘猫")
        _guide(client, tid)
        first.set()
        assert second.waiting.wait(10)
        assert state(client, tid)["guided_message"] is None  # 已经取走放进对话了
        assert client.post(f"/api/research/threads/{tid}/stop").status_code == 200
    end = last(run.evs, "state")
    assert end["status"] == "idle" and len(end["turns"]) == 2 and _kinds(client, tid) == ["say", "new"]
    assert end["turns"][1]["record"]["outcome"] == "stopped" and _types(end["turns"][1])[:3] == ["thinking", "search", "guide"]
    assert end["queued_message"] is None and end["guided_message"] is None


def test_failed_turn_keeps_the_guided_message_for_retry(tmp_path):
    """插的话放进去以后这一轮出错：插的话留在这一轮里，重试接着跑时模型看得到。"""
    armed = {"on": False}

    class Failing(FakeToolModel):
        def _message(self, messages):
            if armed["on"] and any(history.is_guide(m) for m in messages):
                armed["on"] = False
                raise RuntimeError("假模型：网络断了")
            return super()._message(messages)

    p = FakeResearchPlanner()
    p._model = Failing()
    rr = ResearchRunner(planner=p, data_dir=tmp_path / "data")
    http = _client(rr)
    tid = new(http, "直接答：先说一句")["thread_id"]
    armed["on"] = True
    gate = _gate(p)
    with _Running(http, rr, tid, "搜一下猫", gate) as run:
        assert gate.waiting.wait(10)
        _queue(http, tid, "只看橘猫")
        _guide(http, tid)
    failed = last(run.evs, "state")
    assert failed["status"] == "failed" and failed["guided_message"] is None and failed["queued_message"] is None
    assert "guide" in _types(failed["turns"][1])
    end = last(events(http.post(f"/api/research/threads/{tid}/retry")), "state")
    assert end["status"] == "idle" and _reply(end["turns"][1]).startswith("收到：只看橘猫。")
    assert _types(end["turns"][1]).count("guide") == 1


def test_blocked_guided_message_stops_the_turn_as_input(client, rr):
    """插进来的话没通过内容审核：去掉查到的资料也还拦，这一轮按“用户的话没通过”停下，之后不带进上下文。"""
    tid = new(client, "直接答：先说一句")["thread_id"]
    gate = _gate(rr.planner)
    with _Running(client, rr, tid, "搜一下猫", gate):
        assert gate.waiting.wait(10)
        _queue(client, tid, f"只看{FAKE_BLOCKED}")
        _guide(client, tid)
    end = _settled(client, tid, 2)
    turn = end["turns"][1]
    assert turn["record"]["outcome"] == "blocked" and turn["items"][-1] == {**turn["items"][-1], "type": "blocked", "reason": "input"}
    nxt = last(events(say(client, tid, "直接答：接着说")), "state")
    assert nxt["turns"][2]["record"]["outcome"] == "ok"


def test_withdrawn_guided_message_is_not_sent(client, rr):
    tid = new(client, "直接答：先说一句")["thread_id"]
    gate = _gate(rr.planner)
    with _Running(client, rr, tid, "直接答：第二句", gate):
        assert gate.waiting.wait(10)
        _queue(client, tid, "直接答：算了")
        _guide(client, tid)
        assert _unguide(client, tid).json() == {"text": "直接答：算了"}
        assert _unguide(client, tid).json() == {"text": None}
    end = _settled(client, tid, 2)
    assert end["guided_message"] is None and "guide" not in _types(end["turns"][1]) and _kinds(client, tid) == ["say", "new"]


def test_guide_when_nothing_to_guide(client, rr):
    """没在跑：排着的话已经发出去了，没什么可插，返回 null。在跑但没排着话：返回等着插的那句（没有是 null）。"""
    assert _guide(client, "nope0000").status_code == 404
    assert _unguide(client, "nope0000").status_code == 404
    tid = new(client, "直接答：先说一句")["thread_id"]
    assert _guide(client, tid).json() == {"guided": None}
    gate = _gate(rr.planner)
    with _Running(client, rr, tid, "直接答：第二句", gate):
        assert gate.waiting.wait(10)
        assert _guide(client, tid).json() == {"guided": None}
        rr.run_store.put_pending(tid, GUIDE, "已经等着的")
        assert _guide(client, tid).json()["guided"]["text"] == "已经等着的"
        _unguide(client, tid)
    _settled(client, tid, 2)
