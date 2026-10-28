"""调研场景的接口层运行器：状态视图、中断整形、节点事件摘要。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langgraph.types import Command

from app import config
from app.api.runner import BaseRunner, jsonable
from app.graph.research.agent import ask_resume, ask_view, is_ask_interrupt
from app.graph.research.graph import build_graph, open_sqlite_checkpointer
from app.graph.research.state import AskAnswer
from app.research_llm import FakeResearchPlanner, LiveResearchPlanner


class ResearchRunner(BaseRunner):
    api_prefix = "/api/research/threads"

    def __init__(self, fake: bool | None = None, data_dir: Path | None = None, out_dir: Path | None = None):
        fake = config.USE_FAKE_PLANNER if fake is None else fake
        self.out_dir = Path(out_dir or config.OUTPUT_DIR / "research")
        self.planner = FakeResearchPlanner() if fake else LiveResearchPlanner()
        saver = open_sqlite_checkpointer(Path(data_dir or config.DATA_DIR) / "checkpoints_research.sqlite")
        self.graph = build_graph(self.planner, saver, self.out_dir)

    def start(self, thread_id: str, request: str):
        return self.stream(thread_id, {"request": request})

    def resume_input(self, thread_id: str, payload: dict) -> Any:
        pending = self.pending_interrupt(thread_id)
        if is_ask_interrupt(pending):
            a = AskAnswer.model_validate(payload)
            return Command(resume=ask_resume(a.answer, a.skip, pending))
        return Command(resume=payload)

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
        snap = self.graph.get_state({"configurable": {"thread_id": thread_id}})
        v = snap.values
        if not v:
            return {"thread_id": thread_id, "exists": False}
        pending = None
        for task in snap.tasks:
            for it in task.interrupts:
                pending = it.value
        return {
            "thread_id": thread_id,
            "exists": True,
            "scenario": "research",
            "next": list(snap.next),
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
            "interrupt": self._decorate_interrupt(thread_id, pending),
        }

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
