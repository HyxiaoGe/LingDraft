"""Auto 选模型与免费额度（2026-11-08 Sean 定，见 docs/Auto选模型与免费额度方案.md）：第一句话判断难易定下模型、整段沿用；
简单的用免费额度（按到期日），复杂的用 Pro，游客复杂的用 Flash；出问题换备用 Flash（Auto 一直用它，自己选的只顶这一轮）。只用假模型。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import httpx2
import openai
import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGenerationChunk
from pydantic import ValidationError

from app import model_catalog
from app.graph.research import agent as agent_mod
from app.model_catalog import AUTO, CHINA_TZ, FREE_TIER, ZERO, Catalog, ModelUse
from app.model_guard import Breaker, FreeTierUsedUp, GuardedChatModel, Interrupted, ModelUnavailable
from app.research_llm import FAKE_SIMPLE, FailingModel, FakeResearchPlanner, FakeToolModel
from tests.research_helpers import run_async
from tests.test_model_guard import REQ, Scripted
from tests.test_research import Chat

PRO, FLASH = "deepseek:deepseek-v4-pro", "deepseek:deepseek-flash"
K3, V41, K31 = "dashscope:kimi-k3", "dashscope:deepseek-v4.1-flash-1017", "dashscope:kimi-k3.1-pro"
DOUBAO = "ark:doubao-seed-2-1-pro-260915"
GUEST = "guest:g1"
NOW = datetime(2026, 11, 8, 12, tzinfo=CHINA_TZ)


def _always(_ref: str) -> bool:
    return True


def _catalog(days: tuple[int, ...] = (9, 70, 86)) -> Catalog:
    """真的那份，三个免费额度的到期日改成今天往后这么多天（测试不随日子过去失效）。"""
    c = model_catalog.load()
    today = datetime.now(CHINA_TZ).date()
    free = [f.model_copy(update={"until": today + timedelta(days=d)}) for f, d in zip(c.free, days, strict=True)]
    return c.model_copy(update={"free": free})


def _planner(**kw) -> FakeResearchPlanner:
    return FakeResearchPlanner(catalog=_catalog(), **kw)


def _models(v: dict) -> list[str]:
    return [t["record"]["model"]["ref"] for t in v["turns"]]


def _routes(p: FakeResearchPlanner) -> list[str]:
    return [text for kind, text in p.calls if kind == "route"]


def _model_events(events: list[dict]) -> list[str]:
    return [e["data"]["ref"] for e in events if e["event"] == "model"]


# ---- 选哪个（Catalog.pick / plan） ----


def test_models_toml_free_pool_and_roles():
    """仓库里那份：三个百炼的免费额度按到期日排着；备用是 Flash（游客也能用）；判断难易、推荐问题都用 Flash。"""
    c = model_catalog.load()
    assert [f.model for f in sorted(c.free, key=lambda f: f.until)] == [K3, V41, K31]
    assert c.fallback().model == FLASH and c.fallback().allows("guest")
    assert c.roles["route"].model == FLASH and c.roles["suggest"].model == FLASH
    assert set(c.names()) == {K3, V41, K31} and c.names()[K3] == {"name": "Kimi K3", "icon": "moonshot"}


def test_catalog_rejects_free_pool_mistakes():
    good = model_catalog.load().model_dump()
    for broken, why in [
        ({**good, "auto": {"fallback": "nope"}}, "fallback"),
        ({**good, "auto": {"fallback": "deepseek-v4-pro"}}, "游客"),
        ({**good, "free": good["free"] + [good["free"][0]]}, "重复"),
        ({**good, "free": [*good["free"], {**good["free"][0], "model": PRO}]}, "免费"),
    ]:
        with pytest.raises(ValidationError, match=why):
            Catalog.model_validate(broken)


def test_simple_goes_to_the_soonest_free_model_complex_to_pro():
    """简单的：按到期日从早到晚挑第一个能用的（没到期、没用完、配了 key）；都不行用备用。复杂的用 Pro，游客用 Flash。"""
    c = _catalog((9, 70, 86))
    now = datetime.now(CHINA_TZ)
    assert c.pick(True, False, _always, now) == K3
    assert c.pick(True, True, _always, now) == K3  # 游客简单的也用免费的
    assert c.pick(False, False, _always, now) == PRO
    assert c.pick(False, True, _always, now) == FLASH  # 游客不给 Pro
    assert c.pick(True, False, _always, now + timedelta(days=9)) == V41  # 到期日当天北京时间 0 点起不用
    assert c.pick(True, False, _always, now + timedelta(days=8)) == K3
    assert c.pick(True, False, lambda ref: ref != K3, now) == V41  # 没配 key
    c.used_up(V41)
    assert c.pick(True, False, lambda ref: ref != K3, now) == K31
    assert c.pick(True, False, lambda ref: not ref.startswith("dashscope:"), now) == FLASH


def test_plan_follows_the_pick_and_drops_expired_or_used_up():
    """Auto 的对话按定下的模型：免费的子 agent 用 Flash；到期了、用完了的改用备用（还是 Auto 这个选项）。"""
    c = _catalog((9, 70, 86))
    now = datetime.now(CHINA_TZ)
    plan = c.plan(AUTO, None, _always, pick=K3, now=now)
    assert plan.option == AUTO and plan.agent.ref == K3 and plan.subagent == ModelUse(ref=FLASH, thinking=True, effort="high")
    assert c.plan(AUTO, False, _always, pick=K3, now=now).agent == ModelUse(ref=K3, thinking=False)
    assert c.plan(AUTO, None, _always, pick=PRO, now=now).agent.ref == PRO
    assert c.plan(AUTO, None, _always, pick=FLASH, now=now).agent == ModelUse(ref=FLASH, thinking=True, effort="high")
    late = c.plan(AUTO, None, _always, pick=K3, now=now + timedelta(days=9))
    assert late.option == AUTO and late.agent.ref == FLASH
    c.used_up(K31)
    assert c.plan(AUTO, None, _always, pick=K31, now=now).agent.ref == FLASH
    assert c.plan(AUTO, None, _always, pick="dashscope:gone", now=now).agent.ref == FLASH  # models.toml 里删了
    assert c.plan("doubao-seed-2-1-pro", None, _always, pick=K3, now=now).agent.ref == DOUBAO  # 自己选的不看 pick


def test_free_calls_cost_nothing():
    c = _catalog()
    assert c.rate(K3, NOW, 100_000) == (FREE_TIER, ZERO)
    assert c.rate(PRO, NOW, 100)[0] != FREE_TIER


# ---- 出错的分类（model_guard） ----


def _free_tier_error() -> openai.APIStatusError:
    """百炼免费额度用完、开了“用完即停”时的回复（按文档，403）。"""
    body = {"code": "AllocationQuota.FreeTierOnly", "message": "The free tier of the model has been exhausted."}
    return openai.PermissionDeniedError("Error code: 403", response=httpx2.Response(403, request=REQ), body=body)


class Guarded(GuardedChatModel, Scripted):
    first_token: float | None = None

    def _first_token_s(self) -> float | None:
        return self.first_token


@pytest.fixture
def breaker(monkeypatch) -> Breaker:
    b = Breaker(failures=2, cooldown_s=180, clock=lambda: 1000.0)
    monkeypatch.setattr(GuardedChatModel, "breaker", b)
    return b


def test_free_tier_used_up_is_its_own_error_and_not_a_failure(breaker):
    """免费额度用完：单独一种错（调用方换备用、记下用完了），不算进熔断（原来 403 会当成密钥无效）。"""
    m = Guarded(script=[_free_tier_error(), _free_tier_error(), _free_tier_error()])
    for _ in range(3):
        with pytest.raises(FreeTierUsedUp, match="免费额度用完了"):
            m.invoke("hi")
    assert len(m.calls) == 3  # 没断开：每次都发了


class _SlowRaw(Scripted):
    """流式：先等 wait 秒（empty_every 给了就每隔这么久发一个空片段），再吐正文。"""

    wait: float = 0
    empty_every: float | None = None

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append(1)
        waited = 0.0
        while waited < self.wait:
            step = self.empty_every or self.wait
            await asyncio.sleep(step)
            waited += step
            if self.empty_every:
                yield ChatGenerationChunk(message=AIMessageChunk(content=""))
        for part in ("好", "的"):
            yield ChatGenerationChunk(message=AIMessageChunk(content=part))
            await asyncio.sleep(0.05)


class Slow(GuardedChatModel, _SlowRaw):
    first_token: float | None = None

    def _first_token_s(self) -> float | None:
        return self.first_token


def test_first_token_timeout_is_unavailable_not_interrupted(breaker):
    """首字超时：多久没收到第一个有内容的片段（空片段不算）就当出问题，ModelUnavailable 而不是 Interrupted
    （不再同一个模型重调，直接换备用）；算进熔断。收到第一个字以后不再管（之后片段慢照样等）。"""

    async def go():
        m = Slow(first_token=0.05, wait=0.3)
        with pytest.raises(ModelUnavailable, match="首字超时") as e:
            [c async for c in m.astream("hi")]
        assert type(e.value) is ModelUnavailable and not e.value.streamed
        m = Slow(first_token=0.05, wait=0.3, empty_every=0.01)
        with pytest.raises(ModelUnavailable, match="首字超时") as e:
            [c async for c in m.astream("hi")]
        assert not isinstance(e.value, Interrupted)
        with pytest.raises(ModelUnavailable, match="约 3 分钟后再试"):  # 连着第二次：断开
            [c async for c in Slow(first_token=0.05).astream("hi")]

    run_async(go())


def test_first_token_in_time_streams_normally(breaker):
    async def go():
        got = [c.content async for c in Slow(first_token=0.2, wait=0.05).astream("hi")]  # 后面的片段隔 0.05 秒，总共超过也不算
        assert "".join(got) == "好的"
        got = [c.content async for c in Slow(first_token=None, wait=0.1).astream("hi")]  # 不限
        assert "".join(got) == "好的"

    run_async(go())


# ---- 经过 agent：第一句话判断、整段沿用 ----


def test_auto_routes_the_first_message_once_and_keeps_the_model():
    """Auto 的新对话：第一句话判断为简单，用最早到期的免费模型；之后问复杂的也不换，也不再判断。回答开头先收到用的哪个模型。"""
    p = _planner()
    c = Chat(planner=p)
    v = c.say(f"直接答：{FAKE_SIMPLE}，你好")
    assert _model_events(c.events) == [K3]
    v = c.say("直接答：比较一下三家云厂商的数据库方案")
    assert _models(v) == [K3, K3] and _routes(p) == [f"直接答：{FAKE_SIMPLE}，你好"]
    assert v["model"] == AUTO and v["model_ref"] is None  # 没在跑：页面按每轮的记录
    rec = v["turns"][0]["record"]
    assert rec["option"] == AUTO and rec["subagent_model"]["ref"] == FLASH
    assert c.values()["auto_model"] == K3


def test_complex_first_message_goes_to_pro_and_guests_to_flash():
    p = _planner()
    v = Chat(planner=p).say("直接答：比较一下三家云厂商的数据库方案")
    assert _models(v) == [PRO]
    p = _planner()
    v = Chat(planner=p, owner=GUEST).say("直接答：比较一下三家云厂商的数据库方案")
    assert _models(v) == [FLASH]
    v = Chat(planner=p, owner=GUEST, tid="t2").say(f"直接答：{FAKE_SIMPLE}")
    assert _models(v) == [K3]


def test_routing_failure_counts_as_complex():
    p = _planner()
    p.fail_route = True
    c = Chat(planner=p)
    v = c.say(f"直接答：{FAKE_SIMPLE}")
    assert _models(v) == [PRO] and c.values()["auto_model"] == PRO


def test_routing_timeout_counts_as_complex(monkeypatch):
    monkeypatch.setattr(agent_mod, "ROUTE_WAIT_S", 0.05)
    p = _planner()

    async def slow(text: str) -> bool:
        await asyncio.sleep(1)
        return True

    p.route = slow  # type: ignore[method-assign]
    assert _models(Chat(planner=p).say(f"直接答：{FAKE_SIMPLE}")) == [PRO]


def test_chosen_model_and_old_conversations_are_not_routed():
    """自己选了模型的不判断；没定下 Auto 模型的老对话（已经有轮次）也不判断，照原来的 Pro。"""
    p = _planner()
    c = Chat(planner=p)
    c.r.accept(c.tid, f"直接答：{FAKE_SIMPLE}", owner=c.owner, option="doubao-seed-2-1-pro")
    v = c.run()
    assert _models(v) == [DOUBAO] and not _routes(p)
    c = Chat(planner=p, tid="t2")
    c.say(f"直接答：{FAKE_SIMPLE}")
    from app import aio

    aio.run(c.r.graph.aupdate_state(c.r._cfg(c.tid), {"auto_model": ""}, as_node="TurnMiddleware.after_agent"))
    v = c.say(f"直接答：{FAKE_SIMPLE}")
    assert _models(v) == [K3, PRO] and len(_routes(p)) == 1


def test_expired_free_model_switches_the_conversation_to_flash():
    """定下的免费模型到期了：下一轮起用备用，之后都是备用（日子再挪回来也不换回去）。"""
    p = _planner()
    c = Chat(planner=p)
    c.say(f"直接答：{FAKE_SIMPLE}")
    p.clock.shift = timedelta(days=9)
    v = c.say("直接答：接着说")
    p.clock.shift = timedelta()
    v = c.say("直接答：再说")
    assert _models(v) == [K3, FLASH, FLASH] and c.values()["auto_model"] == FLASH


# ---- 出问题换备用 ----


def test_broken_auto_model_switches_to_flash_for_good():
    """Auto 定下的模型不可用：这一次换 Flash 重调，这一轮记成 Flash，之后整段对话都用 Flash（好了也不换回去）。"""
    p = _planner()
    p.broken[K3] = FailingModel(error=ModelUnavailable("模型服务暂时不可用（服务端出错），稍后再试"))
    c = Chat(planner=p)
    v = c.say(f"直接答：{FAKE_SIMPLE}")
    assert v["status"] == "idle" and _models(v) == [FLASH] and _model_events(c.events) == [K3, FLASH]
    assert not [e for e in c.events if e["event"] == "restart"]  # 没流出东西：页面没什么要去掉的
    p.broken.clear()
    v = c.say("直接答：接着说")
    assert _models(v) == [FLASH, FLASH] and c.values()["auto_model"] == FLASH


def test_free_tier_used_up_marks_the_model_for_new_conversations():
    """免费额度用完：这段对话换 Flash；之后新对话的简单问题跳过它，用下一个免费的。"""
    p = _planner()
    p.broken[K3] = FailingModel(error=FreeTierUsedUp("模型服务暂时不可用（免费额度用完了），稍后再试"))
    assert _models(Chat(planner=p).say(f"直接答：{FAKE_SIMPLE}")) == [FLASH]
    assert _models(Chat(planner=p, tid="t2").say(f"直接答：{FAKE_SIMPLE}")) == [V41]


def test_breaking_halfway_drops_the_half_then_switches():
    """流到一半断了：同一个模型先再调一次（InterruptedRetry），还断再换 Flash；两次的半截页面都去掉。"""
    p = _planner()
    p.broken[K3] = FailingModel(error=Interrupted("模型服务暂时不可用（连接失败），稍后再试", streamed=True), chunks=2)
    c = Chat(planner=p)
    v = c.say(f"直接答：{FAKE_SIMPLE}")
    restarts = [e["data"]["id"] for e in c.events if e["event"] == "restart"]
    assert _models(v) == [FLASH] and restarts == ["failing-msg", "failing-msg"]  # 两次都流出了半截（假模型两次的 id 一样）
    assert v["turns"][0]["items"][-1]["text"] == "直接回答：这个问题不用查。"


def test_chosen_model_gets_flash_only_for_that_turn():
    """自己选的模型出问题：这一轮用 Flash，下一轮还是他选的。"""
    p = _planner()
    p.broken[DOUBAO] = FailingModel(error=ModelUnavailable("模型服务暂时不可用（服务端出错），稍后再试"))
    c = Chat(planner=p)
    c.r.accept(c.tid, "直接答：你好", owner=c.owner, option="doubao-seed-2-1-pro")
    v = c.run()
    assert _models(v) == [FLASH] and v["turns"][0]["record"]["option"] == "doubao-seed-2-1-pro"
    assert "auto_model" not in c.values()
    p.broken.clear()
    v = c.say("直接答：接着说")
    assert _models(v) == [FLASH, DOUBAO] and v["model"] == "doubao-seed-2-1-pro"


def test_flash_itself_broken_fails_like_before():
    """已经是备用（Flash）出问题：不再换，照常失败。DeepSeek 自己出故障先不管（2026-11-08 Sean 定）。"""
    p = _planner()
    p.broken[FLASH] = FailingModel(error=ModelUnavailable("模型服务暂时不可用（服务端出错），稍后再试"))
    c = Chat(planner=p, owner=GUEST)
    with pytest.raises(ModelUnavailable):
        c.say("直接答：比较一下三家云厂商的数据库方案")
    assert c.view()["status"] == "failed"


class BareToolCalls(FakeToolModel):
    """百炼上托管的模型带工具时不给思考：工具调用不带 reasoning_content；工具结果回来以后出错。"""

    def _message(self, messages: list[BaseMessage]) -> AIMessage:
        if isinstance(messages[-1], ToolMessage):
            raise ModelUnavailable("模型服务暂时不可用（服务端出错），稍后再试")
        m = super()._message(messages)
        return m.model_copy(update={"additional_kwargs": {"reasoning_content": ""}})


def test_switching_after_bare_tool_calls_turns_thinking_off_for_the_turn():
    """这一轮前面有别的模型发的、不带思考的工具调用：Flash 这一轮剩下的不开思考（DeepSeek 开思考时这一轮的工具调用要带思考，
    不带报 400）。下一轮照常开。"""
    p = _planner()
    p.broken[K3] = BareToolCalls()
    c = Chat(planner=p)
    v = c.say(f"{FAKE_SIMPLE}：查一下局部修改")
    assert v["status"] == "idle"
    assert v["turns"][0]["record"]["model"] == {"ref": FLASH, "thinking": False, "effort": ""}
    assert (ModelUse(ref=FLASH, thinking=False), False) in p.model_uses
    p.broken.clear()
    v = c.say("直接答：接着说")
    assert v["turns"][1]["record"]["model"] == {"ref": FLASH, "thinking": True, "effort": "high"}


def test_status_reports_the_running_model():
    """正在跑时状态里有这一轮用的模型（页面中途打开时回答开头按它显示）；没在跑是 None。"""
    p = _planner()
    c = Chat(planner=p)
    seen: list = []
    c.r.accept(c.tid, f"直接答：{FAKE_SIMPLE}", owner=c.owner)
    v = c.run(stop_on=lambda ev: ev["event"] == "model" and seen.append(c.r._models.get(c.tid)))  # 返回 None：不停
    assert seen == [K3] and v["model_ref"] is None and c.tid not in c.r._models


def test_models_endpoint_names_the_free_models():
    """/api/models 多给选项以外的模型的名字和图标：回答开头显示 Auto 用的免费模型（不写成“已不再提供的模型”）。"""
    from tests.test_model_options import _client

    http, _ = _client(_planner())
    names = http.get("/api/models").json()["names"]
    assert names[K3] == {"name": "Kimi K3", "icon": "moonshot"} and names[V41]["icon"] == "deepseek"


# ---- 用户选的模型先走同一个模型的免费额度（[[free]] 的 same_as） ----

KIMI = "kimi-k3-1-pro"  # 选项 id
KIMI_DIRECT = "moonshot:kimi-k3.1-pro"


def test_chosen_model_uses_its_free_quota_first():
    """选了 Kimi K3.1 Pro：主 agent 先走百炼上同一个模型的免费额度，子 agent 照旧直连；用完、到期、没配 key 走直连。
    没有对应选项的免费额度（K3、V4.1 Flash）不会被选项用上。"""
    c = _catalog()
    now = datetime.now(CHINA_TZ)
    plan = c.plan(KIMI, None, _always, now=now)
    assert plan.option == KIMI and plan.agent == ModelUse(ref=K31, thinking=True) and plan.subagent == ModelUse(ref=KIMI_DIRECT, thinking=True)
    assert c.plan(KIMI, False, _always, now=now).agent == ModelUse(ref=K31, thinking=False)
    assert c.plan(KIMI, None, _always, now=now, free=False).agent.ref == KIMI_DIRECT
    assert c.plan(KIMI, None, _always, now=now + timedelta(days=86)).agent.ref == KIMI_DIRECT
    assert c.plan(KIMI, None, lambda ref: not ref.startswith("dashscope:"), now=now).agent.ref == KIMI_DIRECT
    c.used_up(K31)
    assert c.plan(KIMI, None, _always, now=now).agent.ref == KIMI_DIRECT
    assert c.plan("deepseek-flash", None, _always, now=now).agent.ref == FLASH  # V4.1 Flash 不是它


def test_same_as_must_be_an_option_model():
    good = model_catalog.load().model_dump()
    free = [{**f, "same_as": "moonshot:kimi-k9"} if f["model"] == K31 else f for f in good["free"]]
    with pytest.raises(ValidationError, match="same_as"):
        Catalog.model_validate({**good, "free": free})


def _kimi_chat(p: FakeResearchPlanner) -> Chat:
    c = Chat(planner=p)
    c.r.accept(c.tid, "直接答：你好", owner=c.owner, option=KIMI)
    return c


def test_chosen_models_free_quota_breaks_then_direct():
    """免费额度那边出问题：这一次换直连的同一个模型（不是 Flash），这一轮记成直连；不是用完的，下一轮还先走免费额度。"""
    p = _planner()
    p.broken[K31] = FailingModel(error=ModelUnavailable("模型服务暂时不可用（服务端出错），稍后再试"))
    c = _kimi_chat(p)
    v = c.run()
    assert _models(v) == [KIMI_DIRECT] and _model_events(c.events) == [K31, KIMI_DIRECT] and not _routes(p)
    p.broken.clear()
    v = c.say("直接答：接着说")
    assert _models(v) == [KIMI_DIRECT, K31] and v["model"] == KIMI and "auto_model" not in c.values()


def test_chosen_models_free_quota_used_up_goes_direct_for_good():
    p = _planner()
    p.broken[K31] = FailingModel(error=FreeTierUsedUp("模型服务暂时不可用（免费额度用完了），稍后再试"))
    c = _kimi_chat(p)
    c.run()
    p.broken.clear()
    assert _models(c.say("直接答：接着说")) == [KIMI_DIRECT, KIMI_DIRECT]


def test_free_then_direct_then_flash_in_one_call():
    """免费额度、直连都出问题：同一次调用里接着换 Flash；Flash 也坏了才失败。"""
    p = _planner()
    down = ModelUnavailable("模型服务暂时不可用（服务端出错），稍后再试")
    p.broken.update({K31: FailingModel(error=down), KIMI_DIRECT: FailingModel(error=down)})
    c = _kimi_chat(p)
    v = c.run()
    assert _models(v) == [FLASH] and _model_events(c.events) == [K31, KIMI_DIRECT, FLASH]
    p.broken[FLASH] = FailingModel(error=down)
    with pytest.raises(ModelUnavailable):
        c.say("直接答：接着说")


def test_breaker_is_per_provider_and_model(monkeypatch):
    """熔断按“厂商:模型”记：直连的和托管在百炼上的同一个模型各断各的。"""
    from app import config
    from app.research_llm import make_chat_model

    for name in ("DASHSCOPE_API_KEY", "MOONSHOT_API_KEY"):
        monkeypatch.setattr(config, name, "k")
    hosted, direct = make_chat_model(K31, thinking=True), make_chat_model(KIMI_DIRECT, thinking=True)
    assert hosted._guard_key() == K31 and direct._guard_key() == KIMI_DIRECT
