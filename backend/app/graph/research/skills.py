"""调研技能：按问题类型准备的调研方法和报告结构，文件格式沿用 Agent Skills（agentskills.io）。

目录约定：skills/<name>/SKILL.md，frontmatter 写 name（和目录同名）、description（用途，模型靠它判断用不用）、
metadata.title（页面上显示的中文名），正文是调研方法；references/report.md 是报告结构和论断写法的示例。

agent 的系统提示词里只列目录（名字和用途），模型判断问题属于哪一类后调用 load_skill 读方法和报告结构；
整理和重写拿到同一个技能的报告结构。技能文件随代码发布，写坏了直接报错，不跳过。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import yaml

SKILLS_ROOT = Path(__file__).with_name("skills")
SKILL_FILE = "SKILL.md"
REPORT_FILE = Path("references") / "report.md"
MAX_DESCRIPTION = 1024
_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)


class SkillError(ValueError):
    pass


@dataclass(frozen=True)
class Skill:
    name: str
    title: str
    description: str
    method: str  # SKILL.md 正文
    report: str  # 报告结构和论断写法

    def content(self) -> str:
        """load_skill 返回给 agent 的：方法在前，报告结构在后。"""
        return f"{self.method.strip()}\n\n{self.report.strip()}\n"


def _load(directory: Path) -> Skill:
    m = _FRONTMATTER.match((directory / SKILL_FILE).read_text(encoding="utf-8"))
    if not m:
        raise SkillError(f"{directory.name}/{SKILL_FILE} 缺少 --- 包起来的 frontmatter")
    meta = yaml.safe_load(m.group(1)) or {}
    name, description = meta.get("name"), meta.get("description")
    title = (meta.get("metadata") or {}).get("title")
    if name != directory.name or not isinstance(name, str) or not _NAME.match(name):
        raise SkillError(f"{directory.name}：name 要和目录同名，只用小写字母、数字和连字符")
    if not isinstance(description, str) or not description.strip() or len(description) > MAX_DESCRIPTION:
        raise SkillError(f"{name}：description 不能为空，最长 {MAX_DESCRIPTION} 字")
    if not isinstance(title, str) or not title.strip():
        raise SkillError(f"{name}：缺少 metadata.title")
    report = directory / REPORT_FILE
    if not report.is_file():
        raise SkillError(f"{name}：缺少 {REPORT_FILE}")
    return Skill(name=name, title=title.strip(), description=description.strip(), method=m.group(2),
                 report=report.read_text(encoding="utf-8"))


@cache
def catalog(root: Path = SKILLS_ROOT) -> dict[str, Skill]:
    return {d.name: _load(d) for d in sorted(root.iterdir()) if (d / SKILL_FILE).is_file()}


def catalog_text() -> str:
    """系统提示词里的技能目录：一行一个。"""
    return "\n".join(f"- {s.name}（{s.title}）：{s.description}" for s in catalog().values())
