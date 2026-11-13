"""运行记录：每次执行（发一句话、重试、重新回答……）一行，存进库里，事后能查历史运行和失败原因。

学的是 LangGraph Agent Server 的 run：执行登记（RunRegistry）在进程内存里，跑完就没了；这里把每次执行的来历、状态、时间、错误落盘。
和检查点、长期记忆存在同一个 Postgres 里，共用一个连接池（见 ResearchRunner 的 open_storage）；测试和评测用内存的。
都是协程，在对话的事件循环里调（连接池绑定那个循环）。时间由存储自己记（UTC）。删掉对话时它的运行记录一起删。

同一个库里还有一张 pending_messages：回答进行中用户又说的话，等着交给 agent（Agent Server 的双发策略里的 enqueue）。
每个对话、每种方式一行：排队（queue）是这一轮结束后开新的一轮发；插话（guide）是插进正在跑的这一轮，主 agent 下一次调模型前放进对话。
又说的接在排着的那句后面，合成一句。存进库里，服务重启也不丢。
每行还记着用户发的时候开没开思考（输入框旁边的开关，没给是空）：合成一句时按后发的那句，发出去时带上（见 ResearchRunner._aended）。

还有一张 thread_owners：每段对话归谁（见 app.identity，登录用户的 id 或游客的 guest:…），开对话时记下，列表和所有对话接口按它查。
登录以前的老对话没有这一行（没归谁）：页面上谁都看不到，相当于归档了，库里还在（2026-11-06 Sean 定，都是测试数据）。游客登录后把 TA 的对话整批改归账号（transfer）。
这张表还记着置顶（pinned_at，2026-11-07 批次 D Sean 定存后端，换设备也在）：置顶跟着对话走，并进账号后还在，删对话一起删。
还记着未读（unread、notice，2026-11-13 定时任务第 3 步）：用户不在场时跑完的一轮（现在只有定时任务触发的）每次加一，
notice 是最近那一次的通知 {"title", "text", "ok", "at"}（任务名、回答的开头、跑成没有、什么时候）；用户看了这段对话就清零。同样跟着对话走。
还记着线程列表里这一行从检查点算出来的那几项（summary、summary_rev，2026-11-13）：标题、状态、是不是工作任务这些原来每次取列表都要把每段对话的检查点
整个读出来现算，对话多了就慢。算好的记在这里，下次取列表时那段对话最新的检查点还是算的时候那个就直接用（见 ResearchRunner._alist_threads）。
它只是算好的结果，以检查点为准：没有、对不上的重算，丢了不要紧。summary_rev 每作废一次加一（touch），记的时候对不上就不记：
免得“读检查点 → 这段对话开跑了 → 才记下”把开跑前算的留下来。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, NamedTuple, Protocol

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

QUEUED, RUNNING, SUCCESS, STOPPED, ERROR, LOST, ASKED = "queued", "running", "success", "stopped", "error", "lost", "asked"
# success：跑到头了（这一轮的结果另看检查点里的轮记录，比如被内容审核拦下）；stopped：用户点了停止；
# error：抛了错；lost：进程重启时还在排队或在跑（服务启动时补记，见 sweep）；
# asked：停下来问用户了（见 app.tools.ask_user），这一轮还没完，用户回答以后另起一次执行（kind 是 answer）接着跑
STATUSES = (QUEUED, RUNNING, SUCCESS, STOPPED, ERROR, LOST, ASKED)
QUEUE = "queue"  # 排着的话的方式：这一轮结束后开新的一轮发（见 ResearchRunner.queue）
GUIDE = "guide"  # 插进正在跑的这一轮：主 agent 下一次调模型之前放进对话（见 ResearchRunner.guide）
JOIN = "\n"  # 又说的话接在排着的那句后面，中间换行
class Pending(NamedTuple):
    """取出来的排着的话。"""

    text: str
    thinking: bool | None = None


FIELDS = ("id", "thread_id", "kind", "turn_id", "status", "created_at", "started_at", "ended_at", "error_type", "error", "traceback", "version")


class RunStore(Protocol):
    """一行的字段见 FIELDS：kind 是这次执行怎么来的（new / say / retry / regenerate / edit / answer），turn_id 是这一轮用户那句话的消息 id，
    version 是跑它的代码版本（部署时的提交号）。列表都是新的在前（按写入的先后，同一毫秒建的也分得清），时间是 ISO 字符串。"""

    async def create(self, run_id: str, thread_id: str, kind: str, turn_id: str | None, version: str | None) -> None: ...
    async def started(self, run_id: str) -> None: ...
    async def finish(self, run_id: str, status: str, error_type: str | None = None, error: str | None = None,
                     traceback: str | None = None) -> None: ...
    async def for_thread(self, thread_id: str) -> list[dict]: ...
    async def recent(self, status: str | None, limit: int) -> list[dict]: ...
    async def sweep(self) -> int: ...  # 还在排队、在跑的记成 lost，返回条数。只在服务启动时调：单进程部署，这时它们都已经没在跑了
    async def delete_thread(self, thread_id: str) -> None: ...  # 运行记录、排着的话一起删。归谁不在这里删（见 disown）
    # 删掉归谁那一行，置顶、未读、列表里记着的那几项都在它上面。删对话时最后做（见 ResearchRunner._adelete）：它还在，前面哪一步出错都能再删一次
    async def disown(self, thread_id: str) -> None: ...

    # 对话归谁（见模块说明）
    async def claim(self, thread_id: str, owner: str) -> None: ...  # 记下归谁；已经记过的不改
    async def owner_of(self, thread_id: str) -> str | None: ...  # 没记过（登录以前的老对话）是 None
    async def transfer(self, source: str, target: str) -> int: ...  # source 的对话全改归 target，返回改了几段
    async def pin(self, thread_id: str, pinned: bool) -> str | None: ...  # 置顶（记下时间，置顶过的不改）或取消，返回置顶的时间；没归谁的不动、返回 None
    async def pins_of(self, owner: str) -> dict[str, str]: ...  # 这个人置顶的对话：线程 id → 置顶的时间
    async def notify(self, thread_id: str, notice: dict) -> None: ...  # 未读加一，记下这一次的通知（时间存储自己记）；没归谁的不动
    async def mark_read(self, thread_id: str) -> None: ...  # 看过了：未读清零，通知一起清掉
    async def unread_of(self, owner: str) -> dict[str, dict]: ...  # 这个人有未读的对话：线程 id → {"unread", "notice"}
    # 线程列表里从检查点算出来的那几项（见模块说明）
    async def rows_of(self, owner: str) -> list[dict]: ...  # 这个人的所有对话：{"thread_id", "pinned_at", "summary"（没记是 None）, "rev"}
    async def put_summary(self, thread_id: str, rev: int, summary: dict) -> None: ...  # 记下算好的；rev 不是现在的（其间作废过）就不记
    async def touch(self, thread_id: str) -> None: ...  # 记着的作废、rev 加一；没归谁的不动

    # 排着的话：{"text", "created_at", "updated_at"}
    # 接在排着的那句后面（front：放到前面，是取出来又放回去的，比排着的那句早），没有就新建
    async def put_pending(self, thread_id: str, mode: str, text: str, front: bool = False, thinking: bool | None = None) -> dict: ...
    async def get_pending(self, thread_id: str, mode: str) -> dict | None: ...
    async def take_pending(self, thread_id: str, mode: str) -> Pending | None: ...  # 取出来并删掉；两处同时取，只有一处拿得到


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(t: Any) -> str | None:
    if t is None:
        return None
    if isinstance(t, datetime):
        return t.astimezone(UTC).isoformat(timespec="milliseconds")
    return t


class MemoryRunStore:
    """测试、评测用。只在一个事件循环里用，方法里没有 await，一个调用做完别的才进来，不用锁。"""

    def __init__(self) -> None:
        self._rows: dict[str, dict] = {}  # 按写入的先后
        self._pending: dict[tuple[str, str], dict] = {}
        self._owners: dict[str, str] = {}  # 线程 id → 归谁
        self._pins: dict[str, str] = {}  # 线程 id → 置顶的时间
        self._unread: dict[str, dict] = {}  # 线程 id → {"unread": 几条没看, "notice": 最近那一条}
        self._summaries: dict[str, dict] = {}  # 线程 id → 列表里那一行算好的几项
        self._revs: dict[str, int] = {}  # 线程 id → 作废过几次

    async def create(self, run_id: str, thread_id: str, kind: str, turn_id: str | None, version: str | None) -> None:
        self._rows[run_id] = dict.fromkeys(FIELDS) | {"id": run_id, "thread_id": thread_id, "kind": kind, "turn_id": turn_id,
                                                       "status": QUEUED, "created_at": _iso(_now()), "version": version}

    async def started(self, run_id: str) -> None:
        if row := self._rows.get(run_id):
            row.update(status=RUNNING, started_at=_iso(_now()))

    async def finish(self, run_id: str, status: str, error_type: str | None = None, error: str | None = None,
                     traceback: str | None = None) -> None:
        if row := self._rows.get(run_id):
            row.update(status=status, ended_at=_iso(_now()), error_type=error_type, error=error, traceback=traceback)

    def _newest(self, rows) -> list[dict]:
        return [dict(r) for r in reversed(list(rows))]

    async def for_thread(self, thread_id: str) -> list[dict]:
        return self._newest(r for r in self._rows.values() if r["thread_id"] == thread_id)

    async def recent(self, status: str | None, limit: int) -> list[dict]:
        return self._newest(r for r in self._rows.values() if status is None or r["status"] == status)[:limit]

    async def sweep(self) -> int:
        rows = [r for r in self._rows.values() if r["status"] in (QUEUED, RUNNING)]
        for r in rows:
            r.update(status=LOST, ended_at=_iso(_now()))
        return len(rows)

    async def delete_thread(self, thread_id: str) -> None:
        self._rows = {k: r for k, r in self._rows.items() if r["thread_id"] != thread_id}
        self._pending = {k: r for k, r in self._pending.items() if k[0] != thread_id}

    async def disown(self, thread_id: str) -> None:
        self._owners.pop(thread_id, None)
        self._pins.pop(thread_id, None)
        self._unread.pop(thread_id, None)
        self._summaries.pop(thread_id, None)
        self._revs.pop(thread_id, None)

    async def claim(self, thread_id: str, owner: str) -> None:
        self._owners.setdefault(thread_id, owner)

    async def owner_of(self, thread_id: str) -> str | None:
        return self._owners.get(thread_id)

    async def transfer(self, source: str, target: str) -> int:
        moved = [t for t, o in self._owners.items() if o == source]
        for t in moved:
            self._owners[t] = target
        return len(moved)

    async def pin(self, thread_id: str, pinned: bool) -> str | None:
        if thread_id not in self._owners or not pinned:
            self._pins.pop(thread_id, None)
            return None
        return self._pins.setdefault(thread_id, _iso(_now()))

    async def pins_of(self, owner: str) -> dict[str, str]:
        return {t: at for t, at in self._pins.items() if self._owners.get(t) == owner}

    async def notify(self, thread_id: str, notice: dict) -> None:
        if thread_id in self._owners:
            n = self._unread.get(thread_id, {}).get("unread", 0)
            self._unread[thread_id] = {"unread": n + 1, "notice": notice | {"at": _iso(_now())}}

    async def mark_read(self, thread_id: str) -> None:
        self._unread.pop(thread_id, None)

    async def unread_of(self, owner: str) -> dict[str, dict]:
        return {t: dict(r) for t, r in self._unread.items() if self._owners.get(t) == owner}

    async def rows_of(self, owner: str) -> list[dict]:
        return [{"thread_id": t, "pinned_at": self._pins.get(t), "summary": dict(s) if (s := self._summaries.get(t)) else None,
                 "rev": self._revs.get(t, 0)} for t, o in self._owners.items() if o == owner]

    async def put_summary(self, thread_id: str, rev: int, summary: dict) -> None:
        if thread_id in self._owners and self._revs.get(thread_id, 0) == rev:
            self._summaries[thread_id] = dict(summary)

    async def touch(self, thread_id: str) -> None:
        if thread_id in self._owners:
            self._summaries.pop(thread_id, None)
            self._revs[thread_id] = self._revs.get(thread_id, 0) + 1

    async def put_pending(self, thread_id: str, mode: str, text: str, front: bool = False, thinking: bool | None = None) -> dict:
        now = _iso(_now())
        row = self._pending.get((thread_id, mode))
        if row is None:
            row = self._pending[(thread_id, mode)] = {"text": text, "thinking": thinking, "created_at": now, "updated_at": now}
        elif front:
            row.update(text=text + JOIN + row["text"], thinking=thinking if row["thinking"] is None else row["thinking"], updated_at=now)
        else:
            row.update(text=row["text"] + JOIN + text, thinking=row["thinking"] if thinking is None else thinking, updated_at=now)
        return dict(row)

    async def get_pending(self, thread_id: str, mode: str) -> dict | None:
        row = self._pending.get((thread_id, mode))
        return dict(row) if row else None

    async def take_pending(self, thread_id: str, mode: str) -> Pending | None:
        row = self._pending.pop((thread_id, mode), None)
        return Pending(row["text"], row["thinking"]) if row else None


_POSTGRES_DDL = (
    """CREATE TABLE IF NOT EXISTS runs (
        id text PRIMARY KEY, seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
        thread_id text NOT NULL, kind text NOT NULL, turn_id text, status text NOT NULL,
        created_at timestamptz NOT NULL, started_at timestamptz, ended_at timestamptz,
        error_type text, error text, traceback text, version text)""",
    "CREATE INDEX IF NOT EXISTS runs_thread_idx ON runs (thread_id, seq)",
    "CREATE INDEX IF NOT EXISTS runs_status_idx ON runs (status, seq)",
    "CREATE INDEX IF NOT EXISTS runs_turn_idx ON runs (turn_id, seq)",  # 算额度时查一轮最后一次执行的状态（见 app.quota）
    """CREATE TABLE IF NOT EXISTS pending_messages (
        thread_id text NOT NULL, mode text NOT NULL, text text NOT NULL, created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL,
        thinking boolean, PRIMARY KEY (thread_id, mode))""",
    "ALTER TABLE pending_messages ADD COLUMN IF NOT EXISTS thinking boolean",  # 2026-11-05 加的，之前建好的表补上
    # 2026-11-10 到 11-11 这张表还有一列 work（排着的那句是不是 Work）：模式改成按对话定以后不用了，建好的表里那一列留着不管
    "CREATE TABLE IF NOT EXISTS thread_owners (thread_id text PRIMARY KEY, owner text NOT NULL, created_at timestamptz NOT NULL)",
    "CREATE INDEX IF NOT EXISTS thread_owners_owner_idx ON thread_owners (owner)",
    "ALTER TABLE thread_owners ADD COLUMN IF NOT EXISTS pinned_at timestamptz",  # 2026-11-07 加的置顶，之前建好的表补上
    "ALTER TABLE thread_owners ADD COLUMN IF NOT EXISTS unread integer NOT NULL DEFAULT 0",  # 2026-11-13 加的未读，同上
    "ALTER TABLE thread_owners ADD COLUMN IF NOT EXISTS notice jsonb",
    "ALTER TABLE thread_owners ADD COLUMN IF NOT EXISTS summary jsonb",  # 2026-11-13 加的列表里算好的那几项，同上；老对话是空的，第一次取列表时算
    "ALTER TABLE thread_owners ADD COLUMN IF NOT EXISTS summary_rev integer NOT NULL DEFAULT 0",
)



class PostgresRunStore:
    """用对话的连接池（检查点、长期记忆也用它，见 ResearchRunner 的 open_storage），每条语句从池子里取一个连接。
    先后用 seq（自增列）：同一毫秒建的也分得清。"""

    _COLS = ", ".join(FIELDS)

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool  # 连接参数见 graph.postgres_pool：自动提交、行是字典

    async def setup(self) -> None:
        """表没建的先建。"""
        async with self.pool.connection() as conn:
            for sql in _POSTGRES_DDL:
                await conn.execute(sql)

    async def _exec(self, sql: str, params: tuple = ()) -> list[dict]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return await cur.fetchall() if cur.description else []

    async def _rows(self, sql: str, params: tuple) -> list[dict]:
        return [{k: _iso(v) if k.endswith("_at") else v for k, v in r.items()} for r in await self._exec(sql, params)]

    async def create(self, run_id: str, thread_id: str, kind: str, turn_id: str | None, version: str | None) -> None:
        await self._exec("INSERT INTO runs (id, thread_id, kind, turn_id, status, created_at, version) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                         (run_id, thread_id, kind, turn_id, QUEUED, _now(), version))

    async def started(self, run_id: str) -> None:
        await self._exec("UPDATE runs SET status = %s, started_at = %s WHERE id = %s", (RUNNING, _now(), run_id))

    async def finish(self, run_id: str, status: str, error_type: str | None = None, error: str | None = None,
                     traceback: str | None = None) -> None:
        await self._exec("UPDATE runs SET status = %s, ended_at = %s, error_type = %s, error = %s, traceback = %s WHERE id = %s",
                         (status, _now(), error_type, error, traceback, run_id))

    async def for_thread(self, thread_id: str) -> list[dict]:
        return await self._rows(f"SELECT {self._COLS} FROM runs WHERE thread_id = %s ORDER BY seq DESC", (thread_id,))

    async def recent(self, status: str | None, limit: int) -> list[dict]:
        if status is None:
            return await self._rows(f"SELECT {self._COLS} FROM runs ORDER BY seq DESC LIMIT %s", (limit,))
        return await self._rows(f"SELECT {self._COLS} FROM runs WHERE status = %s ORDER BY seq DESC LIMIT %s", (status, limit))

    async def sweep(self) -> int:
        return len(await self._exec("UPDATE runs SET status = %s, ended_at = %s WHERE status IN (%s, %s) RETURNING id",
                                    (LOST, _now(), QUEUED, RUNNING)))

    async def delete_thread(self, thread_id: str) -> None:
        await self._exec("DELETE FROM runs WHERE thread_id = %s", (thread_id,))
        await self._exec("DELETE FROM pending_messages WHERE thread_id = %s", (thread_id,))

    async def disown(self, thread_id: str) -> None:
        await self._exec("DELETE FROM thread_owners WHERE thread_id = %s", (thread_id,))

    async def claim(self, thread_id: str, owner: str) -> None:
        await self._exec("INSERT INTO thread_owners (thread_id, owner, created_at) VALUES (%s, %s, %s) ON CONFLICT (thread_id) DO NOTHING",
                         (thread_id, owner, _now()))

    async def owner_of(self, thread_id: str) -> str | None:
        rows = await self._exec("SELECT owner FROM thread_owners WHERE thread_id = %s", (thread_id,))
        return rows[0]["owner"] if rows else None

    async def transfer(self, source: str, target: str) -> int:
        return len(await self._exec("UPDATE thread_owners SET owner = %s WHERE owner = %s RETURNING thread_id", (target, source)))

    async def pin(self, thread_id: str, pinned: bool) -> str | None:
        rows = await self._rows("UPDATE thread_owners SET pinned_at = CASE WHEN %s THEN COALESCE(pinned_at, %s) END WHERE thread_id = %s "
                                "RETURNING pinned_at", (pinned, _now(), thread_id))
        return rows[0]["pinned_at"] if rows else None

    async def pins_of(self, owner: str) -> dict[str, str]:
        rows = await self._rows("SELECT thread_id, pinned_at FROM thread_owners WHERE owner = %s AND pinned_at IS NOT NULL", (owner,))
        return {r["thread_id"]: r["pinned_at"] for r in rows}

    async def notify(self, thread_id: str, notice: dict) -> None:
        await self._exec("UPDATE thread_owners SET unread = unread + 1, notice = %s WHERE thread_id = %s",
                         (Jsonb(notice | {"at": _iso(_now())}), thread_id))

    async def mark_read(self, thread_id: str) -> None:
        await self._exec("UPDATE thread_owners SET unread = 0, notice = NULL WHERE thread_id = %s AND unread > 0", (thread_id,))

    async def unread_of(self, owner: str) -> dict[str, dict]:
        rows = await self._exec("SELECT thread_id, unread, notice FROM thread_owners WHERE owner = %s AND unread > 0", (owner,))
        return {r["thread_id"]: {"unread": r["unread"], "notice": r["notice"]} for r in rows}

    async def rows_of(self, owner: str) -> list[dict]:
        return await self._rows("SELECT thread_id, pinned_at, summary, summary_rev AS rev FROM thread_owners WHERE owner = %s ORDER BY created_at",
                                (owner,))

    async def put_summary(self, thread_id: str, rev: int, summary: dict) -> None:
        await self._exec("UPDATE thread_owners SET summary = %s WHERE thread_id = %s AND summary_rev = %s", (Jsonb(summary), thread_id, rev))

    async def touch(self, thread_id: str) -> None:
        await self._exec("UPDATE thread_owners SET summary = NULL, summary_rev = summary_rev + 1 WHERE thread_id = %s", (thread_id,))

    async def put_pending(self, thread_id: str, mode: str, text: str, front: bool = False, thinking: bool | None = None) -> dict:
        # 思考开关按后发的那句：放到前面的（取出来又放回去的）比排着的早
        if front:
            merged, late, early = "excluded.text || CAST(%s AS TEXT) || pending_messages.text", "pending_messages", "excluded"
        else:
            merged, late, early = "pending_messages.text || CAST(%s AS TEXT) || excluded.text", "excluded", "pending_messages"
        now = _now()
        return (await self._rows("INSERT INTO pending_messages (thread_id, mode, text, thinking, created_at, updated_at) "
                                 "VALUES (%s, %s, %s, %s, %s, %s) "
                                 f"ON CONFLICT (thread_id, mode) DO UPDATE SET text = {merged}, "
                                 f"thinking = COALESCE({late}.thinking, {early}.thinking), "
                                 "updated_at = excluded.updated_at RETURNING text, thinking, created_at, updated_at",
                                 (thread_id, mode, text, thinking, now, now, JOIN)))[0]

    async def get_pending(self, thread_id: str, mode: str) -> dict | None:
        rows = await self._rows("SELECT text, thinking, created_at, updated_at FROM pending_messages WHERE thread_id = %s AND mode = %s",
                                (thread_id, mode))
        return rows[0] if rows else None

    async def take_pending(self, thread_id: str, mode: str) -> Pending | None:
        rows = await self._exec("DELETE FROM pending_messages WHERE thread_id = %s AND mode = %s RETURNING text, thinking",
                                (thread_id, mode))
        return Pending(rows[0]["text"], rows[0]["thinking"]) if rows else None
