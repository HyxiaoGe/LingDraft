"""资料库（2026-11-10 Sean 定，界面上叫“资料库”）：一个人可以有几个，各有名字和说明，文件归在某一个里。

- 一个资料库就是文件表里的一个工作区：workspace_id = "kb:<资料库编号>"（见 service.workspace）。文件行上有 owner，认人按 owner，不靠工作区的写法。
- 名字同一个人不重复（不分大小写）：模型按名字指定查哪个（见 app.tools.knowledge）。
- 11-10 以前每人一个库（workspace_id = "kb:<账号>"）：Postgres 建表时一次搬进一个叫 LEGACY_NAME 的资料库（已经搬过的不会再有这种行）。
测试用内存的。都是协程，在对话的事件循环里调。
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any, Protocol

from psycopg.errors import UniqueViolation
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

LEGACY_NAME = "我的资料"


class Library(BaseModel):
    id: str
    owner: str
    name: str
    description: str = ""
    created_at: str = ""


def new_id() -> str:
    return "lib-" + secrets.token_hex(8)


def _iso(t: Any) -> str:
    return t.astimezone(UTC).isoformat(timespec="milliseconds") if isinstance(t, datetime) else t


class LibraryStore(Protocol):
    async def setup(self) -> None: ...
    async def of_owner(self, owner: str) -> list[Library]: ...  # 先建的在前
    async def get(self, library_id: str) -> Library | None: ...
    async def add(self, lib: Library) -> bool: ...  # 名字重了（同一个人）返回 False
    async def update(self, library_id: str, name: str, description: str) -> bool: ...  # 名字重了返回 False
    async def delete(self, library_id: str) -> None: ...


class MemoryLibraryStore:
    def __init__(self) -> None:
        self.rows: dict[str, Library] = {}

    async def setup(self) -> None:
        pass

    def _taken(self, owner: str, name: str, but: str | None = None) -> bool:
        return any(x.owner == owner and x.name.lower() == name.lower() and x.id != but for x in self.rows.values())

    async def of_owner(self, owner: str) -> list[Library]:
        return [x for x in self.rows.values() if x.owner == owner]

    async def get(self, library_id: str) -> Library | None:
        return self.rows.get(library_id)

    async def add(self, lib: Library) -> bool:
        if self._taken(lib.owner, lib.name):
            return False
        self.rows[lib.id] = lib.model_copy(update={"created_at": lib.created_at or datetime.now(UTC).isoformat(timespec="milliseconds")})
        return True

    async def update(self, library_id: str, name: str, description: str) -> bool:
        lib = self.rows.get(library_id)
        if lib is None or self._taken(lib.owner, name, but=library_id):
            return False
        self.rows[library_id] = lib.model_copy(update={"name": name, "description": description})
        return True

    async def delete(self, library_id: str) -> None:
        self.rows.pop(library_id, None)


_DDL = (
    """CREATE TABLE IF NOT EXISTS kb_libraries (
        id text PRIMARY KEY, owner text NOT NULL, name text NOT NULL, description text NOT NULL DEFAULT '',
        created_at timestamptz NOT NULL DEFAULT now())""",
    "CREATE UNIQUE INDEX IF NOT EXISTS kb_libraries_owner_name ON kb_libraries (owner, lower(name))",
)


class PostgresLibraryStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool  # 和文件表共用（见 ResearchRunner）：建表、搬旧文件都在文件表之后

    async def setup(self) -> None:
        async with self.pool.connection() as conn:
            for sql in _DDL:
                await conn.execute(sql)
            # 11-10 以前的：每人一个库（workspace_id = "kb:<账号>"），搬进“我的资料”。一个人一个事务。
            # 已经有叫这个名字的（搬过以后旧版本在换下来之前又收了文件、或者自己建了同名的）就搬进那个，不再建
            cur = await conn.execute("SELECT DISTINCT owner FROM files WHERE workspace_id = 'kb:' || owner")
            for r in await cur.fetchall():
                async with conn.transaction():
                    await conn.execute("INSERT INTO kb_libraries (id, owner, name) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                                       (new_id(), r["owner"], LEGACY_NAME))
                    got = await conn.execute("SELECT id FROM kb_libraries WHERE owner = %s AND lower(name) = lower(%s)", (r["owner"], LEGACY_NAME))
                    lid = (await got.fetchone())["id"]
                    await conn.execute("UPDATE files SET workspace_id = %s WHERE owner = %s AND workspace_id = 'kb:' || owner",
                                       ("kb:" + lid, r["owner"]))

    async def _libs(self, sql: str, params: tuple) -> list[Library]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return [Library.model_validate({**r, "created_at": _iso(r["created_at"])}) for r in await cur.fetchall()]

    async def of_owner(self, owner: str) -> list[Library]:
        return await self._libs("SELECT id, owner, name, description, created_at FROM kb_libraries WHERE owner = %s ORDER BY created_at, id", (owner,))

    async def get(self, library_id: str) -> Library | None:
        got = await self._libs("SELECT id, owner, name, description, created_at FROM kb_libraries WHERE id = %s", (library_id,))
        return got[0] if got else None

    async def add(self, lib: Library) -> bool:
        async with self.pool.connection() as conn:
            cur = await conn.execute("INSERT INTO kb_libraries (id, owner, name, description) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                                     (lib.id, lib.owner, lib.name, lib.description))
            return cur.rowcount > 0

    async def update(self, library_id: str, name: str, description: str) -> bool:
        try:
            async with self.pool.connection() as conn:
                cur = await conn.execute("UPDATE kb_libraries SET name = %s, description = %s WHERE id = %s", (name, description, library_id))
                return cur.rowcount > 0
        except UniqueViolation:  # 同一个人已经有这个名字
            return False

    async def delete(self, library_id: str) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("DELETE FROM kb_libraries WHERE id = %s", (library_id,))
