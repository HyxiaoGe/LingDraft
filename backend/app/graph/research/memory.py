"""长期记忆：从对话里整理出关于用户的事，每次调主 agent 都整份放进系统提示词。

2026-11-03 Sean 定：
- 后台写（做梦）：每天一次，把上次整理以后有新的几轮的对话一段一段交给模型，对照已有的记忆增、改、删。对话里不写记忆。
- 读：记忆整份放进系统提示词。记忆只在整理时变，一天一次；系统提示词里的日期本来也每天变，前缀缓存不用另外操心。
- 页面上能看到全部记忆。现在没有登录，所有人共用一份，以后有了登录再分。
- 删对话不动记忆：记的是人，不是这段对话。

存在 LangGraph 的 Store 里，和检查点同一个库。Store 和检查点不在一个事务里，整理也不是一次写完：
每段对话整理完，记下整理过哪几轮（每一轮的 id 和内容的指纹）。整理到一半出错，下次从没整理完的对话接着来；
重新回答、改过话的一轮内容变了，下次会再看一遍，模型对照已有的记忆，不会重复记。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Literal

from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.store.base import BaseStore
from pydantic import BaseModel, ConfigDict, ValidationError

from app.graph.research import history, prompts
from app.graph.research.state import Turn
from app.model_guard import ContentBlocked

log = logging.getLogger(__name__)
CHINA_TZ = timezone(timedelta(hours=8))

# 记忆按人分（2026-11-06 Sean 定，之前没有登录是所有人共用一份）：每个人的记忆在 ITEMS + (owner,) 下，一条一行，键是记忆的 id：
# {"text", "created_at", "updated_at", "thread_id"（最后从哪段对话改的）}。owner 是对话归谁（见 app.identity：登录用户的 id）；
# 游客的对话不整理（登录后并进账号再整理）。登录以前共用的那几条还留在 ITEMS 这一层，没有人的提示词里有它们
ITEMS = ("memory", "items")
THREADS = ("memory", "threads")  # 每段对话整理过哪几轮，键是线程 id：{"turns": {轮 id: 那一轮内容的指纹}}
DREAMS = ("memory", "dreams")  # 键 LAST：最近一次整理（见 MemoryBook.dream 的返回值）
LAST = "last"
MAX_ITEMS = 1000  # 一次全取出来（Store 的 search 默认只给 10 条）
USER_CHARS = 2000  # 交给模型的每条用户消息最多这么长（字符）
REPLY_CHARS = 2000  # 每条回答最多这么长：记的是人，回答只是帮模型看懂用户在说什么
EARLIER_TURNS = 10  # 之前整理过的几轮里，最近这么多轮用户说的话也给，帮模型看懂新的几轮
PARSE_TRIES = 2  # 模型返回的东西解析不了，最多问几次
TEXT_MAX = 200  # 用户在记忆页改一条，最多这么多字（2026-11-07 本机 4 条最长 100）


class Op(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)  # 全是数字的 id，模型可能写成数字
    action: Literal["add", "update", "delete"]
    id: str = ""
    text: str = ""


class Ops(BaseModel):
    model_config = ConfigDict(extra="ignore")
    operations: list[Op]


@dataclass
class Exchange:
    """一轮里用户说的话和最后的回答。"""

    date: str  # 北京时间的日期
    user: str
    reply: str


@dataclass
class Conversation:
    """一段要整理的对话：之前整理过的最近几轮（只有用户的话），和上次整理以后新的几轮。"""

    thread_id: str
    title: str
    earlier: list[Exchange] = field(default_factory=list)
    new: list[Exchange] = field(default_factory=list)
    first: datetime | None = None  # 新的几轮里最早结束的时间：几段对话按它排，后说的改得了先说的
    seen: dict[str, str] = field(default_factory=dict)  # 整理完记下：这段对话现在所有算数的轮（轮 id → 内容的指纹）


def _in_order(found: Iterable) -> list[dict]:
    rows = [{"id": r.key, **r.value} for r in found]
    return sorted(rows, key=lambda x: (x.get("created_at", ""), x["id"]))


def _prompt_lines(items: list[dict]) -> str:
    return "\n".join(f"- {m['text']}" for m in items)


def _time(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


def _cut(text: str, n: int) -> str:
    text = text.strip()
    return text if len(text) <= n else text[:n] + " …(cut)"


def _chunks(messages: list[BaseMessage]) -> list[list[BaseMessage]]:
    """按轮分：每一轮从开始这一轮的用户消息起。"""
    out: list[list[BaseMessage]] = []
    for m in messages:
        if history.is_request(m):
            out.append([m])
        elif out:
            out[-1].append(m)
    return out


def conversation_of(thread_id: str, values: dict, seen: dict[str, str]) -> Conversation | None:
    """这段对话里没整理过的几轮（seen 里没有，或者内容变了：重新回答过、改过话）；没有新的返回 None。
    还没结束的一轮、被内容审核拦下的一轮不算。"""
    turns: dict[str, Turn] = {t.id: t for t in values.get("turns", [])}
    conv = Conversation(thread_id, values.get("title") or "")
    for chunk in _chunks(values.get("messages", [])):
        rec = turns.get(chunk[0].id or "")
        if rec is None or rec.outcome == "blocked" or any(history.blocked_reason(m) for m in chunk):
            continue
        ended = _time(rec.ended_at)
        if ended is None:
            continue
        user = "\n\n".join(history.text_of(m).strip() for m in chunk if isinstance(m, HumanMessage))
        date = (_time(rec.started_at) or ended).astimezone(CHINA_TZ).date().isoformat()
        reply = history.final_text(chunk)
        mark = hashlib.sha1(f"{user}\x00{reply}".encode()).hexdigest()[:16]
        old = seen.get(rec.id) == mark
        conv.seen[rec.id] = mark
        if old:
            conv.earlier.append(Exchange(date, _cut(user, USER_CHARS), ""))
            continue
        conv.new.append(Exchange(date, _cut(user, USER_CHARS), _cut(reply, REPLY_CHARS)))
        conv.first = ended if conv.first is None else min(conv.first, ended)
    conv.earlier = conv.earlier[-EARLIER_TURNS:]
    return conv if conv.new else None


def parse_ops(text: str) -> list[Op]:
    """模型按 json 返回的增删改；解析不了抛 ValueError。"""
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`").removeprefix("json").strip()
    try:
        return Ops.model_validate(json.loads(s)).operations
    except (json.JSONDecodeError, ValidationError) as e:
        raise ValueError(f"整理记忆的结果解析不了：{e}") from e


def due_at(now: datetime, hour: int) -> datetime:
    """最近一次该整理的时间（不晚于 now）：北京时间 hour 点整。"""
    local = now.astimezone(CHINA_TZ)
    slot = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    return slot if slot <= local else slot - timedelta(days=1)


def next_at(now: datetime, hour: int) -> datetime:
    return due_at(now, hour) + timedelta(days=1)


def _now() -> str:
    return datetime.now(UTC).isoformat()  # 带微秒：同一次整理里记下的几条也分得清先后


class MemoryBook:
    """记忆的读写和整理。整理同一时刻只跑一次（同一个进程里）。"""

    def __init__(self, store: BaseStore) -> None:
        self.store = store
        self._dreaming = threading.Lock()

    # ---- 读 ----
    # 同步的给整理记忆的后台线程和记忆页用，a 开头的给对话的事件循环里用（agent 每次调模型都取一次，见 agent.memory_text）

    def items(self, owner: str) -> list[dict]:
        """这个人的全部记忆，先记下的在前：{"id", "text", "created_at", "updated_at", "thread_id"}。"""
        return _in_order(self.store.search(ITEMS + (owner,), limit=MAX_ITEMS))

    async def aitems(self, owner: str) -> list[dict]:
        return _in_order(await self.store.asearch(ITEMS + (owner,), limit=MAX_ITEMS))

    def by_owner(self) -> dict[str, list[dict]]:
        """所有人的记忆，按人分（命令行看用）。登录以前共用的那几条在键 "" 下。"""
        out: dict[str, list[dict]] = {}
        for r in self.store.search(ITEMS, limit=MAX_ITEMS * 10):
            out.setdefault(r.namespace[len(ITEMS)] if len(r.namespace) > len(ITEMS) else "", []).append(r)
        return {k: _in_order(v) for k, v in out.items()}

    def prompt_text(self, owner: str) -> str:
        """放进系统提示词的那份：一条一行。没有记忆是空串（系统提示词里就没有这一段）。"""
        return _prompt_lines(self.items(owner))

    async def aprompt_text(self, owner: str) -> str:
        return _prompt_lines(await self.aitems(owner))

    def last_dream(self) -> dict | None:
        item = self.store.get(DREAMS, LAST)
        return item.value if item else None

    async def aforget_thread(self, thread_id: str) -> None:
        """删对话时调：只删整理过哪几轮的记录，记忆不动。"""
        await self.store.adelete(THREADS, thread_id)

    def _seen(self, thread_id: str) -> dict[str, str]:
        item = self.store.get(THREADS, thread_id)
        return item.value.get("turns", {}) if item else {}

    # ---- 写 ----
    # 用户在记忆页改、删（2026-11-07 Sean 定放开，推翻 11-03 的“只能看”；不做手动新增）。
    # 和整理不互斥：整理正在写同一个人的记忆时用户改、删了同一条，以后写的为准（整理一次只写一段对话的几条，窗口很短）

    def edit(self, owner: str, item_id: str, text: str) -> dict | None:
        """改一条的文字：出处对话（thread_id）不变，记下修改时间。owner 没有这一条返回 None。返回改过的这一条（带 id）。"""
        ns = ITEMS + (owner,)
        item = self.store.get(ns, item_id)
        if item is None:
            return None
        value = item.value | {"text": text, "updated_at": _now()}
        self.store.put(ns, item_id, value)
        return {"id": item_id, **value}

    def forget(self, owner: str, item_id: str) -> bool:
        """删一条：以后回答不再参考。owner 没有这一条返回 False。整理过的对话不会重看，以后的对话里又提到，可能再记下来。"""
        ns = ITEMS + (owner,)
        if self.store.get(ns, item_id) is None:
            return False
        self.store.delete(ns, item_id)
        return True

    def apply(self, ops: list[Op], thread_id: str, owner: str) -> dict[str, int]:
        """照模型给的增删改写进 owner 的记忆。改、删不存在的 id，或者没有文字的增、改，跳过。"""
        ns = ITEMS + (owner,)
        known = {m["id"]: m for m in self.items(owner)}
        n = {"added": 0, "updated": 0, "deleted": 0, "skipped": 0}
        for op in ops:
            text = op.text.strip()
            at = _now()
            if op.action == "add" and text:
                self.store.put(ns, uuid.uuid4().hex[:8], {"text": text, "created_at": at, "updated_at": at, "thread_id": thread_id})
                n["added"] += 1
            elif op.action == "update" and text and op.id in known:
                kept = {k: v for k, v in known[op.id].items() if k != "id"}
                self.store.put(ns, op.id, kept | {"text": text, "updated_at": at, "thread_id": thread_id})
                n["updated"] += 1
            elif op.action == "delete" and op.id in known:
                self.store.delete(ns, op.id)
                known.pop(op.id)
                n["deleted"] += 1
            else:
                log.warning("整理记忆：跳过一条用不了的操作 %s", op.model_dump())
                n["skipped"] += 1
        return n

    # ---- 整理 ----

    def pending(self, threads: Iterable[tuple[str, dict, str]]) -> list[tuple[Conversation, str]]:
        """上次整理以后有新的几轮的对话（和各归谁），按新的几轮最早结束的时间排。threads 是（线程 id, 检查点里的 state, 归谁）。"""
        convs = [(c, owner) for tid, values, owner in threads if values and (c := conversation_of(tid, values, self._seen(tid)))]
        return sorted(convs, key=lambda x: x[0].first or datetime.min.replace(tzinfo=UTC))

    def prompt_for(self, conv: Conversation, today: str, owner: str) -> str:
        memories = [{"id": m["id"], "text": m["text"]} for m in self.items(owner)]
        return prompts.render("dream.prompt", today=today, memories=memories, title=conv.title, earlier=conv.earlier, new=conv.new)

    def dream(self, threads: Iterable[tuple[str, dict, str]], model: Callable[[str], str]) -> dict | None:
        """整理一次：一段一段交给 model（提示词 → json 文字），照它的结果改这段对话归的那个人的记忆，记下这段整理过哪几轮。
        threads 是（线程 id, state, 归谁），没归谁的、游客的不要传进来（见 ResearchRunner.dream）。
        一段出错跳过（不记整理过，下次再来），接着整理下一段；被内容审核拦下的记成整理过，不然每天都会再拦一次。
        已经有一次在整理，返回 None。"""
        if not self._dreaming.acquire(blocking=False):
            return None
        try:
            run: dict[str, Any] = {"started_at": _now(), "ended_at": "", "threads": 0, "failed": 0,
                                   "added": 0, "updated": 0, "deleted": 0, "skipped": 0}
            self.store.put(DREAMS, LAST, run)
            today = datetime.now(CHINA_TZ).date().isoformat()
            for conv, owner in self.pending(threads):
                try:
                    ops = self._ask(model, self.prompt_for(conv, today, owner))
                except ContentBlocked:
                    log.warning("整理记忆：对话 %s 被内容审核拦下，记成整理过", conv.thread_id)
                    ops = []
                except Exception:
                    log.exception("整理记忆：对话 %s 没整理成，下次再来", conv.thread_id)
                    run["failed"] += 1
                    continue
                for k, v in self.apply(ops, conv.thread_id, owner).items():
                    run[k] += v
                self.store.put(THREADS, conv.thread_id, {"turns": conv.seen})
                run["threads"] += 1
            run["ended_at"] = _now()
            self.store.put(DREAMS, LAST, run)
            log.info("整理记忆：%s", run)
            return run
        finally:
            self._dreaming.release()

    @staticmethod
    def _ask(model: Callable[[str], str], prompt: str) -> list[Op]:
        err: ValueError | None = None
        for _ in range(PARSE_TRIES):
            try:
                return parse_ops(model(prompt))
            except ValueError as e:
                err = e
        raise err  # type: ignore[misc]


class DreamSchedule:
    """后台线程：每天北京时间 hour 点整理一次。服务重启错过了（上次整理早于最近一次该整理的时间），起来就补；
    从来没整理过的不补，等到点：第一次部署不在白天高峰价的时候把所有旧对话整理一遍。"""

    RETRY_S = 600  # 读不到上次整理的时间（比如库连不上），过这么久再看

    def __init__(self, run: Callable[[], Any], last: Callable[[], dict | None], hour: int) -> None:
        self.run, self.last, self.hour = run, last, hour
        self._born = datetime.now(UTC)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="dream")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def due(self, now: datetime) -> bool:
        """现在该不该整理：上次整理早于最近一次该整理的时间就该补；从来没整理过的，进程起来以后过了一次该整理的时间才整理。"""
        slot = due_at(now, self.hour)
        last = self.last()
        if last is None:
            return slot > self._born
        started = _time(last.get("started_at", ""))
        return started is None or started < slot

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.due(datetime.now(UTC)):
                    self.run()
            except Exception:
                log.exception("整理记忆的定时任务出错，%d 秒后再看", self.RETRY_S)
                self._stop.wait(self.RETRY_S)
                continue
            wait = (next_at(datetime.now(UTC), self.hour) - datetime.now(UTC)).total_seconds()
            self._stop.wait(max(wait, 1))
