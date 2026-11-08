"""模型写的文件（Artifact，见 app.tools.write_file）用到的几样：切段、文件名、代码语言、按片段改。都不碰库和磁盘。

- 切段和上传的文件一样按段给 read_file 读，但一个字不改：段落拼起来就是原文（上传的解析时会收拾空行、行尾空格）。
  模型从 read_file 抄出来的 old_text 和原文对得上，edit_file 才能按片段改。
- 改的时候每段 old_text 要在全文里恰好出现一次，不做模糊匹配（2026-11-08 方案：对不上就让模型先 read_file 再改）。
"""

from __future__ import annotations

import re
from typing import NamedTuple

from app.files.parse import PART_CHARS, Part

MARKDOWN, HTML, CODE = "markdown", "html", "code"
TYPES = (MARKDOWN, HTML, CODE)
TITLE_MAX = 100
NAME_MAX = 120
MAX_CHARS = 300_000  # 一份文件最多这么多字（一份很长的报告也就几万字）

# 扩展名 → 代码块的语言（页面高亮用）；没列的照扩展名
LANGS = {
    "py": "python", "js": "javascript", "mjs": "javascript", "cjs": "javascript", "jsx": "jsx", "ts": "typescript", "tsx": "tsx",
    "rb": "ruby", "rs": "rust", "kt": "kotlin", "kts": "kotlin", "cs": "csharp", "sh": "bash", "zsh": "bash", "ps1": "powershell",
    "yml": "yaml", "htm": "html", "md": "markdown", "h": "c", "hpp": "cpp", "cc": "cpp", "golang": "go",
}
BARE_NAMES = {"makefile", "dockerfile", "procfile", "gemfile", "rakefile", "jenkinsfile", "vagrantfile"}  # 没有扩展名也照原样
LANGS.update({"makefile": "makefile", "dockerfile": "dockerfile"})
_HEADING = re.compile(r"^(#{1,6})[ \t]+\S")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def clean_title(title: str) -> str:
    title = re.sub(r"[\x00-\x1f\x7f]", " ", title or "").strip()
    return re.sub(r"\s+", " ", title)[:TITLE_MAX].strip()


def file_name(title: str, kind: str) -> str:
    """下载用的文件名：文档、网页是标题加 .md / .html，代码是模型给的文件名（标题），没有扩展名的加 .txt。去掉路径和文件名里不能有的字。"""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', "_", title).strip(" .") or "未命名"
    if kind == MARKDOWN:
        return name[:NAME_MAX - 3] + ".md"
    if kind == HTML:
        return name[:NAME_MAX - 5] + ".html"
    if "." in name.strip(".") or name.lower() in BARE_NAMES:
        return name[:NAME_MAX]
    return name[:NAME_MAX - 4] + ".txt"


def lang_of(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else name.lower()
    return LANGS.get(ext, ext if "." in name else "")


def split(text: str, kind: str) -> list[Part]:
    """切成段，段落拼起来就是原文。文档在一二级标题处、按长度（parse.PART_CHARS）另起一段，段名是这一段开头时最近的标题；
    网页、代码按行，段名是行号。代码块（```）里的 # 不算标题。一行就超长的（没换行的长段落、压成一行的代码）硬切。"""
    parts: list[Part] = []
    cur: list[str] = []
    size = first = last = 0
    heading = start_heading = ""
    fence: str | None = None

    def flush() -> None:
        nonlocal size
        if cur:
            if kind == MARKDOWN:
                title = start_heading or f"第 {len(parts) + 1} 段"
            else:
                title = f"第 {first}–{last} 行" if last > first else f"第 {first} 行"
            parts.append(Part(title, "".join(cur)))
        cur.clear()
        size = 0

    def add(piece: str, no: int) -> None:
        nonlocal size, first, last, start_heading
        if not cur:
            first, start_heading = no, heading
        cur.append(piece)
        size += len(piece)
        last = no

    for no, line in enumerate(text.splitlines(keepends=True), 1):
        level = 0
        if kind == MARKDOWN:
            if fence:
                if line.lstrip().startswith(fence):
                    fence = None
            elif f := _FENCE.match(line):
                fence = f.group(1)[:3]
            elif m := _HEADING.match(line):
                level = len(m.group(1))
        if cur and (level and level <= 2 and size >= PART_CHARS // 4 or size + len(line) > PART_CHARS):
            flush()
        if level:
            heading = line.lstrip("#").strip()
        while len(line) > PART_CHARS:
            add(line[:PART_CHARS], no)
            flush()
            line = line[PART_CHARS:]
        add(line, no)
    flush()
    return parts or [Part("第 1 段" if kind == MARKDOWN else "第 1 行", "")]


def outline(text: str) -> list[str]:
    """文档的目录（一到三级标题），代码块里的不算。"""
    out, fence = [], None
    for line in text.splitlines():
        if fence:
            if line.lstrip().startswith(fence):
                fence = None
        elif f := _FENCE.match(line):
            fence = f.group(1)[:3]
        elif (m := _HEADING.match(line)) and len(m.group(1)) <= 3:
            out.append("  " * (len(m.group(1)) - 1) + line.lstrip("#").strip())
    return out


class EditError(Exception):
    """按片段改没改成：第几段（从 1 数）、old_text 出现了几次（0 是没找到；空的 old_text 是 -1）。"""

    def __init__(self, n: int, count: int) -> None:
        super().__init__(f"edit {n}: old_text occurs {count} times")
        self.n, self.count = n, count


class Edit(NamedTuple):
    old_text: str
    new_text: str


def apply_edits(text: str, edits: list[Edit]) -> str:
    """按先后改：每段 old_text 在（改到这一段时的）全文里要恰好出现一次，换成 new_text。有一段对不上就抛 EditError，一段都不改。"""
    for n, e in enumerate(edits, 1):
        if not e.old_text:
            raise EditError(n, -1)
        count = text.count(e.old_text)
        if count != 1:
            raise EditError(n, count)
        text = text.replace(e.old_text, e.new_text, 1)
    return text
