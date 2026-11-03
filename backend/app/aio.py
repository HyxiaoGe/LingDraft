"""对话场景的事件循环：一个后台线程上跑一个 asyncio 循环，agent 的执行、检查点、长期记忆和运行记录的读写都在它上面。

异步的检查点、Store（AsyncPostgresSaver、AsyncPostgresStore）和它们共用的连接池绑定创建它们的那个循环，所以要有一个固定的循环：
接口的请求（uvicorn 的线程池里）、整理记忆的定时任务、命令行、评测和测试都经 run 把事交给它、等结果。
不用 uvicorn 的循环：测试里每个 TestClient 都开自己的循环，连接池跟着哪个循环走说不准。
页面读事件流不进这个循环：事件日志是线程安全的（见 api.runner.LocalEventLog），SSE 在 uvicorn 的循环里读。

用户点停止时，运行器在这个循环里取消正在跑的那次执行（见 api.research_runner），在等的模型、搜索、画图马上断开。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Coroutine
from typing import Any, TypeVar

T = TypeVar("T")

_loop: asyncio.AbstractEventLoop | None = None
_mu = threading.Lock()


def loop() -> asyncio.AbstractEventLoop:
    """对话的事件循环，第一次用到时起。线程是守护线程：进程退出不等它。"""
    global _loop
    with _mu:
        if _loop is None:
            lp = asyncio.new_event_loop()
            threading.Thread(target=lp.run_forever, daemon=True, name="research-loop").start()
            _loop = lp
        return _loop


def on_loop() -> bool:
    """当前是不是在对话的事件循环里。"""
    try:
        return asyncio.get_running_loop() is _loop
    except RuntimeError:
        return False


def run(coro: Coroutine[Any, Any, T]) -> T:
    """在对话的事件循环里跑完 coro，阻塞调用的线程等结果。不能在这个循环里调：会自己等自己。"""
    if on_loop():
        coro.close()
        raise RuntimeError("不能在对话的事件循环里同步等它自己的任务")
    return asyncio.run_coroutine_threadsafe(coro, loop()).result()


async def call(coro: Coroutine[Any, Any, T]) -> T:
    """在别的事件循环里（比如 uvicorn 的）等对话的事件循环做完 coro，不阻塞那个循环。"""
    if on_loop():
        return await coro
    return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, loop()))


async def gather(*aws: Awaitable[T]) -> list[T]:
    """并发跑完，一个出错就取消其余的、照原样抛出这个错（asyncio.gather 出错时不管其余的，TaskGroup 抛的是 ExceptionGroup）。"""
    tasks = [asyncio.ensure_future(a) for a in aws]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for t in tasks:
            t.cancel()
        raise
