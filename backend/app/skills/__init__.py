"""技能（Agent Skills，格式照 https://agentskills.io/specification）：这个目录下一个子目录一个技能，
SKILL.md 开头的 YAML 写名字和什么时候用，后面是给模型看的说明（2026-11-14 Sean 定第一批放数据分析三个，方案见 docs/Skills方案.md）。

给模型分两层：
- 目录（名字 + 什么时候用）拼在系统提示词里（agent.toml 的 skills 一段），每次调模型都带着；
- 说明正文等模型调 load_skill 才给（见 app.tools.skills）。

比标准多的两项都放在 metadata 里（标准留给接入方自己用的键值）：
- title：页面上显示的中文名；
- requires：要有哪个工具才用得上。现在只认 run_command：不能跑命令的对话（普通对话、游客、没配沙箱）不给这个技能。

SKILL.md 写错了启动时就报错（建工具时读一遍），不等到模型去读的那一刻。各个技能从哪儿来、改了什么见本目录的 README.md。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import yaml

ROOT = Path(__file__).parent
FILE = "SKILL.md"
MAX_NAME = 64
MAX_DESCRIPTION = 1024
MAX_BODY = 20_000  # 字符。标准建议正文 5000 token 以内：一读就是整篇进上下文
KEYS = frozenset({"name", "description", "license", "compatibility", "metadata", "allowed-tools"})  # 标准里有的；别的多半是写错了
RUN = "run_command"
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)
_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


class SkillError(ValueError):
    """SKILL.md 不合格式。"""


@dataclass(frozen=True)
class Skill:
    name: str
    title: str  # 页面上显示的中文名
    description: str  # 给模型看：做什么、什么时候用（目录里的那一行）
    body: str  # 说明正文，模型读了才给
    needs_run: bool  # 要能跑命令才用得上


def load(directory: Path) -> Skill:
    """读一个技能目录。不合格式的直接报错，说清是哪个技能、哪儿不对。"""
    path = directory / FILE

    def bad(why: str) -> SkillError:
        return SkillError(f"技能 {directory.name} 的 {FILE} {why}")

    if not path.is_file():
        raise bad("不存在")
    m = _FRONTMATTER.match(path.read_text(encoding="utf-8").replace("\r\n", "\n"))
    if m is None:
        raise bad("开头没有用 --- 围起来的 YAML")
    try:
        meta = yaml.safe_load(m.group(1))
    except yaml.YAMLError as e:
        raise bad(f"开头的 YAML 解析不了：{e}") from e
    if not isinstance(meta, dict):
        raise bad("开头的 YAML 不是键值")
    if extra := sorted(set(map(str, meta)) - KEYS):
        raise bad(f"开头有标准里没有的键：{'、'.join(extra)}")
    name, description, body = meta.get("name"), meta.get("description"), m.group(2).strip()
    if not isinstance(name, str) or not _NAME.fullmatch(name) or len(name) > MAX_NAME:
        raise bad(f"的 name 不对（小写字母、数字、连字符，最长 {MAX_NAME}）：{name!r}")
    if name != directory.name:
        raise bad(f"的 name（{name}）和目录名不一样")
    if not isinstance(description, str) or not description.strip():
        raise bad("没有 description")
    if len(description.strip()) > MAX_DESCRIPTION:
        raise bad(f"的 description 超过 {MAX_DESCRIPTION} 个字符")
    if not body:
        raise bad("没有正文")
    if len(body) > MAX_BODY:
        raise bad(f"的正文超过 {MAX_BODY} 个字符（{len(body)}）")
    extras = meta.get("metadata") or {}
    if not isinstance(extras, dict) or not all(isinstance(v, str) for v in extras.values()):
        raise bad("的 metadata 要是字符串到字符串的键值")
    title = extras.get("title", "").strip()
    if not title:
        raise bad("的 metadata 里没有 title（页面上显示的中文名）")
    requires = extras.get("requires", "")
    if requires not in ("", RUN):
        raise bad(f"的 metadata.requires 只认 {RUN}：{requires!r}")
    return Skill(name=name, title=title, description=" ".join(description.split()), body=body, needs_run=requires == RUN)


@cache
def catalog(root: Path = ROOT) -> tuple[Skill, ...]:
    """全部技能，按名字排（顺序固定，系统提示词里那一段每次拼出来的一样）。"""
    return tuple(load(d) for d in sorted(root.iterdir()) if d.is_dir() and not d.name.startswith(("_", ".")))


def title_of(name: str) -> str:
    """页面上显示的名字；目录里没有的（模型写错了名字、技能后来删了）就是原样。"""
    return next((s.title for s in catalog() if s.name == name), name)
