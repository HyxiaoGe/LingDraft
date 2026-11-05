"""成本记账：按调用开始那一刻的单价算钱（峰谷、优惠、分档），钱数写进消息、每次调用记一行账，每轮加起来给 credits。"""

from __future__ import annotations

import asyncio
import functools
import json
import operator
from datetime import datetime
from typing import Any

import httpx
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import Field, ValidationError

from app import config, costs, model_catalog
from app.costs import COST, MeteredChatModel
from app.graph.research import history
from app.graph.research.state import Turn, Usage
from app.model_catalog import CHINA_TZ, Catalog, Rate

PRO, FLASH, HY4, M3 = "deepseek:deepseek-v4-pro", "deepseek:deepseek-flash", "hunyuan:hy4", "minimax:MiniMax-M3"
FRI_10 = datetime(2026, 11, 6, 10, 0, tzinfo=CHINA_TZ)  # 周五上午：DeepSeek 的高峰
USAGE = {"input_tokens": 10_000, "output_tokens": 1_000, "total_tokens": 11_000, "input_token_details": {"cache_read": 8_000}}
PRO_PEAK = (8_000 * 0.30 + 2_000 * 9.0 + 1_000 * 27.0) / 1e6  # 命中缓存、没命中、输出分开算
PRO_OFF_PEAK = PRO_PEAK / 2


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=CHINA_TZ)


class Rows(list):
    """换掉 costs.LEDGER：交过来的行直接收着。"""

    def put(self, row: dict) -> None:
        self.append(row)


@pytest.fixture
def rows(monkeypatch) -> Rows:
    r = Rows()
    monkeypatch.setattr(costs, "LEDGER", r)
    monkeypatch.setattr(costs, "_now", lambda: FRI_10)
    return r


# ---- 单价 ----


def test_rate_follows_promo_peak_and_tiers():
    c = model_catalog.load()
    kinds = {t: c.rate(PRO, at(t), 1)[0] for t in ("2026-11-06T09:00", "2026-11-06T11:59", "2026-11-06T12:00", "2026-11-06T13:59",
                                                    "2026-11-06T14:00", "2026-11-06T18:00", "2026-11-06T08:59", "2026-11-07T10:00")}
    assert kinds == {"2026-11-06T09:00": "regular", "2026-11-06T11:59": "regular", "2026-11-06T12:00": "off_peak",
                     "2026-11-06T13:59": "off_peak", "2026-11-06T14:00": "regular", "2026-11-06T18:00": "off_peak",
                     "2026-11-06T08:59": "off_peak", "2026-11-07T10:00": "off_peak"}  # 周六全天空闲
    assert c.rate(PRO, datetime.fromisoformat("2026-11-06T02:00:00+00:00"), 1)[0] == "regular"  # 按北京时间：UTC 2 点是北京 10 点
    holiday = c.model_copy(update={"peak": c.peak.model_copy(update={"holidays": [FRI_10.date()]})})
    assert holiday.rate(PRO, FRI_10, 1)[0] == "off_peak"  # 法定节假日的工作日全天空闲

    assert c.rate(HY4, at("2026-12-31T23:59:58"), 1) == ("promo", c.prices[HY4].promo)
    assert c.rate(HY4, at("2027-01-01T00:00:00"), 1) == ("regular", c.prices[HY4].regular)
    assert c.rate(M3, FRI_10, 512_000)[0] == "regular" and c.rate(M3, FRI_10, 512_001)[0] == "above_512000"
    assert c.rate("xai:grok-4.7", FRI_10, 1) is None  # 藏起来的选项可以没有价格
    assert Rate(cached=0.30, input=9.0, output=27.0).yuan(10_000, 8_000, 1_000) == pytest.approx(PRO_PEAK)


def test_stale_prices_are_reported():
    c = model_catalog.load()
    assert c.stale(PRO, FRI_10) == [] and c.stale(HY4, FRI_10) == []
    assert "已经过期" in c.stale(HY4, at("2027-01-01T00:00:00"))[0]
    assert "节假日表只填到 2026-12-31" in c.stale(FLASH, at("2027-01-01T08:00:00"))[0]
    assert c.stale("ark:doubao-seed-2-1-pro-260915", at("2027-01-01T08:00:00")) == []  # 不分峰谷的不用节假日表


def test_catalog_rejects_price_mistakes():
    good = model_catalog.load().model_dump()
    prices = good["prices"]
    for broken, why in [
        ({**good, "prices": {k: v for k, v in prices.items() if k != FLASH}}, "没填价格"),
        ({**good, "prices": {**prices, M3: {**prices[M3], "off_peak": prices[M3]["regular"]}}}, "分档和峰谷"),
        ({**good, "prices": {**prices, HY4: {**prices[HY4], "promo": {**prices[HY4]["promo"], "until": datetime(2026, 12, 31)}}}}, "时区"),
        ({**good, "prices": {**prices, M3: {**prices[M3], "tiers": prices[M3]["tiers"] * 2}}}, "从小到大"),
    ]:
        with pytest.raises(ValidationError, match=why):
            Catalog.model_validate(broken)


# ---- 每次调用 ----


class Billed(BaseChatModel):
    """吐一段正文，最后一个片段带用量（和 stream_usage 开着时一样）。fail 是吐完第一个片段后抛的错，before 是还没吐就抛，
    hang 是吐完第一个片段一直不回（等着被取消）。"""

    usage: dict | None = Field(default_factory=lambda: dict(USAGE))
    fail: Any = None
    before: Any = None
    hang: bool = False
    streaming: bool = False

    @property
    def _llm_type(self) -> str:
        return "billed"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        if self.before:
            raise self.before
        return ChatResult(generations=[ChatGeneration(message=AIMessage("好", usage_metadata=self.usage))])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs: Any):
        if self.before:
            raise self.before
        yield ChatGenerationChunk(message=AIMessageChunk(content="好"))
        if self.fail:
            raise self.fail
        yield ChatGenerationChunk(message=AIMessageChunk(content="", usage_metadata=self.usage))

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs: Any):
        for chunk in Billed._stream(self, messages, stop, run_manager, **kwargs):
            yield chunk
            if self.hang:
                await asyncio.sleep(10)


class Metered(MeteredChatModel, Billed):
    ref: str = PRO
    purpose: str = costs.AGENT

    def _cost_ref(self) -> str:
        return self.ref

    def _cost_purpose(self) -> str:
        return self.purpose


def test_each_call_is_charged_and_the_message_carries_it(rows):
    """整条的、流式的（片段合起来，和 LangGraph 里流式调模型一样）都记一行账，钱数在合好的消息上。"""
    whole = Metered().invoke("问")
    streamed = Metered(streaming=True).invoke("问")  # 开着 streaming，invoke 走 _stream 再把片段合成一条
    parts = list(Metered().stream("问"))
    merged = functools.reduce(operator.add, parts)
    assert whole.response_metadata[COST] == pytest.approx(PRO_PEAK)
    assert streamed.response_metadata[COST] == pytest.approx(PRO_PEAK) and streamed.content == "好"
    assert merged.response_metadata[COST] == pytest.approx(PRO_PEAK) and merged.content == "好"
    assert sum(COST in p.response_metadata for p in parts) == 1  # 只有最后多发的那个空片段带钱数，合的时候不冲突
    assert len({p.id for p in parts}) == 1  # 和前面的片段是同一条消息（页面的流按 id 认）
    assert len(rows) == 3
    assert rows[0] == {"started_at": FRI_10, "provider": "deepseek", "model": "deepseek-v4-pro", "purpose": "agent",
                       "thread_id": None, "turn_id": None, "status": "ok", "input_tokens": 10_000, "cached_tokens": 8_000,
                       "output_tokens": 1_000, "yuan": pytest.approx(PRO_PEAK), "rate": {"kind": "regular", "cached": 0.30, "input": 9.0,
                                                                                         "output": 27.0}, "version": config.VERSION or None}


def test_async_stream_is_charged_with_its_scope(rows, monkeypatch):
    """异步流式（对话 agent 就是这样调的）；对话、轮、子 agent 照调用方标的记，内部用途不会被记成子 agent。"""
    monkeypatch.setattr(costs, "_now", lambda: at("2026-11-07T10:00:00"))  # 周六：闲时价

    async def go():
        with costs.within(costs.Scope(thread_id="t1", turn_id="h1")), costs.within(subagent=True):
            agent = functools.reduce(operator.add, [c async for c in Metered().astream("问")])
            title = await Metered(purpose="title").ainvoke("问")
        return agent, title

    agent, _ = asyncio.run(go())
    assert agent.response_metadata[COST] == pytest.approx(PRO_OFF_PEAK)
    assert [(r["purpose"], r["thread_id"], r["turn_id"], r["rate"]["kind"]) for r in rows] == [
        ("subagent", "t1", "h1", "off_peak"), ("title", "t1", "h1", "off_peak")]
    assert costs.SCOPE.get() == costs.Scope()  # 出了这一段就恢复


def test_unknown_usage_and_unpriced_models(rows):
    """接口没返回用量、没填价格的：照样记一行，钱数是空的，消息上没有钱数（这一轮的 credits 就不知道）。"""
    no_usage = Metered(usage=None).invoke("问")
    unpriced = Metered(ref="xai:grok-4.7").invoke("问")
    assert COST not in no_usage.response_metadata and COST not in unpriced.response_metadata
    assert [(r["status"], r["input_tokens"], r["yuan"]) for r in rows] == [("no_usage", None, None), ("ok", 10_000, None)]


def test_calls_cut_short_record_unknown_cost(rows):
    """流到一半断了、点了停止的不知道用了多少（厂商照样收），记一行“不知道”；还没流出东西就失败的没生成，不记。"""
    with pytest.raises(RuntimeError):
        list(Metered(fail=RuntimeError("断了")).stream("问"))
    with pytest.raises(RuntimeError):
        list(Metered(before=RuntimeError("400")).stream("问"))
    with pytest.raises(RuntimeError):
        Metered(before=RuntimeError("400")).invoke("问")

    async def stopped():
        async def consume():
            async for _ in Metered(hang=True).astream("问"):
                pass
        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(stopped())
    assert [(r["status"], r["yuan"]) for r in rows] == [("error", None), ("stopped", None)]


def test_compat_model_charges_from_the_vendor_usage(rows, monkeypatch):
    """真的聊天模型类（豆包走 OpenAI 兼容接口）：流式最后那个用量片段、整条返回的 usage 都算上钱，熔断那层照常。"""
    import openai

    from app.research_llm import make_chat_model

    usage = {"prompt_tokens": 10_000, "completion_tokens": 1_000, "total_tokens": 11_000, "prompt_tokens_details": {"cached_tokens": 8_000}}

    def reply(req: httpx.Request) -> httpx.Response:
        if not json.loads(req.content).get("stream"):
            return httpx.Response(200, json={"id": "x", "object": "chat.completion", "created": 0, "model": "doubao", "usage": usage,
                                             "choices": [{"index": 0, "message": {"role": "assistant", "content": "好"}, "finish_reason": "stop"}]})
        chunks = [{"choices": [{"index": 0, "delta": {"role": "assistant", "content": "好"}, "finish_reason": None}]},
                  {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}, {"choices": [], "usage": usage}]
        body = "".join(f"data: {json.dumps({'id': 'x', 'object': 'chat.completion.chunk', 'created': 0, 'model': 'doubao', **c})}\n\n"
                       for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(config, "ARK_API_KEY", "k")
    llm = make_chat_model("ark:doubao-seed-2-1-pro-260915", thinking=True)
    transport, base = httpx.MockTransport(reply), "http://compat.test/v1"
    llm.root_client = openai.OpenAI(api_key="k", base_url=base, http_client=httpx.Client(transport=transport))
    llm.client = llm.root_client.chat.completions
    expected = (8_000 * 1.20 + 2_000 * 6.00 + 1_000 * 30.00) / 1e6
    assert llm.invoke("问").response_metadata[COST] == pytest.approx(expected)
    merged = functools.reduce(operator.add, llm.stream("问"))
    assert merged.response_metadata[COST] == pytest.approx(expected) and merged.usage_metadata["input_tokens"] == 10_000
    assert [(r["provider"], r["model"], r["cached_tokens"]) for r in rows] == [("ark", "doubao-seed-2-1-pro-260915", 8_000)] * 2
    assert isinstance(make_chat_model(PRO, thinking=True), MeteredChatModel)  # DeepSeek 那个类也混进了


# ---- 每轮 ----


def test_a_turn_through_the_graph_charges_agent_and_subagents(rows):
    """走真的图（假模型套上记账，子 agent 并行两个）：主 agent、子 agent 每次调用各记一行，标着对话、轮和用途；
    钱数随消息存进这一轮的用量，页面上的 credits 就是这一轮这两样加起来，和账上的对得上。"""
    from app.research_llm import FakeResearchPlanner, FakeToolModel
    from tests.test_research import Chat

    class MeteredFake(MeteredChatModel, FakeToolModel):
        ref: str = PRO

        def _cost_ref(self) -> str:
            return self.ref

    planner = FakeResearchPlanner()
    planner._model, planner._sub_model = MeteredFake(), MeteredFake(prefix="fake-sub", ref=FLASH)
    (turn,) = Chat(planner=planner).say("分头查一下杭州美食")["turns"]
    rec = turn["record"]
    agent, sub = ([r for r in rows if r["purpose"] == p] for p in ("agent", "subagent"))
    assert len(agent) == rec["usage"]["model_calls"] == 2 and len(sub) == rec["subagent_usage"]["model_calls"] == 6 and len(rows) == 8
    assert {r["model"] for r in agent} == {"deepseek-v4-pro"} and {r["model"] for r in sub} == {"deepseek-flash"}
    assert {(r["thread_id"], r["turn_id"]) for r in rows} == {("t1", turn["id"])}
    assert rec["usage"]["cost"] == pytest.approx(sum(r["yuan"] for r in agent))
    assert rec["subagent_usage"]["cost"] == pytest.approx(sum(r["yuan"] for r in sub))
    assert rec["credits"] == round(sum(r["yuan"] for r in rows) * 1000, 2)



def _ai(cost: float | None, **meta) -> AIMessage:
    return AIMessage("答", usage_metadata=dict(USAGE), response_metadata={**({COST: cost} if cost is not None else {}), **meta})


def test_turn_usage_adds_up_message_costs():
    assert history.usage_of([HumanMessage("问"), _ai(0.01), _ai(0.002)]).cost == pytest.approx(0.012)
    assert history.usage_of([_ai(0.01), _ai(None)]).cost is None  # 有一次不知道钱数，整轮就不知道
    assert history.usage_of([_ai(0.01), _ai(None, **{history.STOPPED: True})]).cost == pytest.approx(0.01)  # 停下的半截本来就不算一次调用
    assert history.usage_of([]).cost == 0


def test_turn_credits():
    def turn(cost, sub_cost, calls=1) -> Turn:
        return Turn(id="h", started_at="x", usage=Usage(model_calls=calls, cost=cost), subagent_usage=Usage(cost=sub_cost))

    assert costs.credits(turn(0.0474, 0.003)) == 50.4  # ¥1 = 1000 credits，主 agent 加子 agent
    assert costs.credits(turn(0.0474, None)) is None
    assert costs.credits(turn(0.0, 0.0, calls=0)) is None  # 一次都没调完（排队时就停了）
    assert costs.credits(Turn.model_validate({"id": "h", "started_at": "x", "usage": {"model_calls": 3}})) is None  # 记账之前的轮


def test_conversation_record_has_credits():
    from app.api.research_runner import conversation

    msgs = [HumanMessage("问", id="h1"), AIMessage("答", id="a1")]
    turns = [Turn(id="h1", started_at="x", ended_at="y", usage=Usage(model_calls=1, cost=0.0123), subagent_usage=Usage(cost=0.0))]
    assert conversation(msgs, turns)[0]["record"]["credits"] == 12.3


# ---- 账本 ----


def test_ledger_writes_in_the_background_and_survives_failures(caplog):
    written: list[list[dict]] = []
    fail = [True]

    def sink(batch: list[dict]) -> None:
        if fail.pop() if fail else False:
            raise RuntimeError("库连不上")
        written.append(batch)

    led = costs.Ledger()
    led.put({"n": 0})  # 没开：丢掉
    led.start(sink)
    led.put({"n": 1})
    led.stop()  # 写完排着的再停：第一批写不上，打日志丢掉
    led.start(sink)
    led.put({"n": 2})
    led.put({"n": 3})
    led.stop()
    assert [r["n"] for b in written for r in b] == [2, 3]
    assert "成本没记上" in caplog.text


@pytest.mark.skipif(not config.DATABASE_URL, reason="要 Postgres（设 LINGDRAFT_TEST_DATABASE_URL）")
def test_postgres_ledger_and_report(rows):
    import psycopg

    Metered().invoke("问")
    ok = {**rows[0], "started_at": datetime.now(CHINA_TZ)}  # 对账按最近几天取
    lost = {**ok, "model": "deepseek-flash", "status": "stopped", "input_tokens": None, "cached_tokens": None, "output_tokens": None,
            "yuan": None, "rate": None}
    led = costs.PostgresLedger(config.DATABASE_URL)
    led([ok, lost])
    led._close()
    led([ok])  # 断开以后再写会重连
    with psycopg.connect(config.DATABASE_URL) as c:
        got = c.execute("SELECT model, status, yuan, rate->>'kind' FROM model_costs ORDER BY seq").fetchall()
    assert got == [("deepseek-v4-pro", "ok", pytest.approx(PRO_PEAK), "regular"), ("deepseek-flash", "stopped", None, None),
                   ("deepseek-v4-pro", "ok", pytest.approx(PRO_PEAK), "regular")]
    text = costs.report(config.DATABASE_URL, 1)
    assert "deepseek-v4-pro" in text and f"合计 {2 * PRO_PEAK:.4f} 元：算了钱的调用 2 次，另有 1 次不知道用了多少" in text
