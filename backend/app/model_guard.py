"""模型服务出的错分两类：服务不可用（熔断），内容审核拦下（换掉内容才行）。

只认服务商给的协议信号：HTTP 状态、连接错误、流里的错误事件、流结束原因，以及 DeepSeek 内容审核拦下请求时固定的错误文字。
连接错误、超时、429、5xx 由 SDK 先重试（max_retries）；这里看的是重试用完以后的结果。

熔断：同一个模型连续 FAILURES 次不可用就断开 COOLDOWN_S 秒，这期间的调用直接失败、不发请求；
冷却完放一个调用去试，成功就恢复，失败接着断开。请求本身的错误（400 之类）说明服务答复了，不算不可用。
429（限流）也是服务答复了：这次调用失败，但不算进熔断，不然请求一密就把所有线程一起断开几分钟。
状态只在进程内存里，单进程部署够用。
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import openai

FAILURES = 2
COOLDOWN_S = 180
BLOCKED_MESSAGE = "Content Exists Risk"  # DeepSeek 内容审核拦下请求时 error.message 的固定文字（HTTP 400）

log = logging.getLogger(__name__)


class ModelUnavailable(RuntimeError):
    """模型服务不可用：服务商出错、连不上、密钥或余额有问题，或者正在熔断。"""


class ContentBlocked(RuntimeError):
    """内容审核拦下。output=False：请求里的内容没通过（整个请求被拒）；True：模型的输出被截断（finish_reason=content_filter）。"""

    def __init__(self, output: bool) -> None:
        super().__init__("模型的输出没通过内容审核" if output else "请求里的内容没通过内容审核")
        self.output = output


def is_blocked(e: BaseException) -> bool:
    return isinstance(e, openai.APIError) and isinstance(e.body, dict) and e.body.get("message") == BLOCKED_MESSAGE


def unavailable_reason(e: BaseException) -> str | None:
    """服务不可用的原因，一句话给人看；不是这一类错误返回 None。"""
    if isinstance(e, openai.APITimeoutError):
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

    def failure(self, key: str, reason: str, detail: str = "") -> ModelUnavailable:
        """记一次不可用，返回给调用方抛的错。连续够数（或者冷却后的试探也失败）就断开。"""
        with self._mu:
            s = self._s.setdefault(key, {"fails": 0, "until": None, "reason": reason, "probing": False})
            s["fails"] += 1
            s["reason"] = reason
            opened = s["probing"] or s["fails"] >= self.failures
            if opened:
                s["until"], s["probing"] = self.clock() + self.cooldown_s, False
        log.warning("模型服务不可用：%s，%s%s%s", key, reason, f"，断开 {self.cooldown_s:.0f} 秒" if opened else "",
                    f"（{detail[:300]}）" if detail else "")
        return ModelUnavailable(_message(reason, self.cooldown_s if opened else None))

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


class GuardedChatModel:
    """放在聊天模型类的前面（多继承）：每次调用先过熔断，服务商的错误换成 ModelUnavailable / ContentBlocked。

    同一个模型（model_name）共用一个熔断状态：调研 agent 和快模型步骤用的是同一个服务。
    """

    breaker = BREAKER  # 不加类型标注：混进 pydantic 的模型类里，标了会被当成字段

    def _guard_key(self) -> str:
        return str(getattr(self, "model_name", "") or type(self).__name__)

    def _translate(self, key: str, e: Exception) -> Exception:
        if is_blocked(e):
            self.breaker.success(key)  # 服务答复了
            return ContentBlocked(output=False)
        if isinstance(e, openai.RateLimitError):  # SDK 重试完还是限流：这一次失败，服务本身是好的
            self.breaker.success(key)
            return ModelUnavailable(_message("请求太频繁", None))
        reason = unavailable_reason(e)
        if reason:
            return self.breaker.failure(key, reason, str(e))
        if isinstance(e, openai.APIStatusError):
            self.breaker.success(key)
        return e

    def _settle(self, key: str, finish: str | None) -> None:
        if finish == "insufficient_system_resource":  # DeepSeek：推理资源不够，生成被打断
            raise self.breaker.failure(key, "服务端资源不足")
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
        finish = None
        try:
            try:
                for chunk in super()._stream(messages, stop=stop, run_manager=run_manager, **kwargs):  # type: ignore[misc]
                    finish = (chunk.generation_info or {}).get("finish_reason") or finish
                    yield chunk
            except Exception as e:
                err = self._translate(key, e)
                if err is e:
                    raise
                raise err from e
            self._settle(key, finish)
        finally:
            if probe:
                self.breaker.release(key)
