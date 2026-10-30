"""来源网站的图标：后端去网站上取一次、按域名存到数据目录，页面从这里拿；取不到页面显示域名首字母。

取法：先读首页找 <link rel="icon">（不少站只在这里声明、根目录没有 /favicon.ico），找不到再试 /favicon.ico。
取不到的也记下来，一周内不再试。只取公网网站：域名解析到内网或本机地址的不取，跳转过去的也不跟、非默认端口不连，
免得这个接口被当成探内网的跳板（解析和连接之间换 IP 的 DNS rebinding 不防，PoC 先这样）。
机器上开着假 IP 模式的代理（Clash、Surge 的 TUN）时，所有域名都解析到 198.18.0.0/15，由代理去连真实地址；
这段放行，但这时看不到真实地址，内网检查要靠代理自己的规则。
"""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import re
import socket
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

HOST = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}$")
LINK = re.compile(r"<link\b[^>]*>", re.I)
ATTR = re.compile(r"""([a-z-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)
MAX_HTML = 512 * 1024  # 首页只看前面这些，<link> 都在 <head> 里
MAX_ICON = 256 * 1024
RETRY_AFTER = 7 * 24 * 3600
DEADLINE = 10  # 一个网站从读首页到拿到图标最多花这么久，连不上的站不会一直挂着
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"


def sniff(data: bytes) -> str | None:
    """看文件头认图片格式，不是图片返回 None（很多站对不存在的 /favicon.ico 返回一张 HTML 页，状态码还是 200）。"""
    if data[:4] == b"\x00\x00\x01\x00":
        return "image/x-icon"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"GIF":
        return "image/gif"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    head = data[:2048].lstrip().lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head):
        return "image/svg+xml"
    return None


def icon_links(html: str, base: str) -> list[str]:
    """页面声明的图标：rel 含 icon 的（icon、shortcut icon）排前面，apple-touch-icon 排后面；mask-icon 是单色蒙版，不要。"""
    first: list[str] = []
    later: list[str] = []
    for tag in LINK.findall(html):
        attrs = {m.group(1).lower(): next(g for g in m.groups()[1:] if g is not None) for m in ATTR.finditer(tag)}
        rel = attrs.get("rel", "").lower().split()
        href = (attrs.get("href") or "").strip()
        if not href or "mask-icon" in rel or not any("icon" in r for r in rel):
            continue
        try:
            url = href if href.startswith("data:") else urljoin(base, href)
        except ValueError:  # 写坏了的地址（比如 http://[x）跳过
            continue
        (first if "icon" in rel else later).append(url)
    return first + later


FAKE_IP = ipaddress.ip_network("198.18.0.0/15")  # 测试网段，代理软件拿来当假 IP


def _public_ip(ip: str) -> bool:
    a = ipaddress.ip_address(ip.split("%")[0])
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return (a.is_global and not a.is_multicast) or a in FAKE_IP


async def _resolve(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [str(i[4][0]) for i in infos]


class Favicons:
    def __init__(self, root: Path, transport: httpx.AsyncBaseTransport | None = None,
                 resolve: Callable[[str], Awaitable[list[str]]] = _resolve, now: Callable[[], float] = time.time):
        self.root = root
        self._transport = transport
        self._resolve = resolve
        self._now = now
        self._tasks: dict[str, asyncio.Task] = {}

    async def get(self, host: str, wait: float | None = None) -> tuple[bytes, str] | None:
        """返回（图片内容, 类型），取不到返回 None；域名格式不对抛 ValueError。同一个域名同时来几个请求只取一次。

        wait 秒内没取完抛 TimeoutError，但后台接着取、取完存进缓存，下次来就有了：页面上的图标请求不会占着连接等慢网站。
        """
        host = host.lower().rstrip(".")
        if not HOST.match(host):
            raise ValueError(host)
        known, cached = self._cached(host)
        if known:
            return cached
        task = self._tasks.get(host)
        if task is None:
            task = self._tasks[host] = asyncio.create_task(self._fetch_and_store(host))
            task.add_done_callback(lambda _: self._tasks.pop(host, None))
        return await asyncio.wait_for(asyncio.shield(task), wait)

    async def _fetch_and_store(self, host: str) -> tuple[bytes, str] | None:
        try:
            found = await asyncio.wait_for(self._fetch(host), DEADLINE)
        except Exception:  # noqa: BLE001  超时或网站上什么怪东西：都当取不到，一周后再试
            found = None
        self._store(host, found)
        return found

    # ---- 缓存：<域名> 是图片，<域名>.none 表示取过没取到 ----

    def _cached(self, host: str) -> tuple[bool, tuple[bytes, str] | None]:
        """（缓存里有没有结论, 图）：有图返回图，记着取不到返回 None；没缓存或到了该重试的时候第一项是 False。"""
        hit = self.root / host
        if hit.is_file():
            data = hit.read_bytes()
            kind = sniff(data)
            if kind:
                return True, (data, kind)
        miss = self.root / f"{host}.none"
        if miss.is_file() and self._now() - miss.stat().st_mtime < RETRY_AFTER:
            return True, None
        return False, None

    def _store(self, host: str, found: tuple[bytes, str] | None) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if found:
            (self.root / host).write_bytes(found[0])
            (self.root / f"{host}.none").unlink(missing_ok=True)
        else:
            (self.root / f"{host}.none").touch()

    # ---- 取 ----

    async def _fetch(self, host: str) -> tuple[bytes, str] | None:
        async with httpx.AsyncClient(transport=self._transport, timeout=5, headers={"User-Agent": UA},
                                     follow_redirects=False) as c:
            for scheme in ("https", "http"):  # 只有 https 连不上才退到 http
                page = await self._get(c, f"{scheme}://{host}/", MAX_HTML)
                if page is None:
                    continue
                url, html, _ = page
                for href in icon_links(html.decode("utf-8", "ignore"), url)[:3]:
                    if found := await self._icon(c, href):
                        return found
                break
            for scheme in ("https", "http"):
                if found := await self._icon(c, f"{scheme}://{host}/favicon.ico"):
                    return found
        return None

    async def _icon(self, c: httpx.AsyncClient, url: str) -> tuple[bytes, str] | None:
        if url.startswith("data:"):  # 有的站把图标直接写在页面里
            head, _, body = url.partition(",")
            if ";base64" not in head:
                return None
            try:
                data = base64.b64decode(body)
            except ValueError:
                return None
        else:
            got = await self._get(c, url, MAX_ICON)
            if got is None or not got[2]:
                return None
            data = got[1]
        kind = sniff(data) if 0 < len(data) <= MAX_ICON else None
        return (data, kind) if kind else None

    async def _get(self, c: httpx.AsyncClient, url: str, limit: int) -> tuple[str, bytes, bool] | None:
        """GET 一个公网地址，自己跟跳转（每一跳都查地址）；返回（最后的网址, 前 limit 字节, 是否读完），失败返回 None。"""
        for _ in range(4):
            if not await self._allowed(url):
                return None
            try:
                async with c.stream("GET", url) as r:
                    if r.is_redirect and r.headers.get("location"):
                        url = urljoin(url, r.headers["location"])
                        continue
                    if r.status_code != 200:
                        return None
                    buf = bytearray()
                    async for chunk in r.aiter_bytes():
                        buf += chunk
                        if len(buf) > limit:
                            return url, bytes(buf[:limit]), False
                    return url, bytes(buf), True
            except (httpx.HTTPError, ValueError):
                return None
        return None

    async def _allowed(self, url: str) -> bool:
        p = urlsplit(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            return False
        try:
            if p.port not in (None, 80, 443):
                return False
            try:
                return _public_ip(p.hostname)  # 跳转到了 IP 地址
            except ValueError:
                pass
            addrs = await self._resolve(p.hostname)
            return bool(addrs) and all(_public_ip(a) for a in addrs)
        except (OSError, ValueError):
            return False
