"""对话场景的测试工具：经过执行器、接口跑的测试共用。只用假模型。"""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from langchain_core.messages import BaseMessage, ToolMessage

from app import aio
from app.api.research_runner import ResearchRunner
from app.research_llm import FakeResearchPlanner, FakeToolModel

SIMPLE = "查一下局部修改"  # 搜、读、回复


def flaky_runner(tmp_path, fail) -> ResearchRunner:
    """模型在 fail(messages) 为真的那次调用上抛错（只抛一次）。agent 建好后换不了模型，所以在建运行器之前装上。"""
    state = {"armed": True}

    class Flaky(FakeToolModel):
        def _message(self, messages: list[BaseMessage]):
            if state["armed"] and fail(messages):
                state["armed"] = False
                raise RuntimeError("假模型：网络断了")
            return super()._message(messages)

    p = FakeResearchPlanner()
    p._model = Flaky()
    return ResearchRunner(planner=p, data_dir=tmp_path / "data")


def gated_runner(tmp_path, when) -> tuple[ResearchRunner, threading.Event]:
    """模型在 when(messages) 为真的调用上停在闸门上，模拟一轮跑到一半。10 秒兜底；和 flaky_runner 一样在建运行器之前装上。"""
    gate = threading.Event()

    class Gated(FakeToolModel):
        def _message(self, messages: list[BaseMessage]):
            if when(messages):
                gate.wait(10)
            return super()._message(messages)

    p = FakeResearchPlanner()
    p._model = Gated()
    return ResearchRunner(planner=p, data_dir=tmp_path / "data"), gate


def after_tool(name: str):
    return lambda messages: isinstance(messages[-1], ToolMessage) and messages[-1].name == name


def events(resp) -> list[tuple[str, dict]]:
    assert resp.status_code == 200, resp.text
    out, ev = [], None
    for line in resp.text.splitlines():
        if line.startswith("event: "):
            ev = line[7:]
        elif line.startswith("data: "):
            out.append((ev, json.loads(line[6:])))
    return out


def last(evs, name):
    return [d for e, d in evs if e == name][-1]


def state(client, tid: str) -> dict:
    return client.get(f"/api/research/threads/{tid}/state").json()


def new(client, text: str) -> dict:
    return last(events(client.post("/api/research/threads", json={"text": text})), "state")


def say(client, tid: str, text: str, **kw):
    return client.post(f"/api/research/threads/{tid}/messages", json={"text": text, **kw})


def hold(planner, name: str) -> threading.Event:
    """让 planner 的这一步停在闸门上，模拟慢模型。10 秒兜底，修复前的代码不会把测试永远卡住。
    异步的步骤（起标题、推荐问题这些）在线程里等闸门，不卡对话的事件循环。"""
    gate = threading.Event()
    orig = getattr(planner, name)

    if inspect.iscoroutinefunction(orig):
        async def slow(*a, **kw):
            await asyncio.to_thread(gate.wait, 10)
            return await orig(*a, **kw)
    else:
        def slow(*a, **kw):
            gate.wait(10)
            return orig(*a, **kw)

    setattr(planner, name, slow)
    return gate


def hold_model(planner) -> threading.Event:
    """让主 agent 的假模型每次调用都先停在闸门上（还没吐出任何东西），模拟慢模型：执行占着位，还没有回答。"""
    gate = threading.Event()
    planner._model.gates.append(gate)
    return gate


def run_async(coro):
    """在新线程里跑事件循环：同一进程里的海报测试会在主线程留下 Playwright 的循环，主线程不能再 asyncio.run。"""
    with ThreadPoolExecutor(1) as ex:
        return ex.submit(asyncio.run, coro).result(60)


def wait_until(fn, timeout: float = 10) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.05)
    return False


class _Blocking:
    def __init__(self, inner) -> None:
        self._inner = inner

    def __getattr__(self, name: str):
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def call(*a, **kw):
            out = attr(*a, **kw)
            return aio.run(out) if inspect.isawaitable(out) else out
        return call


def blocking(x):
    """测试里同步地用异步的客户端、服务（网关客户端、天气）：返回协程的方法在对话的事件循环里跑完再返回。"""
    return _Blocking(x)


def invoke(tool, call):
    """工具都是异步的：在对话的事件循环里调一次，返回结果。"""
    return aio.run(tool.ainvoke(call))
