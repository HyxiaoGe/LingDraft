"""定时任务的表。测试用内存的；Postgres 的和运行记录共用一个连接池（见 api.research_runner.open_tasks）。都是协程，在对话的事件循环里调。

一行一个任务。改只改给到的那几列（patch）：到点发起（改 next_at、last_*）和用户在页面上改（标题、交代、什么时候跑）可能前后脚，
整行覆盖会把对方刚写的冲掉。
"""

from __future__ import annotations

import secrets
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Any, Protocol

from psycopg import sql
from psycopg_pool import AsyncConnectionPool

ACTIVE, PAUSED, DONE = "active", "paused", "done"  # done：只跑一次的，跑过了
BY_USER, BY_FAILURES = "user", "failures"  # 谁停的：用户自己点的暂停；连着几次没跑成，自己停下的
# 最近一次怎么样：在跑、跑完了、没跑成、用户点了停止、额度用完没跑、那段对话卡着没跑（上一轮出错了没重试、在等用户回答）、服务重启打断了
RUNNING, OK, FAILED, STOPPED, NO_CREDITS, BLOCKED, LOST = "running", "ok", "failed", "stopped", "no_credits", "blocked", "lost"


@dataclass(frozen=True)
class Task:
    id: str
    owner: str
    tier: str  # 这个人是哪一档（user / admin）：到点发起时没有令牌，查额度按它（见 app.quota）
    title: str
    prompt: str  # 每次跑时交给模型的那份完整交代
    created_at: datetime
    updated_at: datetime
    cron: str | None = None  # 重复的：5 段的表达式；只跑一次的是 None
    once_at: datetime | None = None  # 只跑一次的：什么时候
    status: str = ACTIVE
    paused_by: str = ""
    thread_id: str | None = None  # 跑在哪段对话里；None：还没跑过（接口建的）、那段对话删了，下次跑时新开
    work: bool = False  # 新开的那段是不是工作任务
    option: str | None = None  # 新开的那段用哪个模型选项；None 是 Auto
    next_at: datetime | None = None  # 下一次什么时候跑；停着的、跑完了的是 None
    last_at: datetime | None = None
    last_status: str = ""
    failures: int = 0  # 连着几次没跑成


COLUMNS = tuple(Task.__dataclass_fields__)


def new_id() -> str:
    return "task-" + secrets.token_hex(6)


class TaskStore(Protocol):
    async def setup(self) -> None: ...
    async def add(self, task: Task) -> None: ...
    async def get(self, task_id: str) -> Task | None: ...
    async def of_owner(self, owner: str) -> list[Task]: ...  # 先建的在前
    async def patch(self, task_id: str, **fields: Any) -> Task | None: ...  # 只改给到的列，返回改完的；没有这一行是 None
    async def delete(self, task_id: str) -> bool: ...
    async def due(self, now: datetime) -> list[Task]: ...  # 开着、到点了的，先到点的在前
    async def running(self) -> list[Task]: ...  # 最近一次还记着在跑的
    async def detach(self, thread_id: str) -> None: ...  # 这段对话删了：挂在它上面的任务下次跑时新开一段


class MemoryTaskStore:
    def __init__(self) -> None:
        self.rows: dict[str, Task] = {}

    async def setup(self) -> None:
        pass

    async def add(self, task: Task) -> None:
        self.rows[task.id] = task

    async def get(self, task_id: str) -> Task | None:
        return self.rows.get(task_id)

    async def of_owner(self, owner: str) -> list[Task]:
        return sorted((t for t in self.rows.values() if t.owner == owner), key=lambda t: (t.created_at, t.id))

    async def patch(self, task_id: str, **fields: Any) -> Task | None:
        if (t := self.rows.get(task_id)) is None:
            return None
        self.rows[task_id] = t = replace(t, **fields)
        return t

    async def delete(self, task_id: str) -> bool:
        return self.rows.pop(task_id, None) is not None

    async def due(self, now: datetime) -> list[Task]:
        return sorted((t for t in self.rows.values() if t.status == ACTIVE and t.next_at is not None and t.next_at <= now),
                      key=lambda t: (t.next_at, t.id))

    async def running(self) -> list[Task]:
        return [t for t in self.rows.values() if t.last_status == RUNNING]

    async def detach(self, thread_id: str) -> None:
        for t in list(self.rows.values()):
            if t.thread_id == thread_id:
                self.rows[t.id] = replace(t, thread_id=None)


_DDL = (
    """CREATE TABLE IF NOT EXISTS scheduled_tasks (
        id text PRIMARY KEY, owner text NOT NULL, tier text NOT NULL, title text NOT NULL, prompt text NOT NULL,
        created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL,
        cron text, once_at timestamptz, status text NOT NULL, paused_by text NOT NULL DEFAULT '',
        thread_id text, work boolean NOT NULL DEFAULT false, option text,
        next_at timestamptz, last_at timestamptz, last_status text NOT NULL DEFAULT '', failures integer NOT NULL DEFAULT 0)""",
    "CREATE INDEX IF NOT EXISTS scheduled_tasks_owner_idx ON scheduled_tasks (owner, created_at)",
    "CREATE INDEX IF NOT EXISTS scheduled_tasks_due_idx ON scheduled_tasks (next_at) WHERE status = 'active'",
)
_SELECT = "SELECT " + ", ".join(COLUMNS) + " FROM scheduled_tasks"


class PostgresTaskStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    async def setup(self) -> None:
        async with self.pool.connection() as conn:
            for stmt in _DDL:
                await conn.execute(stmt)

    async def _tasks(self, query: Any, params: tuple = ()) -> list[Task]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(query, params)
            return [Task(**r) for r in await cur.fetchall()] if cur.description else []

    async def add(self, task: Task) -> None:
        row = asdict(task)
        await self._tasks(f"INSERT INTO scheduled_tasks ({', '.join(COLUMNS)}) VALUES ({', '.join(['%s'] * len(COLUMNS))})",
                          tuple(row[c] for c in COLUMNS))

    async def get(self, task_id: str) -> Task | None:
        got = await self._tasks(_SELECT + " WHERE id = %s", (task_id,))
        return got[0] if got else None

    async def of_owner(self, owner: str) -> list[Task]:
        return await self._tasks(_SELECT + " WHERE owner = %s ORDER BY created_at, id", (owner,))

    async def patch(self, task_id: str, **fields: Any) -> Task | None:
        if not fields:
            return await self.get(task_id)
        if unknown := set(fields) - set(COLUMNS):
            raise ValueError(f"定时任务没有这几列：{sorted(unknown)}")
        sets = sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(k)) for k in fields)
        query = sql.SQL("UPDATE scheduled_tasks SET {} WHERE id = %s RETURNING {}").format(
            sets, sql.SQL(", ").join(sql.Identifier(c) for c in COLUMNS))
        got = await self._tasks(query, (*fields.values(), task_id))
        return got[0] if got else None

    async def delete(self, task_id: str) -> bool:
        async with self.pool.connection() as conn:
            cur = await conn.execute("DELETE FROM scheduled_tasks WHERE id = %s", (task_id,))
            return cur.rowcount > 0

    async def due(self, now: datetime) -> list[Task]:
        return await self._tasks(_SELECT + " WHERE status = 'active' AND next_at <= %s ORDER BY next_at, id", (now,))

    async def running(self) -> list[Task]:
        return await self._tasks(_SELECT + " WHERE last_status = %s", (RUNNING,))

    async def detach(self, thread_id: str) -> None:
        await self._tasks("UPDATE scheduled_tasks SET thread_id = NULL WHERE thread_id = %s", (thread_id,))
