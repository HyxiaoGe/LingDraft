"""以前的检查点不留：每次执行结束把这段对话清到只剩最新的一个（见 graph.keep_latest、ResearchRunner._akeep_latest），
服务启动时把攒着的都清一遍（_atidy_checkpoints）。这里看的是：清完只剩一个、看到的对话没变、停在半路的（等回答、出错等重试）照样接得上。
只有 Postgres 清；设了 LINGDRAFT_TEST_DATABASE_URL 时整套测试都在“每次执行结束就清”下面跑，别的测试走到的各条路也就顺带验了。只用假模型。"""

from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import aio, config
from app.api import main as api_main
from app.api import research_runner
from app.api.research_runner import ResearchRunner
from app.api.run_store import ASKED
from app.research_llm import FakeResearchPlanner
from app.tools.gateway import GatewayAuthError
from tests.auth_helpers import USER
from tests.research_helpers import events, last, new, say, state

postgres = pytest.mark.skipif(not config.DATABASE_URL, reason="要 Postgres（设 LINGDRAFT_TEST_DATABASE_URL）：内存的检查点不清")
SHOWN = ("status", "title", "turns", "ask", "work")  # 页面上看到的这段对话


def _client(runner: ResearchRunner) -> TestClient:
    api_main.set_research_runner(runner)
    return TestClient(api_main.app, headers=USER)


def _kept(tid: str) -> tuple[int, int]:
    """库里这段对话留着几个检查点、几份 messages（每个版本一份整段对话）。"""
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as c:
        cps = c.execute("SELECT count(*) FROM checkpoints WHERE thread_id = %s", (tid,)).fetchone()[0]
        msgs = c.execute("SELECT count(*) FROM checkpoint_blobs WHERE thread_id = %s AND channel = 'messages'", (tid,)).fetchone()[0]
    return cps, msgs


def _shown(http: TestClient, tid: str) -> dict:
    st = state(http, tid)
    return {k: st[k] for k in SHOWN}


@postgres
def test_each_run_leaves_only_the_latest_checkpoint():
    """聊了几轮，库里这段对话一直只有一个检查点、一份 messages；服务重启以后对话还是那样，能接着说。"""
    http = _client(ResearchRunner(fake=True))
    tid = new(http, "直接答：甲")["thread_id"]
    assert _kept(tid) == (1, 1)
    for x in "乙丙丁":
        last(events(say(http, tid, f"直接答：{x}")), "state")
        assert _kept(tid) == (1, 1)
    before = _shown(http, tid)
    assert len(before["turns"]) == 4 and before["status"] == "idle"

    http = _client(ResearchRunner(fake=True))  # 像服务重启
    assert _shown(http, tid) == before
    end = last(events(say(http, tid, "直接答：戊")), "state")
    assert end["status"] == "idle" and len(end["turns"]) == 5 and _kept(tid) == (1, 1)


@postgres
def test_piled_up_checkpoints_are_tidied_at_startup(monkeypatch):
    """不清的话每轮多出几个检查点、几份整段对话（加这一步以前就是这样）。启动时清一遍：每段只剩一个，对话没变；再清一遍没有要清的。"""
    rr = ResearchRunner(fake=True)
    http = _client(rr)

    async def not_now(self, thread_id):
        return False
    with monkeypatch.context() as m:
        m.setattr(ResearchRunner, "_akeep_latest", not_now)
        a = new(http, "直接答：甲")["thread_id"]
        for x in "乙丙":
            last(events(say(http, a, f"直接答：{x}")), "state")
        b = new(http, "直接答：另一段")["thread_id"]
    assert _kept(a)[0] > 3 and _kept(a)[1] > 3 and _kept(b)[0] > 1
    before = {t: _shown(http, t) for t in (a, b)}

    assert aio.run(rr._atidy_checkpoints()) == 2
    assert _kept(a) == (1, 1) and _kept(b) == (1, 1)
    assert {t: _shown(http, t) for t in (a, b)} == before
    assert aio.run(rr._atidy_checkpoints()) == 0
    assert last(events(say(http, a, "直接答：丁")), "state")["status"] == "idle"


@postgres
def test_a_turn_stopped_halfway_goes_on_from_the_one_checkpoint_left():
    """同一条消息里又搜又问，搜索头一次出错：这一轮没跑完，清完只剩这一个检查点。重试 → 问题问出来（又清一次）→ 回答 → 答完，
    回答以后不重搜：做成了的搜索的结果挂在留下的那个检查点上，没被清掉。"""
    p = FakeResearchPlanner()
    aio.run(p.connect_tools())
    search, calls = p._mcp.web[0].tool, []

    async def down_once(**kw):
        calls.append(kw)
        if len(calls) == 1:
            raise GatewayAuthError("调 /search/mcp 失败：HTTP 401", kind="http_401")
        return await search.coroutine(**kw)
    p._mcp.web[0].tool = search.model_copy(update={"coroutine": down_once})
    http = _client(ResearchRunner(planner=p))
    st = new(http, "边搜边问", work=True)
    tid = st["thread_id"]
    assert st["status"] == "failed" and _kept(tid)[0] == 1
    st = last(events(http.post(f"/api/research/threads/{tid}/retry")), "state")
    assert st["status"] == "asking" and st["ask"] and len(calls) == 2 and _kept(tid)[0] == 1

    http = _client(ResearchRunner(planner=p))  # 等回答的时候服务重启了：问题还在
    assert state(http, tid)["ask"] == st["ask"]
    answer = {"id": st["ask"]["id"], "text": "选型", "skip": False}
    end = last(events(http.post(f"/api/research/threads/{tid}/answer", json=answer)), "state")
    assert end["status"] == "idle" and len(calls) == 2 and _kept(tid) == (1, 1)
    runs = [(r["kind"], r["status"]) for r in reversed(http.get(f"/api/research/threads/{tid}/runs").json()["runs"])]
    assert runs == [("new", "error"), ("retry", ASKED), ("answer", "success")]


def test_a_failed_tidy_does_not_break_the_turn(monkeypatch):
    """清不成（库断了之类）只打日志：这一轮照常答完、事件流照常结束，下一轮照常跑。"""
    async def broken(checkpointer, thread_id):
        raise RuntimeError("库断了")
    monkeypatch.setattr(research_runner, "keep_latest", broken)
    rr = ResearchRunner(fake=True)
    http = _client(rr)
    st = new(http, "直接答：甲")
    tid = st["thread_id"]
    assert st["status"] == "idle" and aio.run(rr._akeep_latest(tid)) is False
    assert last(events(say(http, tid, "直接答：乙")), "state")["status"] == "idle"
    assert len(state(http, tid)["turns"]) == 2


@pytest.mark.skipif(bool(config.DATABASE_URL), reason="看的是内存的检查点")
def test_checkpoints_in_memory_are_left_alone():
    """内存的检查点不清：评测录像靠每一步的检查点回放（见 app.evals.decisions）。"""
    rr = ResearchRunner(fake=True)
    http = _client(rr)
    tid = new(http, "直接答：甲")["thread_id"]
    last(events(say(http, tid, "直接答：乙")), "state")
    n = len(rr.graph.checkpointer.storage[tid][""])
    assert n > 1 and aio.run(rr._atidy_checkpoints()) == 0
    assert len(rr.graph.checkpointer.storage[tid][""]) == n
