"""调研场景的接口层运行器：状态视图、中断整形、节点事件摘要。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langgraph.types import Command

from app import config
from app.api.runner import BaseRunner, jsonable
from app.graph.research.agent import ask_view, is_ask_interrupt
from app.graph.research.answers import resume_value
from app.graph.research.graph import build_graph, open_sqlite_checkpointer
from app.research_llm import FakeResearchPlanner, LiveResearchPlanner


class StaleInterrupt(Exception):
    """回答对着的不是线程当前等着的那张卡。"""


class ResearchRunner(BaseRunner):
    api_prefix = "/api/research/threads"

    def __init__(self, fake: bool | None = None, data_dir: Path | None = None, out_dir: Path | None = None):
        fake = config.USE_FAKE_PLANNER if fake is None else fake
        self.out_dir = Path(out_dir or config.OUTPUT_DIR / "research")
        self.planner = FakeResearchPlanner() if fake else LiveResearchPlanner()
        saver = open_sqlite_checkpointer(Path(data_dir or config.DATA_DIR) / "checkpoints_research.sqlite")
        self.graph = build_graph(self.planner, saver, self.out_dir)

    def start(self, thread_id: str, request: str):
        return self._submit(thread_id, lambda: {"request": request}, info={"request": request})

    def resume_input(self, thread_id: str, payload: dict, interrupt_id: str | None = None) -> Any:
        """回答要对着当前这张卡（中断 id 一致）并通过校验，才交给图；按 id 恢复，图里不会错配到别的中断。"""
        it = self.pending(thread_id)
        if it is None or it.id != interrupt_id:
            raise StaleInterrupt("这张卡已经过期，线程已经往前走了；请按当前进度重新回答")
        return Command(resume={it.id: resume_value(it.value, payload)})

    # ---- 视图 ----

    def _versions(self, thread_id: str, versions: list) -> list[dict]:
        return [{"n": v.n, "note": v.note, "md_url": self.file_url(thread_id, v.path)} for v in versions]

    def _export_view(self, thread_id: str, exported: dict | None) -> dict | None:
        if not exported:
            return None
        base = f"{self.api_prefix}/{thread_id}/package"
        return {
            "version": exported["version"],
            "report_url": f"{base}/report.md",
            "items_url": f"{base}/items.json",
            "sources_url": f"{base}/sources.json",
            "manifest_url": f"{base}/manifest.json",
            "zip_url": f"{self.api_prefix}/{thread_id}/export",
        }

    def state_view(self, thread_id: str) -> dict:
        # 先查占位再取检查点：执行恰好在两步之间结束时，拿到的是结束后的检查点，不会把上一张卡当成当前的
        run = self.runs.get(self._run_key(thread_id))
        snap = self.graph.get_state({"configurable": {"thread_id": thread_id}})
        v = snap.values
        if not v:
            if run is None:
                return {"thread_id": thread_id, "exists": False}
            # 请求已接受、图还没写下第一个检查点（多半在排队）：先能查到，页面刷新回来才不会 404
            return {"thread_id": thread_id, "exists": True, "scenario": "research", "next": [], "running": True,
                    "queued": run["queued"], "request": run.get("request"), "interrupt": None}
        pending = next((it for task in snap.tasks for it in task.interrupts), None)
        return {
            "thread_id": thread_id,
            "exists": True,
            "scenario": "research",
            "next": list(snap.next),
            "running": run is not None,
            "queued": bool(run and run["queued"]),
            "request": v.get("request"),
            "scope": jsonable(v.get("scope")),
            "scope_confirmed": v.get("scope_confirmed", False),
            "outline": jsonable(v.get("outline", [])),
            "answer": v.get("answer"),
            "answer_how": v.get("answer_how", "none"),
            "gap_rounds": v.get("gap_rounds", 0),
            "sources": jsonable(v.get("sources", [])),
            "claims": jsonable(v.get("claims", [])),
            "assumptions": v.get("assumptions", []),
            "asks": jsonable(v.get("asks", [])),
            "asks_used": v.get("asks_used", 0),
            "sections": jsonable(v.get("sections", {})),
            "versions": self._versions(thread_id, v.get("versions", [])),
            "checks": jsonable(v.get("check_results", [])),
            "notes": v.get("notes", []),
            "exported": self._export_view(thread_id, v.get("exported")),
            # 跑着的时候检查点里可能还是上一张卡，不给出去
            "interrupt": self._interrupt_view(thread_id, pending) if pending and run is None else None,
        }

    def _interrupt_view(self, thread_id: str, it) -> dict | None:
        view = self._decorate_interrupt(thread_id, it.value)
        return {**view, "id": it.id} if view else None  # 回答时带回来，对不上就是过期的卡

    def _decorate_interrupt(self, thread_id: str, payload: dict | None) -> dict | None:
        if not payload:
            return None
        if is_ask_interrupt(payload):
            return ask_view(payload)
        payload = dict(payload)
        if payload.get("type") == "research.review":
            payload["md_url"] = self.file_url(thread_id, payload.get("report_path"))
        return payload

    def _summarize(self, thread_id: str, node: str, update: dict | None) -> dict:
        out = {"node": node}
        if not update:
            return out
        if node == "confirm_scope" and update.get("scope"):
            out["scope"] = jsonable(update["scope"])
        if node == "research":
            out["sources"] = len(update.get("sources", []))
            out["answer_how"] = update.get("answer_how")
        if node == "consolidate":
            out["sections"] = len(update.get("outline", []))
            out["claims"] = len(update.get("claims", []))
            out["gap"] = update.get("gap")
        if node == "write" and update.get("versions"):
            v = update["versions"][-1]
            out.update({"version": v.n, "note": v.note, "md_url": self.file_url(thread_id, v.path)})
        if node == "check":
            out["checks"] = jsonable(update.get("check_results", []))
        if update.get("notes"):
            out["notes"] = update["notes"]
        if node == "export":
            out["exported"] = self._export_view(thread_id, update.get("exported"))
        return out
