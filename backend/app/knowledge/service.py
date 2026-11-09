"""资料库（以前叫知识库）：建、改、删资料库，传文件、建索引、查（见模块说明 app.knowledge）。都是协程，在对话的事件循环里调（见 ResearchRunner）。"""

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
from app.knowledge.libraries import Library, LibraryStore, MemoryLibraryStore, new_id
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
NO_LIBRARY, LIBRARIES_FULL, NAME_TAKEN, BAD_NAME, BAD_DESCRIPTION = "no_library", "libraries_full", "name_taken", "bad_name", "bad_description"


class SearchUnavailable(Exception):
    """查不了：向量没算成、关键词也没找到（分不清是没有还是坏了），或者库出问题。"""


class NoSuchLibrary(Exception):
    """模型指定的资料库没有：names 是这个人有的那些（告诉模型该怎么写）。"""

    def __init__(self, names: list[str]) -> None:
        super().__init__("没有这个资料库")
        self.names = names


class LibraryRejected(Exception):
    """建、改资料库不收：status 给接口，code 是原因（上面几个），页面按 code 说人话。"""

    def __init__(self, status: int, code: str, message: str, **extra) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra


class Hit(NamedTuple):
    file_id: str
    name: str
    part: int  # 第几段（read_file 的段号）
    title: str  # 段的标题
    content: str
    library: str = ""  # 在哪个资料库（名字）


def workspace(library_id: str) -> str:
    """一个资料库是文件表里的一个工作区。"""
    return KB_PREFIX + library_id


def library_view(lib: Library) -> dict:
    return {"id": lib.id, "name": lib.name, "description": lib.description, "created_at": lib.created_at}


def _clean(name: str | None, description: str | None) -> tuple[str | None, str | None]:
    """名字去掉多余的空白，空的、太长的不收；说明去掉首尾空白，太长的不收（页面先拦着，这里兜底）。"""
    if name is not None:
        name = " ".join(name.split())
        if not name or len(name) > config.KB_NAME_CHARS:
            raise LibraryRejected(422, BAD_NAME, f"名字要 1 到 {config.KB_NAME_CHARS} 个字", limit=config.KB_NAME_CHARS)
    if description is not None:
        description = description.strip()
        if len(description) > config.KB_DESCRIPTION_CHARS:
            raise LibraryRejected(422, BAD_DESCRIPTION, f"说明最多 {config.KB_DESCRIPTION_CHARS} 个字", limit=config.KB_DESCRIPTION_CHARS)
    return name, description


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
    def __init__(self, files: FileService, chunks: ChunkStore, embedder: Embedder, reranker: Reranker | None = None,
                 libraries: LibraryStore | None = None) -> None:
        self.files, self.chunks, self.embedder, self.reranker = files, chunks, embedder, reranker
        self.libraries = libraries if libraries is not None else MemoryLibraryStore()
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._tasks: set[asyncio.Task] = set()
        files.follow_up = self  # 知识库的文件解析好了交到这里建索引（见 FileService._parse）

    async def start(self) -> None:
        """在文件服务起来之后调（块的表连着文件表，资料库建表时要搬文件表里 11-10 以前的行）。上次没建完的由文件服务重新交过来（见 FileService.start）。"""
        await self.chunks.setup()
        await self.libraries.setup()
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
        """这个人还在的资料库里的文件（先传的在前）。删资料库时正好在往里传的那种落单的不算（见 upload）。"""
        have = {workspace(lib.id) for lib in await self.libraries.of_owner(owner)}
        return [r for r in await self.files.store.of_owner_in(owner, KB_PREFIX) if r.workspace_id in have]

    async def own(self, who: Identity, library_id: str) -> Library | None:
        lib = None if who.guest else await self.libraries.get(library_id)
        return lib if lib is not None and lib.owner == who.sub else None

    async def overview(self, who: Identity) -> dict:
        """资料库页：几个资料库（先建的在前），每个带着文件（先传的在前）；上限（limits：文件个数、字数、单个多少 MB、几个资料库）、
        用了多少（used，按人算）。游客没有资料库（available 是 false）。"""
        libs = [] if who.guest else await self.libraries.of_owner(who.sub)
        rows = [] if who.guest else await self.rows(who.sub)
        by_ws: dict[str, list[FileRow]] = {}
        for r in rows:
            by_ws.setdefault(r.workspace_id or "", []).append(r)
        return {"available": not who.guest,
                "libraries": [{**library_view(lib), "files": [{**view(r), "created_at": r.created_at} for r in by_ws.get(workspace(lib.id), [])]}
                              for lib in libs],
                "limits": {"files": config.KB_MAX_FILES, "chars": config.KB_MAX_CHARS, "mb": config.FILE_MAX_BYTES // (1024 * 1024),
                           "libraries": config.KB_MAX_LIBRARIES, "name": config.KB_NAME_CHARS, "description": config.KB_DESCRIPTION_CHARS},
                "used": {"files": len(rows), "chars": sum(int(r.meta.get("chars") or 0) for r in rows if r.status == READY)}}

    async def create(self, who: Identity, name: str, description: str = "") -> Library:
        """建一个资料库。游客 403、名字不对 422、满了 409（libraries_full）、同名的 409（name_taken，不分大小写）。"""
        if who.guest:
            raise LibraryRejected(403, LOGIN_REQUIRED, "登录后才能用资料库")
        clean_name, clean_desc = _clean(name, description)
        if len(await self.libraries.of_owner(who.sub)) >= config.KB_MAX_LIBRARIES:
            raise LibraryRejected(409, LIBRARIES_FULL, f"最多建 {config.KB_MAX_LIBRARIES} 个资料库", limit=config.KB_MAX_LIBRARIES)
        lib = Library(id=new_id(), owner=who.sub, name=clean_name or "", description=clean_desc or "")
        if not await self.libraries.add(lib):
            raise LibraryRejected(409, NAME_TAKEN, "已经有同名的资料库了")
        return await self.libraries.get(lib.id) or lib

    async def update(self, who: Identity, library_id: str, name: str | None = None, description: str | None = None) -> Library | None:
        """改名字、说明（没给的不改）。别人的、没有的 None；名字不对 422、同名 409。"""
        lib = await self.own(who, library_id)
        if lib is None:
            return None
        clean_name, clean_desc = _clean(name, description)
        if not await self.libraries.update(lib.id, clean_name if clean_name is not None else lib.name,
                                           clean_desc if clean_desc is not None else lib.description):
            raise LibraryRejected(409, NAME_TAKEN, "已经有同名的资料库了")
        return await self.libraries.get(lib.id)

    async def remove(self, who: Identity, library_id: str) -> bool:
        """删一个资料库，里面的文件一起删（块、文字、原件），删了马上搜不到。先删资料库：之后再往里传的都不收。"""
        lib = await self.own(who, library_id)
        if lib is None:
            return False
        await self.libraries.delete(lib.id)
        rows = await self.files.store.of_workspace(workspace(lib.id))
        for r in rows:
            await self.chunks.delete(r.id)
        await self.files.drop(rows)
        return True

    async def upload(self, who: Identity, library_id: str, name: str | None, data: bytes) -> FileRow:
        """传进一个资料库：存好原件、排进后台解析和建索引，马上返回（状态是解析中）。
        游客 403、资料库没有（别人的、删了）404、满了 409，别的照对话里的上传（太大、类型不对）。"""
        if who.guest:
            raise UploadRejected(403, LOGIN_REQUIRED, "登录后才能用资料库")
        lib = await self.own(who, library_id)
        if lib is None:
            raise UploadRejected(404, NO_LIBRARY, "没有这个资料库")
        if len(await self.rows(who.sub)) >= config.KB_MAX_FILES:
            raise UploadRejected(409, KB_FULL, f"资料库里最多放 {config.KB_MAX_FILES} 个文件", limit=config.KB_MAX_FILES)
        row = await self.files.upload(who.sub, who.tier, name, data, workspace_id=workspace(lib.id))
        if await self.libraries.get(lib.id) is None:  # 传的时候资料库被删了：别留下没人看得见的文件
            await self.files.drop([row])
            raise UploadRejected(404, NO_LIBRARY, "没有这个资料库")
        return row

    async def get(self, who: Identity, file_id: str) -> FileRow | None:
        """这个人资料库里的一个文件（文件行上记着是谁的）。"""
        row = None if who.guest else await self.files.store.get(file_id)
        return row if row is not None and row.owner == who.sub and (row.workspace_id or "").startswith(KB_PREFIX) else None

    async def delete(self, who: Identity, file_id: str) -> bool:
        """删一个文件：块、文字、原件一起删，删了马上搜不到。正在建索引的也能删（建完发现没了就丢掉）。"""
        row = await self.get(who, file_id)
        if row is None:
            return False
        await self.chunks.delete(file_id)
        await self.files.drop([row])
        return True

    # ---- 对话里用 ----

    async def catalog(self, owner: str | None) -> list[dict]:
        """可以查的：每个有建好文件的资料库的名字、说明、文件名（工具说明里列出来，见 app.tools.knowledge）。游客、没归谁的没有。"""
        if not owner or is_guest_sub(owner):
            return []
        names: dict[str, list[str]] = {}
        for r in await self.rows(owner):
            if r.status == READY:
                names.setdefault(r.workspace_id or "", []).append(r.name)
        return [{"name": lib.name, "description": lib.description, "files": names[workspace(lib.id)]}
                for lib in await self.libraries.of_owner(owner) if names.get(workspace(lib.id))]

    async def readable(self, owner: str | None, file_id: str) -> FileRow | None:
        """read_file 能读的资料库文件：这个人的、建好了的。"""
        if not owner or is_guest_sub(owner):
            return None
        row = await self.files.store.get(file_id)
        return row if row is not None and row.owner == owner and (row.workspace_id or "").startswith(KB_PREFIX) and row.status == READY else None

    async def search(self, owner: str, query: str, library: str | None = None) -> list[Hit]:
        """查这个人的资料库：library 是名字（不分大小写）时只查那一个，没有这个名字的抛 NoSuchLibrary；不给查全部。
        只查还在的资料库里建好的文件。向量、关键词各取前 CANDIDATES 块，RRF 合并，重排取前 TOP。向量、重排没调成的少一路照样给（打日志）。
        向量没算成、关键词又什么都没找到的抛 SearchUnavailable。查询时调的向量、重排记账（KB_SEARCH，不算给用户）。"""
        libs = await self.libraries.of_owner(owner)
        if library and library.strip():
            want = " ".join(library.split()).lower()
            picked = [lib for lib in libs if lib.name.lower() == want]
            if not picked:
                raise NoSuchLibrary([lib.name for lib in libs])
            libs = picked
        lib_names = {workspace(lib.id): lib.name for lib in libs}
        lib_of = {r.id: lib_names[r.workspace_id] for r in await self.rows(owner) if r.status == READY and r.workspace_id in lib_names}
        if not lib_of:
            return []
        ids = list(lib_of)
        q = query.strip()[:QUERY_CHARS]
        terms = await asyncio.to_thread(text.query_terms, q)
        by_terms = await self.chunks.by_terms(owner, terms, CANDIDATES, ids)
        by_vector: list[Found] = []
        started = costs.now()
        try:
            vecs, tokens = await self.embedder.embed([q])
            costs.charge(self.embedder.ref, costs.KB_SEARCH, started, _usage(tokens))
            by_vector = await self.chunks.by_vector(owner, vecs[0], CANDIDATES, ids)
        except Exception as e:  # 没调成的不记账（没返回就没收钱，见 app.costs）
            if not by_terms:
                raise SearchUnavailable(f"向量没算成：{type(e).__name__}: {str(e)[:200]}") from e
            log.warning("资料库查询的向量没算成，只用关键词：%s: %s", type(e).__name__, str(e)[:200])
        fused = rrf([by_vector, by_terms])[:CANDIDATES]
        if self.reranker is not None and len(fused) > 1:
            started = costs.now()
            try:
                order, tokens = await self.reranker.rerank(q, [f"{text.head(f.name, f.title)}\n{f.content}" for f in fused], TOP)
                costs.charge(self.reranker.ref, costs.KB_SEARCH, started, _usage(tokens))
                fused = [fused[i] for i in order]
            except Exception as e:
                log.warning("资料库查询的重排没调成，按合并的名次给：%s: %s", type(e).__name__, str(e)[:200])
        return [Hit(f.file_id, f.name, f.part, f.title, f.content, lib_of.get(f.file_id, "")) for f in fused[:TOP]]

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
        used = sum(int(r.meta.get("chars") or 0) for r in await self.rows(row.owner) if r.id != fid and r.status == READY)  # 按人算
        if used + chars > config.KB_MAX_CHARS:
            await store.mark(fid, FAILED, {**meta, "code": KB_CHARS}, f"资料库的文字到上限了（{config.KB_MAX_CHARS // 10_000} 万字），删掉一些再传")
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
