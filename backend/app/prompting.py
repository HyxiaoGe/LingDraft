"""给模型看的文字放在 toml 里，这里只负责读取和渲染。

模板是 Jinja2：变量缺了直接报错（StrictUndefined），不会悄悄渲染成空；不转义，原样给模型。
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterator
from functools import cache
from pathlib import Path

from jinja2 import Environment, StrictUndefined, Template

ENV = Environment(undefined=StrictUndefined, keep_trailing_newline=True, autoescape=False)


class PromptFile:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.texts = cache(self._texts)
        self._template = cache(self._compile)

    def _texts(self) -> dict[str, str]:
        """{"段.键": 原文}。"""
        with self.path.open("rb") as f:
            return dict(self._flatten(tomllib.load(f)))

    def _flatten(self, table: dict, prefix: str = "") -> Iterator[tuple[str, str]]:
        for k, v in table.items():
            key = prefix + k
            if isinstance(v, dict):
                yield from self._flatten(v, key + ".")
            elif isinstance(v, str):
                yield key, v
            else:
                raise TypeError(f"{self.path.name} 里 {key} 不是字符串")

    def _compile(self, key: str) -> Template:
        try:
            return ENV.from_string(self.texts()[key])
        except KeyError:
            raise KeyError(f"{self.path.name} 里没有 {key}") from None

    def render(self, key: str, **values) -> str:
        return self._template(key).render(**values)
