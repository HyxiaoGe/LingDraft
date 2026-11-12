"""定时任务跑完以后通知里那两行：回答的开头，去掉 Markdown 的记号和引用编号，压成一段话。
页面右上角的通知、系统通知都直接用它（见 api.research_runner._anotify；页面上只管截成两行）。
"""

from __future__ import annotations

import re

from app.graph.research.sources import SOURCE_ID

OPENING_MAX = 120

_FENCE = re.compile(r"^[ \t]*(?:```|~~~).*$", re.M)  # 代码块的围栏那一行（里面的代码留着）
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")  # 链接只留字
# 分隔线、表格里表头下面那一行
_RULE = re.compile(r"^[ \t]*(?:\|?[ \t]*:?-{2,}:?[ \t]*)+\|?[ \t]*$|^[ \t]*(?:[*_][ \t]*){3,}$", re.M)
_LINE_MARK = re.compile(r"^[ \t]*(?:#{1,6}[ \t]+|>[ \t]?|[-*+][ \t]+)", re.M)  # 行首的标题、引用、列表记号（“1.” 这样的编号留着）
_EMPHASIS = re.compile(r"\*\*|__|~~|`")
_STAR = re.compile(r"(?<![\w*])\*(\S(?:[^*\n]*\S)?)\*(?![\w*])")  # *斜体*


def opening(text: str, limit: int = OPENING_MAX) -> str:
    text = SOURCE_ID.sub("", text)
    for pattern, to in ((_FENCE, ""), (_IMAGE, ""), (_LINK, r"\1"), (_RULE, ""), (_LINE_MARK, ""), (_EMPHASIS, ""), (_STAR, r"\1")):
        text = pattern.sub(to, text)
    text = " ".join(text.replace("|", " ").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"
