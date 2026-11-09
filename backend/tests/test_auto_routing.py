"""Auto 选模型与免费额度（2026-11-08 Sean 定，见 docs/Auto选模型与免费额度方案.md）：第一句话判断难易定下模型、整段沿用；
简单的用免费额度（按到期日），复杂的用 Pro，游客复杂的用 Flash；出问题换备用 Flash（Auto 一直用它，自己选的只顶这一轮）。只用假模型。"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta

import httpx2
import openai
import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGenerationChunk
from pydantic import ValidationError

from app import aio, config, free_used_up, model_catalog
from app.graph.research import agent as agent_mod
from app.model_catalog import AUTO, CHINA_TZ, FREE_TIER, ZERO, Catalog, ModelUse
from app.model_guard import Breaker, FreeTierUsedUp, GuardedChatModel, Interrupted, ModelUnavailable
from app.research_llm import FAKE_SIMPLE, FailingModel, FakeResearchPlanner, FakeToolModel
from tests.research_helpers import run_async
from tests.test_model_guard import REQ, Scripted
from tests.test_research import Chat

PRO, FLASH = "deepseek:deepseek-v4-pro", "deepseek:deepseek-flash"
K3, V41, K31 = "dashscope:kimi-k3", "dashscope:deepseek-v4.1-flash-1017", "dashscope:kimi-k3.1-pro"
TK_K3, TK_V41, TK_V41_1017 = "tokenhub:kimi-k3", "tokenhub:deepseek-v4.1-flash", "tokenhub:deepseek-v4.1-flash-1017"
TK_PRO, TK_K31 = "tokenhub:deepseek-v4-pro", "tokenhub:kimi-k3.1-pro-highspeed"
TK_FLASH, TK_PRO_0813, TK_PRO_202606 = "tokenhub:deepseek/deepseek-flash", "tokenhub:deepseek-v4-pro-0813", "tokenhub:deepseek-v4-pro-202606"
DOUBAO = "ark:doubao-seed-2-1-pro-260915"
GUEST = "guest:g1"
NOW = datetime(2026, 11, 8, 12, tzinfo=CHINA_TZ)


def _always(_ref: str) -> bool:
    return True


def _catalog(days: tuple[int, ...] = (9, 70, 86)) -> Catalog:
    """真的那份，只留百炼的三个免费额度（K3、V4.1 Flash 给 Auto，K3.1 Pro 是选项 Kimi 的 same_as），
    到期日改成今天往后这么多天（测试不随日子过去失效）。TokenHub 的另外测（见 test_tokenhub_*）。
    V4.1 Flash 在仓库里是 Flash 的 same_as，这里改成只给 Auto 的：测 Auto 按到期日挑要两个；Flash 先走免费额度的另外测（见 _flash_catalog）。"""
    c = model_catalog.load()
    today = datetime.now(CHINA_TZ).date()
    bailian = [f for f in c.free if f.model.startswith("dashscope:")]
    free = [f.model_copy(update={"until": today + timedelta(days=d)}) for f, d in zip(bailian, days, strict=True)]
    free = [f.model_copy(update={"same_as": "", "name": "DeepSeek V4.1 Flash", "icon": "deepseek"}) if f.model == V41 else f for f in free]
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
    """仓库里那份：给 Auto 的（没有 same_as 的）只有 K3，按到期日排着，百炼的先用；DeepSeek 的都是直连的同一个模型（same_as），
    写了版本的 Pro 排在没写的前面；备用是 Flash（游客也能用）；判断难易、推荐问题都用 Flash。
    页面上的名字：same_as 的不写就是那个选项的名字、图标。"""
    c = model_catalog.load()
    by_until = sorted(c.free, key=lambda f: f.until)
    assert [f.model for f in by_until if not f.same_as] == [K3, TK_K3]
    assert [f.model for f in by_until if f.same_as == FLASH] == [V41, TK_FLASH, TK_V41, TK_V41_1017]
    assert [f.model for f in by_until if f.same_as == PRO] == [TK_PRO_0813, TK_PRO_202606, TK_PRO]
    assert c.fallback().model == FLASH and c.fallback().allows("guest")
    assert c.roles["route"].model == FLASH and c.roles["suggest"].model == FLASH
    names = c.names()
    assert set(names) == {f.model for f in c.free} and names[K3] == {"name": "Kimi K3", "icon": "moonshot"}
    assert names[TK_PRO] == {"name": c.option("deepseek-v4-pro").name, "icon": "deepseek"} and names[K31]["icon"] == "moonshot"
    assert names[V41] == {"name": c.option("deepseek-flash").name, "icon": "deepseek"}


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
    assert c.pick(True, False, lambda ref: ref != K3, now) == FLASH  # K3.1 Pro 是选项 Kimi 的 same_as：Auto 判为简单的不用


def test_used_up_is_remembered_by_model_and_until():
    """报过用完的记下来（起服务时交回来的、之后头一次报的，见 app.free_used_up）：到期日对得上的才算，续了额度、改了到期日的重新能用；
    同一个只交出去记一次，不是免费额度的不记。"""
    c = _catalog((9, 70, 86))
    now = datetime.now(CHINA_TZ)
    k3, v41 = (next(f for f in c.free if f.model == m) for m in (K3, V41))
    saved = []
    c.track_used_up({(K3, k3.until), (V41, v41.until - timedelta(days=1))}, saved.append)
    assert c.pick(True, False, _always, now) == V41  # K3 记过用完；V4.1 Flash 记的是以前的到期日，现在又能用
    c.used_up(V41)
    c.used_up(V41)
    c.used_up(K3)
    c.used_up(FLASH)
    assert saved == [v41] and c.pick(True, False, _always, now) == FLASH


@pytest.mark.skipif(not config.DATABASE_URL, reason="要 Postgres（设 LINGDRAFT_TEST_DATABASE_URL）")
def test_used_up_survives_a_restart_in_postgres():
    """记在库里：重启（新的一份 catalog）以后照样不用；续了额度（models.toml 改了到期日）的重新能用。
    报用完的可能在别的线程里（起标题这些内部用途），交回对话的事件循环去写。"""
    from app.api.research_runner import open_storage

    _, _, runs = aio.run(open_storage(config.DATABASE_URL))
    c = _catalog()
    aio.run(free_used_up.attach(c, runs._exec))
    c.used_up(K3)
    for _ in range(100):  # 不等写完（见 attach），这里等它写进去
        if aio.run(runs._exec("SELECT model FROM free_used_up", ())):
            break
        time.sleep(0.02)
    now = datetime.now(CHINA_TZ)
    again = _catalog()
    aio.run(free_used_up.attach(again, runs._exec))
    assert again.pick(True, False, _always, now) == V41
    renewed = _catalog((20, 70, 86))
    aio.run(free_used_up.attach(renewed, runs._exec))
    assert renewed.pick(True, False, _always, now) == K3


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
    """选了 Kimi K3.1 Pro：主 agent、子 agent 都先走百炼上同一个模型的免费额度；用完、到期、没配 key 走直连。
    没有对应选项的免费额度（K3）不会被选项用上。"""
    c = _catalog()
    now = datetime.now(CHINA_TZ)
    plan = c.plan(KIMI, None, _always, now=now)
    assert plan.option == KIMI and plan.agent == ModelUse(ref=K31, thinking=True) and plan.subagent == ModelUse(ref=K31, thinking=True)
    assert c.plan(KIMI, None, _always, now=now, free=False).subagent.ref == KIMI_DIRECT
    assert c.plan(KIMI, False, _always, now=now).agent == ModelUse(ref=K31, thinking=False)
    assert c.plan(KIMI, None, _always, now=now, free=False).agent.ref == KIMI_DIRECT
    assert c.plan(KIMI, None, _always, now=now + timedelta(days=86)).agent.ref == KIMI_DIRECT
    assert c.plan(KIMI, None, lambda ref: not ref.startswith("dashscope:"), now=now).agent.ref == KIMI_DIRECT
    c.used_up(K31)
    assert c.plan(KIMI, None, _always, now=now).agent.ref == KIMI_DIRECT


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


# ---- 腾讯 TokenHub 的免费额度 ----


def test_bailian_openai_compatible_quota_error_is_free_tier_used_up(breaker):
    """百炼 OpenAI 兼容接口实际回的（2026-11-09 撞到，原样）：HTTP 403、code insufficient_quota，不是文档写的 AllocationQuota.FreeTierOnly。
    原来当成密钥无效、算进熔断，断开 180 秒以后又去撞。OpenAI 余额不够也是 insufficient_quota 但是 429，那个不算免费额度用完。"""
    body = {"message": 'Free quota exhausted. To continue accessing the model on a paid basis, please add funds or disable the "use free tier only" '
            'mode in the management console.', "id": "e62de711", "type": "insufficient_quota", "code": "insufficient_quota"}
    real = openai.PermissionDeniedError("Error code: 403", response=httpx2.Response(403, request=REQ), body=body)
    m = Guarded(script=[real, real, real])
    for _ in range(3):
        with pytest.raises(FreeTierUsedUp):
            m.invoke("hi")
    assert len(m.calls) == 3  # 不算熔断
    paid = openai.RateLimitError("Error code: 429", response=httpx2.Response(429, request=REQ), body={**body, "message": "You exceeded your current quota"})
    with pytest.raises(ModelUnavailable, match="请求太频繁"):
        Guarded(script=[paid]).invoke("hi")


def test_tokenhub_quota_errors_are_free_tier_used_up(breaker):
    """TokenHub 没开后付费时额度用完、没有免费额度（到期了也是）：HTTP 402，error.code 是 401008 / 401007（有时是数字）。"""
    def err(code):
        body = {"message": "free quota exhausted", "code": code, "type": "gateway_error"}
        return openai.APIStatusError("Error code: 402", response=httpx2.Response(402, request=REQ), body=body)

    m = Guarded(script=[err("401008"), err(401007), err("401008")])
    for _ in range(3):
        with pytest.raises(FreeTierUsedUp):
            m.invoke("hi")
    assert len(m.calls) == 3  # 不算熔断


def test_tokenhub_same_model_goes_free_first_and_auto_simple_skips_it():
    """仓库里那份（按 11-08 看）：选了 Pro、Auto 判为复杂的，主 agent 先走 TokenHub 上的 Pro，pick 记直连的；子 agent 先走百炼上的 Flash；
    Kimi 有两份免费的，百炼的先到期先用；Auto 判为简单的不挑 same_as 的。"""
    c = model_catalog.load()
    for option in ("deepseek-v4-pro", AUTO):
        plan = c.plan(option, None, _always, pick=PRO if option == AUTO else None, now=NOW)
        assert plan.agent.ref == TK_PRO_0813 and plan.pick == PRO and plan.subagent == ModelUse(ref=V41, thinking=True, effort="high")
        assert c.plan(option, None, _always, pick=PRO, now=NOW, free=False).agent.ref == PRO
    assert c.plan("kimi-k3-1-pro", None, _always, now=NOW).agent.ref == K31
    c.used_up(K31)
    assert c.plan("kimi-k3-1-pro", None, _always, now=NOW).agent.ref == TK_K31
    c.used_up(K3)
    assert c.pick(True, False, _always, NOW) == TK_K3
    c.used_up(TK_K3)
    assert c.pick(True, False, _always, NOW) == FLASH and c.plan(AUTO, None, _always, pick=FLASH, now=NOW).agent.ref == V41  # 备用照样先走免费额度
    assert c.plan("hy4", None, _always, now=NOW).agent.ref == "hunyuan:hy4"  # 混元自己的不在 [[free]] 里
    for f in (TK_PRO_0813, TK_PRO_202606):
        c.used_up(f)
        assert c.plan(AUTO, None, _always, pick=PRO, now=NOW).agent.ref != f
    c.used_up(TK_PRO)
    assert c.plan(AUTO, None, _always, pick=PRO, now=NOW).agent.ref == PRO


def _pro_planner(**kw) -> FakeResearchPlanner:
    """百炼的三个 + TokenHub 上的 Pro（选项 Pro 的 same_as）。"""
    c = _catalog()
    tk = next(f for f in model_catalog.load().free if f.model == TK_PRO)
    tk = tk.model_copy(update={"until": datetime.now(CHINA_TZ).date() + timedelta(days=300)})
    return FakeResearchPlanner(catalog=c.model_copy(update={"free": [*c.free, tk]}), **kw)


def test_auto_complex_uses_free_pro_and_remembers_direct():
    """Auto 判为复杂：每轮先走免费的 Pro，对话记下的是直连的 Pro；免费的出问题这一轮换直连 Pro（不是 Flash），下一轮再先走免费的；
    直连也坏了换 Flash，之后一直用 Flash。"""
    p = _pro_planner()
    c = Chat(planner=p)
    v = c.say("直接答：比较一下三家云厂商的数据库方案")
    assert _models(v) == [TK_PRO] and c.values()["auto_model"] == PRO
    p.broken[TK_PRO] = FailingModel(error=ModelUnavailable("模型服务暂时不可用（服务端出错），稍后再试"))
    v = c.say("直接答：接着说")
    assert _models(v) == [TK_PRO, PRO] and c.values()["auto_model"] == PRO and _model_events(c.events) == [TK_PRO, PRO]
    p.broken.clear()
    assert _models(c.say("直接答：再说说"))[-1] == TK_PRO
    down = ModelUnavailable("模型服务暂时不可用（服务端出错），稍后再试")
    p.broken.update({TK_PRO: FailingModel(error=down), PRO: FailingModel(error=down)})
    v = c.say("直接答：最后一个问题")
    assert _models(v)[-1] == FLASH and c.values()["auto_model"] == FLASH
    p.broken.clear()
    assert _models(c.say("直接答：谢谢"))[-1] == FLASH


def test_auto_free_pro_used_up_goes_direct_for_good():
    p = _pro_planner()
    p.broken[TK_PRO] = FailingModel(error=FreeTierUsedUp("模型服务暂时不可用（免费额度用完了），稍后再试"))
    c = Chat(planner=p)
    c.say("直接答：比较一下三家云厂商的数据库方案")
    p.broken.clear()
    v = c.say("直接答：接着说")
    assert _models(v) == [PRO, PRO] and c.values()["auto_model"] == PRO


def test_tokenhub_provider(monkeypatch):
    """TokenHub 上别家的模型：用混元的地址和 key，带 reasoning_split（MiniMax 的思考才单独给），熔断、记账按 tokenhub 记。"""
    from app import config
    from app.research_llm import make_chat_model, usable

    monkeypatch.setattr(config, "HUNYUAN_API_KEY", "k")
    on, off = make_chat_model(TK_PRO, thinking=True), make_chat_model(TK_PRO, thinking=False)
    assert on.extra_body == {"reasoning_split": True} and off.extra_body == {"reasoning_split": True, "thinking": {"type": "disabled"}}
    assert on.openai_api_base == config.HUNYUAN_BASE_URL and on._guard_key() == TK_PRO and on._cost_ref() == TK_PRO
    assert usable(TK_PRO)
    monkeypatch.setattr(config, "HUNYUAN_API_KEY", "")
    assert not usable(TK_PRO)


# ---- Flash、子 agent、内部用途也先走同一个模型的免费额度（2026-11-08） ----


def _flash_catalog() -> Catalog:
    """_catalog 的，再加 Flash 的两个免费额度（deepseek/deepseek-flash 先到期先用）和 TokenHub 上的 Pro，到期日也改成往后数。"""
    c = _catalog()
    today = datetime.now(CHINA_TZ).date()
    real = {f.model: f for f in model_catalog.load().free}
    extra = [real[ref].model_copy(update={"until": today + timedelta(days=d)}) for ref, d in ((TK_FLASH, 100), (TK_V41_1017, 200), (TK_PRO, 300))]
    return c.model_copy(update={"free": [*c.free, *extra]})


def test_subagents_and_roles_go_free_first():
    """子 agent 和主 agent 各看各的免费额度；内部用途先免费、再直连（思考、强度照 [roles]）；用完了的跳过，都用完了只剩直连。"""
    c = _flash_catalog()
    plan = c.plan("deepseek-v4-pro", None, _always)
    assert plan.agent.ref == TK_PRO and plan.subagent == ModelUse(ref=TK_FLASH, thinking=True, effort="high")
    assert c.plan("deepseek-v4-pro", None, _always, free=False).subagent.ref == FLASH
    assert c.plan("deepseek-flash", False, _always).agent == ModelUse(ref=TK_FLASH, thinking=False)
    assert c.plan(AUTO, None, _always, pick=K3).subagent.ref == TK_FLASH  # Auto 判为简单的：子 agent 用备用的，备用也先走免费额度
    assert c.role_uses("title", _always) == [ModelUse(ref=TK_FLASH, thinking=False), ModelUse(ref=FLASH, thinking=False)]
    assert c.role_uses("dream", _always) == [ModelUse(ref=TK_PRO, thinking=True, effort="high"), ModelUse(ref=PRO, thinking=True, effort="high")]
    assert c.role_uses("title", lambda ref: not ref.startswith("tokenhub:")) == [ModelUse(ref=FLASH, thinking=False)]
    c.used_up(TK_FLASH)
    assert c.role_uses("title", _always)[0].ref == TK_V41_1017
    c.used_up(TK_V41_1017)
    assert c.role_uses("title", _always) == [ModelUse(ref=FLASH, thinking=False)]


def _down() -> ModelUnavailable:
    return ModelUnavailable("模型服务暂时不可用（服务端出错），稍后再试")


def _rejected() -> openai.BadRequestError:
    """别家认不出来的出错（内容审核、不认的参数……）：只认得 DeepSeek 的拦截，别家的就是一个 400。"""
    body = {"message": "input contains sensitive content", "code": "400100", "type": "invalid_request_error"}
    return openai.BadRequestError("Error code: 400", response=httpx2.Response(400, request=REQ), body=body)


def _role(monkeypatch, models: dict):
    from langchain_core.language_models import FakeListChatModel

    from app import config
    from app.research_llm import RoleModel

    monkeypatch.setattr(config, "DEEPSEEK_API_KEY", "k")
    c = _flash_catalog()
    role = RoleModel(c, "title", _always, timeout=8)
    role._models.update({FLASH: FakeListChatModel(responses=["直连的"] * 5), **models})
    return c, role


@pytest.mark.parametrize("error", [_down(), _rejected()])
def test_role_free_quota_fails_then_direct(monkeypatch, error):
    """内部用途：免费额度这次没成（服务不可用，或者认不出来的出错）这次换直连；不是用完的，下一次还先走免费额度。"""
    from langchain_core.language_models import FakeListChatModel

    c, role = _role(monkeypatch, {TK_FLASH: FailingModel(error=error)})
    assert asyncio.run(role.ainvoke("起个标题")).content == "直连的"
    role._models[TK_FLASH] = FakeListChatModel(responses=["免费的"])
    assert asyncio.run(role.ainvoke("起个标题")).content == "免费的"


def test_role_free_quota_used_up_moves_on_and_other_errors_are_raised(monkeypatch):
    """额度用完的记下，以后跳过它（有下一个先走下一个）；同步调用一样。别的错（结构化输出没按格式……）照常抛出，不换直连。"""
    from langchain_core.language_models import FakeListChatModel

    used_up = FreeTierUsedUp("模型服务暂时不可用（免费额度用完了），稍后再试")
    c, role = _role(monkeypatch, {TK_FLASH: FailingModel(error=used_up), TK_V41_1017: FakeListChatModel(responses=["下一个"])})
    assert role.invoke("起个标题").content == "直连的"
    assert role.invoke("起个标题").content == "下一个"
    role._models[TK_V41_1017] = FailingModel(error=RuntimeError("模型没有按结构返回"))
    with pytest.raises(RuntimeError, match="没有按结构返回"):
        asyncio.run(role.ainvoke("起个标题"))


def test_probe_stays_direct_and_the_rest_go_free(monkeypatch):
    """单条送审只认得 DeepSeek 的拦截：不走免费额度，别的内部用途都走（结构化输出走工具调用，百炼、TokenHub 都认）。"""
    from app import config
    from app.research_llm import LiveResearchPlanner, RoleModel

    for name, value in (("DEEPSEEK_API_KEY", "k"), ("AGENT_TOOLS_API_KEY", "k")):
        monkeypatch.setattr(config, name, value)
    p = LiveResearchPlanner(catalog=_flash_catalog())
    assert not isinstance(p._prober, RoleModel) and p._prober._guard_key() == PRO
    assert all(isinstance(r, RoleModel) for r in (p._titler, p._suggester, p._summarizer, p._dreamer, p._router))


def _chosen(p: FakeResearchPlanner, option: str, text: str = "直接答：你好") -> Chat:
    c = Chat(planner=p)
    p.model_uses.clear()  # 建图时的占位模型不算
    c.r.accept(c.tid, text, owner=c.owner, option=option)
    return c


def test_free_pro_direct_pro_free_flash_flash_then_stop():
    """选了 Pro：免费的 Pro → 直连 Pro → 免费的 Flash → 直连 Flash，一次调用里接着换；都坏了失败，不在试过的里面来回换。"""
    p = FakeResearchPlanner(catalog=_flash_catalog())
    p.broken.update({TK_PRO: FailingModel(error=_down()), PRO: FailingModel(error=_down()), TK_FLASH: FailingModel(error=_down())})
    c = _chosen(p, "deepseek-v4-pro")
    v = c.run()
    assert _models(v) == [FLASH] and _model_events(c.events) == [TK_PRO, PRO, TK_FLASH, FLASH]
    p.broken[FLASH] = FailingModel(error=_down())
    p.model_uses.clear()
    with pytest.raises(ModelUnavailable):
        c.say("直接答：接着说")
    assert [u.ref for u, sub in p.model_uses if not sub] == [TK_PRO, PRO, TK_FLASH, FLASH]


def test_flash_used_up_tries_the_next_free_one_then_stops():
    """选了 Flash：免费的用完了 → 直连 Flash 坏了 → 下一个免费的（用完的跳过）也坏了 → 回到直连是试过的，失败。"""
    p = FakeResearchPlanner(catalog=_flash_catalog())
    used_up = FreeTierUsedUp("模型服务暂时不可用（免费额度用完了），稍后再试")
    p.broken.update({TK_FLASH: FailingModel(error=used_up), FLASH: FailingModel(error=_down()), TK_V41_1017: FailingModel(error=_down())})
    c = _chosen(p, "deepseek-flash")
    with pytest.raises(ModelUnavailable):
        c.run()
    assert [u.ref for u, sub in p.model_uses if not sub] == [TK_FLASH, FLASH, TK_V41_1017]


def test_unrecognized_error_on_free_quota_goes_direct_but_not_on_direct():
    """免费额度上认不出来的出错（别家的内容审核……）换直连的同一个模型再试，结果照直连的算；直连的出这种错照常失败，不换 Flash。"""
    p = _pro_planner()
    p.broken[TK_PRO] = FailingModel(error=_rejected())
    c = Chat(planner=p)
    assert _models(c.say("直接答：比较一下三家云厂商的数据库方案")) == [PRO]
    p.broken[PRO] = FailingModel(error=_rejected())
    p.model_uses.clear()
    with pytest.raises(openai.BadRequestError):
        c.say("直接答：接着说")
    assert [u.ref for u, sub in p.model_uses if not sub] == [TK_PRO, PRO]


@pytest.mark.parametrize("error", [_down(), FreeTierUsedUp("模型服务暂时不可用（免费额度用完了），稍后再试")])
def test_subagent_free_quota_fails_then_direct_for_the_rest_of_the_task(error):
    """子 agent 的免费额度这次没成：换直连的同一个模型，这个子任务剩下的都用直连，子任务照常做完；主 agent 不受影响，页面上不报换模型。"""
    p = FakeResearchPlanner(catalog=_flash_catalog())
    p.broken[TK_FLASH] = FailingModel(error=error)
    c = _chosen(p, "deepseek-v4-pro", "子任务：查一下杭州美食")
    v = c.run()
    (turn,) = v["turns"]
    (task,) = [i for i in turn["items"] if i["type"] == "step" and i["kind"] == "task"]
    assert v["status"] == "idle" and task["ok"]
    subs = [u.ref for u, sub in p.model_uses if sub]
    assert subs[0] == TK_FLASH and len(subs) > 2 and set(subs[1:]) == {FLASH}
    assert _models(v) == [TK_PRO] and _model_events(c.events) == [TK_PRO]
    p.broken.clear()
    p.model_uses.clear()
    c.say("子任务：再查一下")
    assert [u.ref for u, sub in p.model_uses if sub][0] == (TK_V41_1017 if isinstance(error, FreeTierUsedUp) else TK_FLASH)
