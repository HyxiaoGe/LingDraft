"""额度（见 app.quota）：北京时间的日、周窗口，游客自己的和所有游客合计的，出错的轮不算，重置，接口的 429 和 /api/credits。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import aio, config, costs
from app.api import main as api_main
from app.api.research_runner import ResearchRunner
from app.api.run_store import ERROR, SUCCESS, MemoryRunStore
from app.identity import ADMIN, GUEST, USER, Identity
from app.quota import CHINA_TZ, DAY, GUESTS, WEEK, Exhausted, Limits, MemoryUsage, PostgresUsage, Quota, asks
from tests.auth_helpers import bearer, guest_token, user_token
from tests.research_helpers import events, last

WED = datetime(2026, 11, 4, 15, 0, tzinfo=CHINA_TZ)  # 周三下午
LIMITS = Limits(guest_daily=10, user_daily=100, user_weekly=300, guests_daily=50)
U1 = Identity("u1", USER)


def row(owner: str, yuan: float | None, at: datetime = WED, purpose: str = "agent", turn_id: str = "turn1") -> dict:
    return {"owner": owner, "yuan": yuan, "purpose": purpose, "started_at": at, "turn_id": turn_id}


def make(runs: MemoryRunStore | None = None) -> tuple[Quota, MemoryUsage, list[datetime]]:
    clock = [WED]
    usage = MemoryUsage(runs)
    return Quota(usage, LIMITS, clock=lambda: clock[0]), usage, clock


def windows(q: Quota, who: Identity):
    return aio.run(q.windows(who))


def test_free_tier_calls_count_at_the_normal_price():
    """免费额度的调用账上是 0 元，额度按算给用户的（billed）扣；之前记的行没有 billed，按 yuan（2026-11-09 Sean 定，见 app.costs）。"""
    q, u, _ = make()
    u.put({**row("u1", 0.0), "billed": 0.03})
    u.put(row("u1", 0.0))  # 改之前的免费额度：没有 billed
    u.put({**row("u1", 0.01), "billed": 0.01})
    assert windows(q, U1)[0].used == 40


def test_windows_follow_the_beijing_day_and_week():
    q, u, _ = make()
    u.put(row("u1", 0.02))  # 今天：20 credits
    u.put(row("u1", 0.03, WED - timedelta(days=1)))  # 昨天：只算在这周里
    u.put(row("u1", 0.05, WED - timedelta(days=3)))  # 上周日：都不算
    u.put(row("u1", 0.01, purpose="title"))  # 起标题不算给用户
    u.put(row("u1", None))  # 不知道钱数的不算
    u.put(row("u2", 0.09))  # 别人的
    day, week = windows(q, U1)
    assert (day.kind, day.limit, day.used, day.remaining) == (DAY, 100, 20, 80)
    assert (week.kind, week.limit, week.used) == (WEEK, 300, 50)
    assert day.resets_at == datetime(2026, 11, 5, tzinfo=CHINA_TZ) and week.resets_at == datetime(2026, 11, 9, tzinfo=CHINA_TZ)
    u.put(row("root", 9.0))  # 管理员不限，但也看得到自己用了多少
    rd, rw = windows(q, Identity("root", ADMIN))
    assert (rd.kind, rd.limit, rd.used, rd.remaining, rd.exhausted) == (DAY, None, 9000, float("inf"), False) and (rw.kind, rw.limit) == (WEEK, None)
    aio.run(q.check(Identity("root", ADMIN)))
    assert q.view(U1)["windows"][0] == {"kind": DAY, "limit": 100, "used": 20, "resets_at": "2026-11-05T00:00:00+08:00"}
    assert "login" not in q.view(U1)  # 登录了的不用告诉他登录后会怎样
    assert q.view(Identity("root", ADMIN)) == {"unlimited": True, "warn_at": 0.8, "per_ask": 200, "windows": [
        {"kind": DAY, "limit": None, "used": 9000, "resets_at": "2026-11-05T00:00:00+08:00"},
        {"kind": WEEK, "limit": None, "used": 9000, "resets_at": "2026-11-09T00:00:00+08:00"}]}


def test_guests_have_their_own_day_and_share_a_pool():
    q, u, _ = make()
    g1, g2 = Identity("guest:a", GUEST), Identity("guest:b", GUEST)
    u.put(row("guest:a", 0.004))
    u.put(row("guest:b", 0.003))
    own, pool = windows(q, g1)
    assert (own.kind, own.used, own.limit) == (DAY, 4, 10) and (pool.kind, pool.used, pool.limit) == (GUESTS, 7, 50)
    aio.run(q.check(g1))  # 还没到
    u.put(row("guest:a", 0.006))
    with pytest.raises(Exhausted) as e:
        q.admit(g1)
    assert e.value.window.kind == DAY and e.value.guest and "登录后每天 100 额度，约可问 0 次" in str(e.value) and "credits" not in str(e.value)
    assert e.value.detail() == {"code": "credits_exhausted", "message": str(e.value), "guest": True, "per_ask": 200, "kind": DAY, "limit": 10,
                                "used": 10, "resets_at": "2026-11-05T00:00:00+08:00", "login": {"day": 100, "week": 300}}  # 登录后的按这里的 LIMITS
    assert q.view(g1)["login"] == {"day": 100, "week": 300} and q.view(g1)["per_ask"] == 200
    u.put(row("guest:c", 0.04))  # 所有游客合起来到 53 了：别的游客自己没用多少也进不来
    with pytest.raises(Exhausted) as e:
        q.admit(g2)
    assert e.value.window.kind == GUESTS and "所有游客合用的额度" in str(e.value) and e.value.detail()["login"] == {"day": 100, "week": 300}
    aio.run(q.check(U1))  # 登录用户不受游客总额影响
    u.put(row("u1", 0.1))
    with pytest.raises(Exhausted) as e:
        q.admit(U1)
    assert "login" not in e.value.detail() and "登录" not in str(e.value) and "共 100 额度" in str(e.value)


def test_failed_turns_are_not_charged_and_reset_starts_over():
    runs = MemoryRunStore()
    q, u, clock = make(runs)
    for rid, turn, status in (("r1", "turn1", ERROR), ("r2", "turn2", SUCCESS)):
        aio.run(runs.create(rid, "t1", "say", turn, None))
        aio.run(runs.finish(rid, status))
    u.put(row("u1", 0.05, turn_id="turn1"))
    u.put(row("u1", 0.07, turn_id="turn2"))
    assert windows(q, U1)[0].used == 70  # 出错的那一轮不算
    aio.run(runs.create("r3", "t1", "retry", "turn1", None))  # 重试成功了：这一轮最后一次执行成功，算
    aio.run(runs.finish("r3", SUCCESS))
    assert windows(q, U1)[0].used == pytest.approx(120)
    with pytest.raises(Exhausted):
        q.admit(U1)
    clock[0] += timedelta(minutes=1)
    q.reset("u1", "test")
    assert windows(q, U1)[0].used == 0 and windows(q, U1)[1].used == 0  # 重置之后重新数（日、周都是）
    clock[0] += timedelta(minutes=1)
    u.put(row("u1", 0.01, at=clock[0]))
    assert windows(q, U1)[0].used == 10
    aio.run(q.check(U1))


# ---- 接口 ----


def _new(client, text="直接答：你好") -> str:
    return last(events(client.post("/api/research/threads", json={"text": text})), "state")["thread_id"]


def test_api_refuses_to_start_a_turn_when_exhausted_and_reports_credits():
    api_main.set_research_runner(ResearchRunner(fake=True))
    r = api_main.research()
    r.quota.limits = Limits(10, 100, 300, 50)
    r.quota.usage = MemoryUsage()  # 这里测的是接口的行为，账直接放进内存的（Postgres 的算法见下面那个测试）
    g = guest_token()
    c = TestClient(api_main.app, headers=bearer(g))
    assert c.get("/api/credits").json() == {"unlimited": False, "warn_at": 0.8, "per_ask": 200, "login": {"day": 100, "week": 300}, "windows": [
        {"kind": DAY, "limit": 10, "used": 0, "resets_at": c.get("/api/credits").json()["windows"][0]["resets_at"]},
        {"kind": GUESTS, "limit": 50, "used": 0, "resets_at": c.get("/api/credits").json()["windows"][1]["resets_at"]}]}
    tid = _new(c)
    sub = api_main.authenticator().identify(g).sub
    r.quota.usage.put(row(sub, 0.01, at=costs.now()))  # 用满 10
    assert c.get("/api/credits").json()["windows"][0]["used"] == 10
    for call in (lambda: c.post("/api/research/threads", json={"text": "直接答：再来"}),
                 lambda: c.post(f"/api/research/threads/{tid}/messages", json={"text": "直接答：再说"}),
                 lambda: c.post(f"/api/research/threads/{tid}/queue", json={"text": "直接答：排着"}),
                 lambda: c.post(f"/api/research/threads/{tid}/regenerate"),
                 lambda: c.post(f"/api/research/threads/{tid}/edit", json={"text": "直接答：改了"}),
                 lambda: c.post(f"/api/research/threads/{tid}/retry")):
        resp = call()
        assert resp.status_code == 429, resp.text
        d = resp.json()["detail"]
        assert d["code"] == "credits_exhausted" and d["kind"] == DAY and d["guest"] and "登录后每天" in d["message"]
        assert d["per_ask"] == 200 and d["login"] == {"day": 100, "week": 300}
    assert c.get(f"/api/research/threads/{tid}/state").status_code == 200  # 看还是能看
    u = TestClient(api_main.app, headers=bearer(user_token("u1")))  # 登录用户不受影响
    assert [w["kind"] for w in u.get("/api/credits").json()["windows"]] == [DAY, WEEK]
    _new(u)
    root = TestClient(api_main.app, headers=bearer(user_token("root", admin=True)))
    v = root.get("/api/credits").json()
    assert v["unlimited"] and [(w["kind"], w["limit"], w["used"]) for w in v["windows"]] == [(DAY, None, 0), (WEEK, None, 0)]


def test_limits_come_from_config():
    assert Limits.from_config() == Limits(1000, 10000, 50000, 50000) == Limits(config.CREDITS_GUEST_DAILY, config.CREDITS_USER_DAILY,
                                                                                 config.CREDITS_USER_WEEKLY, config.CREDITS_GUESTS_DAILY)
    assert config.GUEST_PER_IP_DAILY == 5
    assert config.CREDITS_PER_ASK == 200


def test_asks_and_per_ask_follow_config(monkeypatch):
    assert (asks(1000, 200), asks(10000, 200), asks(199.9, 200), asks(0, 200), asks(500, 0)) == (5, 50, 0, 0, 0)
    q, _, _ = make()
    monkeypatch.setattr(config, "CREDITS_PER_ASK", 120)  # 环境变量改了：接口和 429 都跟着
    assert q.view(U1)["per_ask"] == 120
    g = Identity("guest:a", GUEST)
    q.usage.put(row("guest:a", 0.01))
    with pytest.raises(Exhausted) as e:
        q.admit(g)
    assert e.value.detail()["per_ask"] == 120 and "约可问 0 次" in str(e.value)  # LIMITS 里登录后每天 100：不到一次


# ---- Postgres ----


def _full(owner: str, yuan: float | None, turn_id: str, at: datetime, purpose: str = "agent") -> dict:
    return {**dict.fromkeys(costs._COLUMNS), "started_at": at, "provider": "deepseek", "model": "deepseek-flash", "purpose": purpose,
            "thread_id": "t1", "turn_id": turn_id, "owner": owner, "status": "ok", "yuan": yuan}


@pytest.mark.skipif(not config.DATABASE_URL, reason="要 Postgres（设 LINGDRAFT_TEST_DATABASE_URL）")
def test_postgres_usage_counts_like_the_memory_one():
    from app.api.research_runner import open_storage

    _, _, runs = aio.run(open_storage(config.DATABASE_URL))
    usage = PostgresUsage(runs._exec)
    aio.run(usage.setup())
    aio.run(usage.setup())  # 再跑一遍也没事
    now = costs.now()
    for rid, turn, status in (("r1", "turn1", ERROR), ("r2", "turn2", SUCCESS), ("r3", "turn1", SUCCESS)):
        aio.run(runs.create(rid, "t1", "say", turn, None))
        aio.run(runs.finish(rid, status))
    aio.run(runs.create("r4", "t2", "say", "turn3", None))
    aio.run(runs.finish("r4", ERROR))
    led = costs.PostgresLedger(config.DATABASE_URL)
    led([_full("u1", 0.05, "turn1", now), _full("u1", 0.07, "turn2", now), _full("u1", 0.5, "turn3", now),  # turn3 出错了不算
         _full("u1", 0.9, "turn2", now, purpose="title"), _full("u1", None, "turn2", now), _full("u1", 0.2, "turn2", now - timedelta(days=2)),
         _full("guest:a", 0.004, "turn9", now), _full("guest:b", 0.006, "turn9", now),
         {**_full("u1", 0.0, "turn2", now), "billed": 0.04}])  # 免费额度的：账上 0 元，算给用户的按正常价
    led._close()
    assert aio.run(usage.spent("u1", now - timedelta(hours=1))) == pytest.approx(0.16)
    assert aio.run(usage.spent("u1", now - timedelta(days=3))) == pytest.approx(0.36)
    assert aio.run(usage.spent_by_guests(now - timedelta(hours=1))) == pytest.approx(0.01)
    assert aio.run(usage.spent("nobody", now - timedelta(days=3))) == 0
    assert aio.run(usage.last_reset("u1")) is None
    later = now + timedelta(seconds=1)  # 重置在记账之后
    aio.run(usage.reset("u1", "test", later))
    assert aio.run(usage.last_reset("u1")) == later
    q = Quota(usage, LIMITS, clock=lambda: now + timedelta(minutes=1))
    assert windows(q, U1)[0].used == 0  # 重置以后：重置之前的都不算了
