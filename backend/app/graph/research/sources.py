"""来源：从搜索、读页的工具结果里取出来，不另外存。

每个网址的 id 由网址算出来（`s` 加 6 位十六进制），工具结果里直接带给模型，正文里用它引用（见 app.tools.web_mcp）。
同一个网址在整条线程里永远是同一个 id：并行的几次搜索各自算，也对得上。
搜索结果有两种样子（search_hits 两种都认）：11-05 起是搜索服务的结果（{"ok": true, "query", "results": [...]}，每条带 id、
title、url、snippet、published_at），之前存下的是一串条目（每条带 id、title、url、description、content、published_at）。
查文档（Context7 的 query-docs）的每段文档按它的出处算一个来源，带来源的那几段记在结果的 artifact 里（见 doc_snippets、app.tools.docs_mcp）。
查知识库（search_knowledge）的每一段（文件的第几段，网址是 kb://<文件编号>/<段号>）算一个来源，查到的块记在结果的 artifact 里（见 app.tools.knowledge）。
子 agent 的搜索、读页结果在 task 工具结果的 artifact 里（见 subagent_messages），也算这条线程的来源。
"""

from __future__ import annotations

import hashlib
import json
import re
import string
from typing import Any
from urllib.parse import quote, urlsplit

from langchain_core.messages import BaseMessage, ToolMessage, messages_from_dict

from app.graph.research.state import Source

SUBAGENT_TOOL = "task"  # 主 agent 派子 agent 的工具（见 app.graph.research.subagent）
LIBRARY_TOOL = "resolve-library-id"  # 查文档前先按库名找库（Context7，见 app.tools.docs_mcp）
DOCS_TOOL = "query-docs"  # 查文档（Context7，见 app.tools.docs_mcp）
KB_TOOL = "search_knowledge"  # 查用户的知识库（见 app.tools.knowledge）
EXCERPT_CHARS = 400  # 来源卡上显示的原文开头；模型自己看的是完整的工具结果
SEPARATORS = (",", ":")  # 搜索、读页的结果给模型的 JSON 不带空格（照搜索、读页服务给的，省 token）
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


def search_hits(data: Any) -> list[dict] | None:
    """一条搜索结果里的条目；不是成功的搜索结果是 None。"""
    if isinstance(data, list):  # 11-05 以前存下的
        return [h for h in data if isinstance(h, dict)]
    if isinstance(data, dict) and data.get("ok") is True and isinstance(data.get("results"), list):
        return [h for h in data["results"] if isinstance(h, dict)]
    return None


def hit_text(hit: dict) -> str:
    """一条搜索结果的摘要：搜索服务给的是 snippet，11-05 以前存下的是 content（几段摘录）和 description。"""
    return str(hit.get("snippet") or hit.get("content") or hit.get("description") or "")


def doc_snippets(m: BaseMessage) -> list[dict]:
    """一条查文档结果里带来源的那几段（artifact 里，每段有 id、url、title、content，见 app.tools.docs_mcp）。别的消息、没有的是空的。"""
    art = m.artifact if isinstance(m, ToolMessage) and m.name == DOCS_TOOL and isinstance(m.artifact, dict) else {}
    return [s for s in art.get("snippets") or [] if isinstance(s, dict) and s.get("id") and s.get("url")]


def kb_url(file_id: str, part: int) -> str:
    """知识库里一个文件的一段当来源时的网址（只用来算 id、页面认出是知识库的，打不开）。"""
    return f"kb://{file_id}/{part}"


def kb_hits(m: BaseMessage) -> list[dict]:
    """一条查知识库结果里查到的块（artifact 里，每块有 id、url、file_id、name、part、title、content）。别的消息、没有的是空的。"""
    art = m.artifact if isinstance(m, ToolMessage) and m.name == KB_TOOL and isinstance(m.artifact, dict) else {}
    return [h for h in art.get("hits") or [] if isinstance(h, dict) and h.get("id") and h.get("url")]


def subagent_messages(m: BaseMessage) -> list[BaseMessage]:
    """一条 task 工具结果里带着的子 agent 的整段消息（artifact 里，模型看不到；存的是 messages_to_dict）。别的消息、没带的是空的。"""
    art = m.artifact if isinstance(m, ToolMessage) and m.name == SUBAGENT_TOOL else None
    raw = art.get("messages") if isinstance(art, dict) else None
    if not raw:
        return []
    try:
        return messages_from_dict(raw)
    except (KeyError, TypeError, ValueError):
        return []


def flatten(messages: list[BaseMessage]) -> list[BaseMessage]:
    """每条 task 工具结果前面插上它的子 agent 的消息：来源、用量按发生的先后都算上。"""
    out: list[BaseMessage] = []
    for m in messages:
        out += subagent_messages(m)
        out.append(m)
    return out


def collect_sources(messages: list[BaseMessage]) -> list[Source]:
    """按第一次出现的先后列出所有来源，子 agent 查到的也算。读到正文的来源用正文开头替换搜索摘要。
    查文档查到的用第一段文档替换搜索摘要（同一页的几段只取第一段），读到正文的不换。"""
    by_id: dict[str, Source] = {}
    for m in flatten(messages):
        if not isinstance(m, ToolMessage) or m.status == "error":
            continue
        data = parse(m)
        when = (m.artifact or {}).get("retrieved_at", "") if isinstance(m.artifact, dict) else ""
        if m.name == "search_web" and (hits := search_hits(data)) is not None:
            for hit in hits:
                sid, url = hit.get("id"), (hit.get("url") or "").strip()
                if not sid or not url or sid in by_id:
                    continue
                by_id[sid] = Source(id=sid, url=url, title=hit.get("title") or "", retrieved_at=when, published_at=hit.get("published_at"),
                                    excerpt=hit_text(hit)[:EXCERPT_CHARS])
        elif m.name == DOCS_TOOL:
            for s in doc_snippets(m):
                old = by_id.get(s["id"])
                if old is not None and old.excerpt_kind != "summary":
                    continue
                excerpt = {"excerpt": str(s.get("content") or "")[:EXCERPT_CHARS], "excerpt_kind": "docs"}
                by_id[s["id"]] = (Source(id=s["id"], url=s["url"], title=s.get("title") or "", retrieved_at=when, **excerpt) if old is None
                                  else old.model_copy(update={**excerpt, "title": old.title or s.get("title") or ""}))
        elif m.name == KB_TOOL:
            for h in kb_hits(m):
                if h["id"] in by_id:  # 同一段的几块：用先查到的那块
                    continue
                title = f"{h.get('name') or ''} · {h['title']}" if h.get("title") else str(h.get("name") or "")
                by_id[h["id"]] = Source(id=h["id"], url=h["url"], title=title, retrieved_at=when, excerpt=str(h.get("content") or "")[:EXCERPT_CHARS],
                                        excerpt_kind="knowledge", file_id=h.get("file_id"), part=h.get("part"))
        elif m.name == "read_page" and isinstance(data, dict) and data.get("ok") and data.get("id"):
            content = data.get("content") or ""
            if not content.strip():  # 没有正文不算读到，不能拿空串顶掉搜索摘要
                continue
            src = by_id.get(data["id"]) or Source(id=data["id"], url=(data.get("url") or "").strip(), retrieved_at=when)
            by_id[src.id] = src.model_copy(update={"excerpt": content[:EXCERPT_CHARS], "excerpt_kind": "body",
                                                   "title": src.title or data.get("title") or ""})
    return list(by_id.values())


def known_ids(messages: list[BaseMessage]) -> dict[str, str]:
    """之前的工具结果里出现过的 {url_key: id}，给新的搜索、读页、查文档结果编 id 用。"""
    return {url_key(s.url): s.id for s in collect_sources(messages)}


def known_in(state: dict) -> dict[str, str]:
    """工具给新结果编 id 时已经有的 {url_key: id}：子 agent 的 state 里还带着派它时主 agent 那边已有的（parent_sources）。"""
    return {**(state.get("parent_sources") or {}), **known_ids(state.get("messages", []))}


def cited(text: str) -> list[str]:
    """一段文字里引用的来源 id，按第一次出现的先后、去重。"""
    return list(dict.fromkeys(SOURCE_ID.findall(text or "")))
