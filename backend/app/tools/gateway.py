"""agent-tools 网关客户端：传、取、删 image-service 存着的图，以及网关出错时的几种异常（工具都走 MCP，见 app.tools.mcp）。

图是 image-service 的 /v2/mcp 出的、或者用户发的，存在服务里、归灵稿（只有灵稿的密钥取得到、删得掉，见 agent-tools 的
services/image/README.md）：
- POST /image/v1/images → 传一张用户发的图（PNG / JPEG / WebP，不超过 4 MB），回它的编号，之后能当 source_image_id 改；
- GET /image/v1/images/{id} → 图的内容；页面来取时由灵稿后端转给浏览器（见 api.main），不暴露服务地址；
- DELETE /image/v1/images/{id} → 删对话、重新回答时删掉不要的图。服务不自动清理。
这几个网关都不算调用、不占每日上限。

还有沙箱（sandbox-service，见 agent-tools 的 services/sandbox/README.md）：一段对话一个工作区，放文件、取文件、跑命令。
网关只把跑命令算作调用（每日上限），存取文件不算；没起容器的失败也不算。怎么用见 app.tools.run_command。
"""

from __future__ import annotations

import logging
import re

import httpx

logger = logging.getLogger(__name__)

TIMEOUT = 30.0
# 跑一条命令最多等这么久：命令自己最多 120 秒，前面可能排队 20 秒，再加起容器、清点文件；比网关转发的上限（200 秒）短
SANDBOX_RUN_TIMEOUT = 190.0
SANDBOX_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 网关的请求体上限：比这大的文件放不进沙箱
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_UPLOAD_BYTES = 4 * 1024 * 1024  # image-service 收的上限：改图时原图以 base64 内联传给上游，和参考图同一个上限
IMAGE_MIME_TYPES = {"image/png", "image/jpeg", "image/webp"}
IMAGE_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")  # image-service 的图片编号（uuid4）


class GatewayError(RuntimeError):
    """kind 是失败类型（timeout、http_502…），retryable 表示再试一次可能就好。"""

    def __init__(self, message: str, *, kind: str = "error", retryable: bool = False) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


class GatewayAuthError(GatewayError):
    """密钥不对或没有权限：配置问题，再试也不会好。"""


class GatewayLimitError(GatewayError):
    """网关这个服务今天的调用上限到了（HTTP 429，北京时间 0 点重置）：今天再调也一样。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, kind="daily_limit_exceeded")


class ImageRejected(GatewayError):
    """传的图服务不收：kind 是 image_too_large（超过 4 MB）或 unsupported_image（不是读得出宽高的 PNG / JPEG / WebP）。"""


class SandboxError(GatewayError):
    """沙箱没做成：kind 是服务给的错误码（workspace_busy、sandbox_busy、sandbox_unavailable、workspace_over_quota、not_found…，
    见 agent-tools 的 services/sandbox/README.md），没连上、超时、回的不是它的格式是 unavailable；status 是 HTTP 状态码（没连上是 0）。"""

    def __init__(self, message: str, *, kind: str, status: int = 0) -> None:
        super().__init__(message, kind=kind, retryable=status in (0, 503))
        self.status = status


class GatewayClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        scene: str = "research",
        timeout: float = TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise GatewayError("未设置 AGENT_TOOLS_API_KEY")
        headers = {"Authorization": f"Bearer {api_key}", "X-Caller-Scene": scene}
        # 异步的：在对话的事件循环里用（见 app.aio）
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout, transport=transport)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def upload_image(self, data: bytes) -> dict:
        """传一张用户发的图，返回服务给的这张图的信息（id、mime_type、width、height…）。
        服务不收（太大、认不出）抛 ImageRejected，密钥不对抛 GatewayAuthError，别的问题抛 GatewayError。"""
        try:
            resp = await self._client.post("/image/v1/images", content=data, headers={"Content-Type": "application/octet-stream"})
        except httpx.HTTPError as e:
            raise GatewayError(f"传图失败：{type(e).__name__}", kind="request_error", retryable=True) from e
        if resp.status_code == 201:
            return resp.json()
        if resp.status_code in (413, 415):
            raise ImageRejected(f"传图失败：HTTP {resp.status_code}",
                                kind="image_too_large" if resp.status_code == 413 else "unsupported_image")
        if resp.status_code in (401, 403):
            raise GatewayAuthError(f"传图失败：HTTP {resp.status_code}", kind=f"http_{resp.status_code}")
        raise GatewayError(f"传图失败：HTTP {resp.status_code}", kind=f"http_{resp.status_code}", retryable=resp.status_code >= 500)

    async def image(self, image_id: str) -> tuple[bytes, str] | None:
        """取一张图：(内容, MIME 类型)；服务里没有（删了、不是灵稿的）是 None。密钥不对抛 GatewayAuthError，别的问题抛 GatewayError。"""
        try:
            async with self._client.stream("GET", f"/image/v1/images/{image_id}") as resp:
                if resp.status_code == 404:
                    return None
                if resp.status_code in (401, 403):
                    raise GatewayAuthError(f"取图失败：HTTP {resp.status_code}", kind=f"http_{resp.status_code}")
                mime = (resp.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
                if resp.status_code != 200 or mime not in IMAGE_MIME_TYPES:
                    raise GatewayError(f"取图失败：HTTP {resp.status_code} {mime}", kind=f"http_{resp.status_code}", retryable=True)
                chunks, size = [], 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_IMAGE_BYTES:
                        raise GatewayError("图片太大", kind="image_too_large")
                    chunks.append(chunk)
        except httpx.HTTPError as e:
            raise GatewayError(f"取图失败：{type(e).__name__}", kind="request_error", retryable=True) from e
        return b"".join(chunks), mime

    async def delete_images(self, image_ids: list[str]) -> None:
        """删掉这些图。服务那边删过再删、本来就没有都算删了；删不掉只记日志，不影响删对话、重新回答（留下的图没人指着，只占磁盘）。"""
        for image_id in image_ids:
            try:
                resp = await self._client.delete(f"/image/v1/images/{image_id}")
                if resp.status_code != 204:
                    logger.warning("删图没成功：%s HTTP %s", image_id, resp.status_code)
            except httpx.HTTPError as e:
                logger.warning("删图没成功：%s %s", image_id, type(e).__name__)

    # ---- 沙箱 ----

    async def _sandbox(self, method: str, workspace: str, path: str = "", *, what: str, timeout: float | None = None, **kw) -> httpx.Response:
        """调一次沙箱，回 2xx 的响应。密钥不对、网关没放行抛 GatewayAuthError，每日上限抛 GatewayLimitError，别的没做成抛 SandboxError。"""
        try:
            resp = await self._client.request(method, f"/sandbox/v1/workspaces/{workspace}{path}", timeout=timeout or TIMEOUT, **kw)
        except httpx.HTTPError as e:
            raise SandboxError(f"{what}失败：{type(e).__name__}", kind="unavailable") from e
        if resp.is_success:
            return resp
        if resp.status_code in (401, 403):
            raise GatewayAuthError(f"{what}失败：HTTP {resp.status_code}", kind=f"http_{resp.status_code}")
        if resp.status_code == 429:
            raise GatewayLimitError(f"{what}失败：网关今天的上限到了")
        try:
            code = resp.json()["error"]["code"]
        except (ValueError, KeyError, TypeError):
            code = None
        raise SandboxError(f"{what}失败：HTTP {resp.status_code} {code or ''}".rstrip(), status=resp.status_code,
                           kind=code if isinstance(code, str) and code else "unavailable")

    async def sandbox_files(self, workspace: str) -> dict[str, int]:
        """工作区里的文件：路径 → 字节数。工作区还没有（没放过文件、太久没用清掉了）是空的。"""
        try:
            resp = await self._sandbox("GET", workspace, "/files", what="列沙箱文件")
        except SandboxError as e:
            if e.kind == "workspace_not_found":
                return {}
            raise
        try:
            return {str(f["path"]): int(f["size"]) for f in resp.json()["files"]}
        except (ValueError, KeyError, TypeError) as e:
            raise SandboxError("列沙箱文件失败：回的格式不对", kind="unavailable", status=resp.status_code) from e

    async def sandbox_put(self, workspace: str, path: str, data: bytes) -> None:
        await self._sandbox("PUT", workspace, "/file", what="往沙箱放文件", params={"path": path}, content=data,
                            headers={"Content-Type": "application/octet-stream"})

    async def sandbox_get(self, workspace: str, path: str) -> bytes:
        return (await self._sandbox("GET", workspace, "/file", what="从沙箱取文件", params={"path": path})).content

    async def sandbox_remove(self, workspace: str, path: str) -> None:
        """删工作区里的一个文件；本来就没有也算删了。"""
        try:
            await self._sandbox("DELETE", workspace, "/file", what="删沙箱文件", params={"path": path})
        except SandboxError as e:
            if e.status != 404:
                raise

    async def sandbox_run(self, workspace: str, command: str, timeout_seconds: int) -> dict:
        """跑一条命令，等它结束：服务回的整份结果（exit_code、killed、stdout、stderr、changed…）。命令自己失败也是正常返回。"""
        resp = await self._sandbox("POST", workspace, "/run", what="跑命令", timeout=SANDBOX_RUN_TIMEOUT,
                                   json={"command": command, "timeout_seconds": timeout_seconds})
        try:
            data = resp.json()
        except ValueError as e:
            raise SandboxError("跑命令失败：回的格式不对", kind="unavailable", status=resp.status_code) from e
        if not isinstance(data, dict):
            raise SandboxError("跑命令失败：回的格式不对", kind="unavailable", status=resp.status_code)
        return data

    async def sandbox_cancel(self, workspace: str) -> None:
        """停掉这个工作区正在跑的命令（用户点了停止）。没成只记日志：命令到时限自己会被杀掉。"""
        try:
            await self._sandbox("POST", workspace, "/cancel", what="停命令", timeout=10.0)
        except GatewayError as e:
            logger.warning("停沙箱里的命令没成功：%s %s", workspace, e)

    async def sandbox_delete(self, workspace: str) -> bool:
        """删掉整个工作区（删对话、工作区写满了重来）。没成只记日志，返回删了没有：留下的 7 天没用服务自己清。"""
        try:
            await self._sandbox("DELETE", workspace, what="删沙箱工作区")
        except GatewayError as e:
            if isinstance(e, SandboxError) and e.status == 404:
                return True
            logger.warning("删沙箱工作区没成功：%s %s", workspace, e)
            return False
        return True
