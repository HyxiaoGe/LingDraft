"""模型选项：用户在列表里选的选项、内部用途各用哪个模型（正本是 app/models.toml，说明也在那里）。

这里只管"选哪个"：对话定下选项以后，每一轮开始时由 Catalog.plan 定这一轮主 agent、子 agent 用的模型（见 graph.research.agent 的
TurnMiddleware），记进这一轮的记录；以后查权限、余额也加在这一处。模型怎么建在 research_llm.make_chat_model。
模型能做什么（窗口多大、能不能调工具、支不支持思考）看 LangChain 的 model.profile，这里不另记。
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PATH = Path(__file__).with_name("models.toml")
AUTO = "auto"
ROLES = ("title", "suggest", "probe", "summarize", "dream", "judge")
# 这次改动（2026-11-05）之前的轮没记模型：都是它答的，思考 high（dev 和本机的 .env 都没设过 RESEARCH_PROVIDER、RESEARCH_MODEL 这几个）
LEGACY_MODEL = "deepseek:deepseek-v4-pro"


class ModelUse(BaseModel):
    """一次用哪个模型：厂商:模型、开不开思考、思考强度（空是厂商默认）。记进每轮的记录，按它建模型（同样的三样共用一个）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    ref: str
    thinking: bool
    effort: str = ""


LEGACY_USE = ModelUse(ref=LEGACY_MODEL, thinking=True, effort="high")


class Subagent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str
    effort: str = ""


class Option(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    description: str = ""
    model: str
    thinking: Literal["toggle", "always", "never"] = "toggle"
    effort: str = ""
    subagent: Subagent | None = None
    status: Literal["", "hidden", "retired"] = ""

    def refs(self) -> tuple[str, str]:
        return self.model, (self.subagent.model if self.subagent else self.model)


class Role(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str
    thinking: bool = False
    effort: str = ""


class Plan(BaseModel):
    """一轮用的模型：选项（Auto 换进来的是 auto）、主 agent、子 agent。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    option: str
    agent: ModelUse
    subagent: ModelUse


class Catalog(BaseModel):
    model_config = ConfigDict(extra="forbid")
    options: list[Option] = Field(min_length=1)
    roles: dict[str, Role]

    @model_validator(mode="after")
    def _check(self) -> Catalog:
        ids = [o.id for o in self.options]
        if len(set(ids)) != len(ids):
            raise ValueError(f"选项 id 重复了：{sorted({i for i in ids if ids.count(i) > 1})}")
        if ids[0] != AUTO or self.options[0].status:
            raise ValueError("第一个选项要是 auto，而且不能停用")
        if set(self.roles) != set(ROLES):
            raise ValueError(f"内部用途要正好是这几样：{ROLES}，现在是 {sorted(self.roles)}")
        for ref in self.refs():
            provider, _, name = ref.partition(":")
            if not provider or not name:
                raise ValueError(f"模型要写成 厂商:模型：{ref!r}")
        return self

    def refs(self) -> set[str]:
        """用到的所有模型。"""
        return {r for o in self.options for r in o.refs()} | {r.model for r in self.roles.values()}

    def option(self, option_id: str) -> Option | None:
        return next((o for o in self.options if o.id == option_id), None)

    def offered(self, usable: Callable[[str], bool]) -> list[Option]:
        """新对话能选的：没停用、没藏起来、配了 key 的。"""
        return [o for o in self.options if not o.status and all(usable(r) for r in o.refs())]

    def plan(self, option_id: str | None, thinking: bool | None, usable: Callable[[str], bool]) -> Plan:
        """这一轮用哪个模型。option_id 是对话定下的选项（没定的是 Auto）；thinking 是用户这句话的思考开关（没给按选项的默认）。
        选项不在了、厂商停了（retired）、没配 key 的，改用 Auto（2026-11-05 Sean 定）。"""
        o = self.option(option_id or AUTO)
        if o is None or o.status == "retired" or not all(usable(r) for r in o.refs()):
            o = self.options[0]
        chosen = o.id
        if chosen == AUTO:
            o = self._auto(o)
        on = {"always": True, "never": False}.get(o.thinking, thinking if thinking is not None else True)
        sub = o.subagent or Subagent(model=o.model, effort=o.effort)
        return Plan(option=chosen, agent=ModelUse(ref=o.model, thinking=on, effort=o.effort if on else ""),
                    subagent=ModelUse(ref=sub.model, thinking=on, effort=sub.effort if on else ""))

    def _auto(self, auto: Option) -> Option:
        """Auto 的策略：现在就是 models.toml 里给它配的模型。以后按问题选模型改这里（Sean：用户多半不改默认，Auto 是降成本的地方）。"""
        return auto

    def role(self, name: str) -> ModelUse:
        r = self.roles[name]
        return ModelUse(ref=r.model, thinking=r.thinking, effort=r.effort if r.thinking else "")


def load(path: Path = PATH) -> Catalog:
    """读模型选项，写错了（引用的模型格式不对、少了内部用途、第一个不是 auto……）直接报错。"""
    return Catalog.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))
