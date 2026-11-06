"""身份：游客令牌、auth 服务的 access token 验签和吊销、接口认人、对话按人分、游客登录后并进账号、记忆按人分。只用假模型和假 auth 的钥匙。"""

from __future__ import annotations

import time

import pytest
import redis
from fastapi.testclient import TestClient

from app import config
from app.api import main as api_main
from app.api.research_runner import ResearchRunner
from app.identity import ADMIN, GUEST, USER, AuthUnavailable, BadToken, GuestTokens, Identity, Revocations, is_guest_sub
from tests import auth_helpers as ah
from tests.auth_helpers import bearer, guest_token, user_token
from app.research_llm import FakeResearchPlanner, FakeToolModel
from tests.research_helpers import events, last
from tests.test_research import Chat


def _client(**headers) -> TestClient:
    api_main.set_research_runner(ResearchRunner(fake=True))
    return TestClient(api_main.app, headers=headers)


def _new(client, text="直接答：你好", **kw) -> str:
    return last(events(client.post("/api/research/threads", json={"text": text, **kw})), "state")["thread_id"]


def _say(client, tid: str, text: str, headers: dict):
    return client.post(f"/api/research/threads/{tid}/messages", json={"text": text}, headers=headers)


# ---- 令牌 ----


def test_guest_tokens_round_trip_and_daily_cap_per_ip():
    clock = [float(int(time.time()))]
    g = GuestTokens("s" * 32, per_ip_daily=2, clock=lambda: clock[0])
    token, exp = g.issue("1.2.3.4")
    who = g.verify(token)
    assert who.tier == GUEST and who.guest and is_guest_sub(who.sub) and exp == int(clock[0]) + 365 * 86400
    assert g.issue("1.2.3.4") is not None and g.issue("1.2.3.4") is None  # 一天两个
    assert g.issue("5.6.7.8") is not None  # 别的地址不受影响
    clock[0] += 86400
    assert g.issue("1.2.3.4") is not None  # 第二天重来
    with pytest.raises(BadToken):
        GuestTokens("o" * 32, 10).verify(token)  # 密钥不对
    with pytest.raises(BadToken):
        g.verify(user_token())  # 登录的令牌不是游客令牌


def test_access_tokens_are_checked_against_the_auth_contract():
    a = ah.make_authenticator()
    assert a.identify(user_token("u1")) == Identity("u1", USER, "u1@test")
    assert a.identify(user_token("root", admin=True)).tier == ADMIN
    assert a.identify(guest_token()).tier == GUEST
    for bad in (user_token(aud="app_other"), user_token(iss="https://evil.test"), user_token(type="refresh"), user_token(ttl=-100),
                user_token()[:-3] + "xxx", "not-a-jwt", ""):
        with pytest.raises(BadToken):
            a.identify(bad)
    with pytest.raises(BadToken, match="没有开登录"):
        ah.make_authenticator(login=False).identify(user_token())


def test_revocation_marks_from_the_shared_redis():
    class FakeRedis:
        def __init__(self, data):
            self.data = data

        def get(self, k):
            return self.data.get(k)

    rv = Revocations("redis://unused:1/0")
    now = time.time()
    rv._redis = FakeRedis({"revoked_sid:gone": "1", "revoked_user:u2": str(now + 60)})
    assert rv.revoked({"sid": "gone", "sub": "u1", "iat": now})
    assert rv.revoked({"sid": "ok", "sub": "u2", "iat": now})  # 登出之前签的
    assert not rv.revoked({"sid": "ok", "sub": "u2", "iat": now + 120})  # 登出之后重新登录签的
    assert not rv.revoked({"sid": "ok", "sub": "u1", "iat": now})
    assert not rv.revoked({"sid": "bad sid!", "sub": "u1", "iat": now})  # 格式不对的 sid 不拿去查
    a = ah.make_authenticator(revocations=rv)
    with pytest.raises(BadToken, match="退出"):
        a.identify(user_token("u2"))
    assert a.identify(user_token("u1")).sub == "u1"

    class Broken:
        def get(self, k):
            raise redis.ConnectionError("down")
    rv._redis = Broken()
    assert not rv.revoked({"sid": "gone", "sub": "u2", "iat": now})  # Redis 不通：放行


def test_jwks_unreachable_is_unavailable_not_unauthorized():
    a = ah.make_authenticator()
    a.validator._jwks_cache = None  # 缓存没了，要去取，地址不通
    with pytest.raises(AuthUnavailable):
        a.identify(user_token())


# ---- 接口 ----


def test_requests_need_a_token_and_can_get_a_guest_one():
    c = _client()
    assert c.get("/api/research/threads").status_code == 401
    assert c.get("/api/models").status_code == 401
    assert c.get("/api/memory").status_code == 401
    assert c.get("/api/research/threads", headers=bearer("junk")).status_code == 401
    assert c.get("/api/research/threads", headers=bearer(user_token(ttl=-10))).status_code == 401
    assert c.get("/api/health").status_code == 200  # 健康检查不用
    got = c.post("/api/auth/guest").json()
    assert set(got) == {"token", "expires_at"}
    me = c.get("/api/auth/me", headers=bearer(got["token"])).json()
    assert me["guest"] and me["tier"] == "guest" and me["sub"].startswith("guest:") and me["email"] == ""
    me = c.get("/api/auth/me", headers=bearer(user_token("root", admin=True))).json()
    assert me == {"sub": "root", "tier": "admin", "email": "root@test", "guest": False}


def test_guest_issue_cap_is_429():
    api_main.set_authenticator(ah.Authenticator(GuestTokens("s" * 32, per_ip_daily=1), ah.validator()))
    c = _client()
    assert c.post("/api/auth/guest").status_code == 200
    r = c.post("/api/auth/guest")
    assert r.status_code == 429
    assert r.json()["detail"] == {"code": "guests_per_ip", "message": "这个网络今天新领的游客身份太多了，请登录或明天再来", "per_ip": 1,
                                  "per_ask": 200, "login": {"day": 10000, "week": 50000}}  # 页面没有身份，靠这些说清为什么、登录后会怎样
    assert c.post("/api/auth/guest", headers={"X-Forwarded-For": "9.9.9.9, 10.0.0.1"}).status_code == 200  # 经代理来的按原地址数


def test_auth_config_tells_the_page_whether_login_is_on(monkeypatch):
    c = _client()
    monkeypatch.setattr(config, "AUTH_CLIENT_ID", "app_x")
    assert c.get("/api/auth/config").json() == {"login": True, "auth_url": config.AUTH_URL, "client_id": "app_x"}
    api_main.set_authenticator(ah.make_authenticator(login=False))
    assert c.get("/api/auth/config").json() == {"login": False, "auth_url": None, "client_id": None}


def test_threads_belong_to_whoever_started_them():
    """每个人只看到、只能动自己的对话；别人的一律 404（不透露有没有）。没归谁的老对话谁都看不到，管理员也不行。"""
    c = _client()
    u1, u2, root, guest = bearer(user_token("u1")), bearer(user_token("u2")), bearer(user_token("root", admin=True)), bearer(guest_token())
    c.headers.update(u1)
    mine = _new(c)
    c.headers.update(guest)
    theirs = _new(c)
    r = api_main.research()
    r.accept("old", "直接答：老对话")  # 登录以前的：没归谁
    r.run_turn("old")

    def seen(h):
        return {t["thread_id"] for t in c.get("/api/research/threads", headers=h).json()["threads"]}
    assert seen(u1) == {mine} and seen(guest) == {theirs} and seen(u2) == set()
    assert seen(root) == set()  # 管理员也只看自己的：没归谁的老对话相当于归档
    for h, tid in ((u2, mine), (guest, mine), (u1, theirs), (root, mine), (u1, "old"), (root, "old")):
        assert c.get(f"/api/research/threads/{tid}/state", headers=h).status_code == 404
        assert _say(c, tid, "再说", h).status_code == 404
        assert c.delete(f"/api/research/threads/{tid}", headers=h).status_code == 404
        assert c.get(f"/api/research/threads/{tid}/events", headers=h).status_code == 404
    assert c.get(f"/api/research/threads/{mine}/state", headers=u1).status_code == 200
    assert _say(c, mine, "直接答：再说", u1).status_code == 200
    assert c.get("/api/research/runs", headers=u1).status_code == 403  # 跨所有人的运行记录只有管理员看
    assert c.get("/api/research/runs", headers=root).status_code == 200
    assert c.delete(f"/api/research/threads/{mine}", headers=u1).status_code == 200
    assert r.owner_of(mine) is None and seen(u1) == set()  # 删了连归谁也删


def test_logging_in_claims_the_guest_conversations():
    c = _client()
    gtoken = guest_token()
    c.headers.update(bearer(gtoken))
    a, b = _new(c), _new(c, "直接答：第二段")
    assert c.post(f"/api/research/threads/{a}/pin").json()["pinned_at"]  # 游客时置顶的，并进账号后还在
    c.headers.update(bearer(guest_token()))
    stranger = _new(c)  # 另一个游客的，不该被并走
    u1 = bearer(user_token("u1"))
    assert c.post("/api/auth/claim", json={"guest_token": gtoken}, headers=bearer(gtoken)).status_code == 403  # 游客自己不能并
    assert c.post("/api/auth/claim", json={"guest_token": "junk"}, headers=u1).status_code == 400
    assert c.post("/api/auth/claim", json={"guest_token": gtoken}, headers=u1).json() == {"moved": 2}
    assert {t["thread_id"]: bool(t["pinned_at"]) for t in c.get("/api/research/threads", headers=u1).json()["threads"]} == {a: True, b: False}
    assert c.get("/api/research/threads", headers=bearer(gtoken)).json()["threads"] == []
    assert c.get(f"/api/research/threads/{stranger}/state", headers=u1).status_code == 404
    assert c.post("/api/auth/claim", json={"guest_token": gtoken}, headers=u1).json() == {"moved": 0}  # 再并一次没东西了
    assert _say(c, a, "直接答：接着", u1).status_code == 200  # 并过来的能接着聊


def test_guests_pick_models_as_guests():
    c = _client(**bearer(guest_token()))
    locked = {m["id"]: m["locked"] for m in c.get("/api/models").json()["models"]}
    assert locked["deepseek-v4-pro"] and not locked["auto"]
    assert c.post("/api/research/threads", json={"text": "你好", "model": "deepseek-v4-pro"}).status_code == 403
    c.headers.update(bearer(user_token()))
    assert not any(m["locked"] for m in c.get("/api/models").json()["models"] if m["id"] == "deepseek-v4-pro")


# ---- 记忆按人分 ----


def test_memory_is_per_user_and_guests_have_none():
    """各人的对话整理进各人的记忆，提示词里只带自己的；游客的、没归谁的对话不整理，游客的记忆页是空的。"""
    seen: list[list] = []

    class Recording(FakeToolModel):
        def _message(self, messages):
            seen.append(messages)
            return super()._message(messages)

    p = FakeResearchPlanner()
    p._model = Recording()
    chat = Chat(planner=p, owner="u1")
    chat.say("直接答 记住：用户住在杭州")

    def chat_as(tid, owner):  # 同一个运行器（同一份检查点、记忆、归属），换个人说
        c = Chat(planner=p, tid=tid, owner=owner)
        c.r = chat.r
        return c
    other = chat_as("t2", "u2")
    other.say("直接答 记住：用户住在上海")
    g = chat_as("t3", "guest:abc")
    g.say("直接答 记住：游客")
    orphan = chat_as("t4", None)
    orphan.say("直接答 记住：没归谁")
    run = chat.r.dream()
    assert run["threads"] == 2 and run["added"] == 2
    assert [m["text"] for m in chat.r.memory.items("u1")] == ["用户住在杭州"]
    assert [m["text"] for m in chat.r.memory.items("u2")] == ["用户住在上海"]
    assert chat.r.memory.items("guest:abc") == [] and chat.r.memory.by_owner().keys() == {"u1", "u2"}

    chat.say("直接答 今天吃什么")
    assert "<memory>\n- 用户住在杭州\n</memory>" in seen[-1][0].content and "上海" not in seen[-1][0].content  # 只带自己的
    g.say("直接答 今天吃什么")
    assert "[What you remember about the user]" not in seen[-1][0].content  # 游客没有这一段
    orphan.say("直接答 今天吃什么")
    assert "[What you remember about the user]" not in seen[-1][0].content

    api_main.set_research_runner(chat.r)
    c = TestClient(api_main.app)
    assert [m["text"] for m in c.get("/api/memory", headers=bearer(user_token("u1"))).json()["memories"]] == ["用户住在杭州"]
    assert [m["text"] for m in c.get("/api/memory", headers=bearer(user_token("u2"))).json()["memories"]] == ["用户住在上海"]
    body = c.get("/api/memory", headers=bearer(guest_token())).json()
    assert body["memories"] == [] and body["last_dream"]["threads"] == 2
