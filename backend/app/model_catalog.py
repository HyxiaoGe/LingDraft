"""模型选项：用户在列表里选的选项、内部用途各用哪个模型（正本是 app/models.toml，说明也在那里）。

这里只管"选哪个"：对话定下选项以后，每一轮开始时由 Catalog.plan 定这一轮主 agent、子 agent 用的模型（见 graph.research.agent 的
TurnMiddleware），记进这一轮的记录；以后查权限、余额也加在这一处。模型怎么建在 research_llm.make_chat_model。
各模型的价格也在 models.toml（[prices]、[peak]），Catalog.rate 查某一刻的单价，记账在 app.costs。
模型能做什么（窗口多大、能不能调工具、支不支持思考）看 LangChain 的 model.profile，这里不另记。
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PATH = Path(__file__).with_name("models.toml")
AUTO = "auto"
ROLES = ("title", "suggest", "probe", "summarize", "dream", "judge")
CHINA_TZ = timezone(timedelta(hours=8))  # 峰谷按北京时间
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


class Rate(BaseModel):
    """一组单价，元 / 百万 token：输入命中缓存的、没命中的、输出（含思考）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    cached: float = Field(ge=0)
    input: float = Field(ge=0)
    output: float = Field(ge=0)

    def yuan(self, input_tokens: int, cached_tokens: int, output_tokens: int) -> float:
        """一次调用多少元。input_tokens 是整个输入（含命中缓存的，LangChain 的 usage_metadata 就是这样记的）。"""
        return (cached_tokens * self.cached + (input_tokens - cached_tokens) * self.input + output_tokens * self.output) / 1_000_000


class Promo(Rate):
    until: datetime = Field(description="这一刻之前按优惠价")

    @field_validator("until")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("优惠的 until 要写时区，比如 2026-12-31T23:59:59+08:00")
        return v


class Tier(Rate):
    above_input: int = Field(gt=0, description="这次调用的输入超过这么多 token 按这一档")


class Price(BaseModel):
    """一个模型的价格（models.toml 的 [prices."厂商:模型"]）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    regular: Rate
    off_peak: Rate | None = None
    promo: Promo | None = None
    tiers: list[Tier] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> Price:
        if self.tiers and (self.off_peak or self.promo):
            raise ValueError("分档和峰谷、优惠写在了一起：怎么叠还没定")
        if [t.above_input for t in self.tiers] != sorted({t.above_input for t in self.tiers}):
            raise ValueError("分档要按 above_input 从小到大写，不能重复")
        return self


class Peak(BaseModel):
    """什么时候是高峰（models.toml 的 [peak]，现在只有 DeepSeek 分峰谷）：北京时间工作日的这几段，法定节假日除外。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    hours: list[tuple[int, int]] = Field(description="几点到几点，前面的含、后面的不含")
    holidays: list[date] = Field(default_factory=list, description="法定节假日放假的日子")
    holidays_through: date = Field(description="节假日表填到哪天")

    def busy(self, at: datetime) -> bool:
        t = at.astimezone(CHINA_TZ)
        if t.weekday() >= 5 or t.date() in self.holidays:
            return False
        return any(a <= t.hour < b for a, b in self.hours)


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
    peak: Peak
    prices: dict[str, Price]

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
        used = {r for o in self.options if not o.status for r in o.refs()} | {r.model for r in self.roles.values()}
        if missing := sorted(used - set(self.prices)):
            raise ValueError(f"这些模型没填价格（models.toml 的 [prices]）：{missing}")
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

    def rate(self, ref: str, at: datetime, input_tokens: int) -> tuple[str, Rate] | None:
        """这个模型在 at 这一刻、输入这么多 token 的一次调用按哪组单价：返回（哪一种, 单价），没填价格的是 None。
        先看优惠（没过期的），再看峰谷，再看分档，都不是就是常规价。"""
        p = self.prices.get(ref)
        if p is None:
            return None
        if p.promo and at < p.promo.until:
            return "promo", p.promo
        if p.off_peak and not self.peak.busy(at):
            return "off_peak", p.off_peak
        for t in reversed(p.tiers):
            if input_tokens > t.above_input:
                return f"above_{t.above_input}", t
        return "regular", p.regular

    def stale(self, ref: str, at: datetime) -> list[str]:
        """价格表里该更新的（记账时打警告）：优惠过期了还没删、峰谷要用的节假日表没填到这一天。"""
        p = self.prices.get(ref)
        out = []
        if p and p.promo and at >= p.promo.until:
            out.append(f"{ref} 的优惠 {p.promo.until.isoformat()} 已经过期，按常规价算了：models.toml 里删掉它")
        if p and p.off_peak and at.astimezone(CHINA_TZ).date() > self.peak.holidays_through:
            out.append(f"节假日表只填到 {self.peak.holidays_through}，{ref} 的峰谷可能算错：models.toml 的 [peak] 补上新一年的放假安排")
        return out

    def role(self, name: str) -> ModelUse:
        r = self.roles[name]
        return ModelUse(ref=r.model, thinking=r.thinking, effort=r.effort if r.thinking else "")


def load(path: Path = PATH) -> Catalog:
    """读模型选项，写错了（引用的模型格式不对、少了内部用途、第一个不是 auto……）直接报错。"""
    return Catalog.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))
