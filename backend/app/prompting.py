"""给模型看的文字放在 toml 里（一份，或者一个目录里的几份），这里只负责读取和渲染。

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
    """path 是一份 toml，或者放着几份 toml 的目录：目录里的合成一份读，同一个键在两份里都有直接报错。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.texts = cache(self._texts)
        self._template = cache(self._compile)

    def _texts(self) -> dict[str, str]:
        """{"段.键": 原文}。"""
        files = sorted(self.path.glob("*.toml")) if self.path.is_dir() else [self.path]
        if not files:
            raise FileNotFoundError(f"{self.path} 里没有 toml")
        texts: dict[str, str] = {}
        where: dict[str, str] = {}
        for file in files:
            with file.open("rb") as f:
                for key, text in self._flatten(tomllib.load(f), file.name):
                    if key in texts:
                        raise ValueError(f"{key} 在 {where[key]} 和 {file.name} 里都有")
                    texts[key], where[key] = text, file.name
        return texts

    def _flatten(self, table: dict, name: str, prefix: str = "") -> Iterator[tuple[str, str]]:
        for k, v in table.items():
            key = prefix + k
            if isinstance(v, dict):
                yield from self._flatten(v, name, key + ".")
            elif isinstance(v, str):
                yield key, v
            else:
                raise TypeError(f"{name} 里 {key} 不是字符串")

    def _compile(self, key: str) -> Template:
        try:
            return ENV.from_string(self.texts()[key])
        except KeyError:
            raise KeyError(f"{self.path.name} 里没有 {key}") from None

    def render(self, key: str, **values) -> str:
        return self._template(key).render(**values)
