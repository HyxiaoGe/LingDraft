"""模型服务出错的分类和熔断：按服务商的协议信号分，不可用的连续够数就断开，冷却后放一个去试。"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import httpx2
import openai
import pytest
from langchain_openai.chat_models._client_utils import StreamChunkTimeoutError
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import Field

from tests.research_helpers import run_async
from app.model_guard import BLOCKED_MESSAGE, Breaker, ContentBlocked, GuardedChatModel, ModelUnavailable, unavailable_reason

REQ = httpx2.Request("POST", "https://api.test/chat/completions")
_STATUS = {400: openai.BadRequestError, 401: openai.AuthenticationError, 429: openai.RateLimitError, 500: openai.InternalServerError}


def status(code: int, message: str = "x") -> openai.APIStatusError:
    cls = _STATUS.get(code, openai.APIStatusError)
    return cls(f"Error code: {code} - {message}", response=httpx2.Response(code, request=REQ), body={"message": message})


class Scripted(BaseChatModel):
    """按 script 依次给结果：异常就抛，字符串是这次的 finish_reason。流式先吐一段正文再给结果。"""

    script: list[Any] = Field(default_factory=list)
    calls: list[int] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _next(self):
        self.calls.append(1)
        return self.script.pop(0) if self.script else "stop"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        act = self._next()
        if isinstance(act, Exception):
            raise act
        return ChatResult(generations=[ChatGeneration(message=AIMessage("好"), generation_info={"finish_reason": act})])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs: Any):
        act = self._next()
        yield ChatGenerationChunk(message=AIMessageChunk(content="好"))
        if isinstance(act, Exception):
            raise act
        yield ChatGenerationChunk(message=AIMessageChunk(content=""), generation_info={"finish_reason": act})

    # 异步的照同步的来（真的聊天模型类自己实现异步，不经过同步的；这里点名调 Scripted 的，免得又过一遍熔断）。"hang" 是一直不回

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        if self.script and self.script[0] == "hang":
            self.script.pop(0)
            await asyncio.sleep(10)
        return Scripted._generate(self, messages, stop, run_manager, **kwargs)

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs: Any):
        for chunk in Scripted._stream(self, messages, stop, run_manager, **kwargs):
            yield chunk
            await asyncio.sleep(0)


class Guarded(GuardedChatModel, Scripted):
    pass


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock(monkeypatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(GuardedChatModel, "breaker", Breaker(failures=2, cooldown_s=180, clock=c))
    return c


def test_reasons_come_from_the_protocol():
    assert unavailable_reason(openai.APITimeoutError(request=REQ)) == "超时"
    assert unavailable_reason(StreamChunkTimeoutError(120.0, model_name="deepseek-v4-pro")) == "超时"  # 异步流式太久没片段
    assert unavailable_reason(openai.APIConnectionError(request=REQ)) == "连接失败"
    assert [unavailable_reason(status(c)) for c in (401, 402, 500, 503)] == ["密钥无效", "余额不足", "服务端出错", "服务端出错"]
    assert unavailable_reason(openai.APIError("流里的错误", REQ, body={"message": "boom"})) == "服务端出错"
    assert unavailable_reason(status(400)) is None and unavailable_reason(status(404)) is None and unavailable_reason(ValueError()) is None


def test_two_failures_open_the_breaker_then_one_probe_after_cooldown(clock):
    m = Guarded(script=[status(500), openai.APIConnectionError(request=REQ)])
    with pytest.raises(ModelUnavailable, match="服务端出错.*稍后再试"):
        m.invoke("hi")
    with pytest.raises(ModelUnavailable, match="连接失败.*约 3 分钟后再试"):
        m.invoke("hi")
    clock.t += 60
    with pytest.raises(ModelUnavailable, match="约 2 分钟后再试"):  # 断开中：不发请求
        m.invoke("hi")
    assert len(m.calls) == 2
    clock.t += 121
    assert m.invoke("hi").content == "好"  # 冷却完放一个去试，成功就恢复
    assert m.invoke("hi").content == "好" and len(m.calls) == 4


def test_failed_probe_reopens_and_only_one_probe_at_a_time(clock):
    m = Guarded(script=[status(500), status(500)])
    for _ in range(2):
        with pytest.raises(ModelUnavailable):
            m.invoke("hi")
    clock.t += 181
    m.script = [status(503)]
    with pytest.raises(ModelUnavailable, match="约 3 分钟后再试"):  # 试探失败：直接再断开
        m.invoke("hi")
    clock.t += 181
    stream = m.stream("hi")
    next(stream)  # 试探还没有结果
    with pytest.raises(ModelUnavailable):
        m.invoke("hi")
    stream.close()  # 中途不读了：名额让给下一个
    assert m.invoke("hi").content == "好"


def test_moderation_block_is_content_blocked_and_not_a_failure(clock):
    m = Guarded(script=[status(500), status(400, BLOCKED_MESSAGE), status(500), status(400, "bad tool schema")])
    with pytest.raises(ModelUnavailable):
        m.invoke("hi")
    with pytest.raises(ContentBlocked) as e:
        m.invoke("hi")
    assert not e.value.output
    with pytest.raises(ModelUnavailable, match="稍后再试"):  # 拦下说明服务答复了：不可用的计数重新算，没断开
        m.invoke("hi")
    with pytest.raises(openai.BadRequestError):  # 别的请求错误原样抛出
        m.invoke("hi")
    assert m.invoke("hi").content == "好"


def test_block_inside_the_stream_and_finish_reasons(clock):
    m = Guarded(script=[openai.APIError("x", REQ, body={"message": BLOCKED_MESSAGE}), "content_filter", "insufficient_system_resource"])
    with pytest.raises(ContentBlocked) as e:
        list(m.stream("hi"))
    assert not e.value.output
    with pytest.raises(ContentBlocked) as e:
        list(m.stream("hi"))
    assert e.value.output  # 输出被截断
    with pytest.raises(ModelUnavailable, match="服务端资源不足"):
        list(m.stream("hi"))
    m.script = ["content_filter"]
    with pytest.raises(ContentBlocked):
        m.invoke("hi")


def test_rate_limit_fails_the_call_but_does_not_open_the_breaker(clock):
    m = Guarded(script=[status(500), status(429), status(429), status(500)])
    with pytest.raises(ModelUnavailable, match="服务端出错"):
        m.invoke("hi")
    for _ in range(2):
        with pytest.raises(ModelUnavailable, match="请求太频繁.*稍后再试"):
            m.invoke("hi")
    with pytest.raises(ModelUnavailable, match="服务端出错.*稍后再试"):  # 限流说明服务答复了：前面那次不可用不再接着算，没断开
        m.invoke("hi")
    assert m.invoke("hi").content == "好" and len(m.calls) == 5


def test_async_calls_go_through_the_same_breaker(clock):
    """对话 agent 异步地调（ainvoke、astream）：一样过熔断、一样换错误；点了停止被取消的调用不算对错，试探的名额让出来。"""

    async def go():
        m = Guarded(script=[status(500), openai.APIConnectionError(request=REQ)])
        for _ in range(2):
            with pytest.raises(ModelUnavailable):
                await m.ainvoke("hi")
        with pytest.raises(ModelUnavailable, match="约 3 分钟后再试"):  # 断开中：不发请求
            await m.ainvoke("hi")
        assert len(m.calls) == 2
        clock.t += 181
        stream = m.astream("hi")
        await stream.__anext__()  # 试探还没有结果
        with pytest.raises(ModelUnavailable):
            await m.ainvoke("hi")
        await stream.aclose()  # 中途不读了：名额让给下一个
        assert (await m.ainvoke("hi")).content == "好"

        m.script = [status(500), status(500)]
        for _ in range(2):
            with pytest.raises(ModelUnavailable):
                await m.ainvoke("hi")
        clock.t += 181
        m.script = ["hang"]
        probe = asyncio.ensure_future(m.ainvoke("hi"))
        await asyncio.sleep(0.05)
        probe.cancel()  # 点了停止
        with contextlib.suppress(asyncio.CancelledError):
            await probe
        assert (await m.ainvoke("hi")).content == "好"  # 取消的试探没有定论：名额让出来，这一次去试、成功就恢复

        m.script = [openai.APIError("x", REQ, body={"message": BLOCKED_MESSAGE}), "content_filter", "insufficient_system_resource"]
        with pytest.raises(ContentBlocked) as e:
            [c async for c in m.astream("hi")]
        assert not e.value.output
        with pytest.raises(ContentBlocked) as e:
            [c async for c in m.astream("hi")]
        assert e.value.output
        with pytest.raises(ModelUnavailable, match="服务端资源不足"):
            [c async for c in m.astream("hi")]

    run_async(go())


def test_async_stream_that_stalls_is_a_timeout_and_counts(clock):
    """服务端排队、一直不出字：langchain-openai 抛它自己的超时（异步才有），一样换成超时、算进熔断，页面上不出现英文原话。"""

    async def go():
        m = Guarded(script=[StreamChunkTimeoutError(120.0, model_name="deepseek-v4-pro"), StreamChunkTimeoutError(120.0)])
        with pytest.raises(ModelUnavailable, match="超时.*稍后再试"):
            [c async for c in m.astream("hi")]
        with pytest.raises(ModelUnavailable, match="超时.*约 3 分钟后再试"):  # 第二次：断开
            [c async for c in m.astream("hi")]

    run_async(go())


def test_title_timeouts_only_break_titles(clock, monkeypatch):
    """起标题超时短，超时只说明不想等：单独记熔断。连续超时只停起标题，同一个模型上的子 agent、写短版照常调。"""
    from langchain_deepseek import ChatDeepSeek

    from app import config
    from app.research_llm import LiveResearchPlanner, make_chat_model

    monkeypatch.setattr(config, "DEEPSEEK_API_KEY", "k")
    monkeypatch.setattr(config, "RESEARCH_PROVIDER", "deepseek")
    p = LiveResearchPlanner()
    flash = config.DEEPSEEK_FLASH_MODEL
    assert p._titler._guard_key() == f"{flash}·起标题" and p._summarizer._guard_key() == flash
    other = make_chat_model("deepseek", flash, thinking=False)  # 子 agent 默认也是它
    calls = []

    async def service(self, messages, stop=None, run_manager=None, **kwargs):
        calls.append(self._guard_key())
        if self is p._titler:
            raise openai.APITimeoutError(request=REQ)
        return ChatResult(generations=[ChatGeneration(message=AIMessage("好"), generation_info={"finish_reason": "stop"})])

    monkeypatch.setattr(ChatDeepSeek, "_agenerate", service)

    async def go():
        for _ in range(2):
            with pytest.raises(ModelUnavailable, match="超时"):
                await p.title("问题")
        with pytest.raises(ModelUnavailable, match="约 3 分钟后再试"):  # 起标题断开了：不发请求
            await p.title("问题")
        assert len(calls) == 2
        assert (await other.ainvoke("hi")).content == "好"  # 同一个模型的其他用途没断开
        assert (await p._summarizer.ainvoke("hi")).content == "好"

    run_async(go())
