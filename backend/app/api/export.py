"""右栏的“下载成 PDF / Word”（2026-11-12 Sean 定，见 docs/沙箱方案.md）：模型写的文档原样转一份，不经过模型、不扣额度。
右栏看 PPT 每一页排出来的样子也走这里（同一天定）：PPT 排成 PDF，一张一页，页面拿去给浏览器自己的 PDF 查看器看，也能存下来。

和对话里说一句“导出成 PDF”用的是同一个转换程序（沙箱镜像里的 convert-doc，见 agent-tools 的 services/sandbox/README.md），
交出去的内容也一样（来源 id 换成编号、文末附来源，见 sources.export_markdown）。不同的是不碰这段对话的工作区：
每次转换用一个临时的工作区（export-<文件编号>-<格式>），放进去、转、取回来、删掉；转出来的不登记进文件表。

- 文件的一版写好就不变（PPT 是一个文件一行，也不变），转过的留在内存里（按字节数封顶，最久没用的先丢），再要直接给；
  同一个文件同一种格式同时要两次只转一次。
- 网关给沙箱的每日次数是所有人共用的，所以一个人一天真去转的次数有上限（DAILY，管理员不限；从内存里直接给的不算）。在内存里数，重启清零。
- 命令的输出、文档的标题是用户的内容，不进日志：没转成只记文件编号和退出码。
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from weakref import WeakValueDictionary

from app import quota
from app.identity import ADMIN, Identity
from app.tools.gateway import SANDBOX_MAX_UPLOAD_BYTES, GatewayClient, GatewayError, GatewayLimitError, SandboxError
from app.tools.run_command import BUSY, MAX_TIMEOUT_S

log = logging.getLogger(__name__)

PDF, DOCX = "pdf", "docx"
MIME = {PDF: "application/pdf", DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
DAILY = 50  # 一个人一天最多真去转这么多次
# 转好的最多留这么多（一份带表格的报告 PDF 约 300 KB，Word 十几 KB；8 页带图表的 PPT 排出来约 300 KB，40 页的 1.2 MB）
CACHE_BYTES = 32 * 1024 * 1024
SOURCE = "uploads/document.md"
SLIDES = "uploads/slides.pptx"
TOO_BIG_KINDS = ("file_too_large", "workspace_over_quota")

# 没转成的类别（接口回的 detail.code；页面按它说人话，见 frontend/src/artifact.jsx 的 EXPORT_ERRORS）
NOT_FOUND = "not_found"  # 没有这份文件、不是模型写的文档
LOGIN_REQUIRED = "login_required"  # 游客不能用（跑命令游客也没有）
NOT_READY = "not_ready"  # 写它的那一轮还没答完（来源还没进检查点，见 research_runner.DURABILITY）
DAILY_LIMIT = "daily_limit"  # 这个人今天的次数用完了，或者网关今天的次数到了
BUSY_NOW = "busy"  # 沙箱正忙
UNAVAILABLE = "unavailable"  # 沙箱没开、连不上
TOO_BIG = "too_big"  # 太大：转的时候超时、内存不够，或者放不进去（沙箱一个文件最多收 SANDBOX_MAX_UPLOAD_BYTES）
FAILED = "failed"  # 转换程序报错


class ExportFailed(Exception):
    """没转成。status 是回给页面的 HTTP 状态码，code 是上面的类别。"""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status, self.code = status, code


class Exporter:
    def __init__(self, gateway: GatewayClient, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.gateway, self.clock = gateway, clock
        self._kept: OrderedDict[tuple[str, str], bytes] = OrderedDict()
        self._locks: WeakValueDictionary[tuple[str, str], asyncio.Lock] = WeakValueDictionary()
        self._used: dict[str, tuple[datetime, int]] = {}  # 谁 → (哪一天, 真去转了几次)

    async def convert(self, file_id: str, fmt: str, text: str, title: str, who: Identity) -> bytes:
        """把一版文档（已经是交出去的样子）转成 fmt。text、title 是这一版的，file_id 只用来认是哪一版。"""
        # --title=…：标题以 - 开头又没有空格的（“--draft”）分开写会被 argparse 当成另一个选项
        return await self._once(file_id, fmt, SOURCE, text.encode(), who, f" --title={shlex.quote(title)}")

    async def slides(self, file_id: str, data: bytes, who: Identity) -> bytes:
        """把一份 PPT（原件）排成 PDF，一张一页。file_id 只用来认是哪个文件。"""
        return await self._once(file_id, PDF, SLIDES, data, who)

    async def _once(self, file_id: str, fmt: str, source: str, data: bytes, who: Identity, options: str = "") -> bytes:
        key = (file_id, fmt)
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        async with lock:
            if (kept := self._kept.get(key)) is not None:
                self._kept.move_to_end(key)
                return kept
            if len(data) > SANDBOX_MAX_UPLOAD_BYTES:  # 放不进沙箱：不用去试，也不算次数
                raise ExportFailed(413, TOO_BIG)
            self._admit(who)
            made = await self._run(file_id, fmt, source, data, options)
            self._keep(key, made)
            return made

    def _admit(self, who: Identity) -> None:
        if who.tier == ADMIN:
            return
        today = quota.day_start(self.clock())
        self._used = {sub: used for sub, used in self._used.items() if used[0] == today}
        count = self._used.get(who.sub, (today, 0))[1]
        if count >= DAILY:
            raise ExportFailed(429, DAILY_LIMIT)
        self._used[who.sub] = (today, count + 1)

    def _keep(self, key: tuple[str, str], data: bytes) -> None:
        if len(data) > CACHE_BYTES:
            return
        self._kept[key] = data
        total = sum(map(len, self._kept.values()))
        while total > CACHE_BYTES:
            total -= len(self._kept.popitem(last=False)[1])

    async def _run(self, file_id: str, fmt: str, source: str, data: bytes, options: str) -> bytes:
        workspace = f"export-{file_id.removeprefix('file-')}-{fmt}"
        output = f"outputs/{source.rsplit('/', 1)[-1].rsplit('.', 1)[0]}.{fmt}"
        try:
            await self.gateway.sandbox_put(workspace, source, data)
            run = await self.gateway.sandbox_run(workspace, f"convert-doc {source} {output}{options}", MAX_TIMEOUT_S)
            if run.get("killed"):
                log.warning("导出没转完：%s %s %s", file_id, fmt, run["killed"])
                raise ExportFailed(502, TOO_BIG)
            if run.get("exit_code") != 0:
                log.warning("导出没转成：%s %s 退出码 %s", file_id, fmt, run.get("exit_code"))
                raise ExportFailed(502, FAILED)
            return await self.gateway.sandbox_get(workspace, output)
        except GatewayLimitError as e:
            raise ExportFailed(429, DAILY_LIMIT) from e
        except SandboxError as e:
            if e.kind in BUSY:
                raise ExportFailed(503, BUSY_NOW) from e
            if e.kind in TOO_BIG_KINDS:
                raise ExportFailed(502, TOO_BIG) from e
            log.warning("导出时沙箱没做成：%s %s %s", file_id, fmt, e.kind)
            raise ExportFailed(503, UNAVAILABLE) from e
        except GatewayError as e:  # 密钥不对、网关没放行：配置问题
            log.error("导出时网关不认：%s", e)
            raise ExportFailed(503, UNAVAILABLE) from e
        finally:
            await self.gateway.sandbox_delete(workspace)  # 没删成只记日志：留下的 7 天没用服务自己清
