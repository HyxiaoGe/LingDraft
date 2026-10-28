"""调研场景的接口层运行器：状态视图、中断整形、节点事件摘要。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langgraph.graph import START
from langgraph.types import Command

from app import config
from app.api.runner import BaseRunner, Executor, jsonable
from app.graph.research.agent import ask_view, is_ask_interrupt
from app.graph.research.answers import resume_value
from app.graph.research.graph import build_graph, open_sqlite_checkpointer
from app.research_llm import FakeResearchPlanner, LiveResearchPlanner


class StaleInterrupt(Exception):
    """回答对着的不是线程当前等着的那张卡。"""


class NothingToRetry(Exception):
    """线程没有失败（在等用户、已完成），不需要重试。"""


CUT_SHORT = "上次运行没跑完就停了（多半是服务重启过）"
_INTERRUPT = "__interrupt__"  # 检查点里中断写入的通道名；LangGraph 1.0 起把这个常量设成了私有，改名的话停在提问卡上的测试会先挂


class ResearchRunner(BaseRunner):
    api_prefix = "/api/research/threads"

    def __init__(self, fake: bool | None = None, data_dir: Path | None = None, out_dir: Path | None = None, workers: int | None = None):
        fake = config.USE_FAKE_PLANNER if fake is None else fake
        self.out_dir = Path(out_dir or config.OUTPUT_DIR / "research")
        self.planner = FakeResearchPlanner() if fake else LiveResearchPlanner()
        saver = open_sqlite_checkpointer(Path(data_dir or config.DATA_DIR) / "checkpoints_research.sqlite")
        self.graph = build_graph(self.planner, saver, self.out_dir)
        # 调研不碰浏览器，几个线程各跑各的；检查点共用一个连接，SqliteSaver 自己加锁
        self.executor = Executor(workers or config.RESEARCH_WORKERS, "research")

    def start(self, thread_id: str, request: str):
        return self._submit(thread_id, lambda: self.accept(thread_id, request))

    def accept(self, thread_id: str, request: str) -> None:
        """把线程 id 交给页面之前，先把需求写进检查点，图从这里接着跑（输入为 None）。

        排队时服务重启也查得到这个线程：没在跑、没走完，算失败，可以重试。
        """
        self.graph.update_state({"configurable": {"thread_id": thread_id}}, {"request": request}, as_node=START)
        return None

    def retry(self, thread_id: str):
        return self._submit(thread_id, lambda: self.retry_input(thread_id))

    def retry_input(self, thread_id: str) -> None:
        """只有失败的线程能重试：从最后一个检查点接着跑（输入为 None），失败那一步重做，之前的步骤和 agent 里做过的搜索都不重来。"""
        snap = self.graph.get_state({"configurable": {"thread_id": thread_id}})
        if not snap.values or not snap.next or self._pending(thread_id, snap) is not None:
            raise NothingToRetry("这个线程没有失败，不需要重试")
        return None

    def pending(self, thread_id: str):
        return self._pending(thread_id, self.graph.get_state({"configurable": {"thread_id": thread_id}}))

    def _pending(self, thread_id: str, snap):
        """线程在等的那张卡；没有就是 None。

        agent 里的提问会同时挂在 research 节点上。回答已经被 agent 用上、后面某一步出错（或进程停了）时，
        外层那条中断还留着，子图里却已经没有了：这张卡不再等人回答，再答也不会被用上——线程算失败，重试会带着已给的回答接着跑。
        """
        for task in snap.tasks:
            for it in task.interrupts:
                if not self._answered(thread_id, task, it):
                    return it
        return None

    def _answered(self, thread_id: str, task, it) -> bool:
        sub = self.graph.checkpointer.get_tuple({"configurable": {"thread_id": thread_id, "checkpoint_ns": f"{task.name}:{task.id}"}})
        if sub is None:  # 节点自己发的中断（澄清、终审）：回答通过之前不会写进检查点，一直有效
            return False
        return not any(ch == _INTERRUPT and any(getattr(i, "id", None) == it.id for i in v) for _, ch, v in sub.pending_writes)

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
        if not v:  # 线程 id 交出去之前需求已经写进检查点（见 accept），没有就是不存在
            return {"thread_id": thread_id, "exists": False}
        pending = self._pending(thread_id, snap)
        status, error = self._status(snap, run, pending)
        return {
            "thread_id": thread_id,
            "exists": True,
            "scenario": "research",
            "next": list(snap.next),
            "status": status,
            "error": error,
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

    @staticmethod
    def _status(snap, run: dict | None, pending) -> tuple[str, str | None]:
        """queued / running：有执行占着位；waiting：停在卡上等用户；done：图走完了；failed：没在跑、没在等、也没走完。

        failed 有两种：某一步抛了错（错误记在检查点里，重启后也在），或执行半路被掐断（进程重启，没有错误记录）。
        """
        if run is not None:
            return ("queued" if run["queued"] else "running"), None
        if pending is not None:
            return "waiting", None
        if not snap.next:
            return "done", None
        err = next((t.error for t in snap.tasks if t.error), None)
        return "failed", str(err) if err else CUT_SHORT

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
