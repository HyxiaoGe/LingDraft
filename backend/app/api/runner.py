"""把同步的图执行放到单个工作线程里，以 SSE 事件流的形式吐给接口层。

单线程的原因：Playwright 同步浏览器绑定线程；PoC 不需要并发渲染。
同一个线程同时只能有一次执行：请求被接受时就占位（排队也算），执行结束才释放，浏览器断开不影响。
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol

from langgraph.types import Command
from pydantic import BaseModel

from app import config
from app.graph.poster.graph import build_graph, open_sqlite_checkpointer
from app.graph.poster.nodes import _element_view
from app.llm import ArkPlanner, FakePlanner

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="graph")
_THREAD_ID = re.compile(r"[0-9A-Za-z_-]{1,64}")


def valid_thread_id(thread_id: str) -> bool:
    return bool(_THREAD_ID.fullmatch(thread_id))


class RunConflict(Exception):
    """这个线程已经有一次执行在排队或运行。"""


class RunRegistry(Protocol):
    """线程的运行占位：页面刷新回来时据此判断“还在跑”，重复提交据此拒绝。

    admit 是原子的“占位成功 / 已被占用”，调用方不加锁；多 worker 部署时换成共享存储的实现（如 Redis SET NX），接口不变。
    """

    def admit(self, key: str, info: dict | None = None) -> bool: ...
    def started(self, key: str) -> None: ...
    def get(self, key: str) -> dict | None: ...
    def release(self, key: str) -> None: ...


class LocalRunRegistry:
    """进程内实现，单进程部署够用。"""

    def __init__(self) -> None:
        self._mu = threading.Lock()
        self._runs: dict[str, dict] = {}

    def admit(self, key: str, info: dict | None = None) -> bool:
        with self._mu:
            if key in self._runs:
                return False
            self._runs[key] = {**(info or {}), "queued": True}
            return True

    def started(self, key: str) -> None:
        with self._mu:
            if key in self._runs:
                self._runs[key]["queued"] = False

    def get(self, key: str) -> dict | None:
        with self._mu:
            run = self._runs.get(key)
            return dict(run) if run else None

    def release(self, key: str) -> None:
        with self._mu:
            self._runs.pop(key, None)


_runs = LocalRunRegistry()


def jsonable(x: Any) -> Any:
    if isinstance(x, BaseModel):
        return x.model_dump()
    if isinstance(x, dict):
        return {k: jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [jsonable(v) for v in x]
    return x


class BaseRunner:
    """场景无关的部分：把图的 stream 变成 progress / node / interrupt / state 事件。子类定义状态视图与事件摘要。"""

    graph: Any
    out_dir: Path
    api_prefix = "/api/threads"
    runs: RunRegistry = _runs

    def thread_dir(self, thread_id: str) -> Path:
        """thread_id 来自 URL：先查格式，再确认解析后的路径仍在输出目录下。"""
        base = self.out_dir.resolve()
        if not valid_thread_id(thread_id) or (base / thread_id).resolve().parent != base:
            raise ValueError(f"非法 thread_id：{thread_id!r}")
        return self.out_dir / thread_id

    def file_url(self, thread_id: str, path: str | None) -> str | None:
        if not path:
            return None
        return f"{self.api_prefix}/{thread_id}/files/{Path(path).name}"

    def pending(self, thread_id: str):
        """当前挂起的中断（带 id 和 value）；没有就是 None。"""
        for task in self.graph.get_state({"configurable": {"thread_id": thread_id}}).tasks:
            for it in task.interrupts:
                return it
        return None

    def _run_key(self, thread_id: str) -> str:
        return f"{self.api_prefix}:{thread_id}"

    def state_view(self, thread_id: str) -> dict:
        raise NotImplementedError

    def _decorate_interrupt(self, thread_id: str, payload: dict | None) -> dict | None:
        return payload

    def _interrupt_view(self, thread_id: str, it) -> dict | None:
        return self._decorate_interrupt(thread_id, it.value)

    def _summarize(self, thread_id: str, node: str, update: dict | None) -> dict:
        return {"node": node}

    def resume_input(self, thread_id: str, payload: dict, interrupt_id: str | None = None) -> Any:
        return Command(resume=payload)

    # ---- 执行 ----

    def _events(self, thread_id: str, inp: Any):
        cfg = {"configurable": {"thread_id": thread_id}}
        # subgraphs=True 是为了拿到 agent 子图里写的进度（搜索、读页）；子图自己的节点更新不往外吐，
        # 子图里的中断会同时出现在子图和外层，只吐外层那一次
        for ns, mode, chunk in self.graph.stream(inp, cfg, stream_mode=["updates", "custom"], subgraphs=True):
            if mode == "custom":
                yield {"event": "progress", "data": jsonable(chunk)}
                continue
            if ns != ():
                continue
            for node, update in chunk.items():
                if node == "__interrupt__":
                    yield {"event": "interrupt", "data": self._interrupt_view(thread_id, update[0])}
                else:
                    yield {"event": "node", "data": self._summarize(thread_id, node, update)}

    def _submit(self, thread_id: str, make_input: Callable[[], Any], info: dict | None = None) -> AsyncIterator[str]:
        """占位 → 算出图的输入（校验在这一步，不通过就退位）→ 提交执行。占着位，别的请求改不了这个线程。"""
        key = self._run_key(thread_id)
        if not self.runs.admit(key, info):
            raise RunConflict("这一步还在处理，等它完成后再提交")
        try:
            return self._launch(thread_id, make_input())
        except BaseException:
            self.runs.release(key)
            raise

    def _launch(self, thread_id: str, inp: Any) -> AsyncIterator[str]:
        """立刻提交到工作线程，返回 SSE 文本行。执行结束才释放占位：浏览器断开、事件流没人读都照常跑完。"""
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()
        key = self._run_key(thread_id)

        def emit(ev: dict | None) -> None:
            try:
                loop.call_soon_threadsafe(q.put_nowait, ev)
            except RuntimeError:  # 接口那边的事件循环已经关了：没人听，继续跑
                pass

        def work():
            self.runs.started(key)
            try:
                for ev in self._events(thread_id, inp):
                    emit(ev)
            except Exception as e:  # noqa: BLE001
                emit({"event": "error", "data": {"message": str(e)}})
            finally:
                self.runs.release(key)
            try:
                emit({"event": "state", "data": self.state_view(thread_id)})
            except Exception as e:  # noqa: BLE001
                emit({"event": "error", "data": {"message": str(e)}})
            emit(None)

        q.put_nowait({"event": "thread", "data": {"thread_id": thread_id}})  # 排队时也先把 id 给页面，中途关掉也能回来
        _executor.submit(work)

        async def sse() -> AsyncIterator[str]:
            while (ev := await q.get()) is not None:
                yield f"event: {ev['event']}\ndata: {json.dumps(ev['data'], ensure_ascii=False)}\n\n"

        return sse()

    def resume(self, thread_id: str, payload: dict, interrupt_id: str | None = None) -> AsyncIterator[str]:
        return self._submit(thread_id, lambda: self.resume_input(thread_id, payload, interrupt_id))


class GraphRunner(BaseRunner):
    """海报场景。"""

    def __init__(self, fake: bool | None = None, data_dir: Path | None = None, out_dir: Path | None = None):
        fake = config.USE_FAKE_PLANNER if fake is None else fake
        self.out_dir = Path(out_dir or config.OUTPUT_DIR)
        self.planner = FakePlanner() if fake else ArkPlanner()
        self.graph = build_graph(self.planner, open_sqlite_checkpointer(Path(data_dir or config.DATA_DIR) / "checkpoints.sqlite"), self.out_dir)

    # ---- 查询 ----

    def state_view(self, thread_id: str) -> dict:
        snap = self.graph.get_state({"configurable": {"thread_id": thread_id}})
        v = snap.values
        if not v:
            return {"thread_id": thread_id, "exists": False}
        facts = v.get("facts", {})
        pending = None
        for task in snap.tasks:
            for it in task.interrupts:
                pending = it.value
        return {
            "thread_id": thread_id,
            "exists": True,
            "next": list(snap.next),
            "request": v.get("request"),
            "facts": {k: f.model_dump() for k, f in facts.items()},
            "style": jsonable(v.get("style")),
            "elements": [_element_view(e, facts) for e in v.get("elements", [])],
            "versions": [
                {"n": x.n, "note": x.note, "png_url": self.file_url(thread_id, x.png_path), "html_url": self.file_url(thread_id, x.html_path)}
                for x in v.get("versions", [])
            ],
            "checks": jsonable(v.get("check_results", [])),
            "notes": v.get("notes", []),
            "exported": self._export_view(thread_id, v.get("exported")),
            "interrupt": self._decorate_interrupt(thread_id, pending),
        }

    def _export_view(self, thread_id: str, exported: dict | None) -> dict | None:
        if not exported:
            return None
        return {
            "version": exported["version"],
            "png_url": self.file_url(thread_id, exported["png"]),
            "html_url": self.file_url(thread_id, exported["html"]),
            "brief_url": self.file_url(thread_id, exported["brief"]),
            "zip_url": f"/api/threads/{thread_id}/export",
        }

    def _decorate_interrupt(self, thread_id: str, payload: dict | None) -> dict | None:
        if not payload:
            return None
        payload = dict(payload)
        if payload.get("type") == "review":
            payload["png_url"] = self.file_url(thread_id, payload.get("png_path"))
            payload["html_url"] = self.file_url(thread_id, payload.get("html_path"))
        return payload

    def _summarize(self, thread_id: str, node: str, update: dict | None) -> dict:
        out = {"node": node}
        if not update:
            return out
        if node == "render" and update.get("versions"):
            v = update["versions"][-1]
            out.update({"version": v.n, "note": v.note, "png_url": self.file_url(thread_id, v.png_path)})
        if node == "check":
            out["checks"] = jsonable(update.get("check_results", []))
        if update.get("notes"):
            out["notes"] = update["notes"]
        if "last_change" in update:
            out["changed"] = update["last_change"]
        if node == "export":
            out["exported"] = self._export_view(thread_id, update.get("exported"))
        return out

    def start(self, thread_id: str, request: str, product_image: str | None):
        return self._submit(thread_id, lambda: {"request": request, "product_image": product_image})
