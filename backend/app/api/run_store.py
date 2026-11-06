"""运行记录：每次执行（发一句话、重试、重新回答……）一行，存进库里，事后能查历史运行和失败原因。

学的是 LangGraph Agent Server 的 run：执行登记（RunRegistry）在进程内存里，跑完就没了；这里把每次执行的来历、状态、时间、错误落盘。
和检查点、长期记忆存在同一个 Postgres 里，共用一个连接池（见 ResearchRunner 的 open_storage）；测试和评测用内存的。
都是协程，在对话的事件循环里调（连接池绑定那个循环）。时间由存储自己记（UTC）。删掉对话时它的运行记录一起删。

同一个库里还有一张 pending_messages：回答进行中用户又说的话，等着交给 agent（Agent Server 的双发策略里的 enqueue）。
每个对话、每种方式一行：排队（queue）是这一轮结束后开新的一轮发；插话（guide）是插进正在跑的这一轮，主 agent 下一次调模型前放进对话。
又说的接在排着的那句后面，合成一句。存进库里，服务重启也不丢。
每行还记着用户发的时候开没开思考（输入框旁边的开关，没给是空）：合成一句时按后发的那句，发出去时带上（见 ResearchRunner._aended）。

还有一张 thread_owners：每段对话归谁（见 app.identity，登录用户的 id 或游客的 guest:…），开对话时记下，列表和所有对话接口按它查。
登录以前的老对话没有这一行（没归谁）：只有管理员看得到（2026-11-06 Sean 定，都是测试数据）。游客登录后把 TA 的对话整批改归账号（transfer）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, NamedTuple, Protocol

from psycopg_pool import AsyncConnectionPool

QUEUED, RUNNING, SUCCESS, STOPPED, ERROR, LOST = "queued", "running", "success", "stopped", "error", "lost"
# success：跑到头了（这一轮的结果另看检查点里的轮记录，比如被内容审核拦下）；stopped：用户点了停止；
# error：抛了错；lost：进程重启时还在排队或在跑（服务启动时补记，见 sweep）
STATUSES = (QUEUED, RUNNING, SUCCESS, STOPPED, ERROR, LOST)
QUEUE = "queue"  # 排着的话的方式：这一轮结束后开新的一轮发（见 ResearchRunner.queue）
GUIDE = "guide"  # 插进正在跑的这一轮：主 agent 下一次调模型之前放进对话（见 ResearchRunner.guide）
JOIN = "\n"  # 又说的话接在排着的那句后面，中间换行
class Pending(NamedTuple):
    """取出来的排着的话。"""

    text: str
    thinking: bool | None = None


FIELDS = ("id", "thread_id", "kind", "turn_id", "status", "created_at", "started_at", "ended_at", "error_type", "error", "traceback", "version")


class RunStore(Protocol):
    """一行的字段见 FIELDS：kind 是这次执行怎么来的（new / say / retry / regenerate / edit），turn_id 是这一轮用户那句话的消息 id，
    version 是跑它的代码版本（部署时的提交号）。列表都是新的在前（按写入的先后，同一毫秒建的也分得清），时间是 ISO 字符串。"""

    async def create(self, run_id: str, thread_id: str, kind: str, turn_id: str | None, version: str | None) -> None: ...
    async def started(self, run_id: str) -> None: ...
    async def finish(self, run_id: str, status: str, error_type: str | None = None, error: str | None = None,
                     traceback: str | None = None) -> None: ...
    async def for_thread(self, thread_id: str) -> list[dict]: ...
    async def recent(self, status: str | None, limit: int) -> list[dict]: ...
    async def sweep(self) -> int: ...  # 还在排队、在跑的记成 lost，返回条数。只在服务启动时调：单进程部署，这时它们都已经没在跑了
    async def delete_thread(self, thread_id: str) -> None: ...  # 运行记录、排着的话、归谁一起删

    # 对话归谁（见模块说明）
    async def claim(self, thread_id: str, owner: str) -> None: ...  # 记下归谁；已经记过的不改
    async def owner_of(self, thread_id: str) -> str | None: ...  # 没记过（登录以前的老对话）是 None
    async def threads_of(self, owner: str) -> list[str]: ...  # 这个人的所有对话（线程 id）
    async def owned(self) -> set[str]: ...  # 记过归谁的所有线程 id（列管理员能看的没归谁的对话时，拿全部线程减掉它）
    async def transfer(self, source: str, target: str) -> int: ...  # source 的对话全改归 target，返回改了几段

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
        self._owners.pop(thread_id, None)

    async def claim(self, thread_id: str, owner: str) -> None:
        self._owners.setdefault(thread_id, owner)

    async def owner_of(self, thread_id: str) -> str | None:
        return self._owners.get(thread_id)

    async def threads_of(self, owner: str) -> list[str]:
        return [t for t, o in self._owners.items() if o == owner]

    async def owned(self) -> set[str]:
        return set(self._owners)

    async def transfer(self, source: str, target: str) -> int:
        moved = [t for t, o in self._owners.items() if o == source]
        for t in moved:
            self._owners[t] = target
        return len(moved)

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
    """CREATE TABLE IF NOT EXISTS pending_messages (
        thread_id text NOT NULL, mode text NOT NULL, text text NOT NULL, created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL,
        thinking boolean, PRIMARY KEY (thread_id, mode))""",
    "ALTER TABLE pending_messages ADD COLUMN IF NOT EXISTS thinking boolean",  # 2026-11-05 加的，之前建好的表补上
    "CREATE TABLE IF NOT EXISTS thread_owners (thread_id text PRIMARY KEY, owner text NOT NULL, created_at timestamptz NOT NULL)",
    "CREATE INDEX IF NOT EXISTS thread_owners_owner_idx ON thread_owners (owner)",
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
        await self._exec("DELETE FROM thread_owners WHERE thread_id = %s", (thread_id,))

    async def claim(self, thread_id: str, owner: str) -> None:
        await self._exec("INSERT INTO thread_owners (thread_id, owner, created_at) VALUES (%s, %s, %s) ON CONFLICT (thread_id) DO NOTHING",
                         (thread_id, owner, _now()))

    async def owner_of(self, thread_id: str) -> str | None:
        rows = await self._exec("SELECT owner FROM thread_owners WHERE thread_id = %s", (thread_id,))
        return rows[0]["owner"] if rows else None

    async def threads_of(self, owner: str) -> list[str]:
        return [r["thread_id"] for r in await self._exec("SELECT thread_id FROM thread_owners WHERE owner = %s ORDER BY created_at", (owner,))]

    async def owned(self) -> set[str]:
        return {r["thread_id"] for r in await self._exec("SELECT thread_id FROM thread_owners")}

    async def transfer(self, source: str, target: str) -> int:
        return len(await self._exec("UPDATE thread_owners SET owner = %s WHERE owner = %s RETURNING thread_id", (target, source)))

    async def put_pending(self, thread_id: str, mode: str, text: str, front: bool = False, thinking: bool | None = None) -> dict:
        # 思考开关按后发的那句：放到前面的（取出来又放回去的）比排着的早
        if front:
            merged, think = "excluded.text || CAST(%s AS TEXT) || pending_messages.text", "COALESCE(pending_messages.thinking, excluded.thinking)"
        else:
            merged, think = "pending_messages.text || CAST(%s AS TEXT) || excluded.text", "COALESCE(excluded.thinking, pending_messages.thinking)"
        now = _now()
        return (await self._rows("INSERT INTO pending_messages (thread_id, mode, text, thinking, created_at, updated_at) "
                                 "VALUES (%s, %s, %s, %s, %s, %s) "
                                 f"ON CONFLICT (thread_id, mode) DO UPDATE SET text = {merged}, thinking = {think}, "
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
