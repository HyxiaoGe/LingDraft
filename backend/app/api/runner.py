"""把同步的图执行放到工作线程里，事件写进事件日志，再以 SSE 的形式吐给接口层。

海报场景只用一个工作线程：Playwright 同步浏览器绑定线程，PoC 不需要并发渲染；调研场景的执行器有多个线程（见 ResearchRunner）。
同一个线程同时只能有一次执行：请求被接受时就占位（排队也算），执行结束才释放，浏览器断开不影响。
执行的事件先进日志、再由连接去读：断线或刷新后按事件 id 接着读，执行本身不受连接影响。
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from langgraph.types import Command
from pydantic import BaseModel

from app import config
from app.graph.poster.graph import build_graph, open_sqlite_checkpointer
from app.graph.poster.nodes import _element_view
from app.llm import ArkPlanner, FakePlanner

_THREAD_ID = re.compile(r"[0-9A-Za-z_-]{1,64}")
POLL_S = 0.2  # 连接查日志的间隔
HEARTBEAT_S = 15  # 这么久没有事件就发一行注释保活：nginx 之类的代理默认 60 秒没数据就断开


def valid_thread_id(thread_id: str) -> bool:
    return bool(_THREAD_ID.fullmatch(thread_id))


class RunConflict(Exception):
    """这个线程已经有一次执行在排队或运行。"""


class RunRegistry(Protocol):
    """线程的运行占位：页面刷新回来时据此判断“还在跑”，重复提交据此拒绝。

    admit 是原子的“占位成功 / 已被占用”，调用方不加锁；多 worker 部署时换成共享存储的实现（如 Redis SET NX），接口不变。
    get 返回 {"queued": 是否还在排队, "since": 开始执行的时间（ISO，开始后才有）, "stopping": 用户是不是点了停止}。
    request_stop 给占着位的执行记一个“要停”，执行自己去查（stop_requested）、自己停；没有执行占着位返回 False。
    """

    def admit(self, key: str) -> bool: ...
    def started(self, key: str) -> None: ...
    def get(self, key: str) -> dict | None: ...
    def request_stop(self, key: str) -> bool: ...
    def stop_requested(self, key: str) -> bool: ...
    def release(self, key: str) -> None: ...


class LocalRunRegistry:
    """进程内实现，单进程部署够用。"""

    def __init__(self) -> None:
        self._mu = threading.Lock()
        self._runs: dict[str, dict] = {}

    def admit(self, key: str) -> bool:
        with self._mu:
            if key in self._runs:
                return False
            self._runs[key] = {"queued": True, "stopping": False}
            return True

    def started(self, key: str) -> None:
        with self._mu:
            if key in self._runs:
                self._runs[key].update(queued=False, since=datetime.now(UTC).isoformat(timespec="seconds"))

    def get(self, key: str) -> dict | None:
        with self._mu:
            run = self._runs.get(key)
            return dict(run) if run else None

    def request_stop(self, key: str) -> bool:
        with self._mu:
            if key not in self._runs:
                return False
            self._runs[key]["stopping"] = True
            return True

    def stop_requested(self, key: str) -> bool:
        with self._mu:
            return bool(self._runs.get(key, {}).get("stopping"))

    def release(self, key: str) -> None:
        with self._mu:
            self._runs.pop(key, None)


_runs = LocalRunRegistry()


class EventLog(Protocol):
    """每个线程最近一次执行的事件，按序号编号（1 起）；页面断线或刷新后从序号接着读。

    open 开始一次执行并把它记成这个线程的当前执行；append / close 只写自己那次执行，旧执行收尾时写不进新执行。
    close 追加一个 end 事件。多 worker 部署时换成共享存储的实现（如 Redis Stream），接口不变。
    """

    def open(self, key: str) -> str: ...
    def append(self, run: str, event: dict) -> None: ...
    def close(self, run: str) -> None: ...
    def current(self, key: str) -> str | None: ...
    def read(self, run: str, after: int) -> tuple[list[tuple[int, dict]], bool]: ...  # (序号大于 after 的事件, 是否已结束)


class LocalEventLog:
    """进程内实现：只留最近 max_runs 次执行。进程重启就没了，那时执行本身也停了，状态以检查点为准。"""

    def __init__(self, max_runs: int = 64) -> None:
        self._mu = threading.Lock()
        self._runs: OrderedDict[str, dict] = OrderedDict()
        self._current: dict[str, str] = {}
        self._max = max_runs

    def open(self, key: str) -> str:
        run = uuid.uuid4().hex[:8]
        with self._mu:
            self._runs[run] = {"key": key, "events": [], "ended": False}
            self._current[key] = run
            ended = [r for r, log in self._runs.items() if log["ended"]]
            for r in ended[: max(0, len(self._runs) - self._max)]:  # 只丢已结束的，最早的先丢
                log = self._runs.pop(r)
                if self._current.get(log["key"]) == r:
                    del self._current[log["key"]]
        return run

    def append(self, run: str, event: dict) -> None:
        with self._mu:
            log = self._runs.get(run)
            if log and not log["ended"]:
                log["events"].append(event)

    def close(self, run: str) -> None:
        with self._mu:
            log = self._runs.get(run)
            if log and not log["ended"]:
                log["events"].append({"event": "end", "data": {}})
                log["ended"] = True

    def current(self, key: str) -> str | None:
        with self._mu:
            return self._current.get(key)

    def read(self, run: str, after: int) -> tuple[list[tuple[int, dict]], bool]:
        with self._mu:
            log = self._runs.get(run)
            if log is None:
                return [], True
            return list(enumerate(log["events"][after:], start=after + 1)), log["ended"]


_log = LocalEventLog()


class Executor:
    """先进先出的线程池，记着已提交、没跑完的任务数，据此判断新任务要不要排队。"""

    def __init__(self, workers: int, name: str) -> None:
        self.workers = workers
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=name)
        self._mu = threading.Lock()
        self._inflight = 0

    def reserve(self) -> bool:
        """为接下来的 submit 占一个名额；返回 True 表示前面没跑完的已经占满线程，这次要排队。"""
        with self._mu:
            self._inflight += 1
            return self._inflight > self.workers

    def submit(self, fn: Callable[[], None]) -> None:
        """提交 reserve 过的任务，跑完归还名额；提交本身失败时由调用方 release。"""
        def run():
            try:
                fn()
            finally:
                self.release()

        self._pool.submit(run)

    def release(self) -> None:
        with self._mu:
            self._inflight -= 1


_poster_executor = Executor(1, "graph")


def _sse_text(ev: dict, eid: str | None = None) -> str:
    head = f"id: {eid}\n" if eid else ""
    return f"{head}event: {ev['event']}\ndata: {json.dumps(ev['data'], ensure_ascii=False)}\n\n"


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
    events: EventLog = _log
    executor: Executor = _poster_executor

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

    def _graph_events(self, thread_id: str, inp: Any):
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

    def _submit(self, thread_id: str, make_input: Callable[[], Any]) -> AsyncIterator[str]:
        """占位 → 算出图的输入（校验在这一步，不通过就退位；新的调研线程在这一步先把需求落盘）→ 提交执行。占着位，别的请求改不了这个线程。"""
        key = self._run_key(thread_id)
        if not self.runs.admit(key):
            raise RunConflict("这一步还在处理，等它完成后再提交")
        try:
            return self._launch(thread_id, make_input())
        except BaseException:
            self.runs.release(key)
            raise

    def _launch(self, thread_id: str, inp: Any) -> AsyncIterator[str]:
        """开一段新的事件日志并提交执行，返回读这段日志的 SSE。执行结束才释放占位：浏览器断开、没人读都照常跑完。"""
        key = self._run_key(thread_id)
        run = self.events.open(key)
        queued = self.executor.reserve()

        def work():
            try:
                self.runs.started(key)
                self.events.append(run, {"event": "started", "data": {}})
                try:
                    for ev in self._graph_events(thread_id, inp):
                        self.events.append(run, ev)
                except Exception as e:  # noqa: BLE001
                    self.events.append(run, {"event": "error", "data": {"message": str(e)}})
                finally:
                    self.runs.release(key)
                try:
                    self.events.append(run, {"event": "state", "data": self.state_view(thread_id)})
                except Exception as e:  # noqa: BLE001
                    self.events.append(run, {"event": "error", "data": {"message": str(e)}})
            finally:
                self.events.close(run)

        try:
            self.events.append(run, {"event": "thread", "data": {"thread_id": thread_id, "queued": queued}})  # 排队时也先把 id 给页面
            self.executor.submit(work)
        except BaseException:
            self.executor.release()
            self.events.close(run)
            raise
        return self._stream(run)

    async def _stream(self, run: str, after: int = 0) -> AsyncIterator[str]:
        """把一次执行里序号大于 after 的事件吐成 SSE（id 是“执行-序号”），读到 end 为止；安静太久就发注释行保活。"""
        quiet = 0.0
        while True:
            evs, ended = self.events.read(run, after)
            for n, ev in evs:
                after = n
                yield _sse_text(ev, f"{run}-{n}")
            if ended:
                return
            await asyncio.sleep(POLL_S)
            quiet = 0.0 if evs else quiet + POLL_S
            if quiet >= HEARTBEAT_S:
                quiet = 0.0
                yield ": ping\n\n"

    def follow(self, thread_id: str, after: str | None = None) -> AsyncIterator[str]:
        """接上这个线程当前的执行。after 是页面收到的最后一个事件 id。

        执行还没结束：属于这次执行就从它后面接着读，否则（刷新过、或是上一次执行的 id）从头补发。
        已经结束：只补这个页面自己那次执行没收到的尾巴；其余情况（没见过这次执行、日志没了、进程重启过）只发当前状态。
        """
        run = self.events.current(self._run_key(thread_id))
        prev, _, n = (after or "").rpartition("-")
        if run is not None:
            start = int(n) if prev == run and n.isdigit() else 0
            evs, ended = self.events.read(run, start)
            if not ended or (start and evs):
                return self._stream(run, start)
        return self._snapshot(thread_id)

    async def _snapshot(self, thread_id: str) -> AsyncIterator[str]:
        yield _sse_text({"event": "state", "data": self.state_view(thread_id)})
        yield _sse_text({"event": "end", "data": {}})

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
