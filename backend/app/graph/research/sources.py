"""来源：从搜索、读页的工具结果里取出来，不另外存。

每个网址的 id 由网址算出来（`s` 加 6 位十六进制），工具结果里直接带给模型，正文里用它引用。
同一个网址在整条线程里永远是同一个 id：并行的几次搜索各自算，也对得上。
"""

from __future__ import annotations

import hashlib
import json
import re
import string
from typing import Any
from urllib.parse import quote, urlsplit

from langchain_core.messages import BaseMessage, ToolMessage

from app.graph.research.state import Source

EXCERPT_CHARS = 400  # 来源卡上显示的原文开头；模型自己看的是完整的工具结果
SOURCE_ID = re.compile(r"\[(s[0-9a-f]{6}(?:[0-9a-f]{4})?)\]")  # 正文里的引用：[s3fa9c1]

_DEFAULT_PORT = {"http": 80, "https": 443}
_UNRESERVED = frozenset(string.ascii_letters + string.digits + "-._~")
_PRINTABLE_ASCII = "".join(chr(i) for i in range(0x21, 0x7F))


def _pct(s: str) -> str:
    """百分号编码统一成一种写法：非 ASCII 字符编码成 %XX，编了码的非保留字符解开，十六进制大写。

    保留字符（/ ? & = 等）编码与否意思不同（%2F 不是路径分隔，%26 不是参数分隔），保持原样。
    """
    s = quote(s, safe=_PRINTABLE_ASCII)
    return re.sub(r"%([0-9A-Fa-f]{2})",
                  lambda m: c if (c := chr(int(m.group(1), 16))) in _UNRESERVED else "%" + m.group(1).upper(), s)


def url_key(url: str) -> str:
    """去重用：只做不改变指向的规范化——协议和域名的大小写、协议的默认端口、#锚点、百分号编码的写法、空路径。

    www、http / https、结尾斜杠、保留字符的编码都可能指向不同的页面，不合并：
    误合并会把一页的正文挂到另一页的来源上（证据错配），漏合并只是多一个来源。
    """
    url = url.strip()
    try:
        p = urlsplit(url)
        scheme, host, port = p.scheme.lower(), (p.hostname or "").lower(), p.port
    except ValueError:  # 端口之类写坏了：按原样比较
        return url
    if not host:
        return url
    netloc = (f"[{host}]" if ":" in host else host) + (f":{port}" if port is not None and port != _DEFAULT_PORT.get(scheme) else "")
    return f"{scheme}://{netloc}{_pct(p.path) or '/'}" + (f"?{_pct(p.query)}" if p.query else "")


def source_id(url: str, known: dict[str, str]) -> str:
    """known 是之前出现过的 {url_key: id}。见过的网址沿用原来的 id；6 位撞上了别的网址就用 10 位。"""
    key = url_key(url)
    if key in known:
        return known[key]
    digest = hashlib.sha1(key.encode()).hexdigest()
    sid = "s" + digest[:6]
    return sid if sid not in set(known.values()) else "s" + digest[:10]


def parse(msg: ToolMessage) -> Any:
    try:
        return json.loads(msg.content if isinstance(msg.content, str) else "")
    except (TypeError, ValueError):
        return None


def collect_sources(messages: list[BaseMessage]) -> list[Source]:
    """按第一次出现的先后列出所有来源。读到正文的来源用正文开头替换搜索摘要。"""
    by_id: dict[str, Source] = {}
    for m in messages:
        if not isinstance(m, ToolMessage) or m.status == "error":
            continue
        data = parse(m)
        when = (m.artifact or {}).get("retrieved_at", "") if isinstance(m.artifact, dict) else ""
        if m.name == "search_web" and isinstance(data, list):
            for hit in data:
                sid, url = hit.get("id"), (hit.get("url") or "").strip()
                if not sid or not url or sid in by_id:
                    continue
                by_id[sid] = Source(id=sid, url=url, title=hit.get("title") or "", retrieved_at=when, published_at=hit.get("published_at"),
                                    excerpt=(hit.get("content") or hit.get("description") or "")[:EXCERPT_CHARS])
        elif m.name == "read_page" and isinstance(data, dict) and data.get("ok") and data.get("id"):
            content = data.get("content") or ""
            if not content.strip():  # 没有正文不算读到，不能拿空串顶掉搜索摘要
                continue
            src = by_id.get(data["id"]) or Source(id=data["id"], url=(data.get("url") or "").strip(), retrieved_at=when)
            by_id[src.id] = src.model_copy(update={"excerpt": content[:EXCERPT_CHARS], "excerpt_kind": "body",
                                                   "title": src.title or data.get("title") or ""})
    return list(by_id.values())


def known_ids(messages: list[BaseMessage]) -> dict[str, str]:
    """之前的工具结果里出现过的 {url_key: id}，给新的搜索、读页结果编 id 用。"""
    return {url_key(s.url): s.id for s in collect_sources(messages)}


def cited(text: str) -> list[str]:
    """一段文字里引用的来源 id，按第一次出现的先后、去重。"""
    return list(dict.fromkeys(SOURCE_ID.findall(text or "")))
