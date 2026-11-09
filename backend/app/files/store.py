"""文件表：用户上传的文件、模型写的文件、图片（用户发的、画的）一行一个，抽出来的文字按段另存（2026-11-08 Sean 定，见 docs/图片和文件理解方案.md）。

- 文件按工作区归属：现在一段对话就是一个工作区（workspace_id = 对话编号），以后跨对话的项目是一个工作区挂几段对话，表不用改。
  刚传上来、还没发出去的不归哪个工作区（workspace_id 为空），发出去时绑上（bind）；没发出去的过一天清掉（见 service）。
- 上传的文件只读（Sean 定）：模型要改只能产出新文件。上传的 version 永远是 1。
- 模型写的文件（origin = generated，见 app.tools.write_file）每改一次出新一版、一版一行、存全文：prev_id 指上一版，
  series_id 是第一版的编号（各版串起来），turn_id 是哪一轮写的（那一轮用户那句话的编号）。上传的这两列是空的。
- 原件存在数据卷上（storage 是相对 config.FILES_DIR 的路径），文字存这里的 file_parts（段号从 1 起，PDF 段号就是页码）。
- 图片（kind = image）在 image-service 里，这里只登记归哪段对话、哪一轮（行的编号、storage 都是图的编号），没有文字；
  用户发的 origin = upload，画的 origin = generated（turn_id 是哪一轮画的）。上传的次数、额度只算文件，不算图。
- 知识库（app.knowledge）也是一个工作区，不挂对话：workspace_id = "kb:<账号>"（KB_PREFIX），传上来就归它，不会被当成没发出去的清掉。
  解析好了还要建索引，建好之前一直是 parsing（meta 的 stage 记着到哪一步），建好了才 ready（mark）。

和运行记录同一个 Postgres、同一个连接池（见 ResearchRunner）；测试用内存的。都是协程，在对话的事件循环里调。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict, Field

from app.files.parse import Part

PARSING, READY, FAILED = "parsing", "ready", "failed"
UPLOAD, GENERATED = "upload", "generated"
DOCUMENT, IMAGE = "document", "image"
KB_PREFIX = "kb:"  # 知识库的工作区：kb:<账号>


class FileRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    owner: str
    workspace_id: str | None = None
    origin: Literal["upload", "generated"] = UPLOAD
    kind: Literal["document", "image"] = DOCUMENT
    name: str
    type: str  # 上传的是 parse.TYPES 之一；模型写的是 markdown / html / code
    size: int
    storage: str
    status: Literal["parsing", "ready", "failed"] = PARSING
    error: str = ""  # 没读成的原因（人话）
    meta: dict[str, Any] = Field(default_factory=dict)
    version: int = 1
    prev_id: str | None = None
    series_id: str | None = None
    turn_id: str | None = None
    created_at: str = ""


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(t: Any) -> str:
    return t.astimezone(UTC).isoformat(timespec="milliseconds") if isinstance(t, datetime) else t


class FileStore(Protocol):
    async def setup(self) -> None: ...
    async def add(self, row: FileRow, parts: list[Part] | None = None) -> None: ...  # 模型写的直接带着文字（READY）
    async def get(self, file_id: str) -> FileRow | None: ...
    async def finish(self, file_id: str, status: str, meta: dict, error: str, parts: list[Part]) -> None: ...  # 解析完：状态、文字一起写
    async def mark(self, file_id: str, status: str, meta: dict, error: str) -> bool: ...  # 只改状态（知识库建好索引），返回这一行还在不在
    async def parts(self, file_id: str) -> list[Part]: ...
    async def bind(self, file_ids: list[str], owner: str, workspace_id: str) -> int: ...  # 还没归哪个工作区、解析好了的才绑，返回绑了几个
    async def of_workspace(self, workspace_id: str) -> list[FileRow]: ...
    async def delete(self, file_ids: list[str]) -> None: ...  # 行和文字一起删（原件由 service 删）
    async def unbound_before(self, at: datetime) -> list[FileRow]: ...
    async def with_status(self, status: str) -> list[FileRow]: ...
    async def uploads_since(self, owner: str, at: datetime) -> int: ...  # 只算对话里上传的文件（不算图、不算知识库的）


class MemoryFileStore:
    """测试、评测用，只在一个事件循环里用。"""

    def __init__(self) -> None:
        self._rows: dict[str, FileRow] = {}
        self._at: dict[str, datetime] = {}
        self._parts: dict[str, list[Part]] = {}

    async def setup(self) -> None:
        pass

    async def add(self, row: FileRow, parts: list[Part] | None = None) -> None:
        at = _now()
        self._rows[row.id] = row.model_copy(update={"created_at": _iso(at)})
        self._at[row.id] = at
        if parts is not None:
            self._parts[row.id] = list(parts)

    async def get(self, file_id: str) -> FileRow | None:
        r = self._rows.get(file_id)
        return r.model_copy(deep=True) if r else None

    async def finish(self, file_id: str, status: str, meta: dict, error: str, parts: list[Part]) -> None:
        if r := self._rows.get(file_id):
            self._rows[file_id] = r.model_copy(update={"status": status, "meta": meta, "error": error})
            self._parts[file_id] = list(parts)

    async def mark(self, file_id: str, status: str, meta: dict, error: str) -> bool:
        if (r := self._rows.get(file_id)) is None:
            return False
        self._rows[file_id] = r.model_copy(update={"status": status, "meta": meta, "error": error})
        return True

    async def parts(self, file_id: str) -> list[Part]:
        return list(self._parts.get(file_id, []))

    async def bind(self, file_ids: list[str], owner: str, workspace_id: str) -> int:
        n = 0
        for i in file_ids:
            r = self._rows.get(i)
            if r and r.owner == owner and r.workspace_id is None and r.status == READY:
                self._rows[i] = r.model_copy(update={"workspace_id": workspace_id})
                n += 1
        return n

    async def of_workspace(self, workspace_id: str) -> list[FileRow]:
        return [r.model_copy(deep=True) for r in self._rows.values() if r.workspace_id == workspace_id]

    async def delete(self, file_ids: list[str]) -> None:
        for i in file_ids:
            self._rows.pop(i, None)
            self._at.pop(i, None)
            self._parts.pop(i, None)

    async def unbound_before(self, at: datetime) -> list[FileRow]:
        return [r.model_copy(deep=True) for i, r in self._rows.items() if r.workspace_id is None and self._at[i] < at]

    async def with_status(self, status: str) -> list[FileRow]:
        return [r.model_copy(deep=True) for r in self._rows.values() if r.status == status]

    async def uploads_since(self, owner: str, at: datetime) -> int:
        return sum(1 for i, r in self._rows.items() if r.owner == owner and r.origin == UPLOAD and r.kind == DOCUMENT and self._at[i] >= at
                   and not (r.workspace_id or "").startswith(KB_PREFIX))


_DDL = (
    """CREATE TABLE IF NOT EXISTS files (
        id text PRIMARY KEY, seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
        owner text NOT NULL, workspace_id text, origin text NOT NULL, kind text NOT NULL,
        name text NOT NULL, type text NOT NULL, size bigint NOT NULL, storage text NOT NULL,
        status text NOT NULL, error text NOT NULL DEFAULT '', meta jsonb NOT NULL DEFAULT '{}',
        version integer NOT NULL DEFAULT 1, prev_id text, created_at timestamptz NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS files_workspace_idx ON files (workspace_id)",
    "CREATE INDEX IF NOT EXISTS files_owner_idx ON files (owner, created_at)",
    "CREATE INDEX IF NOT EXISTS files_status_idx ON files (status)",
    """CREATE TABLE IF NOT EXISTS file_parts (
        file_id text NOT NULL, n integer NOT NULL, title text NOT NULL, text text NOT NULL, PRIMARY KEY (file_id, n))""",
    # 2026-11-08 模型写的文件（Artifact）加的两列；表是 PR #102 建的，已经有了的照这里补上
    "ALTER TABLE files ADD COLUMN IF NOT EXISTS series_id text",
    "ALTER TABLE files ADD COLUMN IF NOT EXISTS turn_id text",
)
_COLS = ("id", "owner", "workspace_id", "origin", "kind", "name", "type", "size", "storage", "status", "error", "meta", "version", "prev_id",
         "series_id", "turn_id", "created_at")


class PostgresFileStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool  # 连接参数见 graph.postgres_pool：自动提交、行是字典

    async def setup(self) -> None:
        async with self.pool.connection() as conn:
            for sql in _DDL:
                await conn.execute(sql)

    async def _rows(self, sql: str, params: tuple = ()) -> list[FileRow]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return [FileRow.model_validate({**r, "created_at": _iso(r["created_at"])}) for r in await cur.fetchall()]

    async def _exec(self, sql: str, params: tuple = ()) -> int:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return cur.rowcount

    async def add(self, row: FileRow, parts: list[Part] | None = None) -> None:
        values = row.model_dump() | {"meta": Jsonb(row.meta), "created_at": _now()}
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(f"INSERT INTO files ({', '.join(_COLS)}) VALUES ({', '.join(['%s'] * len(_COLS))})", tuple(values[c] for c in _COLS))
            if parts:
                async with conn.cursor() as cur:
                    await cur.executemany("INSERT INTO file_parts (file_id, n, title, text) VALUES (%s, %s, %s, %s)",
                                          [(row.id, n, p.title, p.text) for n, p in enumerate(parts, 1)])

    async def get(self, file_id: str) -> FileRow | None:
        rows = await self._rows(f"SELECT {', '.join(_COLS)} FROM files WHERE id = %s", (file_id,))
        return rows[0] if rows else None

    async def finish(self, file_id: str, status: str, meta: dict, error: str, parts: list[Part]) -> None:
        async with self.pool.connection() as conn, conn.transaction():
            cur = await conn.execute("UPDATE files SET status = %s, meta = %s, error = %s WHERE id = %s", (status, Jsonb(meta), error, file_id))
            if cur.rowcount == 0:  # 解析的时候被去掉了（输入框里点了叉）：文字不写，免得留下没人指着的段落
                return
            await conn.execute("DELETE FROM file_parts WHERE file_id = %s", (file_id,))
            if parts:
                async with conn.cursor() as cur:
                    await cur.executemany("INSERT INTO file_parts (file_id, n, title, text) VALUES (%s, %s, %s, %s)",
                                          [(file_id, n, p.title, p.text) for n, p in enumerate(parts, 1)])

    async def mark(self, file_id: str, status: str, meta: dict, error: str) -> bool:
        return await self._exec("UPDATE files SET status = %s, meta = %s, error = %s WHERE id = %s", (status, Jsonb(meta), error, file_id)) > 0

    async def parts(self, file_id: str) -> list[Part]:
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT title, text FROM file_parts WHERE file_id = %s ORDER BY n", (file_id,))
            return [Part(r["title"], r["text"]) for r in await cur.fetchall()]

    async def bind(self, file_ids: list[str], owner: str, workspace_id: str) -> int:
        return await self._exec("UPDATE files SET workspace_id = %s WHERE id = ANY(%s) AND owner = %s AND workspace_id IS NULL AND status = %s",
                                (workspace_id, file_ids, owner, READY))

    async def of_workspace(self, workspace_id: str) -> list[FileRow]:
        return await self._rows(f"SELECT {', '.join(_COLS)} FROM files WHERE workspace_id = %s ORDER BY seq", (workspace_id,))

    async def delete(self, file_ids: list[str]) -> None:
        if not file_ids:
            return
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("DELETE FROM file_parts WHERE file_id = ANY(%s)", (file_ids,))
            await conn.execute("DELETE FROM files WHERE id = ANY(%s)", (file_ids,))

    async def unbound_before(self, at: datetime) -> list[FileRow]:
        return await self._rows(f"SELECT {', '.join(_COLS)} FROM files WHERE workspace_id IS NULL AND created_at < %s", (at,))

    async def with_status(self, status: str) -> list[FileRow]:
        return await self._rows(f"SELECT {', '.join(_COLS)} FROM files WHERE status = %s ORDER BY seq", (status,))

    async def uploads_since(self, owner: str, at: datetime) -> int:
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT count(*) AS n FROM files WHERE owner = %s AND origin = %s AND kind = %s AND created_at >= %s"
                                     " AND (workspace_id IS NULL OR NOT starts_with(workspace_id, %s))", (owner, UPLOAD, DOCUMENT, at, KB_PREFIX))
            return (await cur.fetchone())["n"]
