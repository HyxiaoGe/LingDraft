"""调研场景的接口层运行器：接用户的话、把 agent 的流变成页面的事件、线程状态视图。"""

from __future__ import annotations

import ast
import time
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import START
from langgraph.types import Command

from app import config
from app.api.runner import BaseRunner, Executor, jsonable
from app.graph.research import history, workbench
from app.graph.research.answers import ask_view, is_ask_interrupt, resume_value, user_message
from app.graph.research.graph import build_graph, open_sqlite_checkpointer, thread_ids
from app.graph.research.state import Turn
from app.graph.research.workbench import Draft
from app.research_llm import FakeResearchPlanner, LiveResearchPlanner, ResearchPlanner


class StaleInterrupt(Exception):
    """回答对着的不是线程当前等着的那张卡。"""


class NothingToRetry(Exception):
    """线程没有失败（在等用户、空闲），不需要重试。"""


class NotIdle(Exception):
    """线程在等提问的回答，或者上一轮没跑完：这时不能接着说。"""


CUT_SHORT = "上次运行没跑完就停了（多半是服务重启过）"
DELTA_S = 0.08  # 逐字片段攒这么久发一次
WRITING_TOOLS = ("write_report", "revise_section")  # 参数就是报告草稿：模型写的时候报字数


class StreamBuffer:
    """把模型的流式片段攒成事件：思考和正文按（消息 id，种类）合并，写报告草稿只报字数，约每 80ms 发一次。"""

    def __init__(self) -> None:
        self._deltas: list[dict] = []
        self._writing: dict[str, dict] = {}  # 消息 id → {index: {name, chars}}
        self._dirty: set[str] = set()
        self._last = time.monotonic()

    def add(self, chunk: AIMessageChunk) -> list[dict]:
        mid = chunk.id or ""
        thinking = chunk.additional_kwargs.get("reasoning_content")
        for kind, text in (("thinking", thinking), ("text", chunk.content if isinstance(chunk.content, str) else "")):
            if not text:
                continue
            if self._deltas and self._deltas[-1]["id"] == mid and self._deltas[-1]["kind"] == kind:
                self._deltas[-1]["text"] += text
            else:
                self._deltas.append({"id": mid, "kind": kind, "text": text})
        calls = self._writing.setdefault(mid, {})
        for c in chunk.tool_call_chunks or []:
            slot = calls.setdefault(c.get("index") or 0, {"name": "", "id": "", "chars": 0})
            slot["name"] = slot["name"] or c.get("name") or ""
            slot["id"] = slot["id"] or c.get("id") or ""
            slot["chars"] += len(c.get("args") or "")
            if slot["name"] in WRITING_TOOLS:
                self._dirty.add(mid)
        return self.flush() if time.monotonic() - self._last >= DELTA_S else []

    def flush(self) -> list[dict]:
        out = [{"event": "delta", "data": d} for d in self._deltas]
        for mid in sorted(self._dirty):
            for slot in self._writing[mid].values():
                if slot["name"] in WRITING_TOOLS:
                    out.append({"event": "writing", "data": {"message_id": mid, "id": slot["id"], "tool": slot["name"], "chars": slot["chars"]}})
        self._deltas, self._dirty, self._last = [], set(), time.monotonic()
        return out


def conversation(messages: list[BaseMessage], turns: list[Turn]) -> list[dict]:
    """对话按轮：用户的话，然后这一轮里模型的思考、正文、工具步骤、提问按先后排，被内容审核拦下时最后是一条 blocked。
    每轮带这一轮的记录（版本、推荐问题、用量）。"""
    by_id = {t.id: t for t in turns}
    results = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)}
    out: list[dict] = []
    cur: dict | None = None
    for m in messages:
        if isinstance(m, HumanMessage):
            t = by_id.get(m.id)
            cur = {"id": m.id, "user": history.said(m), "items": [], "record": t.model_dump(exclude={"id"}) if t else None}
            out.append(cur)
            continue
        if cur is None or not isinstance(m, AIMessage):
            continue
        items = cur["items"]
        if reason := history.blocked_reason(m):
            items.append({"type": "blocked", "id": m.id, "reason": reason})
            continue
        if thinking := m.additional_kwargs.get("reasoning_content"):
            items.append({"type": "thinking", "id": m.id, "text": thinking, "ms": m.response_metadata.get("thinking_ms")})
        if text := history.text_of(m).strip():
            items.append({"type": "text", "id": m.id, "text": text})
        for tc in m.tool_calls:
            res = results.get(tc["id"])
            if tc["name"] == "ask_user":
                data = history.ask_result(res) if res is not None else {}
                if data.get("unshown"):
                    continue
                args = tc.get("args") or {}
                items.append({"type": "ask", "id": tc["id"], "question": str(args.get("question") or ""), "options": list(args.get("options") or []),
                              "why": str(args.get("why") or ""), "answer": data.get("answer"), "skipped": bool(data.get("skipped")),
                              "pending": res is None})
            elif step := history.step_of(tc, res):
                items.append({"type": "step", **step.model_dump()})
    return out


def _error_text(err: str) -> str:
    """检查点里存的是异常的 repr（ModelUnavailable('…')），页面上只要里面那句话；解不开就原样给。"""
    name, sep, rest = err.partition("(")
    if sep and name.isidentifier() and rest.endswith(")"):
        try:
            msg = ast.literal_eval("(" + rest)
        except (ValueError, SyntaxError):
            return err
        if isinstance(msg, str):
            return msg
    return err


class ResearchRunner(BaseRunner):
    api_prefix = "/api/research/threads"

    def __init__(self, fake: bool | None = None, data_dir: Path | None = None, out_dir: Path | None = None, workers: int | None = None,
                 planner: ResearchPlanner | None = None, checkpointer: BaseCheckpointSaver | None = None):
        fake = config.USE_FAKE_PLANNER if fake is None else fake
        self.out_dir = Path(out_dir or config.OUTPUT_DIR / "research")
        self.planner = planner or (FakeResearchPlanner() if fake else LiveResearchPlanner())
        saver = checkpointer or open_sqlite_checkpointer(Path(data_dir or config.DATA_DIR) / "checkpoints_research.sqlite")
        self.graph = build_graph(self.planner, saver, self.out_dir)
        # 调研不碰浏览器，几个线程各跑各的；检查点共用一个连接，SqliteSaver 自己加锁
        self.executor = Executor(workers or config.RESEARCH_WORKERS, "research")

    @staticmethod
    def _cfg(thread_id: str) -> dict:
        return {"configurable": {"thread_id": thread_id}}

    # ---- 执行 ----

    def start(self, thread_id: str, text: str):
        return self._submit(thread_id, lambda: self.accept(thread_id, text))

    def say(self, thread_id: str, text: str, quote: dict | None = None, action: dict | None = None):
        return self._submit(thread_id, lambda: self.accept(thread_id, text, quote, action))

    def accept(self, thread_id: str, text: str, quote: dict | None = None, action: dict | None = None) -> None:
        """把用户的话写进检查点，agent 从这里接着跑（输入为 None）。

        先落盘再交出线程 id：排队时服务重启也查得到这句话，算没跑完，可以重试。
        """
        snap = self.graph.get_state(self._cfg(thread_id))
        if snap.values and snap.interrupts:
            raise NotIdle("先回答上面的提问，或者跳过")
        if snap.values and snap.next:
            raise NotIdle("上一轮没跑完，先重试")
        msg = user_message(snap.values or {}, text, quote, action)
        self.graph.update_state(self._cfg(thread_id), {"messages": [msg]}, as_node=START)
        return None

    def retry(self, thread_id: str):
        return self._submit(thread_id, lambda: self.retry_input(thread_id))

    def retry_input(self, thread_id: str) -> None:
        """只有没跑完的线程能重试：从最后一个检查点接着跑，出错那一步重做，之前做过的搜索、读页都不重来。"""
        snap = self.graph.get_state(self._cfg(thread_id))
        if not snap.values or not snap.next or snap.interrupts:
            raise NothingToRetry("这个线程没有失败，不需要重试")
        return None

    def pending(self, thread_id: str):
        snap = self.graph.get_state(self._cfg(thread_id))
        return snap.interrupts[0] if snap.interrupts else None

    def resume_input(self, thread_id: str, payload: dict, interrupt_id: str | None = None) -> Any:
        """回答要对着当前这张卡（中断 id 一致）并通过校验，才交给 agent。"""
        it = self.pending(thread_id)
        if it is None or it.id != interrupt_id:
            raise StaleInterrupt("这张卡已经过期，线程已经往前走了；请按当前进度重新回答")
        return Command(resume={it.id: resume_value(it.value, payload)})

    def _graph_events(self, thread_id: str, inp: Any):
        """agent 的流 → 页面事件。messages 只转发 model 节点的（快模型在别的节点里调用，不算）；
        custom 是中间件写的进度（step）和标题（title）；updates 里改了 state 的节点做完发 node，页面据此重新取状态。"""
        buf = StreamBuffer()
        for mode, chunk in self.graph.stream(inp, self._cfg(thread_id), stream_mode=["messages", "updates", "custom"]):
            if mode == "messages":
                msg, meta = chunk
                if meta.get("langgraph_node") == "model" and isinstance(msg, AIMessageChunk):
                    yield from buf.add(msg)
                continue
            yield from buf.flush()
            if mode == "custom":
                yield {"event": chunk["type"], "data": jsonable({k: v for k, v in chunk.items() if k != "type"})}
                continue
            for node, update in chunk.items():
                if node == "__interrupt__":
                    yield {"event": "interrupt", "data": self._interrupt_view(thread_id, update[0])}
                elif update:  # 没改 state 的中间件节点不发
                    yield {"event": "node", "data": {"node": node}}
        yield from buf.flush()

    # ---- 视图 ----

    def state_view(self, thread_id: str) -> dict:
        # 先查占位再取检查点：执行恰好在两步之间结束时，拿到的是结束后的检查点
        run = self.runs.get(self._run_key(thread_id))
        snap = self.graph.get_state(self._cfg(thread_id))
        v = snap.values
        if not v:  # 线程 id 交出去之前用户的话已经写进检查点（见 accept），没有就是不存在
            return {"thread_id": thread_id, "exists": False}
        pending = snap.interrupts[0] if snap.interrupts else None
        status, error = self._status(snap, run, pending)
        return {
            "thread_id": thread_id,
            "exists": True,
            "scenario": "research",
            "status": status,
            "error": error,
            "title": v.get("title") or history.requests(v["messages"])[0],
            "updated_at": snap.created_at,  # 最近一个检查点的时间
            "running_since": run.get("since") if run else None,  # 这次执行开始的时间；排队中、没在跑为 None
            "turns": jsonable(conversation(v["messages"], v.get("turns", []))),
            "sources": {s.id: s.model_dump() for s in workbench.sources_of(v)},
            "report": self._report_view(v),
            "versions": [{"n": x.n, "note": x.note, "turn": x.turn} for x in v.get("versions", [])],
            # 跑着的时候检查点里可能还是上一张卡，不给出去
            "interrupt": self._interrupt_view(thread_id, pending) if pending and run is None else None,
        }

    @staticmethod
    def _report_view(v: dict) -> dict | None:
        """当前的报告。这一轮改过、还没结束时是工作稿（draft），版本号是下一版。"""
        d = Draft.of(v)
        if not d.outline:
            return None
        published = len(v.get("versions", []))
        draft = bool(v.get("changes"))
        claims = d.visible_claims()
        return {
            "version": published + 1 if draft else published,
            "draft": draft,
            "title": d.title,
            "blocks": workbench.blocks(v),
            "outline": jsonable(d.outline),
            "sections": jsonable(d.sections),
            "claims": jsonable(list(claims.values())),
            "assumptions": d.assumptions,
            "excluded": d.excluded,
            "time_issues": d.time_issues,
            "checks": jsonable(v.get("check_results", [])) if not draft else [],  # 校验是对着发布的那一版跑的
        }

    def summary_view(self, thread_id: str) -> dict | None:
        """任务列表里的一行：标题、状态、第几版。不存在返回 None。"""
        run = self.runs.get(self._run_key(thread_id))
        snap = self.graph.get_state(self._cfg(thread_id))
        v = snap.values
        if not v:
            return None
        pending = snap.interrupts[0] if snap.interrupts else None
        status, _ = self._status(snap, run, pending)
        versions = v.get("versions", [])
        return {
            "thread_id": thread_id,
            "title": v.get("title") or history.requests(v["messages"])[0],
            "status": status,
            "version": versions[-1].n if versions else None,
            "updated_at": snap.created_at,
            "running_since": run.get("since") if run else None,
        }

    def list_threads(self) -> list[dict]:
        """所有调研线程，最近有动静的在前。"""
        rows = [x for t in thread_ids(self.graph.checkpointer) if (x := self.summary_view(t))]
        return sorted(rows, key=lambda x: x["updated_at"] or "", reverse=True)

    @staticmethod
    def _status(snap, run: dict | None, pending) -> tuple[str, str | None]:
        """queued / running：有执行占着位；waiting：停在提问上等用户；idle：这一轮跑完了，可以接着说；
        failed：没在跑、没在等、也没跑完——某一步抛了错（错误记在检查点里），或执行半路被掐断（进程重启，没有错误记录）。"""
        if run is not None:
            return ("queued" if run["queued"] else "running"), None
        if pending is not None:
            return "waiting", None
        if not snap.next:
            return "idle", None
        err = next((t.error for t in snap.tasks if t.error), None)
        return "failed", _error_text(str(err)) if err else CUT_SHORT

    def _interrupt_view(self, thread_id: str, it) -> dict | None:
        if not it or not is_ask_interrupt(it.value):
            return None
        return {**ask_view(it.value), "id": it.id}  # 回答时带回来，对不上就是过期的卡
