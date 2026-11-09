"""切块和分词。

- 切块：解析出来的段（约 4000 字，Word、Markdown 按标题分，PDF 按页分）再切成约 800 字、重叠 100 字的块，优先在空行、换行、句号、分号处断
  （11-09 实验：800 字一块比整段好）。算向量时块前面加“文件名 · 章节标题”。
- 分词：jieba，在 Python 里分好再交给 Postgres 的 simple 配置（CI 的库里没有 pg_jieba）。块按搜索引擎模式分（长词也拆出短词），
  查询的词之间是“或”（“所有词都要出现”36 题只中 2 个）。只剩一个字的、不是字母数字汉字的去掉：单字多是虚词，“或”起来什么都中。
"""

from __future__ import annotations

import functools
import logging
import re
from typing import NamedTuple

from app.files.parse import Part

CHUNK_CHARS = 800
CHUNK_OVERLAP = 100
SEPARATORS = ["\n\n", "\n", "。", "；", "，", " ", ""]
QUERY_TERMS = 32  # 查询最多取这么多个词
_WORD = re.compile(r"\w+")


class Chunk(NamedTuple):
    part: int  # 第几段（从 1 起，对应 file_parts）
    idx: int  # 段里第几块（从 0 起）
    title: str  # 段的标题（章节名、“第 3 页”）
    content: str


@functools.cache
def _splitter():
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    return RecursiveCharacterTextSplitter(chunk_size=CHUNK_CHARS, chunk_overlap=CHUNK_OVERLAP, separators=SEPARATORS, keep_separator="end")


def chunks(parts: list[Part]) -> list[Chunk]:
    out = []
    for n, p in enumerate(parts, 1):
        for k, t in enumerate(t for t in _splitter().split_text(p.text) if t.strip()):
            out.append(Chunk(n, k, p.title, t.strip()))
    return out


def head(name: str, title: str) -> str:
    return f"{name} · {title}" if title else name


@functools.cache
def _jieba():
    import jieba

    jieba.setLogLevel(logging.WARNING)
    jieba.initialize()
    return jieba


def _words(tokens) -> list[str]:
    out = []
    for t in tokens:
        out += [w for w in _WORD.findall(t.lower()) if len(w) > 1]
    return out


def doc_terms(text: str) -> str:
    """块的词，空格隔开（写进 tsvector）。"""
    return " ".join(_words(_jieba().cut_for_search(text)))


def query_terms(query: str) -> list[str]:
    """查询的词，去重、按出现的先后。"""
    return list(dict.fromkeys(_words(_jieba().cut_for_search(query))))[:QUERY_TERMS]
