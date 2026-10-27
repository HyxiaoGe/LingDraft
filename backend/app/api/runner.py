"""把同步的图执行放到单个工作线程里，以 SSE 事件流的形式吐给接口层。

单线程的原因：Playwright 同步浏览器绑定线程；PoC 不需要并发渲染。
"""
from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from langgraph.types import Command
from pydantic import BaseModel

from app import config
from app.graph.poster.graph import build_graph, open_sqlite_checkpointer
from app.graph.poster.nodes import _element_view
from app.llm import ArkPlanner, FakePlanner

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="graph")
_lock = threading.Lock()


def jsonable(x: Any) -> Any:
    if isinstance(x, BaseModel):
        return x.model_dump()
    if isinstance(x, dict):
        return {k: jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [jsonable(v) for v in x]
    return x


class GraphRunner:
    def __init__(self, fake: bool | None = None, data_dir: Path | None = None, out_dir: Path | None = None):
        fake = config.USE_FAKE_PLANNER if fake is None else fake
        self.out_dir = Path(out_dir or config.OUTPUT_DIR)
        self.planner = FakePlanner() if fake else ArkPlanner()
        self.graph = build_graph(self.planner, open_sqlite_checkpointer(Path(data_dir or config.DATA_DIR) / "checkpoints.sqlite"), self.out_dir)

    # ---- 查询 ----

    def thread_dir(self, thread_id: str) -> Path:
        return self.out_dir / thread_id

    def file_url(self, thread_id: str, path: str | None) -> str | None:
        if not path:
            return None
        return f"/api/threads/{thread_id}/files/{Path(path).name}"

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

    # ---- 执行 ----

    def _events(self, thread_id: str, inp: Any):
        cfg = {"configurable": {"thread_id": thread_id}}
        with _lock:
            for mode, chunk in self.graph.stream(inp, cfg, stream_mode=["updates", "custom"]):
                if mode == "custom":
                    yield {"event": "progress", "data": jsonable(chunk)}
                    continue
                for node, update in chunk.items():
                    if node == "__interrupt__":
                        yield {"event": "interrupt", "data": self._decorate_interrupt(thread_id, update[0].value)}
                        continue
                    yield {"event": "node", "data": self._summarize(thread_id, node, update)}
            yield {"event": "state", "data": self.state_view(thread_id)}

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

    async def stream(self, thread_id: str, inp: Any) -> AsyncIterator[str]:
        """把工作线程里的事件转成 SSE 文本行。"""
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()

        def work():
            try:
                for ev in self._events(thread_id, inp):
                    loop.call_soon_threadsafe(q.put_nowait, ev)
            except Exception as e:  # noqa: BLE001
                loop.call_soon_threadsafe(q.put_nowait, {"event": "error", "data": {"message": str(e)}})
            finally:
                loop.call_soon_threadsafe(q.put_nowait, None)

        _executor.submit(work)
        while True:
            ev = await q.get()
            if ev is None:
                break
            yield f"event: {ev['event']}\ndata: {json.dumps(ev['data'], ensure_ascii=False)}\n\n"

    def start(self, thread_id: str, request: str, product_image: str | None):
        return self.stream(thread_id, {"request": request, "product_image": product_image})

    def resume(self, thread_id: str, payload: dict):
        return self.stream(thread_id, Command(resume=payload))
