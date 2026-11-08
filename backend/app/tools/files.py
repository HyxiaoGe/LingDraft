"""用户发的文件怎么给模型（2026-11-08 Sean 定，见 docs/图片和文件理解方案.md 的“二、文件”）。

- 存着的消息里只记文件的编号、名字、字数（history.FILES），文字在文件表里（app.files）。每次调模型时在 ModelSelect 里拼进那句话前面（with_files）：
  - 短的（不超过 INLINE_FILE_CHARS 字）整篇放进去，一句话里整篇放的加起来不超过 INLINE_MESSAGE_CHARS；
  - 长的、这句话里放不下的只给目录、开头（HEAD_CHARS 字），模型用 read_file 按段读、在文件里找；
  - 之前几轮的，压缩第一层压过了（见 compaction）也只给目录和开头。
  整篇还是只给开头，只看这句话带的文件的字数和压没压过，不看别的：同一句话每次拼出来都一样，前缀缓存接得上。
- read_file：只认这段对话里用户发的文件（history.owned_file_ids），一次最多回 READ_MAX_CHARS 字。不收钱（models.toml 的 [tools] 没定价）。
  只给主 agent：子 agent 看不到用户发的文件，要用的内容主 agent 写进交代里。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.tools import ToolException, tool
from pydantic import BaseModel, Field

from app.files.parse import MAX_ROWS, Part
from app.graph.research import prompts
from app.graph.research.history import FILE_TOOL, FILES_BRIEF, attached_files, owned_file_ids
from app.graph.research.state import AttachedFile

log = logging.getLogger(__name__)

INLINE_FILE_CHARS = 20_000  # 不超过这么多字的文件整篇放进那句话
INLINE_MESSAGE_CHARS = 50_000  # 一句话里整篇放的文件加起来最多这么多字
HEAD_CHARS = 2_000  # 只给开头的给这么多字
OUTLINE_LINES = 40
READ_MAX_CHARS = 8_000  # read_file 一次最多回这么多字
SNIPPET_CHARS = 300  # 找到的地方前后各带这么多字
MATCH_PARTS = 8  # 在文件里找，最多列这么多段
BRIEF_GUESS = 1_500  # 估算时，只给开头的文件除了开头另算的字数（目录、几句说明）


def full_ids(files: list[AttachedFile], brief: bool) -> set[str]:
    """这句话带的文件里哪几个整篇放：按发的先后，短的、还放得下的整篇。压过的（brief）都不整篇。"""
    if brief:
        return set()
    out, room = set(), INLINE_MESSAGE_CHARS
    for f in files:
        if f.chars <= INLINE_FILE_CHARS and f.chars <= room:
            out.add(f.id)
            room -= f.chars
    return out


def extra_chars(m: BaseMessage) -> int:
    """估算用：这句话带的文件拼进去大约多少字（压缩按它估 token，见 compaction.message_tokens）。"""
    files = attached_files(m)
    if not files:
        return 0
    full = full_ids(files, bool(m.additional_kwargs.get(FILES_BRIEF)))
    return sum(f.chars if f.id in full else min(f.chars, HEAD_CHARS) + BRIEF_GUESS for f in files)


def _attr(v: Any) -> str:
    return str(v).replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace("\n", " ")


def _parts_text(parts: list[tuple[int, Part]], limit: int | None = None) -> str:
    out, size = [], 0
    for n, p in parts:
        head = prompts.render("files.part", n=n, title=p.title)
        text = p.text
        if limit is not None and size + len(text) > limit:
            text = text[:max(0, limit - size)].rstrip() + "…"
        out.append(f"{head}\n{text}")
        size += len(text)
        if limit is not None and size >= limit:
            break
    return "\n\n".join(out)


def _facts(meta: dict) -> str:
    lines = []
    if sheets := meta.get("sheets"):
        lines.append(prompts.render("files.sheets", sheets="; ".join(f"{s['name']} ({s['rows']} rows)" for s in sheets)))
        if cut := [s["name"] for s in sheets if s.get("cut")]:
            lines.append(prompts.render("files.sheet_cut", rows=MAX_ROWS, names=", ".join(cut)))
    elif meta.get("cut"):
        lines.append(prompts.render("files.sheet_cut", rows=MAX_ROWS, names="the table"))
    if meta.get("truncated"):
        lines.append(prompts.render("files.truncated", chars=meta.get("chars", 0)))
    return "\n".join(lines)


def render(f: AttachedFile, content: tuple[Any, list[Part]] | None, full: bool) -> str:
    """一个文件给模型的那段（<file> 标签）。取不到的（删了、出问题）说读不了。"""
    attrs = {"id": _attr(f.id), "name": _attr(f.name), "type": f.type}
    if content is None:
        return prompts.render("files.gone", **attrs)
    row, parts = content
    numbered = list(enumerate(parts, 1))
    if full:
        return prompts.render("files.full", **attrs, count=len(parts), body=_parts_text(numbered))
    outline = (row.meta.get("outline") or [])[:OUTLINE_LINES]
    return prompts.render("files.brief", **attrs, count=len(parts), facts=_facts(row.meta), outline="\n".join(outline),
                          body=_parts_text(numbered, HEAD_CHARS))


async def with_files(messages: list[BaseMessage], files) -> list[BaseMessage]:
    """给模型的那份：带了文件的用户消息，前面接上文件（见模块说明）。files 是 FileService（见 ChatContext.files），没有时都当读不了。
    没带文件的消息原样返回。"""
    out = list(messages)
    for i, m in enumerate(messages):
        attached = attached_files(m)
        if not attached:
            continue
        brief = bool(m.additional_kwargs.get(FILES_BRIEF))
        full = full_ids(attached, brief)
        blocks = []
        for f in attached:
            got = None
            if files is not None:
                try:
                    got = await files.content(f.id)
                except Exception as e:  # noqa: BLE001  取不到当读不了，这次照常调模型
                    log.warning("取文件给模型没取到：%s: %s: %s", f.id, type(e).__name__, str(e)[:200])
            blocks.append(render(f, got, f.id in full))
        if shortened := [f for f in attached if f.id not in full]:
            blocks.append(prompts.render("files.brief_note", n=len(shortened), earlier=brief))
        text = "\n\n".join(blocks)
        content = f"{text}\n\n{m.content}" if isinstance(m.content, str) else [{"type": "text", "text": text}, *m.content]
        out[i] = m.model_copy(update={"content": content})
    return out


# ---- read_file ----


class ReadFileArgs(BaseModel):
    file_id: str = Field(description=prompts.render("tools.read_file.file_id"))
    parts: str | None = Field(default=None, description=prompts.render("tools.read_file.parts"))
    query: str | None = Field(default=None, description=prompts.render("tools.read_file.query"))


def _wanted(spec: str, count: int) -> list[int] | None:
    """“3”“5-8”“2,7,9” → 段号（去重、按给的先后）；格式不对是 None。超出范围的去掉。"""
    out: list[int] = []
    for piece in re.split(r"[,，、\s]+", spec.strip()):
        if not piece:
            continue
        m = re.fullmatch(r"(\d+)(?:\s*[-–~]\s*(\d+))?", piece)
        if not m:
            return None
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        if b < a:
            a, b = b, a
        out += [n for n in range(a, min(b, count) + 1) if n >= 1 and n not in out]
    return out


def read_parts(parts: list[Part], wanted: list[int]) -> str:
    out, size = [], 0
    for k, n in enumerate(wanted):
        p = parts[n - 1]
        room = READ_MAX_CHARS - size
        if len(p.text) > room and out:  # 放不下这一段了：说从哪接着读
            out.append(prompts.render("tools.read_file.cut", next=n, count=len(parts)))
            break
        text = p.text
        if len(text) > room:
            text = text[:room].rstrip() + "\n" + prompts.render("tools.read_file.part_cut", n=n)
        out.append(prompts.render("files.part", n=n, title=p.title) + "\n" + text)
        size += len(text)
        if k + 1 < len(wanted) and size >= READ_MAX_CHARS:
            out.append(prompts.render("tools.read_file.cut", next=wanted[k + 1], count=len(parts)))
            break
    return "\n\n".join(out)


def search(name: str, parts: list[Part], query: str) -> tuple[str, int]:
    """在文件里找：整句找得到的段排前面，再按几个词都出现、出现次数排；每段给找到的地方前后的字。不分大小写。
    返回给模型的结果和找到了几段（页面上写）。"""
    q = query.strip()
    terms = [t for t in dict.fromkeys(q.lower().split()) if t]
    scored = []
    for n, p in enumerate(parts, 1):
        low = p.text.lower()
        whole = low.count(q.lower())
        hits = {t: low.count(t) for t in terms}
        covered = sum(1 for v in hits.values() if v)
        if whole or covered:
            scored.append(((whole > 0, covered, whole + sum(hits.values())), n))
    if not scored:
        return prompts.render("tools.read_file.no_match", name=name, query=q, count=len(parts)), 0
    scored.sort(key=lambda x: (x[0][0], x[0][1], x[0][2]), reverse=True)
    picked = sorted(n for _, n in scored[:MATCH_PARTS])
    out = [prompts.render("tools.read_file.matches", found=len(scored), shown=len(picked), name=name, query=q)]
    size = 0
    for n in picked:
        snippet = _snippets(parts[n - 1].text, [q.lower(), *terms])
        if size + len(snippet) > READ_MAX_CHARS:
            break
        out.append(prompts.render("files.part", n=n, title=parts[n - 1].title) + "\n" + snippet)
        size += len(snippet)
    return "\n\n".join(out), len(scored)


def _snippets(text: str, needles: list[str]) -> str:
    """找到的地方前后各 SNIPPET_CHARS 字，挨着的合成一段，最多三处。"""
    low = text.lower()
    spans: list[list[int]] = []
    for nd in needles:
        start = 0
        while nd and (i := low.find(nd, start)) >= 0 and len(spans) < 12:
            spans.append([max(0, i - SNIPPET_CHARS), min(len(text), i + len(nd) + SNIPPET_CHARS)])
            start = i + len(nd)
    spans.sort()
    merged: list[list[int]] = []
    for s in spans:
        if merged and s[0] <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], s[1])
        else:
            merged.append(s)
    return "\n…\n".join(("…" if a else "") + text[a:b].strip() + ("…" if b < len(text) else "") for a, b in merged[:3])


def make_read_file_tool():
    """读用户发的文件：按段读或在文件里找。文件服务从这次运行的上下文里取（ChatContext.files）。
    编号不是这段对话里用户发的、文件没了、段号不对：给模型一句说明、status=error。
    结果的 artifact 记着文件名和类型（页面上写读的是哪个文件、第几页还是第几段），在文件里找的还记着找到了几段。"""

    @tool(FILE_TOOL, description=prompts.render("tools.read_file.description"), args_schema=ReadFileArgs, response_format="content_and_artifact")
    async def read_file(runtime: ToolRuntime, file_id: str, parts: str | None = None, query: str | None = None) -> tuple[str, dict]:
        fid = file_id.strip()
        messages = runtime.state.get("messages", [])
        if fid not in owned_file_ids(messages):
            raise ToolException(prompts.render("tools.read_file.not_found", id=fid))
        f = next(a for m in messages if isinstance(m, HumanMessage) for a in attached_files(m) if a.id == fid)
        service = getattr(runtime.context, "files", None)
        got = await service.content(fid) if service is not None else None
        if got is None:
            raise ToolException(prompts.render("tools.read_file.unavailable", name=f.name))
        all_parts = got[1]
        art = {"name": f.name, "file_id": fid, "type": f.type}
        if query and query.strip():
            text, found = search(f.name, all_parts, query)
            return text, {**art, "query": query.strip(), "found": found}
        if parts and parts.strip():
            wanted = _wanted(parts, len(all_parts))
            if wanted is None:
                raise ToolException(prompts.render("tools.read_file.bad_parts", parts=parts, count=len(all_parts)))
            if not wanted:
                raise ToolException(prompts.render("tools.read_file.out_of_range", count=len(all_parts)))
        else:
            wanted = list(range(1, len(all_parts) + 1))
        return read_parts(all_parts, wanted), {**art, "parts": parts.strip() if parts else ""}

    read_file.handle_tool_error = True
    return read_file
