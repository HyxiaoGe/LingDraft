"""知识库的块：一块一行，向量和关键词都在这张表上查（pgvector，和文件表同一个库、同一个连接池）。

- file_id 外键连着文件表，删文件时块在同一个事务里没了（Fusion 把向量放在 Milvus，才多了清理任务表、清理 worker）。
- 向量索引先不建：按账号过滤后精确算，一个人几千块几十毫秒（11-09 dev 上演练过，2 万块 130–360 ms）。总量大了再建 HNSW，
  同时打开 hnsw.iterative_scan，按账号过滤以后不少回结果。
- 关键词：tsvector 用 simple 配置，词在 Python 里用 jieba 分好（见 text），查询的词之间是“或”，按 ts_rank_cd 排。
- model 记着是哪个向量模型算的：以后换模型后台全部重算，不新旧混着查。
测试用内存的（余弦、词的命中数照着算）。都是协程，在对话的事件循环里调。
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence
from typing import NamedTuple, Protocol

from psycopg_pool import AsyncConnectionPool

DIMS = 1024


class StoredChunk(NamedTuple):
    file_id: str
    owner: str
    part: int
    idx: int
    name: str  # 文件名（知识库的文件不改名）
    title: str  # 段的标题
    content: str
    emb: Sequence[float]  # 建索引时存成 array('f')：最大的文件（200 万字）近 3000 块，存成 list 要 90 MB
    terms: str  # 分好的词，空格隔开（见 text.doc_terms）


class Found(NamedTuple):
    file_id: str
    part: int
    idx: int
    name: str
    title: str
    content: str

    @property
    def key(self) -> tuple[str, int, int]:
        return self.file_id, self.part, self.idx


class ChunkStore(Protocol):
    async def setup(self) -> None: ...
    async def replace(self, file_id: str, chunks: list[StoredChunk], model: str) -> None: ...  # 这个文件的块整个换掉
    async def delete(self, file_id: str) -> None: ...
    async def by_vector(self, owner: str, emb: Sequence[float], k: int) -> list[Found]: ...
    async def by_terms(self, owner: str, terms: list[str], k: int) -> list[Found]: ...  # 任一词出现的，按命中排
    async def count(self, owner: str) -> int: ...


class MemoryChunkStore:
    def __init__(self) -> None:
        self.rows: dict[str, list[StoredChunk]] = {}

    async def setup(self) -> None:
        pass

    async def replace(self, file_id: str, chunks: list[StoredChunk], model: str) -> None:
        self.rows[file_id] = list(chunks)

    async def delete(self, file_id: str) -> None:
        self.rows.pop(file_id, None)

    def _of(self, owner: str) -> list[StoredChunk]:
        return [c for cs in self.rows.values() for c in cs if c.owner == owner]

    @staticmethod
    def _found(c: StoredChunk) -> Found:
        return Found(c.file_id, c.part, c.idx, c.name, c.title, c.content)

    async def by_vector(self, owner: str, emb: Sequence[float], k: int) -> list[Found]:
        def cos(v: Sequence[float]) -> float:
            n = math.sqrt(sum(x * x for x in v)) * math.sqrt(sum(x * x for x in emb))
            return sum(a * b for a, b in zip(v, emb, strict=True)) / n if n else 0.0
        return [self._found(c) for c in sorted(self._of(owner), key=lambda c: cos(c.emb), reverse=True)[:k]]

    async def by_terms(self, owner: str, terms: list[str], k: int) -> list[Found]:
        scored = []
        for c in self._of(owner):
            words = c.terms.split()
            if score := sum(words.count(t) for t in terms):
                scored.append((score, c))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [self._found(c) for _, c in scored[:k]]

    async def count(self, owner: str) -> int:
        return len(self._of(owner))


_DDL = (
    "CREATE EXTENSION IF NOT EXISTS vector",  # dev 的两个库 11-09 已经建好；灵稿连库的账号不是超级用户，扩展已有时这句不报错（本机库试过）
    f"""CREATE TABLE IF NOT EXISTS kb_chunks (
        file_id text NOT NULL REFERENCES files (id) ON DELETE CASCADE, owner text NOT NULL, part integer NOT NULL, idx integer NOT NULL,
        name text NOT NULL, title text NOT NULL, content text NOT NULL, emb vector({DIMS}) NOT NULL, tsv tsvector NOT NULL, model text NOT NULL,
        PRIMARY KEY (file_id, part, idx))""",
    "CREATE INDEX IF NOT EXISTS kb_chunks_owner_idx ON kb_chunks (owner)",
    "CREATE INDEX IF NOT EXISTS kb_chunks_tsv_idx ON kb_chunks USING gin (tsv)",
)
_COLS = "file_id, part, idx, name, title, content"


def _vec(v: Sequence[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"  # 库里存的是 float4，7 位有效数字就够


def _tsquery(terms: list[str]) -> str:
    """词之间是“或”。词只有字母数字汉字和下划线（见 text._words），加引号就不会被当成运算符。"""
    return " | ".join(f"'{t}'" for t in terms)


class PostgresChunkStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool  # 和文件表共用（见 ResearchRunner）：建表在文件表之后

    async def setup(self) -> None:
        async with self.pool.connection() as conn:
            for sql in _DDL:
                await conn.execute(sql)

    async def replace(self, file_id: str, chunks: list[StoredChunk], model: str) -> None:
        # 向量转成文字在线程里做：大文件几百万个数，放在对话的事件循环里要卡住好几秒
        rows = await asyncio.to_thread(lambda: [(c.file_id, c.owner, c.part, c.idx, c.name, c.title, c.content, _vec(c.emb), c.terms, model)
                                                for c in chunks])
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("DELETE FROM kb_chunks WHERE file_id = %s", (file_id,))
            async with conn.cursor() as cur:
                await cur.executemany(
                    "INSERT INTO kb_chunks (file_id, owner, part, idx, name, title, content, emb, tsv, model)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector, to_tsvector('simple', %s), %s)", rows)

    async def delete(self, file_id: str) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("DELETE FROM kb_chunks WHERE file_id = %s", (file_id,))

    async def _found(self, sql: str, params: tuple) -> list[Found]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return [Found(r["file_id"], r["part"], r["idx"], r["name"], r["title"], r["content"]) for r in await cur.fetchall()]

    async def by_vector(self, owner: str, emb: Sequence[float], k: int) -> list[Found]:
        return await self._found(f"SELECT {_COLS} FROM kb_chunks WHERE owner = %s ORDER BY emb <=> %s::vector LIMIT %s", (owner, _vec(emb), k))

    async def by_terms(self, owner: str, terms: list[str], k: int) -> list[Found]:
        if not terms:
            return []
        return await self._found(f"SELECT {_COLS} FROM kb_chunks, to_tsquery('simple', %s) q WHERE owner = %s AND tsv @@ q"
                                 " ORDER BY ts_rank_cd(tsv, q) DESC LIMIT %s", (_tsquery(terms), owner, k))

    async def count(self, owner: str) -> int:
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT count(*) AS n FROM kb_chunks WHERE owner = %s", (owner,))
            return (await cur.fetchone())["n"]
