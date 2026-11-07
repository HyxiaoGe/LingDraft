"""模型服务出的错分两类：服务不可用（熔断），内容审核拦下（换掉内容才行）。

只认服务商给的协议信号：HTTP 状态、连接错误、流里的错误事件、流结束原因，以及 DeepSeek 内容审核拦下请求时固定的错误文字。
连接错误、超时、429、5xx 由 SDK 先重试（max_retries）；这里看的是重试用完以后的结果。
异步流式调用时 langchain-openai 另有一个超时：太久收不到片段抛 TimeoutError，不重试，也算超时。
SDK 只重试发请求那一下（拿到状态码为止）。模型开始生成以后断的（流读到一半断开、流里来了错误、DeepSeek 资源不足、太久没片段）它不管，
这几种换成 Interrupted：调用方可以把这次调用整个再发一次（对话 agent 再发一次，见 graph.research.agent.InterruptedRetryMiddleware）。

首字超时：对话 agent 的流式调用发出去以后太久（建模型时给的秒数，见 research_llm.FIRST_TOKEN_S）没收到第一个有内容的片段（思考、正文、工具调用都算，
服务端排队时的保活注释、开头只带角色的空片段不算）就当不可用，断开这次请求，调用方换备用（2026-11-08 Sean 定 30 秒）。
免费额度用完：厂商用一种固定的错误说额度用完了（百炼开了“用完即停”时是 AllocationQuota.FreeTierOnly），换成 FreeTierUsedUp，不算熔断（服务答复了）。

熔断：同一个模型连续 FAILURES 次不可用就断开 COOLDOWN_S 秒，这期间的调用直接失败、不发请求；
冷却完放一个调用去试，成功就恢复，失败接着断开。请求本身的错误（400 之类）说明服务答复了，不算不可用。
429（限流）也是服务答复了：这次调用失败，但不算进熔断，不然请求一密就把所有线程一起断开几分钟。
状态只在进程内存里，单进程部署够用。
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import openai

FAILURES = 2
COOLDOWN_S = 180
BLOCKED_MESSAGE = "Content Exists Risk"  # DeepSeek 内容审核拦下请求时 error.message 的固定文字（HTTP 400）
# 厂商说免费额度用完了的错误码：百炼开了“免费额度用完即停”时额度用完返回这个（官方文档“新人免费额度”，2026-11-08 查的）。
# 火山、腾讯接进来时把它们的加在这里
FREE_TIER_CODES = {"AllocationQuota.FreeTierOnly"}

log = logging.getLogger(__name__)


class ModelUnavailable(RuntimeError):
    """模型服务不可用：服务商出错、连不上、密钥或余额有问题，或者正在熔断。
    streamed：断之前已经流出了片段（页面上有半截，再调或换备用之前要去掉）。"""

    def __init__(self, message: str, streamed: bool = False) -> None:
        super().__init__(message)
        self.streamed = streamed


class Interrupted(ModelUnavailable):
    """模型开始生成以后断了，SDK 不重试这种：整个再调一次多半能好。这次失败已经让熔断断开的不给这个（再调也会被拒）。"""


class FreeTierUsedUp(ModelUnavailable):
    """这个模型的免费额度用完了（见 FREE_TIER_CODES）：调用方记下以后不再用它（model_catalog.Catalog.used_up），这次换备用。"""


class ContentBlocked(RuntimeError):
    """内容审核拦下。output=False：请求里的内容没通过（整个请求被拒）；True：模型的输出被截断（finish_reason=content_filter）。"""

    def __init__(self, output: bool) -> None:
        super().__init__("模型的输出没通过内容审核" if output else "请求里的内容没通过内容审核")
        self.output = output


def is_blocked(e: BaseException) -> bool:
    return isinstance(e, openai.APIError) and isinstance(e.body, dict) and e.body.get("message") == BLOCKED_MESSAGE


def is_free_tier_used_up(e: BaseException) -> bool:
    if not isinstance(e, openai.APIStatusError):
        return False
    body = e.body if isinstance(e.body, dict) else {}
    return getattr(e, "code", None) in FREE_TIER_CODES or body.get("code") in FREE_TIER_CODES


def has_content(chunk: Any) -> bool:
    """流式片段里有没有模型生成的东西：正文、思考、工具调用（开头只带角色的空片段、最后的用量片段不算）。"""
    m = chunk.message
    return bool(m.content or m.additional_kwargs.get("reasoning_content") or getattr(m, "tool_call_chunks", None))


def unavailable_reason(e: BaseException) -> str | None:
    """服务不可用的原因，一句话给人看；不是这一类错误返回 None。"""
    # TimeoutError：异步流式调用时 langchain-openai 自己的超时（stream_chunk_timeout，默认 120 秒没收到一个片段就抛）。
    # 服务端排队时只发保活注释、连接不断，SDK 的超时不会触发，只有它会
    if isinstance(e, openai.APITimeoutError | TimeoutError):
        return "超时"
    if isinstance(e, openai.APIConnectionError):  # 流读到一半断开也包成这个
        return "连接失败"
    if isinstance(e, openai.APIStatusError):
        code = e.status_code
        if code in (401, 403):
            return "密钥无效"
        if code == 402:
            return "余额不足"
        return "服务端出错" if code >= 500 else None
    if type(e) is openai.APIError:  # 流里的错误事件：生成到一半服务端出错
        return "服务端出错"
    return None


class Breaker:
    def __init__(self, failures: int = FAILURES, cooldown_s: float = COOLDOWN_S, clock: Callable[[], float] = time.monotonic) -> None:
        self.failures, self.cooldown_s, self.clock = failures, cooldown_s, clock
        self._mu = threading.Lock()
        self._s: dict[str, dict] = {}  # 模型 → {fails, until（断开到什么时候，没断开为 None）, reason, probing}

    def check(self, key: str) -> bool:
        """断开中直接失败；冷却完了放这一个调用去试（返回 True），别的接着失败，等它的结果。"""
        with self._mu:
            s = self._s.get(key)
            if not s or s["until"] is None:
                return False
            left = s["until"] - self.clock()
            if left > 0 or s["probing"]:
                raise ModelUnavailable(_message(s["reason"], max(left, 0)))
            s["probing"] = True
            return True

    def success(self, key: str) -> None:
        with self._mu:
            s = self._s.pop(key, None)
        if s and s["until"] is not None:
            log.warning("模型服务恢复：%s", key)

    def failure(self, key: str, reason: str, detail: str = "", *, interrupted: bool = False, streamed: bool = False) -> ModelUnavailable:
        """记一次不可用，返回给调用方抛的错。连续够数（或者冷却后的试探也失败）就断开。
        interrupted：模型开始生成以后断的，没因此断开时给 Interrupted（streamed 照传），调用方可以再调一次。"""
        with self._mu:
            s = self._s.setdefault(key, {"fails": 0, "until": None, "reason": reason, "probing": False})
            s["fails"] += 1
            s["reason"] = reason
            opened = s["probing"] or s["fails"] >= self.failures
            if opened:
                s["until"], s["probing"] = self.clock() + self.cooldown_s, False
        log.warning("模型服务不可用：%s，%s%s%s", key, reason, f"，断开 {self.cooldown_s:.0f} 秒" if opened else "",
                    f"（{detail[:300]}）" if detail else "")
        message = _message(reason, self.cooldown_s if opened else None)
        return Interrupted(message, streamed) if interrupted and not opened else ModelUnavailable(message, streamed)

    def release(self, key: str) -> None:
        """试探的调用没有定论（中途不读了、别的错误）：名额让给下一个。成功、失败时已经处理过，这里不再变。"""
        with self._mu:
            s = self._s.get(key)
            if s:
                s["probing"] = False


def _message(reason: str, wait_s: float | None) -> str:
    wait = f"约 {max(1, math.ceil(wait_s / 60))} 分钟后" if wait_s else "稍后"
    return f"模型服务暂时不可用（{reason}），{wait}再试"


BREAKER = Breaker()


def guard_key(model: str, use: str | None = None) -> str:
    """熔断按什么记：默认模型名；某个用途单独记时是“模型名·用途”，日志里也看得出是哪个用途断开的。"""
    return f"{model}·{use}" if use else model


class GuardedChatModel:
    """放在聊天模型类的前面（多继承）：每次调用先过熔断，服务商的错误换成 ModelUnavailable / ContentBlocked。

    同一个模型（model_name）默认共用一个熔断状态：调研 agent 和快模型步骤用的是同一个服务。某个用途要单独记，子类改 _guard_key（见 guard_key）。
    """

    breaker = BREAKER  # 不加类型标注：混进 pydantic 的模型类里，标了会被当成字段

    def _guard_key(self) -> str:
        return str(getattr(self, "model_name", "") or type(self).__name__)

    def _first_token_s(self) -> float | None:
        """流式调用等第一个有内容的片段最多多少秒（见模块说明）；None 是不限（只有 langchain-openai 自己的片段超时）。建模型的地方按用途改。"""
        return None

    def _translate(self, key: str, e: Exception, streamed: bool = False) -> Exception:
        """streamed：流式调用里已经收到过片段。"""
        if is_blocked(e):
            self.breaker.success(key)  # 服务答复了
            return ContentBlocked(output=False)
        if is_free_tier_used_up(e):
            self.breaker.success(key)  # 服务答复了
            log.warning("免费额度用完了：%s", key)
            return FreeTierUsedUp(_message("免费额度用完了", None))
        if isinstance(e, openai.RateLimitError):  # SDK 重试完还是限流：这一次失败，服务本身是好的
            self.breaker.success(key)
            return ModelUnavailable(_message("请求太频繁", None))
        reason = unavailable_reason(e)
        if reason:
            # 开始生成以后断的：收到过片段，或者是 SDK 从不重试的那两种（流里的错误事件、langchain-openai 的片段超时）。
            # 还没收到片段的连接错误、读超时是发请求那一下，SDK 已经重试过
            interrupted = streamed or type(e) is openai.APIError or isinstance(e, TimeoutError)
            return self.breaker.failure(key, reason, str(e), interrupted=interrupted, streamed=streamed)
        if isinstance(e, openai.APIStatusError):
            self.breaker.success(key)
        return e

    def _settle(self, key: str, finish: str | None, streamed: bool = False) -> None:
        if finish == "insufficient_system_resource":  # DeepSeek：推理资源不够，生成被打断
            raise self.breaker.failure(key, "服务端资源不足", interrupted=True, streamed=streamed)
        self.breaker.success(key)
        if finish == "content_filter":
            raise ContentBlocked(output=True)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs: Any):
        key = self._guard_key()
        probe = self.breaker.check(key)
        try:
            try:
                res = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)  # type: ignore[misc]
            except Exception as e:
                err = self._translate(key, e)
                if err is e:
                    raise
                raise err from e
            self._settle(key, (res.generations[0].generation_info or {}).get("finish_reason") if res.generations else None)
            return res
        finally:
            if probe:
                self.breaker.release(key)

    def _stream(self, messages, stop=None, run_manager=None, **kwargs: Any) -> Iterator:
        key = self._guard_key()
        probe = self.breaker.check(key)
        finish, streamed = None, False
        try:
            try:
                for chunk in super()._stream(messages, stop=stop, run_manager=run_manager, **kwargs):  # type: ignore[misc]
                    finish = (chunk.generation_info or {}).get("finish_reason") or finish
                    streamed = True
                    yield chunk
            except Exception as e:
                err = self._translate(key, e, streamed)
                if err is e:
                    raise
                raise err from e
            self._settle(key, finish, streamed)
        finally:
            if probe:
                self.breaker.release(key)

    # 异步的两个和上面一样：对话 agent 在事件循环里异步地调（见 app.aio），整理记忆、评测的裁判还是同步的。
    # 点了停止时调用被取消（CancelledError 不是 Exception），不算服务的对错，试探的名额让出来

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs: Any):
        key = self._guard_key()
        probe = self.breaker.check(key)
        try:
            try:
                res = await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)  # type: ignore[misc]
            except Exception as e:
                err = self._translate(key, e)
                if err is e:
                    raise
                raise err from e
            self._settle(key, (res.generations[0].generation_info or {}).get("finish_reason") if res.generations else None)
            return res
        finally:
            if probe:
                self.breaker.release(key)

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs: Any) -> AsyncIterator:
        key = self._guard_key()
        probe = self.breaker.check(key)
        finish, streamed = None, False
        limit = self._first_token_s()
        deadline = None if limit is None else asyncio.get_running_loop().time() + limit  # 收到第一个有内容的片段以后就不管了
        it = super()._astream(messages, stop=stop, run_manager=run_manager, **kwargs)  # type: ignore[misc]
        try:
            try:
                while True:
                    try:
                        async with asyncio.timeout_at(deadline) as waiting:
                            chunk = await anext(it)
                    except StopAsyncIteration:
                        break
                    except TimeoutError:
                        if not waiting.expired():
                            raise  # langchain-openai 自己的片段超时，照下面的处理
                        raise self.breaker.failure(key, "首字超时", f"{limit:.0f} 秒没收到第一个片段", streamed=streamed) from None
                    if deadline is not None and has_content(chunk):
                        deadline = None
                    finish = (chunk.generation_info or {}).get("finish_reason") or finish
                    streamed = True
                    yield chunk
            except ModelUnavailable:
                raise
            except Exception as e:
                err = self._translate(key, e, streamed)
                if err is e:
                    raise
                raise err from e
            self._settle(key, finish, streamed)
        finally:
            await it.aclose()  # 首字超时、点了停止：断开这次请求
            if probe:
                self.breaker.release(key)
