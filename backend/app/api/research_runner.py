"""对话场景的接口层运行器：接用户的话、把 agent 的流变成页面的事件、停止和重新生成、线程状态视图。

回答进行中用户又说的话不打断这一轮：排着（存进 run_store 的 pending_messages），这一轮答完或停下就接着发成新的一轮（见 queue、_ended）。
没发出去的话最多一句。排着的话也可以插进正在跑的这一轮（见 guide）：主 agent 下一次调模型之前放进对话；这一轮没再调模型就结束了的，退回去排着。
"""

from __future__ import annotations

import ast
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage, RemoveMessage, ToolMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import START

from app import config
from app.api.run_store import GUIDE, QUEUE, STOPPED, SUCCESS, MemoryRunStore, RunStore, open_run_store
from app.api.runner import BaseRunner, Executor, RunConflict, jsonable, log
from app.graph.research import history
from app.graph.research.agent import TURN_END, ChatContext, now, stop_turn
from app.graph.research.graph import build_graph, open_checkpointer, thread_ids
from app.graph.research.sources import collect_sources, subagent_messages
from app.graph.research.state import Turn
from app.research_llm import THINKING_MS, FakeResearchPlanner, LiveResearchPlanner, ResearchPlanner, ThinkingClock
from app.tools.images import ImageStore


class NothingToRetry(Exception):
    """线程没有失败，不需要重试。"""


class NotIdle(Exception):
    """上一轮没跑完：这时不能接着说，也不能重新生成。"""


class AlreadyPending(Exception):
    """回答时又说的话已经有一句没发出去（排着或等着插进这一轮）：没发出去的话最多一句，先撤回改好再发。"""


class NothingToRegenerate(Exception):
    """还没有一轮回答。"""


class Stopped(Exception):
    """用户点了停止。"""


CUT_SHORT = "上次运行没跑完就停了"
# 检查点只在一次运行开头（用户的话写进去，见 accept）和结束时各存一份，中间每一步不存：每份都带着整段对话，每步都存的话按平方增长。
# 出错、点停止也算结束，结束前把做到哪儿存下来，重试从断点接着跑；进程直接崩掉或被杀，这一轮做到一半的没存，重试从头跑这一轮。
# 执行中页面要的东西（来源、标题、进度）都跟着事件走，不从库里取。
DURABILITY = "exit"
DELTA_S = 0.08  # 逐字片段攒这么久发一次
SENT_AT = "sent_at"  # 用户那句话的发送时间，记在消息的 additional_kwargs 里（不会发给模型）


def _said(text: str, **kw: Any) -> HumanMessage:
    return HumanMessage(content=text.strip(), additional_kwargs={SENT_AT: now()}, **kw)


def _guided(text: str) -> HumanMessage:
    """插进这一轮的话：发送时间记成放进对话的时候。id 先定下来，页面实时收到的和存下来的对得上。"""
    return HumanMessage(content=text, id=str(uuid.uuid4()), additional_kwargs={SENT_AT: now(), history.GUIDE: True})


class StopCheck(BaseCallbackHandler):
    """用户点了停止就在下一个回调里抛 Stopped：模型每吐一个片段、每个节点开始都查一次。
    在模型的流里抛出，HTTP 流随之关掉，不再生成；正在跑的工具一般不打断，等它返回、下一步开始时停。
    画图要等很久，它边等边调 check（经 ChatContext），点了停止就不等了。
    （只在外面不读流了不行：图的执行器关掉时会等正在跑的任务做完，模型照样生成到底。）"""

    raise_error = True

    def __init__(self, stopped: Callable[[], bool]) -> None:
        self.stopped = stopped

    def check(self) -> None:
        if self.stopped():
            raise Stopped()

    def on_chat_model_start(self, *args: Any, **kwargs: Any) -> None:
        self.check()

    def on_llm_new_token(self, *args: Any, **kwargs: Any) -> None:
        self.check()

    def on_chain_start(self, *args: Any, **kwargs: Any) -> None:
        self.check()


class _Streamed:
    """一条消息到现在流出来的全部思考和正文，以及思考了多久。"""

    def __init__(self, mid: str) -> None:
        self.mid, self.text, self.thinking, self.clock = mid, "", "", ThinkingClock()

    def message(self) -> AIMessage:
        meta: dict[str, Any] = {history.STOPPED: True}
        if (ms := self.clock.ms()) is not None:
            meta[THINKING_MS] = ms
        return AIMessage(id=self.mid, content=self.text, additional_kwargs={"reasoning_content": self.thinking} if self.thinking else {},
                         response_metadata=meta)


class StreamBuffer:
    """把模型的流式片段攒成事件：思考和正文按（子任务，消息 id，种类）合并，约每 80ms 发一次。子 agent 的片段带上派它的 task 调用 id。
    一条消息想完了（下一个片段是正文或工具调用），思考那一段带上 ms（思考了多久，后端计的）：页面按收到片段的时间自己算不准，
    中途打开对话时前面的事件一下子补发过来、标签页在后台时浏览器拖着不处理，都会算短。
    同时记着主 agent 和每个子 agent 最后一条消息流出来的东西：停止时存下来（主 agent 的是这一轮的回复，子 agent 的进它的子任务卡）。"""

    def __init__(self) -> None:
        self._deltas: list[dict] = []
        self._last = time.monotonic()
        self._cur: dict[str | None, _Streamed] = {}  # None 是主 agent

    def add(self, chunk: AIMessageChunk, task: str | None = None) -> list[dict]:
        mid = chunk.id or ""
        cur = self._cur.get(task)
        if cur is None or cur.mid != mid:
            cur = self._cur[task] = _Streamed(mid)
        thought = cur.clock.see(chunk)
        thinking = chunk.additional_kwargs.get("reasoning_content") or ""
        text = chunk.content if isinstance(chunk.content, str) else ""
        cur.thinking += thinking
        cur.text += text
        if thought:  # 思考到这里为止：用时记在思考那一段上发给页面，排在正文前面
            self._piece(mid, task, "thinking", "")["ms"] = cur.clock.ms()
        for kind, piece in (("thinking", thinking), ("text", text)):
            if piece:
                self._piece(mid, task, kind, piece)
        return self.flush() if time.monotonic() - self._last >= DELTA_S else []

    def _piece(self, mid: str, task: str | None, kind: str, piece: str) -> dict:
        """接上一段片段，返回它所在的那条。并行的几个子 agent 的片段交错着来：接在同一条消息最近的那一段后面，种类变了才另起一段。"""
        prev = next((d for d in reversed(self._deltas) if d["id"] == mid and d.get("task") == task), None)
        if prev is not None and prev["kind"] == kind:
            prev["text"] += piece
            return prev
        self._deltas.append(d := {"id": mid, "kind": kind, "text": piece, **({"task": task} if task else {})})
        return d

    def flush(self) -> list[dict]:
        out = [{"event": "delta", "data": d} for d in self._deltas]
        self._deltas, self._last = [], time.monotonic()
        return out

    def partial(self, task: str | None = None) -> AIMessage | None:
        cur = self._cur.get(task)
        return cur.message() if cur is not None and cur.mid else None


def conversation(messages: list[BaseMessage], turns: list[Turn]) -> list[dict]:
    """对话按轮：用户的话，然后这一轮里模型的思考、正文、工具步骤按先后排，被内容审核拦下时最后是一条 blocked；
    用户插进这一轮的话是一条 guide，排在它放进对话的地方。
    每轮带这一轮的记录（推荐问题、用量、结果）。子任务那一步带着它的子 agent 的条目（items），排法一样。"""
    by_id = {t.id: t for t in turns}
    results = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)}
    out: list[dict] = []
    cur: dict | None = None
    for m in messages:
        if history.is_guide(m):
            if cur is not None:
                cur["items"].append({"type": "guide", "id": m.id, "text": history.text_of(m), "sent_at": m.additional_kwargs.get(SENT_AT, "")})
        elif isinstance(m, HumanMessage):
            t = by_id.get(m.id)
            cur = {"id": m.id, "user": history.text_of(m), "sent_at": m.additional_kwargs.get(SENT_AT, ""), "items": [],
                   "record": t.model_dump(exclude={"id"}) if t else None}
            out.append(cur)
        elif cur is not None and isinstance(m, AIMessage):
            cur["items"] += _items_of(m, results)
    return out


def _items_of(m: AIMessage, results: dict[str, ToolMessage]) -> list[dict]:
    """一条模型消息 → 条目：思考、正文、工具步骤；被内容审核拦下的是一条 blocked。"""
    if reason := history.blocked_reason(m):
        return [{"type": "blocked", "id": m.id, "reason": reason}]
    items: list[dict] = []
    if thinking := m.additional_kwargs.get("reasoning_content"):
        items.append({"type": "thinking", "id": m.id, "text": thinking, "ms": m.response_metadata.get(THINKING_MS)})
    if text := history.text_of(m).strip():
        items.append({"type": "text", "id": m.id, "text": text})
    for tc in m.tool_calls:
        result = results.get(tc["id"])
        if step := history.step_of(tc, result):
            item = {"type": "step", **step.model_dump()}
            if step.kind == "task":
                item["items"] = subagent_items(result)
            items.append(item)
    return items


def subagent_items(result: ToolMessage | None) -> list[dict]:
    """子任务卡里的条目：它的子 agent 每次调模型的思考、正文（最后一段是报告）、搜索和读页。还没有结果时是空的，页面用实时的。"""
    sub = subagent_messages(result) if result is not None else []
    results = {x.tool_call_id: x for x in sub if isinstance(x, ToolMessage)}
    return [i for x in sub if isinstance(x, AIMessage) for i in _items_of(x, results)]


def _public(run: dict) -> dict:
    return {k: v for k, v in run.items() if k != "traceback"}


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

    def __init__(self, fake: bool | None = None, data_dir: Path | None = None, workers: int | None = None,
                 planner: ResearchPlanner | None = None, checkpointer: BaseCheckpointSaver | None = None,
                 run_store: RunStore | None = None):
        fake = config.USE_FAKE_PLANNER if fake is None else fake
        self.planner = planner or (FakeResearchPlanner() if fake else LiveResearchPlanner())
        data = Path(data_dir or config.DATA_DIR)
        saver = checkpointer or open_checkpointer(data)
        self.graph = build_graph(self.planner, saver)
        # 运行记录和检查点存在一处：传了检查点（测试、评测用内存的）就记在内存里，不碰数据目录和配置的库
        self.run_store = run_store or (MemoryRunStore() if checkpointer is not None else open_run_store(data))
        self.images = ImageStore(data / "images")  # 画好的图，按对话分目录
        # 对话不碰浏览器，几个线程各跑各的；检查点的读写 SqliteSaver / PostgresSaver 自己加锁
        self.executor = Executor(workers or config.RESEARCH_WORKERS, "research")
        # 正文写完这一轮就结束，推荐问题在结束之后才起（见 _after_run）。线程 → 等着起推荐问题的那一轮（轮 id, 用户的话, 回复）。
        # 用户又说了话、重新回答或删掉对话时撤掉：起好了也不写。撤掉和写检查点都在锁里，不会和用户那边的写交错
        self._suggest_mu = threading.Lock()
        self._suggest_for: dict[str, tuple[str, str, str]] = {}
        # 占位都经过这把锁（见 _admit）：话排进去时看线程在不在跑、这一轮结束时退掉占位并取走排着的话，两件事不会交错，
        # 不会出现话排进去了、这一轮却已经结束没人发的情况
        self._queue_mu = threading.Lock()

    @staticmethod
    def _cfg(thread_id: str) -> dict:
        return {"configurable": {"thread_id": thread_id}}

    # ---- 执行 ----

    def start(self, thread_id: str, text: str):
        return self._submit(thread_id, lambda: self.accept(thread_id, text), "new")

    def say(self, thread_id: str, text: str):
        return self._submit(thread_id, lambda: self.accept(thread_id, text), "say")

    def queue(self, thread_id: str, text: str) -> dict | None:
        """正在回答时又说的话：排着，这一轮答完或停下就接着发（见 _ended）。返回排着的那句。
        没发出去的话最多一句：已经有一句排着或等着插进这一轮的，抛 AlreadyPending（页面上这时发不出去，这里兜底）。
        刚好没在跑（这一轮刚结束）就直接发、返回 None，页面去接这次执行的事件流。上一轮没跑完的抛 NotIdle（先重试）。"""
        key = self._run_key(thread_id)
        with self._queue_mu:
            if not self.runs.admit(key):
                if self.run_store.get_pending(thread_id, QUEUE) or self.run_store.get_pending(thread_id, GUIDE):
                    raise AlreadyPending("上面那句还没发出去，先撤回改好再发")
                return self.run_store.put_pending(thread_id, QUEUE, text.strip())
        try:
            self._launch(thread_id, self.accept(thread_id, text), "say")
        except BaseException:
            self.runs.release(key)
            raise
        return None

    def unqueue(self, thread_id: str) -> str | None:
        """撤回排着的话，返回原文（页面放回输入框）；已经发出去了、或者没有排着的，返回 None。"""
        return self.run_store.take_pending(thread_id, QUEUE)

    def guide(self, thread_id: str) -> dict | None:
        """排着的话不等这一轮答完，插进这一轮：主 agent 下一次调模型之前放进对话（见 agent.TurnMiddleware.before_model）。
        已经有一句等着插的，接在它后面。返回等着插的那句；没在跑（排着的话已经发出去了）返回 None。"""
        with self._queue_mu:  # 和这一轮结束时的交接错开：在锁里看到还在跑，结束时一定看得到这句话
            if self.runs.get(self._run_key(thread_id)) is None:
                return None
            text = self.run_store.take_pending(thread_id, QUEUE)
            return self.run_store.put_pending(thread_id, GUIDE, text) if text else self.run_store.get_pending(thread_id, GUIDE)

    def unguide(self, thread_id: str) -> str | None:
        """撤回等着插进这一轮的话，返回原文；已经插进去了、或者没有，返回 None。"""
        return self.run_store.take_pending(thread_id, GUIDE)

    def _take_guide(self, thread_id: str) -> HumanMessage | None:
        """主 agent 调模型之前取等着插进这一轮的话。取不到就当没有：话留在库里，这一轮结束时退回去排着。"""
        try:
            text = self.run_store.take_pending(thread_id, GUIDE)
        except Exception:  # noqa: BLE001
            log.warning("插进这一轮的话没取出来：%s", self._run_key(thread_id), exc_info=True)
            return None
        return _guided(text) if text else None

    def _admit(self, key: str) -> bool:
        with self._queue_mu:
            return self.runs.admit(key)

    def _ended(self, thread_id: str, status: str) -> None:
        """一次执行结束：退掉占位。答完或停下了、又排着话，就把它发成新的一轮（kind 是 say），占位直接交过去，中间别的请求插不进来；
        这次执行最后的状态里线程已经又在跑了，页面看到就接着读下一次执行的事件。
        出错的不发：排着的话留着，页面把它放回输入框，用户重试完再发。
        等着插进这一轮、这一轮却没再调模型的话，先退回去排着（排在这期间又说的话前面），和排着的一样处理。"""
        key = self._run_key(thread_id)
        with self._queue_mu:
            try:
                if left := self.run_store.take_pending(thread_id, GUIDE):
                    self.run_store.put_pending(thread_id, QUEUE, left, front=True)
            except Exception:  # noqa: BLE001
                log.warning("没插进去的话没退回去排着：%s", key, exc_info=True)
            text = None
            if status in (SUCCESS, STOPPED):
                try:
                    text = self.run_store.take_pending(thread_id, QUEUE)
                except Exception:  # noqa: BLE001  取不到就当没有，话留在库里，页面会放回输入框
                    log.warning("排着的话没取出来：%s", key, exc_info=True)
            self.runs.release(key)
            if text is None:
                return
            if not self.runs.admit(key):  # 占位都经过这把锁，不会被抢；万一抢了，话放回去
                self._put_back(thread_id, text)
                return
        try:
            self._launch(thread_id, self.accept(thread_id, text), "say")
        except Exception:  # noqa: BLE001
            self.runs.release(key)
            log.exception("排着的话没发出去：%s", key)
            self._put_back(thread_id, text)

    def _put_back(self, thread_id: str, text: str) -> None:
        """取出来却没发出去的话放回去，排在这期间又说的话前面；页面看到线程空闲还排着话，会放回输入框。"""
        try:
            self.run_store.put_pending(thread_id, QUEUE, text, front=True)
        except Exception:  # noqa: BLE001
            log.warning("排着的话没放回去：%s", self._run_key(thread_id), exc_info=True)

    def accept(self, thread_id: str, text: str) -> None:
        """把用户的话写进检查点，agent 从这里接着跑（输入为 None）。

        先落盘再交出线程 id：排队时服务重启也查得到这句话，算没跑完，可以重试。
        """
        self._drop_suggestions(thread_id)
        snap = self.graph.get_state(self._cfg(thread_id))
        if snap.values and snap.next:
            raise NotIdle("上一轮没跑完，先重试")
        self.graph.update_state(self._cfg(thread_id), {"messages": [_said(text)]}, as_node=START)
        return None

    def retry(self, thread_id: str):
        return self._submit(thread_id, lambda: self.retry_input(thread_id), "retry")

    def retry_input(self, thread_id: str) -> None:
        """只有没跑完的线程能重试：从最后一个检查点接着跑，出错那一步重做，之前做过的搜索、读页都不重来。"""
        snap = self.graph.get_state(self._cfg(thread_id))
        if not snap.values or not snap.next:
            raise NothingToRetry("这个线程没有失败，不需要重试")
        return None

    def regenerate(self, thread_id: str, text: str | None = None):
        return self._submit(thread_id, lambda: self.regenerate_input(thread_id, text), "regenerate" if text is None else "edit")

    def regenerate_input(self, thread_id: str, text: str | None = None) -> None:
        """最后一轮重新回答：去掉用户最后那句话之后的消息和这一轮的记录，从那句话接着跑。
        带 text 是用户改了最后那句话：先换成 text（消息 id 不变，还是同一轮，发送时间记成改的时候），再重新回答。标题不重起。"""
        self._drop_suggestions(thread_id)
        snap = self.graph.get_state(self._cfg(thread_id))
        v = snap.values
        if v and snap.next:
            raise NotIdle("上一轮没跑完，先重试")
        msgs = (v or {}).get("messages", [])
        start = history.last_human(msgs)
        if start < 0:
            raise NothingToRegenerate("还没有回答，不需要重新生成")
        human = msgs[start]
        turns = [t for t in v.get("turns", []) if t.id != human.id]
        edited = [_said(text, id=human.id)] if text is not None else []
        self.graph.update_state(self._cfg(thread_id), {"messages": edited + [RemoveMessage(id=m.id) for m in msgs[start + 1:]], "turns": turns},
                                as_node=START)
        self.images.remove(thread_id, [img.file for m in msgs[start + 1:] if (img := history.image_of(m))])  # 这一轮原来画的图不要了
        return None

    @contextmanager
    def _hold(self, thread_id: str) -> Iterator[None]:
        """占着这个线程的运行位做一件事（改标题、删除）：做的时候这个线程跑不起来，在跑的也不让做。"""
        key = self._run_key(thread_id)
        if not self._admit(key):
            raise RunConflict("正在回答，答完或停下后才能改名、删除")
        try:
            yield
        finally:
            self.runs.release(key)

    def rename(self, thread_id: str, title: str) -> None:
        """改标题。没跑完的线程不能改：写一次状态，出错的那一步就丢了，重试接不上。"""
        with self._hold(thread_id), self._suggest_mu:  # 和写推荐问题错开
            if self.graph.get_state(self._cfg(thread_id)).next:
                raise NotIdle("上一轮没跑完，先重试")
            self.graph.update_state(self._cfg(thread_id), {"title": title}, as_node=TURN_END)

    def delete(self, thread_id: str) -> None:
        """删掉整段对话（这个线程的所有检查点、运行记录和画的图），找不回来。
        运行记录先删：删不掉就整个报错，对话还在，可以再删一次；反过来对话先没了，剩下的记录就再也删不到了。"""
        with self._hold(thread_id):
            self._drop_suggestions(thread_id)
            self.run_store.delete_thread(thread_id)
            self.graph.checkpointer.delete_thread(thread_id)
            self.images.drop(thread_id)

    def stop(self, thread_id: str) -> bool:
        """让这个线程正在跑（或排队）的这一轮停下；没有在跑的返回 False。真正停下由执行自己做（见 _graph_events）。"""
        return self.runs.request_stop(self._run_key(thread_id))

    def _stop_requested(self, thread_id: str) -> bool:
        return self.runs.stop_requested(self._run_key(thread_id))

    def _write_stop(self, thread_id: str, buf: StreamBuffer | None = None, tasks: dict[str, str] | None = None) -> None:
        """这一轮以停止收尾，写成这一轮最后一个节点（TURN_END）的更新：线程停在这一轮结束的地方，可以接着说。
        tasks 是这次运行里派出去的子 agent（命名空间 → task 调用 id）：还没做完的，把它检查点里已经做了的和流出来的半截存进它的子任务卡。"""
        cfg = self._cfg(thread_id)
        v = self.graph.get_state(cfg).values
        partial = buf.partial() if buf else None
        if partial is not None and any(m.id == partial.id for m in v["messages"]):  # 这条消息已经完整存下来了
            partial = None
        subs: dict[str, list[BaseMessage]] = {}
        for ns, call_id in (tasks or {}).items():
            tup = self.graph.checkpointer.get_tuple({"configurable": {"thread_id": thread_id, "checkpoint_ns": ns}})
            msgs = list(tup.checkpoint["channel_values"].get("messages", [])) if tup else []
            p = buf.partial(call_id) if buf else None
            if p is not None and (p.content or p.additional_kwargs) and not any(m.id == p.id for m in msgs):
                msgs.append(p)
            if msgs:
                subs[call_id] = msgs
        self.graph.update_state(cfg, stop_turn(v, partial, subs), as_node=TURN_END)

    def _graph_events(self, thread_id: str, inp: Any):
        """agent 的流 → 页面事件。messages 只转发 model 节点的（快模型在别的节点里调用，不算）；
        custom 是中间件写的进度（step，做完的带来源）、标题（title）和阶段（phase：起标题、等模型这些空档在等什么）；updates 里改了 state 的节点做完发 node（页面不靠它）。
        子 agent（task 工具里跑的另一个 agent）的流要开 subgraphs 才有，按命名空间认出是哪个 task 调用派的：
        它的片段、进度、阶段带上 task（调用 id），页面放进那张子任务卡；它的节点做完不发 node。它的片段 langgraph_node 也是 model，只按节点名分不开。
        用户点了停止：排队时就点的不跑，直接收尾；跑着的在下一个片段或下一步停下（见 StopCheck），已经流出来的正文留着。停下的返回 STOPPED（记进运行记录）。"""
        if self._stop_requested(thread_id):
            self._write_stop(thread_id)
            yield {"event": "node", "data": {"node": TURN_END}}
            return STOPPED
        buf = StreamBuffer()
        tasks: dict[str, str] = {}  # 子 agent 的命名空间 → 派它的 task 调用 id（task 工具开跑前写进自定义流的 subagent）
        stop = StopCheck(lambda: self._stop_requested(thread_id))
        cfg = {**self._cfg(thread_id), "callbacks": [stop]}
        ctx = ChatContext(images=self.images, check_stop=stop.check, take_guide=lambda: self._take_guide(thread_id))
        try:
            for ns, mode, chunk in self.graph.stream(inp, cfg, context=ctx, stream_mode=["messages", "updates", "custom"], subgraphs=True,
                                                durability=DURABILITY):
                task = tasks.get(ns[0]) if ns else None
                if ns and task is None:  # 不是子 agent 的嵌套图（现在没有）
                    continue
                if mode == "messages":
                    msg, meta = chunk
                    if meta.get("langgraph_node") == "model" and isinstance(msg, AIMessageChunk):
                        yield from buf.add(msg, task)
                    continue
                yield from buf.flush()
                if mode == "custom":
                    if chunk["type"] == "subagent":
                        tasks[chunk["ns"]] = chunk["id"]
                        continue
                    data = {k: v for k, v in chunk.items() if k != "type"} | ({"task": task} if task else {})
                    yield {"event": chunk["type"], "data": jsonable(data)}
                    continue
                if task:
                    continue
                for node, update in chunk.items():
                    if update:  # 没改 state 的中间件节点不发
                        yield {"event": "node", "data": {"node": node}}
        except Stopped:
            yield from buf.flush()
            self._write_stop(thread_id, buf, tasks)
            yield {"event": "node", "data": {"node": TURN_END}}
            return STOPPED
        yield from buf.flush()
        self._want_suggestions(thread_id)
        return None

    # ---- 推荐问题：这一轮结束之后再起 ----

    def _want_suggestions(self, thread_id: str) -> None:
        """这一轮跑完、有回复，记下要起推荐问题。还占着运行位时记：记下之前用户插不进话，插进来的话一定会把它撤掉。"""
        snap = self.graph.get_state(self._cfg(thread_id))
        msgs = snap.values["messages"]
        start = history.last_human(msgs)
        reply = history.final_text(msgs[start + 1:])
        if snap.next or not reply:
            return
        with self._suggest_mu:
            self._suggest_for[thread_id] = (msgs[start].id, history.text_of(msgs[start]), reply)

    def _drop_suggestions(self, thread_id: str) -> None:
        """用户又说了话（或重新回答、删掉对话）：上一轮还没起好的推荐问题不要了。正在写的写完才返回，之后不会再写。"""
        with self._suggest_mu:
            self._suggest_for.pop(thread_id, None)

    def _after_run(self, thread_id: str) -> Iterator[dict]:
        """起推荐问题。这时这一轮已经结束，页面拿到了最终状态，线程可以接着说；起好了写进这一轮的记录，再发一次状态。"""
        with self._suggest_mu:
            job = self._suggest_for.get(thread_id)
        if job is None:
            return
        turn_id, question, reply = job
        yield {"event": "phase", "data": {"phase": "suggest"}}  # 页面在回答下面先说一声正在起
        try:
            items = self.planner.suggest(question, reply)[:3]
        except Exception:  # noqa: BLE001  推荐问题失败就不给
            items = []
        with self._suggest_mu:
            if self._suggest_for.get(thread_id) is not job:  # 起的时候用户又说了话：不要了
                return
            del self._suggest_for[thread_id]
            cfg = self._cfg(thread_id)
            snap = self.graph.get_state(cfg)
            turns = list(snap.values.get("turns", []))
            if not items or snap.next or not turns or turns[-1].id != turn_id:
                return
            turns[-1] = turns[-1].model_copy(update={"suggestions": items})
            self.graph.update_state(cfg, {"turns": turns}, as_node=TURN_END)
        yield {"event": "state", "data": self.state_view(thread_id)}

    def _turn_of(self, thread_id: str) -> str | None:
        """这次执行跑的是哪一轮：用户最后那句话的消息 id（接着说、重试、重新回答都是）。"""
        msgs = self.graph.get_state(self._cfg(thread_id)).values.get("messages", [])
        i = history.last_human(msgs)
        return msgs[i].id if i >= 0 else None

    # ---- 运行记录 ----

    def recover(self) -> None:
        """服务启动时调一次：上次进程退出时还在排队、在跑的运行，记成没跑完（lost）。单进程部署，这时它们都已经停了。"""
        if n := self.run_store.sweep():
            log.warning("上次进程退出时有 %d 次运行没跑完，已记成 lost", n)

    def thread_runs(self, thread_id: str) -> list[dict]:
        """这段对话的每次运行，新的在前。堆栈不给：网站没有登录，堆栈只留在库和日志里。"""
        return [_public(r) for r in self.run_store.for_thread(thread_id)]

    def recent_runs(self, status: str | None = None, limit: int = 50) -> list[dict]:
        """所有对话里最近的运行，可以只看某个状态（比如 error）。"""
        return [_public(r) for r in self.run_store.recent(status, limit)]

    # ---- 视图 ----

    def state_view(self, thread_id: str) -> dict:
        # 先查占位再取检查点：执行恰好在两步之间结束时，拿到的是结束后的检查点
        run = self.runs.get(self._run_key(thread_id))
        snap = self.graph.get_state(self._cfg(thread_id))
        v = snap.values
        if not v:  # 线程 id 交出去之前用户的话已经写进检查点（见 accept），没有就是不存在
            return {"thread_id": thread_id, "exists": False}
        status, error = self._status(snap, run)
        return {
            "thread_id": thread_id,
            "exists": True,
            "scenario": "research",
            "status": status,
            "error": error,
            "stopping": bool(run and run.get("stopping")),
            "title": v.get("title") or history.requests(v["messages"])[0],
            "updated_at": snap.created_at,  # 最近一个检查点的时间
            "running_since": run.get("since") if run else None,  # 这次执行开始的时间；排队中、没在跑为 None
            "turns": jsonable(conversation(v["messages"], v.get("turns", []))),
            "sources": {s.id: s.model_dump() for s in collect_sources(v["messages"])},
            "queued_message": self.run_store.get_pending(thread_id, QUEUE),  # 排着、这一轮答完就发的话（{text, created_at, updated_at}）；没有为 None
            "guided_message": self.run_store.get_pending(thread_id, GUIDE),  # 等着插进这一轮的话，同上
        }

    def summary_view(self, thread_id: str) -> dict | None:
        """线程列表里的一行：标题、状态。不存在返回 None。"""
        run = self.runs.get(self._run_key(thread_id))
        snap = self.graph.get_state(self._cfg(thread_id))
        v = snap.values
        if not v:
            return None
        status, _ = self._status(snap, run)
        return {
            "thread_id": thread_id,
            "title": v.get("title") or history.requests(v["messages"])[0],
            "status": status,
            "updated_at": snap.created_at,
            "running_since": run.get("since") if run else None,
        }

    def list_threads(self) -> list[dict]:
        """所有对话线程，最近有动静的在前。"""
        rows = [x for t in thread_ids(self.graph.checkpointer) if (x := self.summary_view(t))]
        return sorted(rows, key=lambda x: x["updated_at"] or "", reverse=True)

    @staticmethod
    def _status(snap, run: dict | None) -> tuple[str, str | None]:
        """queued / running：有执行占着位；idle：这一轮跑完了（或停下了），可以接着说；
        failed：没在跑、也没跑完——某一步抛了错（错误记在检查点里），或执行半路被掐断（进程重启，没有错误记录）。"""
        if run is not None:
            return ("queued" if run["queued"] else "running"), None
        if not snap.next:
            return "idle", None
        err = next((t.error for t in snap.tasks if t.error), None)
        return "failed", _error_text(str(err)) if err else CUT_SHORT
