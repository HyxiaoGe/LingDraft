"""调研链路给模型看的文字都在 prompts.toml，这里只负责读取和渲染。

模板是 Jinja2：变量缺了直接报错（StrictUndefined），不会悄悄渲染成空；不转义，原样给模型。
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterator
from functools import cache
from pathlib import Path

from jinja2 import Environment, StrictUndefined, Template

PROMPTS_FILE = Path(__file__).with_name("prompts.toml")

_env = Environment(undefined=StrictUndefined, keep_trailing_newline=True, autoescape=False)


def _flatten(table: dict, prefix: str = "") -> Iterator[tuple[str, str]]:
    for k, v in table.items():
        key = prefix + k
        if isinstance(v, dict):
            yield from _flatten(v, key + ".")
        elif isinstance(v, str):
            yield key, v
        else:
            raise TypeError(f"{PROMPTS_FILE.name} 里 {key} 不是字符串")


@cache
def texts() -> dict[str, str]:
    """{"段.键": 原文}。"""
    with PROMPTS_FILE.open("rb") as f:
        return dict(_flatten(tomllib.load(f)))


@cache
def _template(key: str) -> Template:
    try:
        return _env.from_string(texts()[key])
    except KeyError:
        raise KeyError(f"{PROMPTS_FILE.name} 里没有 {key}") from None


def render(key: str, **values) -> str:
    return _template(key).render(**values)
