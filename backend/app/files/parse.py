"""上传的文件转成按段的文字。

看内容认类型，不看扩展名（见 sniff）：PDF、docx / xlsx / pptx（都是 zip，看里面是哪种）、能解码的文字（CSV、Markdown、代码、配置……按扩展名细分）。
老格式（doc、xls、ppt）和加了密码的 Office 文件是 OLE 容器，不收；图片走发图那条路，这里也不收。

切段（存进 file_parts，模型用 read_file 按段读，以后 Artifact 改文件也按段）：
- PDF 一页一段（段号就是页码，空白页也占一段）；PPT 一张一段（带备注）。
- Excel 一张表按行切成几段，每段都带表头；CSV 一样。每张表最多读 MAX_ROWS 行。
- Word、Markdown 按标题和长度切（大约 PART_CHARS 字一段），表格转成 Markdown 表格；别的文字按行和长度切。

这里的函数在单独的子进程里跑（见 service.isolated_parse）：只用标准库和解析库，不碰数据库、配置。出问题抛 Unreadable，
message 是给用户看的（人话），code 给日志和测试。
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, NamedTuple

PART_CHARS = 4000  # 一段大约多少字
MAX_CHARS = 2_000_000  # 一个文件最多抽这么多字（约一千页的书），多了后面的不要
MAX_ROWS = 2000  # Excel、CSV 每张表最多读这么多行（不算表头）
MAX_COLS = 100
MAX_UNZIPPED = 200 * 1024 * 1024  # docx、xlsx、pptx 解压后的上限（防压缩炸弹）
MAX_ZIP_ENTRIES = 20000
SCANNED_CHARS_PER_PAGE = 20  # PDF 平均每页不到这么多字、一多半的页没字，当扫描版（第一版读不了，以后按页转图看）
OUTLINE_MAX = 60  # 目录最多列这么多条

PDF, DOCX, XLSX, PPTX, CSV, MARKDOWN, CODE, TEXT = "pdf", "docx", "xlsx", "pptx", "csv", "markdown", "code", "text"
TYPES = (PDF, DOCX, XLSX, PPTX, CSV, MARKDOWN, CODE, TEXT)
CODE_EXT = {
    "py", "js", "jsx", "mjs", "cjs", "ts", "tsx", "java", "kt", "kts", "go", "rs", "c", "h", "cc", "cpp", "hpp", "cs", "rb", "php", "swift",
    "scala", "sh", "bash", "zsh", "ps1", "sql", "r", "lua", "dart", "vue", "svelte", "html", "htm", "css", "scss", "less", "xml", "json",
    "jsonl", "yaml", "yml", "toml", "ini", "cfg", "conf", "env", "properties", "gradle", "dockerfile", "makefile", "proto", "graphql", "tf",
}

# Unreadable 的 code
UNSUPPORTED, LEGACY, ENCRYPTED, BROKEN, SCANNED, EMPTY, IMAGE, TOO_LARGE = (
    "unsupported", "legacy_office", "encrypted", "broken", "scanned", "empty", "image", "unzipped_too_large")
SUPPORTED_HINT = "可以传 PDF、Word（docx）、Excel（xlsx）、PPT（pptx）、CSV、文本、Markdown 或代码文件"


class Unreadable(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


class Part(NamedTuple):
    title: str  # “第 3 页”“第 2 张：路线图”“销售（第 1–180 行）”“二、付款方式”
    text: str


@dataclass
class Parsed:
    type: str
    parts: list[Part]
    meta: dict[str, Any] = field(default_factory=dict)  # pages / slides / sheets（每张的 name、rows、cut）/ rows、cut（CSV）、lines、chars、outline、truncated

    @property
    def chars(self) -> int:
        return sum(len(p.text) for p in self.parts)


# ---- 认类型 ----


def sniff(data: bytes, name: str) -> str:
    """看内容认是哪种（TYPES 之一），认不了、不收的抛 Unreadable。zip 要整个文件（目录在末尾）。"""
    head = data[:2048]
    if b"%PDF-" in head[:1024]:
        return PDF
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        raise Unreadable(LEGACY, "老格式的 doc、xls、ppt 和加了密码的 Office 文件读不了，请另存成 docx、xlsx、pptx 再传")
    if _is_image(head):
        raise Unreadable(IMAGE, "图片请直接作为图片发送")
    if head.startswith(b"PK\x03\x04"):
        return _office_kind(data)
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):  # UTF-16 带 BOM 的文字
        return _text_kind(name)
    if b"\x00" in data[:8192]:
        raise Unreadable(UNSUPPORTED, f"不支持这种文件，{SUPPORTED_HINT}")
    return _text_kind(name)


def _is_image(head: bytes) -> bool:
    return (head.startswith((b"\x89PNG", b"\xff\xd8\xff", b"GIF87a", b"GIF89a"))
            or (head[:4] == b"RIFF" and head[8:12] == b"WEBP") or head[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftypavif"))


def _office_kind(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos = z.infolist()
            names = {i.filename for i in infos}
    except zipfile.BadZipFile:
        raise Unreadable(BROKEN, "文件打不开，可能已经损坏") from None
    if len(infos) > MAX_ZIP_ENTRIES or sum(i.file_size for i in infos) > MAX_UNZIPPED:
        raise Unreadable(TOO_LARGE, "文件解压后太大，读不了")
    if "word/document.xml" in names:
        return DOCX
    if "xl/workbook.xml" in names:
        return XLSX
    if "ppt/presentation.xml" in names:
        return PPTX
    raise Unreadable(UNSUPPORTED, f"不支持压缩包这类文件，{SUPPORTED_HINT}")


def _text_kind(name: str) -> str:
    stem, _, ext = name.lower().rpartition(".")
    ext = ext if stem else name.lower()  # 没有扩展名的（Dockerfile、Makefile）按整个名字认
    if ext in ("csv", "tsv"):
        return CSV
    if ext in ("md", "markdown"):
        return MARKDOWN
    if ext in CODE_EXT:
        return CODE
    return TEXT


def decode(data: bytes) -> str:
    """文字文件解码：UTF-8（带不带 BOM）、UTF-16 带 BOM，别的交给 charset-normalizer 猜（GBK 常见）。解不出抛 Unreadable。"""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    from charset_normalizer import from_bytes

    best = from_bytes(data).best()
    if best is None:
        raise Unreadable(UNSUPPORTED, f"认不出文件的文字编码，{SUPPORTED_HINT}")
    return str(best)


# ---- 解析 ----


def parse_file(path: Path, name: str) -> Parsed:
    data = path.read_bytes()
    kind = sniff(data, name)
    parsed = _PARSERS[kind](data, name)
    return _finish(parsed)


def parse_bytes(data: bytes, name: str) -> Parsed:
    kind = sniff(data, name)
    return _finish(_PARSERS[kind](data, name))


def _finish(p: Parsed) -> Parsed:
    """字数到上限的截掉后面的；一个字都没有的不收。"""
    total, kept = 0, []
    for part in p.parts:
        if total + len(part.text) > MAX_CHARS:
            kept.append(Part(part.title, part.text[:max(0, MAX_CHARS - total)]))
            p.meta["truncated"] = True
            break
        kept.append(part)
        total += len(part.text)
    p.parts = kept
    p.meta["chars"] = p.chars
    if not any(part.text.strip() for part in p.parts):
        raise Unreadable(EMPTY, "文件里没有能读出来的文字")
    return p


def _tidy(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _pdf(data: bytes, name: str) -> Parsed:
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as e:
        if "password" in str(e).lower():
            raise Unreadable(ENCRYPTED, "这个 PDF 有密码，读不了") from None
        raise Unreadable(BROKEN, "这个 PDF 打不开，可能已经损坏") from None
    try:
        n = len(pdf)
        parts, total = [], 0
        for i in range(n):
            page = pdf[i]
            tp = page.get_textpage()
            try:
                text = _tidy(tp.get_text_bounded())
            finally:
                tp.close()
                page.close()
            parts.append(Part(f"第 {i + 1} 页", text))
            total += len(text)
            if total > MAX_CHARS:
                break
        outline = []
        try:
            for item in pdf.get_toc(max_depth=3):
                title = _tidy(item.get_title() if hasattr(item, "get_title") else item.title)
                level = getattr(item, "level", 0)
                dest = item.get_dest() if hasattr(item, "get_dest") else None
                page_no = (dest.get_index() + 1) if dest is not None and dest.get_index() is not None else getattr(item, "page_index", None)
                if title:
                    outline.append(("  " * level) + title + (f"（第 {page_no} 页）" if isinstance(page_no, int) and page_no > 0 else ""))
                if len(outline) >= OUTLINE_MAX:
                    break
        except Exception:  # noqa: BLE001  目录读不出来不要紧
            outline = []
    finally:
        pdf.close()
    # 扫描版：整体字很少，而且一多半的页上几乎没字（每页只有个标题、配图为主的不算，照样能读）
    if n and sum(len(p.text) for p in parts) < SCANNED_CHARS_PER_PAGE * n and sum(len(p.text) < 4 for p in parts) * 2 > n:  # 页码这类不到 4 个字的当没字
        raise Unreadable(SCANNED, "这个 PDF 是扫描的图片，读不出文字")
    return Parsed(PDF, parts, {"pages": n, "outline": outline})


_HEADING = re.compile(r"(?i)^(?:heading|标题)\s*(\d)$")


def _heading_level(style: str) -> int:
    if style.lower() == "title":
        return 1
    m = _HEADING.match(style.strip())
    return int(m.group(1)) if m else 0


def _docx(data: bytes, name: str) -> Parsed:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        doc = Document(io.BytesIO(data))
    except Exception:  # noqa: BLE001
        raise Unreadable(BROKEN, "这个 Word 文件打不开，可能已经损坏") from None
    blocks: list[tuple[int, str]] = []
    for el in doc.element.body.iterchildren():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(el, doc)
            text = p.text.strip()
            if not text:
                continue
            level = _heading_level(p.style.name if p.style is not None and p.style.name else "")
            blocks.append((level, f"{'#' * level} {text}" if level else text))
        elif tag == "tbl":
            rows = [[c.text for c in _dedupe(row.cells)] for row in Table(el, doc).rows]
            if any(any(c.strip() for c in r) for r in rows):
                blocks.append((0, _md_table(rows)))
    parts, outline = _blocks_to_parts(blocks)
    return Parsed(DOCX, parts, {"outline": outline})


def _dedupe(cells) -> list:
    """合并单元格 python-docx 会重复给同一个格子，相邻的同一个只留一个。"""
    out = []
    for c in cells:
        if not out or c._tc is not out[-1]._tc:
            out.append(c)
    return out


def _markdown(data: bytes, name: str) -> Parsed:
    text = _tidy(decode(data))
    blocks: list[tuple[int, str]] = []
    fence = False
    for para in re.split(r"\n\s*\n", text):
        first = para.lstrip().split("\n", 1)[0]
        if first.startswith("```"):
            fence = not fence if para.count("```") % 2 else fence
        m = re.match(r"^(#{1,6})\s+\S", first) if not fence else None
        blocks.append((len(m.group(1)) if m else 0, para))
    parts, outline = _blocks_to_parts(blocks)
    return Parsed(MARKDOWN, parts, {"outline": outline})


def _blocks_to_parts(blocks: list[tuple[int, str]]) -> tuple[list[Part], list[str]]:
    """按标题和长度切段：碰到一二级标题、而这一段已经有不少字了就另起一段；加上这块超过 PART_CHARS 也另起。
    一块本身就超长的（大表格、长段落）按行切开。段名是这一段开头时最近的标题，没有标题的是“第 n 段”。"""
    parts: list[Part] = []
    outline: list[str] = []
    cur: list[str] = []
    size = 0
    heading = ""
    start_heading = ""

    def flush() -> None:
        nonlocal cur, size
        if cur:
            parts.append(Part(start_heading or f"第 {len(parts) + 1} 段", "\n\n".join(cur)))
        cur, size = [], 0

    for level, text in blocks:
        if level:
            title = text.lstrip("#").strip()
            if level <= 3 and len(outline) < OUTLINE_MAX:
                outline.append("  " * (level - 1) + title)
            if cur and (level <= 2 and size >= PART_CHARS // 4 or size + len(text) > PART_CHARS):
                flush()
            heading = title
        elif cur and size + len(text) > PART_CHARS:
            flush()
        if not cur:
            start_heading = heading
        for piece in _split_long(text):
            if cur and size + len(piece) > PART_CHARS:
                flush()
                start_heading = heading
            cur.append(piece)
            size += len(piece) + 2
    flush()
    return parts, outline


def _split_long(text: str) -> list[str]:
    if len(text) <= PART_CHARS:
        return [text]
    out, cur, size = [], [], 0
    for line in text.split("\n"):
        while len(line) > PART_CHARS:  # 一行就超长的（没换行的长段落）硬切
            if cur:
                out.append("\n".join(cur))
                cur, size = [], 0
            out.append(line[:PART_CHARS])
            line = line[PART_CHARS:]
        if cur and size + len(line) > PART_CHARS:
            out.append("\n".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += len(line) + 1
    if cur:
        out.append("\n".join(cur))
    return out


def _lines(data: bytes, name: str, kind: str) -> Parsed:
    """代码、纯文字：按行切，段名是行号。"""
    text = decode(data).replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    lines = text.split("\n")
    parts, cur, size, first = [], [], 0, 1
    for no, line in enumerate(lines, 1):
        if cur and size + len(line) > PART_CHARS:
            parts.append(Part(f"第 {first}–{no - 1} 行", "\n".join(cur)))
            cur, size, first = [], 0, no
        cur.append(line[:PART_CHARS])
        size += len(line) + 1
    if cur:
        parts.append(Part(f"第 {first}–{first + len(cur) - 1} 行", "\n".join(cur)))
    return Parsed(kind, parts, {"lines": len(lines)})


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.date().isoformat() if v.time() == time(0) else v.isoformat(sep=" ", timespec="minutes")
    if isinstance(v, (date, time)):
        return v.isoformat()
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return " ".join(str(v).split()).replace("|", "\\|")


def _md_table(rows: list[list[str]]) -> str:
    rows = [[_cell(c) for c in r[:MAX_COLS]] for r in rows]
    width = max((len(r) for r in rows), default=0)
    if not width:
        return ""
    rows = [r + [""] * (width - len(r)) for r in rows]
    head, body = rows[0], rows[1:]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * width]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(lines)


def _table_parts(label: str, rows: list[list[Any]]) -> list[Part]:
    """一张表切成几段，每段都带表头（第一行），段名写第几行到第几行（不算表头，空行不算）。"""
    if not rows:
        return []
    head, body = rows[0], rows[1:]
    if not body:
        return [Part(f"{label}（只有表头）", _md_table([head]))]
    parts, chunk, size, first = [], [], 0, 1
    head_size = len(_md_table([head]))
    for i, row in enumerate(body, 1):
        line = sum(len(_cell(c)) + 3 for c in row[:MAX_COLS])
        if chunk and head_size + size + line > PART_CHARS:
            parts.append(Part(f"{label}（第 {first}–{i - 1} 行）", _md_table([head, *chunk])))
            chunk, size, first = [], 0, i
        chunk.append(row)
        size += line
    parts.append(Part(f"{label}（第 {first}–{first + len(chunk) - 1} 行）", _md_table([head, *chunk])))
    return parts


def _xlsx(data: bytes, name: str) -> Parsed:
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:  # noqa: BLE001
        raise Unreadable(BROKEN, "这个 Excel 文件打不开，可能已经损坏") from None
    parts: list[Part] = []
    sheets: list[dict] = []
    try:
        for ws in wb.worksheets:
            rows: list[list[Any]] = []
            cut = False
            for row in ws.iter_rows(values_only=True):
                cells = list(row[:MAX_COLS])
                while cells and (cells[-1] is None or str(cells[-1]).strip() == ""):
                    cells.pop()
                if not cells:
                    continue
                if len(rows) > MAX_ROWS:  # 表头 + MAX_ROWS 行
                    cut = True
                    break
                rows.append(cells)
            total = max(len(rows) - 1, 0)
            sheets.append({"name": ws.title, "rows": total, "cut": cut})
            parts += _table_parts(ws.title, rows)
    finally:
        wb.close()
    return Parsed(XLSX, parts, {"sheets": [{"name": s["name"], "rows": s["rows"], "cut": s["cut"]} for s in sheets]})


def _csv(data: bytes, name: str) -> Parsed:
    text = decode(data)
    sample = text[:20000]
    if name.lower().endswith(".tsv"):
        dialect: Any = csv.excel_tab
    else:
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
    rows, cut = [], False
    for row in csv.reader(io.StringIO(text), dialect):
        cells = list(row[:MAX_COLS])
        while cells and not cells[-1].strip():
            cells.pop()
        if not cells:
            continue
        if len(rows) > MAX_ROWS:
            cut = True
            break
        rows.append(cells)
    return Parsed(CSV, _table_parts("表格", rows), {"rows": max(len(rows) - 1, 0), "cut": cut})


def _pptx(data: bytes, name: str) -> Parsed:
    from pptx import Presentation

    try:
        prs = Presentation(io.BytesIO(data))
    except Exception:  # noqa: BLE001
        raise Unreadable(BROKEN, "这个 PPT 文件打不开，可能已经损坏") from None
    parts, outline = [], []
    for i, slide in enumerate(prs.slides, 1):
        title = ""
        shape_title = slide.shapes.title
        if shape_title is not None and shape_title.has_text_frame:
            title = " ".join(shape_title.text_frame.text.split())
        lines: list[str] = []
        for shape in slide.shapes:
            lines += _shape_text(shape)
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                lines.append("备注：" + notes)
        parts.append(Part(f"第 {i} 张" + (f"：{title}" if title else ""), _tidy("\n".join(lines))))
        if title and len(outline) < OUTLINE_MAX:
            outline.append(f"{i}. {title}")
    return Parsed(PPTX, parts, {"slides": len(parts), "outline": outline})


def _shape_text(shape) -> list[str]:
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
        return [line for s in shape.shapes for line in _shape_text(s)]
    if getattr(shape, "has_table", False) and shape.has_table:
        return [_md_table([[c.text for c in row.cells] for row in shape.table.rows])]
    if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
        text = shape.text_frame.text.strip()
        return [text] if text else []
    return []


_PARSERS = {
    PDF: _pdf, DOCX: _docx, XLSX: _xlsx, PPTX: _pptx, CSV: _csv, MARKDOWN: _markdown,
    CODE: lambda d, n: _lines(d, n, CODE), TEXT: lambda d, n: _lines(d, n, TEXT),
}
