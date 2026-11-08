"""上传的文件：传上来、后台解析、发消息时绑到对话、删对话一起删（2026-11-08 Sean 定，见 docs/图片和文件理解方案.md 的“二、文件”）。
模型写的文件（Artifact）也在这里存：一版一行、不用解析（见 write、app.tools.write_file）。
图片（用户发的、画的）在 image-service 里，这里只登记（add_image）：这段对话有哪些图以表为准，取图时认是不是这段对话的（image_in）。

- 上传是单独的接口，不经过对话的运行器：存好原件、记一行就回编号，解析排进后台（一次一个，在单独的子进程里，有超时），页面问状态。
  模型正在回答时也能传，别的对话不受影响；要等的只是这句话本身（页面上有文件还在解析，发送按钮先不能点）。
- 认类型看内容（parse.sniff），传上来时就认：不收的直接回 415，不用等解析。
- 刚传上来的不归哪段对话（新对话还没有编号），发出去时绑上（bind）；没发出去的过一天清掉（sweep）。服务重启时还在解析的重新排上。
- 每人每天能传几个有上限（config.FILE_UPLOADS_DAILY），管理员不限。

都是协程，在对话的事件循环里调（见 ResearchRunner）。
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing as mp
import re
import time
import uuid
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app import config
from app.files import artifacts, parse
from app.files.parse import OUTLINE_MAX, Part, Unreadable
from app.files.store import DOCUMENT, FAILED, GENERATED, IMAGE, PARSING, READY, UPLOAD, FileRow, FileStore
from app.graph.research.history import MAX_FILES
from app.graph.research.state import AttachedFile
from app.quota import day_start

log = logging.getLogger(__name__)

UNBOUND_TTL = timedelta(days=1)  # 传上来、一直没发出去的，过这么久清掉
SWEEP_S = 3600
CACHE_CHARS = 8_000_000  # 给模型的文字在内存里最多缓存这么多字（上传的原件不会变，一段对话每次调模型都要）
PREVIEW_CHARS = 200_000  # 右栏预览用户发的文件最多给这么多字，再多的页面让下载原件看（大约 100 页的书）
TOO_COMPLEX = "文件太复杂，读不了"
NOT_READ = "文件没读成，请重新上传"
NAME_MAX = 120


# 传不上来、发不出去的原因（页面按它说人话，见 frontend/src/upload.js 的 fileErrorText；message 只给接口和日志看）
TOO_BIG, EMPTY_FILE, DAILY_LIMIT = "too_large", "empty_file", "daily_limit"
TOO_MANY, EXPIRED, NOT_READY, UNREADABLE, NOT_HERE = "too_many", "expired", "parsing", "unreadable", "not_here"


class UploadRejected(Exception):
    """传不上来：status 是给接口的 HTTP 状态码，code 是原因（上面几个和 parse 的 Unreadable 的 code），extra 是页面写提示要的数。"""

    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra


class NotSendable(Exception):
    """发消息时带的文件不能发：code 是 TOO_MANY / EXPIRED（不是这个人的、过期清掉了、已经发在别的对话里）/ NOT_READY / UNREADABLE / NOT_HERE。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def isolated_parse(path: Path, name: str, timeout: float) -> tuple:
    """在单独的子进程里解析（spawn：不从带着线程和连接池的服务进程 fork）。返回 ("ok", Parsed) / ("unreadable", code, message) /
    ("failed", 原因) / ("timeout",) / ("killed",)（子进程没交结果就没了，比如内存不够被杀）。超时、出事都把子进程杀掉。"""
    from app.files import child

    ctx = mp.get_context("spawn")
    recv, send = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=child.run, args=(str(path), name, send), daemon=True)
    proc.start()
    send.close()
    try:
        if not recv.poll(timeout):
            return ("timeout",)
        try:
            return recv.recv()
        except EOFError:
            return ("killed",)
    finally:
        if proc.is_alive():
            proc.kill()
        proc.join(5)
        recv.close()


def direct_parse(path: Path, name: str, timeout: float) -> tuple:
    """在当前进程里解析（测试用：起子进程每次要一秒多）。"""
    try:
        return ("ok", parse.parse_file(path, name))
    except Unreadable as e:
        return ("unreadable", e.code, e.message)
    except Exception as e:  # noqa: BLE001
        return ("failed", f"{type(e).__name__}: {str(e)[:300]}")


def clean_name(name: str | None) -> str:
    name = re.sub(r"[\x00-\x1f\x7f]", "", (name or "").replace("\\", "/").rsplit("/", 1)[-1]).strip()
    if len(name) > NAME_MAX:
        stem, dot, ext = name.rpartition(".")
        name = (stem[:NAME_MAX - len(ext) - 1] + dot + ext) if dot and len(ext) <= 10 else name[:NAME_MAX]
    return name or "未命名文件"


def attached(row: FileRow) -> AttachedFile:
    m = row.meta
    pages = m.get("pages") or m.get("slides") or (len(m["sheets"]) if m.get("sheets") else None)
    return AttachedFile(id=row.id, name=row.name, type=row.type, size=row.size, pages=pages, chars=int(m.get("chars") or 0))


def view(row: FileRow) -> dict:
    """给页面的一个文件：状态、显示用的几项。没读成的 reason 是原因（parse 的 Unreadable 的 code，或 timeout / killed / failed / missing），
    页面按它说人话；error 是同一个原因的中文，只给接口和日志看。"""
    return {"id": row.id, "name": row.name, "type": row.type, "size": row.size, "status": row.status, "error": row.error,
            **({"reason": row.meta.get("code") or "failed"} if row.status == FAILED else {}),
            **({"file": attached(row).model_dump()} if row.status == READY else {})}


class FileService:
    def __init__(self, store: FileStore, root: Path, isolated: bool = True, timeout: float = 60.0) -> None:
        self.store, self.root, self.isolated, self.timeout = store, root, isolated, timeout
        self._queue: asyncio.Queue[str] | None = None
        self._tasks: set[asyncio.Task] = set()
        self._cache: OrderedDict[str, tuple[FileRow, list[Part]]] = OrderedDict()
        self._cached = 0

    async def start(self) -> None:
        """在对话的事件循环里起后台的解析和清理；上次没解析完的（服务重启了）重新排上。"""
        await self.store.setup()
        self._queue = asyncio.Queue()
        for row in await self.store.with_status(PARSING):
            self._queue.put_nowait(row.id)
        for work in (self._work(), self._sweep_loop()):
            t = asyncio.create_task(work)
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)

    async def settle(self) -> None:
        """等排着的都解析完（测试用）。"""
        if self._queue is not None:
            await self._queue.join()

    # ---- 上传 ----

    async def upload(self, owner: str, tier: str, name: str | None, data: bytes) -> FileRow:
        name = clean_name(name)
        if len(data) > config.FILE_MAX_BYTES:
            raise UploadRejected(413, TOO_BIG, f"文件不能超过 {config.FILE_MAX_BYTES // (1024 * 1024)} MB", mb=config.FILE_MAX_BYTES // (1024 * 1024))
        if not data:
            raise UploadRejected(415, EMPTY_FILE, "文件是空的")
        limit = config.FILE_UPLOADS_DAILY.get(tier)
        if limit is not None and await self.store.uploads_since(owner, day_start(datetime.now(UTC))) >= limit:
            guest = tier == "guest"
            more = f"，登录后每天能传 {config.FILE_UPLOADS_DAILY['user']} 个" if guest else "，明天再传"
            raise UploadRejected(429, DAILY_LIMIT, f"今天传的文件到上限了（{limit} 个）{more}", limit=limit, guest=guest,
                                 login_limit=config.FILE_UPLOADS_DAILY["user"] if guest else None)
        try:
            kind = await asyncio.to_thread(parse.sniff, data, name)
        except Unreadable as e:
            raise UploadRejected(415, e.code, e.message) from None
        fid = f"file-{uuid.uuid4().hex[:16]}"
        rel = f"{fid[5:7]}/{fid}"
        await asyncio.to_thread(self._write, rel, data)
        await self.store.add(FileRow(id=fid, owner=owner, name=name, type=kind, size=len(data), storage=rel))
        assert self._queue is not None, "FileService.start 还没调"
        self._queue.put_nowait(fid)
        row = await self.store.get(fid)
        assert row is not None
        return row

    def _write(self, rel: str, data: bytes) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(path)

    def _unlink(self, rel: str) -> None:
        try:
            (self.root / rel).unlink(missing_ok=True)
        except OSError as e:
            log.warning("删文件原件没删掉：%s：%s", rel, e)

    # ---- 后台解析 ----

    async def _work(self) -> None:
        assert self._queue is not None
        while True:
            fid = await self._queue.get()
            try:
                await self._parse(fid)
            except Exception:
                log.exception("解析文件出错：%s", fid)
            finally:
                self._queue.task_done()

    async def _parse(self, fid: str) -> None:
        row = await self.store.get(fid)
        if row is None or row.status != PARSING:
            return
        path = self.root / row.storage
        if not path.exists():
            await self.store.finish(fid, FAILED, {"code": "missing"}, NOT_READ, [])
            return
        t0 = time.monotonic()
        out = await asyncio.to_thread(isolated_parse if self.isolated else direct_parse, path, row.name, self.timeout)
        took = time.monotonic() - t0
        if out[0] == "ok":
            parsed = out[1]
            await self.store.finish(fid, READY, parsed.meta, "", parsed.parts)
            log.info("解析好了 %s（%s，%d 字节）：%d 段 %d 字，%.1f 秒", fid, row.type, row.size, len(parsed.parts), parsed.chars, took)
        elif out[0] == "unreadable":
            await self.store.finish(fid, FAILED, {"code": out[1]}, out[2], [])
            log.info("读不了 %s（%s）：%s，%.1f 秒", fid, row.type, out[1], took)
        else:
            await self.store.finish(fid, FAILED, {"code": out[0]}, TOO_COMPLEX if out[0] in ("timeout", "killed") else NOT_READ, [])
            log.warning("解析失败 %s（%s，%d 字节）：%s，%.1f 秒", fid, row.type, row.size, out, took)

    # ---- 页面 ----

    async def get(self, fid: str, owner: str) -> FileRow | None:
        row = await self.store.get(fid)
        return row if row is not None and row.owner == owner else None

    async def remove(self, fid: str, owner: str) -> bool:
        """去掉还没发出去的文件（输入框里点叉）。发出去的跟着对话走，这里不删。"""
        row = await self.store.get(fid)
        if row is None or row.owner != owner or row.workspace_id is not None:
            return False
        await self.store.delete([fid])
        await asyncio.to_thread(self._unlink, row.storage)
        return True

    async def original(self, fid: str, workspace_id: str) -> tuple[Path, FileRow] | None:
        """这段对话里的文件的原件（下载）。"""
        row = await self.store.get(fid)
        if row is None or row.workspace_id != workspace_id or row.kind != DOCUMENT:
            return None
        path = self.root / row.storage
        return (path, row) if path.exists() else None

    async def preview(self, fid: str, workspace_id: str) -> tuple[FileRow, list[Part], bool] | None:
        """这段对话里用户发的文件抽出来的文字（页面右栏预览）：那一行、前面的段落（加起来不超过 PREVIEW_CHARS 字，至少一段）、
        后面还有没有。不是这段对话的、还没解析好的是 None。"""
        got = await self.content(fid)
        if got is None or got[0].workspace_id != workspace_id or got[0].kind != DOCUMENT:
            return None
        row, parts = got
        shown, size = [], 0
        for p in parts:
            if shown and size + len(p.text) > PREVIEW_CHARS:
                break
            shown.append(p)
            size += len(p.text)
        return row, shown, len(shown) < len(parts)

    # ---- 图片：只登记，图在 image-service 里 ----

    async def add_image(self, image_id: str, *, owner: str, workspace_id: str, generated: bool, turn_id: str | None,
                        mime_type: str, width: int, height: int) -> bool:
        """一张图登记进文件表：用户发的（发出去时）、画好的（画好时，turn_id 是哪一轮）。行的编号就是图的编号，页面、模型认的都是它。
        已经登记过的不再记（补登记可以重跑），返回这次记了没有。"""
        if await self.store.get(image_id) is not None:
            return False
        ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}.get(mime_type, "png")
        await self.store.add(FileRow(id=image_id, owner=owner, workspace_id=workspace_id, origin=GENERATED if generated else UPLOAD, kind=IMAGE,
                                     name=f"{'生成的图' if generated else '发的图'}.{ext}", type=ext, size=0, storage=image_id, status=READY,
                                     meta={"mime_type": mime_type, "width": width, "height": height}, turn_id=turn_id if generated else None))
        return True

    async def image_in(self, image_id: str, workspace_id: str) -> bool:
        """这张图是不是这段对话的（取图时认）。"""
        row = await self.store.get(image_id)
        return row is not None and row.kind == IMAGE and row.workspace_id == workspace_id

    # ---- 对话 ----

    async def bind(self, ids: list[str], owner: str, workspace_id: str) -> list[AttachedFile]:
        """发消息时带的文件绑到这段对话，返回记在消息上的信息。有一个不能发就都不绑，抛 NotSendable。"""
        if not ids:
            return []
        if len(ids) > MAX_FILES or len(set(ids)) != len(ids):
            raise NotSendable(TOO_MANY, f"一次最多带 {MAX_FILES} 个文件")
        rows = [await self.store.get(i) for i in ids]
        for r in rows:
            if r is None or r.owner != owner or r.workspace_id not in (None, workspace_id):
                raise NotSendable(EXPIRED, "有文件已经过期了，请重新上传")
            if r.status == PARSING:
                raise NotSendable(NOT_READY, "文件还在解析，解析好了再发")
            if r.status == FAILED:
                raise NotSendable(UNREADABLE, "有文件读不了，去掉再发")
        new = [r.id for r in rows if r and r.workspace_id is None]
        if new and await self.store.bind(new, owner, workspace_id) != len(new):
            raise NotSendable(EXPIRED, "有文件已经过期了，请重新上传")  # 同时被清掉、被别处发出去了
        return [attached(r) for r in rows if r is not None]

    async def content(self, fid: str) -> tuple[FileRow, list[Part]] | None:
        """给模型的：文件的那一行和全部段落。上传的原件、模型写的每一版都不会变，按编号缓存，超过 CACHE_CHARS 先丢最久没用的。"""
        if (got := self._cache.get(fid)) is not None:
            self._cache.move_to_end(fid)
            return got
        row = await self.store.get(fid)
        if row is None or row.status != READY:
            return None
        parts = await self.store.parts(fid)
        self._cache[fid] = (row, parts)
        self._cached += sum(len(p.text) for p in parts)
        while self._cached > CACHE_CHARS and len(self._cache) > 1:
            _, (_, old) = self._cache.popitem(last=False)
            self._cached -= sum(len(p.text) for p in old)
        return row, parts

    # ---- 模型写的文件（见 app.tools.write_file）----

    async def write(self, *, owner: str, workspace_id: str, turn_id: str, title: str, kind: str, content: str, note: str = "",
                    prev_id: str | None = None, series_id: str | None = None, version: int = 1) -> FileRow:
        """存一版：原件写进数据卷，全文切段（段落拼起来就是原文，见 files.artifacts）一起记进表，直接是 READY。
        第一版不给 prev_id、series_id；之后的给上一版的编号、第一版的编号和这一版是第几版（版本号按对话里的消息数，见 history.written_files）。"""
        fid = f"file-{uuid.uuid4().hex[:16]}"
        rel = f"{fid[5:7]}/{fid}"
        data = content.encode()
        name = artifacts.file_name(title, kind)
        meta: dict[str, Any] = {"title": title, "chars": len(content), "note": note}
        if kind == artifacts.CODE:
            meta["lang"] = artifacts.lang_of(name)
        if kind == artifacts.MARKDOWN:
            meta["outline"] = artifacts.outline(content)[:OUTLINE_MAX]
        row = FileRow(id=fid, owner=owner, workspace_id=workspace_id, origin=GENERATED, name=name, type=kind, size=len(data), storage=rel,
                      status=READY, meta=meta, version=version, prev_id=prev_id, series_id=series_id or fid, turn_id=turn_id)
        await asyncio.to_thread(self._write, rel, data)
        try:
            await self.store.add(row, artifacts.split(content, kind))
        except BaseException:
            await asyncio.to_thread(self._unlink, rel)
            raise
        got = await self.store.get(fid)
        assert got is not None
        return got

    async def text(self, fid: str) -> str | None:
        """模型写的一版的全文（段落拼起来）；没有了是 None。"""
        got = await self.content(fid)
        return "".join(p.text for p in got[1]) if got is not None else None

    async def discard(self, fid: str) -> None:
        """同一轮里模型又改了这一轮刚写的那版（见 app.tools.write_file）：那一版作废，行、文字、原件一起删。"""
        if (row := await self.store.get(fid)) is not None and row.origin == GENERATED and row.kind == DOCUMENT:
            await self._drop([row])

    async def delete_turn(self, workspace_id: str, turn_id: str) -> int:
        """重新回答、改了那句话：那一轮写的各版、画的图（登记的行；图本身由调用的地方从 image-service 删）都删掉
        （连同进程崩掉时写了、对话里没记下的），返回删了几行。用户那句话带的图、文件留着。"""
        rows = [r for r in await self.store.of_workspace(workspace_id) if r.origin == GENERATED and r.turn_id == turn_id]
        await self._drop(rows)
        return len(rows)

    async def _drop(self, rows: list[FileRow]) -> None:
        await self.store.delete([r.id for r in rows])
        for r in rows:
            if (got := self._cache.pop(r.id, None)) is not None:
                self._cached -= sum(len(p.text) for p in got[1])
            if r.kind == DOCUMENT:  # 图不在数据卷上
                await asyncio.to_thread(self._unlink, r.storage)

    async def delete_workspace(self, workspace_id: str) -> None:
        """删对话：这段对话的文件、文字、原件、图的登记一起删。原件没删掉只记日志（没人指着，只占磁盘）。"""
        await self._drop(await self.store.of_workspace(workspace_id))

    # ---- 清理 ----

    async def _sweep_loop(self) -> None:
        while True:
            try:
                await self.sweep()
            except Exception:
                log.exception("清理没发出去的文件出错")
            await asyncio.sleep(SWEEP_S)

    async def sweep(self, now: datetime | None = None) -> int:
        """传上来过了 UNBOUND_TTL 还没发出去的删掉，返回删了几个。"""
        rows = await self.store.unbound_before((now or datetime.now(UTC)) - UNBOUND_TTL)
        await self.store.delete([r.id for r in rows])
        for r in rows:
            await asyncio.to_thread(self._unlink, r.storage)
        if rows:
            log.info("清掉 %d 个传上来一天没发出去的文件", len(rows))
        return len(rows)
