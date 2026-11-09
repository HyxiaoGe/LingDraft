"""知识库：传文件、建索引、查（见模块说明 app.knowledge）。都是协程，在对话的事件循环里调（见 ResearchRunner）。"""

from __future__ import annotations

import asyncio
import logging
import time
from array import array
from typing import NamedTuple

from langchain_core.messages.ai import UsageMetadata

from app import config, costs
from app.files.service import INDEXING, FileService, UploadRejected, view
from app.files.store import FAILED, KB_PREFIX, PARSING, READY, FileRow
from app.identity import Identity, is_guest_sub
from app.knowledge import text
from app.knowledge.remote import BATCH, Embedder, Reranker
from app.knowledge.store import ChunkStore, Found, StoredChunk

log = logging.getLogger(__name__)

CANDIDATES = 20  # 向量、关键词各取这么多块，合并后也留这么多交给重排
TOP = 8  # 给模型的块数（每块最多约 800 字）
RRF_K = 60
EMBED_CONCURRENCY = 3
QUERY_CHARS = 500  # 查询最多这么多字

# 不收、没建成的原因（页面按它说人话；message 只给接口和日志看）
LOGIN_REQUIRED, KB_FULL, KB_CHARS, INDEX_FAILED, NO_TEXT = "login_required", "kb_full", "kb_chars", "index_failed", "no_text"


class SearchUnavailable(Exception):
    """查不了：向量没算成、关键词也没找到（分不清是没有还是坏了），或者库出问题。"""


class Hit(NamedTuple):
    file_id: str
    name: str
    part: int  # 第几段（read_file 的段号）
    title: str  # 段的标题
    content: str


def workspace(owner: str) -> str:
    return KB_PREFIX + owner


def rrf(lists: list[list[Found]], k: int = RRF_K) -> list[Found]:
    """几路结果按名次合并：每路第 r 名加 1/(k+r)。"""
    score: dict[tuple, float] = {}
    by_key: dict[tuple, Found] = {}
    for lst in lists:
        for r, f in enumerate(lst, 1):
            score[f.key] = score.get(f.key, 0.0) + 1 / (k + r)
            by_key.setdefault(f.key, f)
    return [by_key[key] for key in sorted(score, key=lambda x: score[x], reverse=True)]


def _usage(tokens: int) -> UsageMetadata:
    return UsageMetadata(input_tokens=tokens, output_tokens=0, total_tokens=tokens)


class KnowledgeBase:
    def __init__(self, files: FileService, chunks: ChunkStore, embedder: Embedder, reranker: Reranker | None = None) -> None:
        self.files, self.chunks, self.embedder, self.reranker = files, chunks, embedder, reranker
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._tasks: set[asyncio.Task] = set()
        files.follow_up = self  # 知识库的文件解析好了交到这里建索引（见 FileService._parse）

    async def start(self) -> None:
        """在文件服务起来之后调（块的表连着文件表）。上次没建完的由文件服务重新交过来（见 FileService.start）。"""
        await self.chunks.setup()
        t = asyncio.create_task(self._work())
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def settle(self) -> None:
        """等排着的都解析完、建完（测试用）。"""
        await self.files.settle()
        await self._queue.join()

    # ---- 文件服务交过来的 ----

    def wants(self, row: FileRow) -> bool:
        return (row.workspace_id or "").startswith(KB_PREFIX)

    def enqueue(self, file_id: str) -> None:
        self._queue.put_nowait(file_id)

    # ---- 页面 ----

    async def rows(self, owner: str) -> list[FileRow]:
        return await self.files.store.of_workspace(workspace(owner))

    async def overview(self, who: Identity) -> dict:
        """知识库页：文件（先传的在前）、上限、用了多少。游客没有知识库（available 是 false）。"""
        rows = [] if who.guest else await self.rows(who.sub)
        return {"available": not who.guest,
                "files": [{**view(r), "created_at": r.created_at} for r in rows],
                "limits": {"files": config.KB_MAX_FILES, "chars": config.KB_MAX_CHARS, "mb": config.FILE_MAX_BYTES // (1024 * 1024)},
                "used": {"files": len(rows), "chars": sum(int(r.meta.get("chars") or 0) for r in rows if r.status == READY)}}

    async def upload(self, who: Identity, name: str | None, data: bytes) -> FileRow:
        """传进知识库：存好原件、排进后台解析和建索引，马上返回（状态是解析中）。游客 403、满了 409，别的照对话里的上传（太大、类型不对）。"""
        if who.guest:
            raise UploadRejected(403, LOGIN_REQUIRED, "登录后才能用知识库")
        if len(await self.rows(who.sub)) >= config.KB_MAX_FILES:
            raise UploadRejected(409, KB_FULL, f"知识库最多放 {config.KB_MAX_FILES} 个文件", limit=config.KB_MAX_FILES)
        return await self.files.upload(who.sub, who.tier, name, data, workspace_id=workspace(who.sub))

    async def get(self, who: Identity, file_id: str) -> FileRow | None:
        row = None if who.guest else await self.files.store.get(file_id)
        return row if row is not None and row.workspace_id == workspace(who.sub) else None

    async def delete(self, who: Identity, file_id: str) -> bool:
        """删一个文件：块、文字、原件一起删，删了马上搜不到。正在建索引的也能删（建完发现没了就丢掉）。"""
        row = await self.get(who, file_id)
        if row is None:
            return False
        await self.chunks.delete(file_id)
        await self.files.drop([row])
        return True

    # ---- 对话里用 ----

    async def names(self, owner: str | None) -> list[str]:
        """可以查的文件名（工具说明里列出来，见 app.tools.knowledge）。游客、没归谁的没有。"""
        if not owner or is_guest_sub(owner):
            return []
        return [r.name for r in await self.rows(owner) if r.status == READY]

    async def readable(self, owner: str | None, file_id: str) -> FileRow | None:
        """read_file 能读的知识库文件：这个人的、建好了的。"""
        if not owner or is_guest_sub(owner):
            return None
        row = await self.files.store.get(file_id)
        return row if row is not None and row.workspace_id == workspace(owner) and row.status == READY else None

    async def search(self, owner: str, query: str) -> list[Hit]:
        """向量、关键词各取前 CANDIDATES 块，RRF 合并，重排取前 TOP。向量、重排没调成的少一路照样给（打日志）。
        向量没算成、关键词又什么都没找到的抛 SearchUnavailable。查询时调的向量、重排记账（KB_SEARCH，不算给用户）。"""
        q = query.strip()[:QUERY_CHARS]
        terms = await asyncio.to_thread(text.query_terms, q)
        by_terms = await self.chunks.by_terms(owner, terms, CANDIDATES)
        by_vector: list[Found] = []
        started = costs.now()
        try:
            vecs, tokens = await self.embedder.embed([q])
            costs.charge(self.embedder.ref, costs.KB_SEARCH, started, _usage(tokens))
            by_vector = await self.chunks.by_vector(owner, vecs[0], CANDIDATES)
        except Exception as e:  # 没调成的不记账（没返回就没收钱，见 app.costs）
            if not by_terms:
                raise SearchUnavailable(f"向量没算成：{type(e).__name__}: {str(e)[:200]}") from e
            log.warning("知识库查询的向量没算成，只用关键词：%s: %s", type(e).__name__, str(e)[:200])
        fused = rrf([by_vector, by_terms])[:CANDIDATES]
        if self.reranker is not None and len(fused) > 1:
            started = costs.now()
            try:
                order, tokens = await self.reranker.rerank(q, [f"{text.head(f.name, f.title)}\n{f.content}" for f in fused], TOP)
                costs.charge(self.reranker.ref, costs.KB_SEARCH, started, _usage(tokens))
                fused = [fused[i] for i in order]
            except Exception as e:
                log.warning("知识库查询的重排没调成，按合并的名次给：%s: %s", type(e).__name__, str(e)[:200])
        return [Hit(f.file_id, f.name, f.part, f.title, f.content) for f in fused[:TOP]]

    # ---- 后台建索引 ----

    async def _work(self) -> None:
        while True:
            fid = await self._queue.get()
            try:
                await self._index(fid)
            except Exception:
                log.exception("知识库建索引出错：%s", fid)
            finally:
                self._queue.task_done()

    async def _index(self, fid: str) -> None:
        store = self.files.store
        row = await store.get(fid)
        if row is None or row.status != PARSING or row.meta.get("stage") != INDEXING:
            return
        meta = {k: v for k, v in row.meta.items() if k != "stage"}
        chars = int(meta.get("chars") or 0)
        used = sum(int(r.meta.get("chars") or 0) for r in await store.of_workspace(row.workspace_id or "") if r.id != fid and r.status == READY)
        if used + chars > config.KB_MAX_CHARS:
            await store.mark(fid, FAILED, {**meta, "code": KB_CHARS}, f"知识库的文字到上限了（{config.KB_MAX_CHARS // 10_000} 万字），删掉一些再传")
            return
        cs = await asyncio.to_thread(text.chunks, await store.parts(fid))
        if not cs:
            await store.mark(fid, FAILED, {**meta, "code": NO_TEXT}, "文件里没有能读出来的文字")
            return
        t0, started = time.monotonic(), costs.now()
        docs = [f"{text.head(row.name, c.title)}\n{c.content}" for c in cs]
        terms = await asyncio.to_thread(lambda: [text.doc_terms(d) for d in docs])
        spent = [0]
        try:
            vecs = await self._embed_all(docs, spent)
            stored = [StoredChunk(fid, row.owner, c.part, c.idx, row.name, c.title, c.content, v, t) for c, v, t in zip(cs, vecs, terms, strict=True)]
            await self.chunks.replace(fid, stored, self.embedder.model)
        except Exception as e:
            if await store.get(fid) is None:  # 建的时候被删了（Postgres 上插块时外键就不让插）
                return await self._dropped(fid, row.owner, started, spent[0])
            self._charge(row.owner, costs.KB_INDEX_FAILED, started, spent[0])  # 没建成的不算给用户
            await store.mark(fid, FAILED, {**meta, "code": INDEX_FAILED}, "文件没处理好，请重新上传")
            err = e.exceptions[0] if isinstance(e, ExceptionGroup) else e
            log.warning("知识库建索引没建成 %s：%s: %s", fid, type(err).__name__, str(err)[:200])
            return
        if not await store.mark(fid, READY, {**meta, "chunks": len(cs)}, ""):
            return await self._dropped(fid, row.owner, started, spent[0])
        self._charge(row.owner, costs.KB_INDEX, started, spent[0])
        log.info("知识库建好索引 %s：%d 块 %d 字，%d token，%.1f 秒", fid, len(cs), chars, spent[0], time.monotonic() - t0)

    async def _dropped(self, fid: str, owner: str, started, tokens: int) -> None:
        """建的时候文件被删了：块丢掉。算好了的向量照算（钱已经花了，和建好以后再删一样）。"""
        await self.chunks.delete(fid)
        self._charge(owner, costs.KB_INDEX, started, tokens)
        log.info("知识库建索引时文件被删了：%s", fid)

    def _charge(self, owner: str, purpose: str, started, tokens: int) -> None:
        with costs.within(costs.Scope(), owner=owner):  # 不在哪一轮里：记在这个人名下
            costs.charge(self.embedder.ref, purpose, started, _usage(tokens))

    async def _embed_all(self, docs: list[str], spent: list[int]) -> list[array]:
        """每次 BATCH 条，同时最多 EMBED_CONCURRENCY 个，一个没成其余的取消；spent[0] 累计用了多少 token（没全算成的也知道花了多少）。"""
        sem = asyncio.Semaphore(EMBED_CONCURRENCY)

        async def one(batch: list[str]) -> list[array]:
            async with sem:
                vecs, tokens = await self.embedder.embed(batch)
                spent[0] += tokens
                return [array("f", v) for v in vecs]  # 4 字节一个数，list 要 32 字节
        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(one(docs[i:i + BATCH])) for i in range(0, len(docs), BATCH)]
        return [v for t in tasks for v in t.result()]
