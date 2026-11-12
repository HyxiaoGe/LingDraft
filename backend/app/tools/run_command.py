"""在沙箱里跑命令（2026-11-11 Sean 定，方案见 docs/沙箱方案.md）：run_command。

沙箱是 agent-tools 的 sandbox-service（经网关，见 app.tools.gateway）：一段对话一个工作区（工作区编号就是对话编号），每条命令在
gVisor 隔离的一次性容器里跑，不联网、装不了包，留得下来的只有工作目录里的文件。第一个场景是分析用户发的表格、出图表。

- 只在工作任务里给，游客没有（app.graph.research.agent.can_run）。工具一直在列表里，不该给的调模型时去掉；模型硬调了没给的，这里也不跑。
- 文件表仍然是唯一的真相（Sean 定）：每次跑之前把这段对话里用户发的文件的原件放进 uploads/（命令只能读；已经在的不重放，
  对话里没有了的删掉），跑完把 outputs/ 里新增、改过的文件登记进文件表：图（PNG / JPEG / WebP，不超过 4 MB）传进 image-service、
  和画的图放在一起，别的原件存数据卷（见 FileService.add_output）。页面上图跟在回复里，别的是文件卡片。工作目录里别的文件只是草稿，不登记。
- 模型写的文件（create_file / edit_file）也放进去（2026-11-12 Sean 定做出文件）：每份最新的一版在 uploads/documents/，命令只能读，
  用来转成 PDF / Word（沙箱里的 convert-doc，见 agent-tools 的 services/sandbox/README.md“出文档”）。文档里的来源 id 放进去时换成编号、
  文末列出来源（sources.export_markdown，和页面上下载的一样）：沙箱里不知道这段对话有哪些来源，原样转出来就是一串 [s1a2b3c]。
- 一轮里同一个路径只留最后一版：命令又写了这一轮登记过的路径（比如图画得不对重画），前面那个作废删掉。和模型写文件一轮一版是一个道理。
- 工作目录没了（服务 7 天没用就清掉、写满了删掉重来）不要紧：uploads/ 每次跑之前都对一遍；以前产出的文件也照文件表放回 outputs/
  原来的路径（2026-11-12 Sean 定，见 restore_outputs），隔了很久再说“在上次那份 Excel 里加一列”时沙箱里还有它。命令自己写的脚本、草稿不登记，没了就没了。
- 同一段对话的命令一条一条跑（服务那边一个工作区同时只跑一条，一条消息里并行调的在这里排队）。
- 用户点了停止：这次执行被取消，顺手让服务把正在跑的命令停掉，不等它跑到时限。
- 给模型看的输出再截一次（标准输出、报错各 OUTPUT_CHARS 字，开头结尾各一半）：服务各留 64 KB，全给模型太占上下文。页面上看的也是这一份。
- 不收钱（models.toml 的 [tools] 没定价）：不调付费的上游。网关给灵稿每天 500 次。

结果的 artifact：跑了的是 {"run": CommandRun}（命令自己失败、超时被杀也是跑了）；没跑成的是 {"failure": 原因}，status=error，
页面按 history.RUN_FAILURES 说人话。给模型的是英文（prompts 的 tools.run_command）。
"""

from __future__ import annotations

import asyncio
import logging
import re
import weakref
from collections import OrderedDict
from pathlib import Path
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.messages import BaseMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app import config
from app.files.service import OTHER, PATH
from app.files.store import IMAGE, READY, FileRow
from app.graph.research import prompts
from app.graph.research.history import RUN_TOOL, last_human, owned_file_ids, written_files
from app.graph.research.sources import collect_sources, export_markdown
from app.graph.research.state import CommandRun, ProducedFile, WrittenFile
from app.tools.gateway import (
    MAX_UPLOAD_BYTES,
    SANDBOX_MAX_UPLOAD_BYTES,
    GatewayAuthError,
    GatewayClient,
    GatewayError,
    GatewayLimitError,
    SandboxError,
)

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S, MAX_TIMEOUT_S = 60, 120  # 和服务的默认、上限一样（服务超过 120 按 120）
OUTPUT_CHARS = 10_000  # 给模型看的标准输出、报错各留这么多字
MAX_OUTPUTS = 10  # 一条命令最多登记这么多个产出，再多的告诉模型没给用户
UPLOADS, OUTPUTS = "uploads/", "outputs/"
DOCUMENTS = UPLOADS + "documents/"  # 模型写的文件最新的一版放这里（在 uploads/ 下面：服务把 uploads/ 挂成只读）
PLACED_WORKSPACES = 500  # 记着“沙箱里放的是哪一版”的对话最多这么多段，再多的忘掉最久没用的（忘了只是下次重放一遍）
RESTORE_BYTES = 256 * 1024 * 1024  # 工作目录没了的时候，以前的产出一共最多放回这么多（新的先放）：工作区上限 1 GiB，别一放回去又写满
WORKSPACE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")  # 服务认的工作区编号；对话编号是 uuid，都对得上
NAME_BYTES = 200  # 沙箱里一级路径最长 255 字节，重名时还要加“ (2)”
# 起不来（Docker、gVisor 不在）之外，这些都当“这会儿正忙”：工作区上一条命令还没停完、别的对话的命令占着
BUSY = ("workspace_busy", "sandbox_busy")
# 工作区坏了、写满了：删掉整个工作区重来（uploads/、以前的产出下次跑之前会重新放进去，命令自己写的草稿没了）
RESET = ("workspace_over_quota", "workspace_corrupted")
UNPLACEABLE = ("invalid_path", "file_too_large")  # 服务不收这一个文件（名字不合它的规矩、太大）：换谁重试都一样

_cancels: set[asyncio.Task] = set()  # 点了停止以后去停命令的那一下：不等它，留个引用别被回收


class Sandbox:
    """沙箱（经网关）。挂在 ChatContext.sandbox 上；没有的（评测、命令行、配置里关了）不给跑命令的工具。"""

    def __init__(self, gateway: GatewayClient) -> None:
        self.gateway = gateway
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._placed: OrderedDict[str, dict[str, str]] = OrderedDict()
        self.unrestored: set[str] = set()  # 以前的产出放回到一半断了的对话（见 restore_outputs）：下一条命令之前接着放

    def lock(self, workspace: str) -> asyncio.Lock:
        """这段对话的锁：命令一条一条跑。没人用了自己回收。"""
        lock = self._locks.get(workspace)
        if lock is None:
            lock = self._locks[workspace] = asyncio.Lock()
        return lock

    def placed(self, workspace: str) -> dict[str, str]:
        """这段对话的 uploads/documents/ 里各个路径放的是文件表里哪一版（路径 → 那一版的编号）。只在内存里：模型改了文件是新的一版、
        新的编号，对不上就重放。光看大小认不出改没改（“2024”改成“2025”大小一样）；进程重启就都忘了，下一条命令之前全部重放一遍。"""
        got = self._placed.get(workspace)
        if got is None:
            got = self._placed[workspace] = {}
            while len(self._placed) > PLACED_WORKSPACES:
                self._placed.popitem(last=False)
        self._placed.move_to_end(workspace)
        return got

    async def forget(self, workspace: str) -> None:
        """删对话时：工作区一起删。没成只记日志，服务 7 天没用自己清。"""
        self._placed.pop(workspace, None)
        self.unrestored.discard(workspace)
        if WORKSPACE.fullmatch(workspace):
            await self.gateway.sandbox_delete(workspace)


class RunCommandArgs(BaseModel):
    command: str = Field(description=prompts.render("tools.run_command.command"))
    timeout_seconds: int | None = Field(default=None, description=prompts.render("tools.run_command.timeout_seconds", max_timeout=MAX_TIMEOUT_S))


def clip(text: str, limit: int = OUTPUT_CHARS) -> str:
    """太长的输出只留开头和结尾各一半：开头看得出在跑什么，报错一般在结尾。"""
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n[... {len(text) - 2 * half} characters omitted ...]\n{text[-half:]}"


def _fit(name: str) -> str:
    """文件名放进沙箱的一级路径：太长的按字节截短（留着扩展名），“.”“..”这种换个名字。别的字符文件表里的名字本来就没有。"""
    name = name.strip() or "file"
    if name in (".", ".."):
        name = "file"
    if len(name.encode()) <= NAME_BYTES:
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot or len(ext.encode()) > 20:
        stem, dot, ext = name, "", ""
    room = NAME_BYTES - len((dot + ext).encode())
    return stem.encode()[:room].decode("utf-8", "ignore") + dot + ext


def _free_path(taken: Any, folder: str, name: str) -> str:
    """folder 下面还没人用的路径：名字被占了的加“ (2)”“ (3)”（加在扩展名前面）。"""
    name = _fit(name)
    stem, dot, ext = name.rpartition(".")
    path, n = folder + name, 2
    while path in taken:
        path = folder + (f"{stem} ({n}){dot}{ext}" if dot else f"{name} ({n})")
        n += 1
    return path


def upload_paths(rows: list[FileRow]) -> dict[str, FileRow]:
    """用户发的文件在沙箱里的路径（uploads/ 加文件名）→ 那个文件，按发的先后。重名的后来的加“ (2)”“ (3)”。
    正好叫 documents 的也算重名：uploads/documents 是放模型写的文件的目录。"""
    out: dict[str, FileRow] = {}
    reserved = DOCUMENTS.rstrip("/")
    for row in rows:
        out[_free_path({*out, reserved}, UPLOADS, row.name)] = row
    return out


def document_paths(written: list[WrittenFile]) -> dict[str, WrittenFile]:
    """模型写的文件在沙箱里的路径（uploads/documents/ 加文件名）→ 最新的那一版，按第一次写的先后。重名的后来的加“ (2)”。"""
    out: dict[str, WrittenFile] = {}
    for w in written:
        out[_free_path(out, DOCUMENTS, w.name)] = w
    return out


def expected_path(w: WrittenFile) -> str:
    """模型照标题想得到的路径（文档、网页是标题加扩展名，代码是它起的文件名）。实际的路径和它不一样的才在结果里告诉模型。"""
    return DOCUMENTS + (w.title + {"markdown": ".md", "html": ".html"}.get(w.type, ""))


async def sync_documents(sandbox: Sandbox, files: Any, workspace: str, messages: list[BaseMessage],
                         have: dict[str, int]) -> list[tuple[str, str]]:
    """把模型写的文件最新的一版放进沙箱的 uploads/documents/：放过这一版的不重放（见 Sandbox.placed），不在了的、不是最新的删掉。
    文档（Markdown）放的是交出去的样子：来源 id 换成编号、文末列出来源。返回路径和标题对不上的 [(标题, 沙箱里的路径)]。
    服务不收的那一份（名字不合它的规矩、太大）跳过、记日志，别的照放：一份文件放不进去不该让这段对话从此跑不了命令。"""
    gateway, placed = sandbox.gateway, sandbox.placed(workspace)
    wanted = document_paths(list(written_files(messages).values()))
    known: dict[str, Any] | None = None
    kept: dict[str, WrittenFile] = {}
    for path, w in wanted.items():
        if placed.get(path) == w.id and path in have:
            kept[path] = w
            continue
        text = await files.text(w.id)
        if text is None:  # 这一版已经不在了（文件被清掉了）
            continue
        if w.type == "markdown":
            if known is None:
                known = {s.id: s for s in collect_sources(messages)}
            text = export_markdown(text, known)
        data = text.encode()
        if len(data) > SANDBOX_MAX_UPLOAD_BYTES:  # 模型写的最多 30 万字，到不了这里；真到了就不放
            continue
        placed.pop(path, None)
        try:
            await gateway.sandbox_put(workspace, path, data)
        except SandboxError as e:
            if e.kind not in UNPLACEABLE:  # 工作区写满了（也是 413）、服务这会儿不行的照旧往外报
                raise
            log.warning("模型写的文件没放进沙箱：%s %s %s", workspace, w.id, e.kind)
            continue
        placed[path] = w.id
        kept[path] = w
    for path in have:
        if path.startswith(DOCUMENTS) and path not in kept:
            await gateway.sandbox_remove(workspace, path)
    for path in [p for p in placed if p not in kept]:
        del placed[path]
    return [(w.title, path) for path, w in kept.items() if path != expected_path(w)]


async def sync_uploads(gateway: GatewayClient, files: Any, workspace: str, messages: list[BaseMessage],
                       have: dict[str, int]) -> tuple[dict[str, str], list[str]]:
    """把这段对话里用户发的文件放进沙箱的 uploads/：已经在的（名字、大小都对得上）不重放，对话里没有了的删掉。
    have 是工作区里现有的文件（路径 → 大小）。返回（在沙箱里换了名字的：原名 → 沙箱里的名字，太大放不进去的原名）。"""
    sources: dict[str, Path] = {}
    rows: list[FileRow] = []
    too_big: list[str] = []
    for fid in dict.fromkeys(owned_file_ids(messages)):
        got = await files.original(fid, workspace)
        if got is None:  # 文件已经不在了
            continue
        path, row = got
        if row.size > SANDBOX_MAX_UPLOAD_BYTES:
            too_big.append(row.name)
            continue
        sources[row.id] = path
        rows.append(row)
    wanted = upload_paths(rows)
    for path in have:
        if path.startswith(UPLOADS) and not path.startswith(DOCUMENTS) and path not in wanted:
            await gateway.sandbox_remove(workspace, path)
    for path, row in wanted.items():
        if have.get(path) != row.size:
            await gateway.sandbox_put(workspace, path, await asyncio.to_thread(sources[row.id].read_bytes))
    renamed = {row.name: path[len(UPLOADS):] for path, row in wanted.items() if path != UPLOADS + row.name}
    return renamed, too_big


async def _stored(gateway: GatewayClient, files: Any, row: FileRow, workspace: str) -> bytes | None:
    """文件表里一个产出的内容：图从 image-service 取，别的读数据卷上的原件。不在了、图这会儿取不到是 None。"""
    if row.kind == IMAGE:
        try:
            got = await gateway.image(row.id)
        except GatewayAuthError:
            raise
        except GatewayError as e:
            log.warning("以前产出的图没取到，没放回沙箱：%s %s %s", workspace, row.id, e)
            return None
        return got[0] if got is not None else None
    got = await files.original(row.id, workspace)
    return await asyncio.to_thread(got[0].read_bytes) if got is not None else None


async def restore_outputs(sandbox: Sandbox, files: Any, workspace: str, have: dict[str, int], gone: bool) -> None:
    """沙箱的工作目录没了的时候，把以前跑命令产出的文件照文件表放回 outputs/ 原来的路径（每个路径放最后登记的那个，见 FileService.outputs）。
    have 是这次跑之前工作区里现有的文件，gone 是那时服务说没有这个工作区。

    - 只在工作区整个没了的时候放：服务清工作目录是整个删掉（7 天没用、写满了删掉重来，agent-tools 的 services/sandbox/app/workspaces.py
      的 _remove），之后列文件回的是 workspace_not_found。工作区还在的时候不看缺了谁，哪怕里面是空的：模型自己删掉、改了名的产出不该
      又冒出来（它 ls、打包 outputs/ 时会多出来）；已经在的也不覆盖（里面可能是没登记上的更新的内容）。
    - 放回去的在这条命令跑之前就在了，服务拿跑之前、跑之后的目录对比着认“这次改了什么”，所以不会又登记一遍；模型之后改了它才算新的一版。
    - 放到一半断了（服务这会儿不行、用户点了停止）：记在 Sandbox.unrestored 里，下一条命令之前把还缺的接着放，不然工作区又有了就再也不放。
      所以要在往工作区放任何东西之前调（放用户发的文件也会让工作区又有了），而且先记下再动手。只记在内存里，正好这时进程重启的话缺的就缺着。
    - 放不回去的跳过、记日志，别的照放：超过网关请求体上限的（SANDBOX_MAX_UPLOAD_BYTES）、原件不在了的、图取不到的、服务不收的。
      一共最多放 RESTORE_BYTES，新的先放。"""
    if gone:
        sandbox.unrestored.add(workspace)
    elif workspace not in sandbox.unrestored:
        return
    gateway, room = sandbox.gateway, RESTORE_BYTES
    for row in reversed(await files.outputs(workspace)):
        path = row.meta[PATH]
        if path in have or not path.startswith(OUTPUTS):
            continue
        if row.size > min(SANDBOX_MAX_UPLOAD_BYTES, room):
            log.warning("以前的产出太大，没放回沙箱：%s %s %d 字节", workspace, row.id, row.size)
            continue
        data = await _stored(gateway, files, row, workspace)
        if data is None or len(data) > min(SANDBOX_MAX_UPLOAD_BYTES, room):
            continue
        try:
            await gateway.sandbox_put(workspace, path, data)
        except SandboxError as e:
            if e.kind not in UNPLACEABLE:
                raise
            log.warning("以前的产出没放回沙箱：%s %s %s", workspace, row.id, e.kind)
            continue
        room -= len(data)
    sandbox.unrestored.discard(workspace)


def _image_ext(data: bytes) -> str | None:
    """看开头认是不是 image-service 收的图（PNG / JPEG / WebP），是的话回扩展名。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


async def _as_image(gateway: GatewayClient, files: Any, data: bytes, *, path: str, name: str, owner: str, workspace: str,
                    turn: str) -> ProducedFile | None:
    """是图就传进 image-service、登记成图；不是图、太大、服务不收、服务这会儿不通的回 None（当普通文件存，用户还能下载）。"""
    ext = _image_ext(data)
    if ext is None or len(data) > MAX_UPLOAD_BYTES:
        return None
    try:
        img = await gateway.upload_image(data)
    except GatewayAuthError:
        raise
    except GatewayError as e:
        log.warning("命令产出的图没传进 image-service，当普通文件存：%s %s", path, e)
        return None
    await files.add_image(img["id"], owner=owner, workspace_id=workspace, generated=True, turn_id=turn, mime_type=img["mime_type"],
                          width=img["width"], height=img["height"], name=name, size=len(data), path=path)
    return ProducedFile(id=img["id"], path=path, name=name, kind="image", type=ext, size=len(data), mime_type=img["mime_type"],
                        width=img["width"], height=img["height"])


async def register_outputs(gateway: GatewayClient, files: Any, changed: list, *, owner: str, workspace: str,
                           turn: str) -> tuple[list[ProducedFile], list[dict[str, str]]]:
    """这次运行新增、改过的文件里 outputs/ 下的登记进文件表。返回（登记了的，没登记的 [{path, reason}]：
    too_many / empty / too_large / unavailable）。"""
    produced: list[ProducedFile] = []
    skipped: list[dict[str, str]] = []
    for item in changed:
        path = str(item.get("path") or "") if isinstance(item, dict) else ""
        if not path.startswith(OUTPUTS):
            continue
        size = item.get("size") if isinstance(item.get("size"), int) else 0
        name = path.rsplit("/", 1)[-1]
        reason = ("too_many" if len(produced) >= MAX_OUTPUTS else "empty" if size <= 0 else "too_large" if size > config.FILE_MAX_BYTES else "")
        if reason:
            skipped.append({"path": path, "reason": reason})
            continue
        try:
            data = await gateway.sandbox_get(workspace, path)
            made = await _as_image(gateway, files, data, path=path, name=name, owner=owner, workspace=workspace, turn=turn)
            if made is None:
                row = await files.add_output(owner=owner, workspace_id=workspace, turn_id=turn, path=path, name=name, data=data)
                made = ProducedFile(id=row.id, path=path, name=row.name, kind="file", type=row.type, size=row.size,
                                    preview=row.status == READY and row.type != OTHER)
        except GatewayAuthError:
            raise
        except Exception:  # 取不回来、存不了：这一个没给用户，别的照常
            log.exception("登记命令产出的文件出错：%s %s", workspace, path)
            skipped.append({"path": path, "reason": "unavailable"})
            continue
        produced.append(made)
    return produced, skipped


async def _replace_earlier(gateway: GatewayClient, files: Any, produced: list[ProducedFile], workspace: str, turn: str) -> None:
    """这一轮前面登记过、这次又写了的路径：前面那个作废。没删掉只记日志（重新回答、删对话时按轮、按对话一起删）。"""
    if not produced:
        return
    try:
        gone = await files.discard_outputs(workspace, turn, {f.path for f in produced}, {f.id for f in produced})
        if gone:
            await gateway.delete_images(gone)
    except Exception:
        log.exception("删作废的产出出错：%s", workspace)


def _fail(runtime: ToolRuntime, code: str, **kw) -> ToolMessage:
    return ToolMessage(content=prompts.render(f"tools.run_command.{code}", **kw), name=RUN_TOOL, tool_call_id=runtime.tool_call_id or "",
                       status="error", artifact={"failure": code})


def _stop_later(gateway: GatewayClient, workspace: str) -> None:
    task = asyncio.ensure_future(gateway.sandbox_cancel(workspace))
    _cancels.add(task)
    task.add_done_callback(_cancels.discard)


def make_run_command_tool():
    from app.graph.research.agent import can_run  # agent 用到本模块（build_agent），放在这里导入

    @tool(RUN_TOOL, description=prompts.render("tools.run_command.description", default_timeout=DEFAULT_TIMEOUT_S, max_timeout=MAX_TIMEOUT_S,
                                               output_chars=OUTPUT_CHARS),
          args_schema=RunCommandArgs)
    async def run_command(runtime: ToolRuntime, command: str, timeout_seconds: int | None = None) -> ToolMessage:
        ctx = runtime.context
        sandbox, files, owner = getattr(ctx, "sandbox", None), getattr(ctx, "files", None), getattr(ctx, "owner", None) or ""
        workspace = getattr(ctx, "thread_id", None) or ((runtime.config or {}).get("configurable") or {}).get("thread_id") or ""
        if not can_run(runtime.state, runtime) or files is None or not WORKSPACE.fullmatch(workspace):
            return _fail(runtime, "unavailable")
        if not command.strip():
            return _fail(runtime, "empty")
        timeout = min(max(timeout_seconds or DEFAULT_TIMEOUT_S, 1), MAX_TIMEOUT_S)
        messages = runtime.state.get("messages", [])
        i = last_human(messages)
        turn = (messages[i].id if i >= 0 else None) or ""
        gateway: GatewayClient = sandbox.gateway
        async with sandbox.lock(workspace):
            try:
                found = await gateway.sandbox_files(workspace)
                have = found or {}
                await restore_outputs(sandbox, files, workspace, have, gone=found is None)
                renamed, too_big = await sync_uploads(gateway, files, workspace, messages, have)
                documents = await sync_documents(sandbox, files, workspace, messages, have)
                try:
                    result = await gateway.sandbox_run(workspace, command, timeout)
                except asyncio.CancelledError:
                    _stop_later(gateway, workspace)
                    raise
                produced, skipped = await register_outputs(gateway, files, result.get("changed") or [], owner=owner, workspace=workspace,
                                                           turn=turn)
            except GatewayLimitError:
                return _fail(runtime, "limit")
            except SandboxError as e:
                if e.kind in RESET:
                    log.warning("沙箱工作区%s，删掉重来：%s", "写满了" if e.kind == "workspace_over_quota" else "坏了", workspace)
                    await gateway.sandbox_delete(workspace)
                    return _fail(runtime, "reset")
                log.warning("命令没跑成：%s %s", workspace, e)
                return _fail(runtime, "busy" if e.kind in BUSY else "unavailable")
            await _replace_earlier(gateway, files, produced, workspace, turn)
        killed = result.get("killed") if isinstance(result.get("killed"), str) else ""
        code = result.get("exit_code") if isinstance(result.get("exit_code"), int) else None
        ms = result.get("duration_ms") if isinstance(result.get("duration_ms"), int) else 0
        run = CommandRun(exit_code=code, killed=killed or "", duration_ms=ms, stdout=clip(str(result.get("stdout") or "")),
                         stderr=clip(str(result.get("stderr") or "")), files=produced)
        workspace_full = bool((result.get("workspace") or {}).get("over_quota")) if isinstance(result.get("workspace"), dict) else False
        text = prompts.render("tools.run_command.result", run=run, seconds=f"{ms / 1000:.1f}", timeout=timeout, max_timeout=MAX_TIMEOUT_S,
                              skipped=skipped, max_outputs=MAX_OUTPUTS, max_mb=config.FILE_MAX_BYTES // (1024 * 1024), renamed=renamed,
                              too_big=too_big, upload_mb=SANDBOX_MAX_UPLOAD_BYTES // (1024 * 1024), workspace_full=workspace_full,
                              documents=documents)
        return ToolMessage(content=text, name=RUN_TOOL, tool_call_id=runtime.tool_call_id or "", artifact={"run": run.model_dump()})

    run_command.handle_tool_error = True
    return run_command
