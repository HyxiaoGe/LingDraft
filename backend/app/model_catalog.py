"""模型选项：用户在列表里选的选项、内部用途各用哪个模型（正本是 app/models.toml，说明也在那里）。

这里只管"选哪个"：对话定下选项以后，每一轮开始时由 Catalog.plan 定这一轮主 agent、子 agent 用的模型（见 graph.research.agent 的
TurnMiddleware），记进这一轮的记录。Auto 第一句话按难易定一个模型、整段对话沿用（Catalog.pick，2026-11-08 Sean 定）：简单的先用各家的免费额度
（[[free]]），复杂的用 Auto 选项的模型；出问题换备用（[auto] fallback），见 graph.research.agent 的 ModelSelect。哪一档的人能选哪些选项看 Option.access（游客、登录用户、管理员，见 TIERS）；以后查余额也加在这一处。模型怎么建在 research_llm.make_chat_model。
各模型的价格也在 models.toml（[prices]、[peak]），Catalog.rate 查某一刻的单价，记账在 app.costs。
模型能做什么（窗口多大、能不能调工具、支不支持思考）看 LangChain 的 model.profile，这里不另记。
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

PATH = Path(__file__).with_name("models.toml")
AUTO = "auto"
ROLES = ("title", "suggest", "probe", "summarize", "dream", "judge", "route")
FREE_TIER = "free_tier"  # 免费额度的调用记账时的价格类型（见 Catalog.rate）
# 谁在用：游客（没登录也能聊）、登录用户、管理员，从低到高（2026-11-06 Sean 定，credits 的额度也按这三档）。选项的 access 是最低哪一档能选
TIERS = ("guest", "user", "admin")
Access = Literal["guest", "user", "admin"]
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
    access: Access = "user"  # 不写是要登录：新加的模型默认不给游客

    def refs(self) -> tuple[str, str]:
        return self.model, (self.subagent.model if self.subagent else self.model)

    def allows(self, tier: str) -> bool:
        """这一档的人能不能选它。"""
        return TIERS.index(tier) >= TIERS.index(self.access)


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


class Free(BaseModel):
    """一个模型的免费额度（models.toml 的 [[free]]）：Auto 判断为简单的对话按到期日从早到晚用。不记还剩多少，
    厂商报额度用完（model_guard.FreeTierUsedUp）就当用完了（见 Catalog.used_up）。
    same_as 给了的，是某个选项的模型托管在别家的同一个模型：用了那个模型的（选了那个选项、Auto 判为复杂的、子 agent、内部用途），
    先走这里的免费额度，用完、到期、出错再走直连；页面上还是那个选项，不标免费。这些 Auto 判为简单的不挑：大模型的额度留给选了它的
    （2026-11-08 Sean 定）；Flash 的是备用的，Auto 判为简单的挑不到别的时也就用上了。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    model: str
    name: str = Field(default="", description="页面上每轮开头显示的名字；有 same_as 的不写就用那个选项的")
    icon: str = Field(default="", description="页面上用哪家的图标（厂商前缀，比如 moonshot）；不写按 same_as 的厂商，没有 same_as 按 model 的（托管在百炼上的 Kimi 就要写）")
    until: date = Field(description="控制台写的到期日：这天北京时间 0 点起不再用（不赌控制台的日期算到当天几点）")
    same_as: str = Field(default="", description="直连的同一个模型（某个选项的 model，厂商:模型）；没有对应的选项就不写，只给 Auto 用")


class AutoRules(BaseModel):
    """Auto 怎么选（models.toml 的 [auto]），2026-11-08 Sean 定。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    fallback: str = Field(description="备用的选项 id：模型出问题换它；免费的都用不了时简单的对话也用它；游客的复杂对话也用它（游客不给 Pro）")


ZERO = Rate(cached=0, input=0, output=0)


class Plan(BaseModel):
    """一轮用的模型：选项（Auto 换进来的是 auto）、主 agent、子 agent。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    option: str
    agent: ModelUse
    subagent: ModelUse
    pick: str = Field(default="", description="走免费额度以前的模型（直连的）：Auto 的对话记下的是它，每轮再看免费额度还能不能用")


class Catalog(BaseModel):
    model_config = ConfigDict(extra="forbid")
    options: list[Option] = Field(min_length=1)
    roles: dict[str, Role]
    peak: Peak
    prices: dict[str, Price]
    tools: dict[str, float] = Field(default_factory=dict, description="工具每次调用多少元（[tools]，只算成功的）；没列的不收钱")
    auto: AutoRules
    free: list[Free] = Field(default_factory=list)
    _used_up: set[str] = PrivateAttr(default_factory=set)  # 报过额度用完的免费模型：只在进程内存里，重启后最多再撞一次就又记上

    @model_validator(mode="after")
    def _check(self) -> Catalog:
        ids = [o.id for o in self.options]
        if len(set(ids)) != len(ids):
            raise ValueError(f"选项 id 重复了：{sorted({i for i in ids if ids.count(i) > 1})}")
        if ids[0] != AUTO or self.options[0].status:
            raise ValueError("第一个选项要是 auto，而且不能停用")
        if self.options[0].access != "guest":
            raise ValueError("auto 要游客也能用（access = \"guest\"）：没选模型的、选的模型不在了的都用它")
        if set(self.roles) != set(ROLES):
            raise ValueError(f"内部用途要正好是这几样：{ROLES}，现在是 {sorted(self.roles)}")
        for ref in self.refs():
            provider, _, name = ref.partition(":")
            if not provider or not name:
                raise ValueError(f"模型要写成 厂商:模型：{ref!r}")
        used = {r for o in self.options if not o.status for r in o.refs()} | {r.model for r in self.roles.values()}
        if missing := sorted(used - set(self.prices)):
            raise ValueError(f"这些模型没填价格（models.toml 的 [prices]）：{missing}")
        if bad := sorted(t for t, y in self.tools.items() if y <= 0):
            raise ValueError(f"工具的价格要大于 0（不收钱的别列）：{bad}")
        fb = self.option(self.auto.fallback)
        if fb is None or fb.status or fb.access != "guest":
            raise ValueError(f"[auto] fallback 要是一个没停用、游客也能用的选项：{self.auto.fallback!r}")
        free = [f.model for f in self.free]
        if len(set(free)) != len(free):
            raise ValueError(f"[[free]] 里有重复的模型：{free}")
        paid = {r for o in self.options for r in o.refs()} | {r.model for r in self.roles.values()}
        if both := sorted(set(free) & paid):
            raise ValueError(f"免费额度的模型不能同时是选项、内部用途（记账会记成 0 元）：{both}")
        direct = {o.model for o in self.options if o.id != AUTO}
        if odd := sorted(f.same_as for f in self.free if f.same_as and f.same_as not in direct):
            raise ValueError(f"[[free]] 的 same_as 要是某个选项（auto 以外）的 model：{odd}")
        if nameless := sorted(f.model for f in self.free if not f.name and not f.same_as):
            raise ValueError(f"[[free]] 没有 same_as 的要写 name：{nameless}")
        return self

    def refs(self) -> set[str]:
        """用到的所有模型。"""
        return {r for o in self.options for r in o.refs()} | {r.model for r in self.roles.values()} | {f.model for f in self.free}

    def option(self, option_id: str) -> Option | None:
        return next((o for o in self.options if o.id == option_id), None)

    def offered(self, usable: Callable[[str], bool]) -> list[Option]:
        """新对话能选的：没停用、没藏起来、配了 key 的。"""
        return [o for o in self.options if not o.status and all(usable(r) for r in o.refs())]

    def plan(self, option_id: str | None, thinking: bool | None, usable: Callable[[str], bool], *, pick: str | None = None,
             now: datetime | None = None, free: bool = True) -> Plan:
        """这一轮用哪个模型。option_id 是对话定下的选项（没定的是 Auto）；thinking 是用户这句话的思考开关（没给按选项的默认）。
        选项不在了、厂商停了（retired）、没配 key 的，改用 Auto（2026-11-05 Sean 定）。
        pick 是 Auto 的对话定下的模型（"厂商:模型"，见 pick）：没给的按 Auto 选项的模型；给的免费模型到期了、用完了，改用备用。
        now 看免费额度到没到期，评测回放时按录像那时。
        定下的模型有免费额度（[[free]] 的 same_as）还能用的，先走免费额度，主 agent、子 agent 各看各的，Auto 判为复杂的也是；
        free=False 不走（换直连时）。Plan.pick 是主 agent 走免费额度以前的那个，Auto 的对话记下它当以后的 pick。"""
        o = self.option(option_id or AUTO)
        if o is None or o.status == "retired" or not all(usable(r) for r in o.refs()):
            o = self.options[0]
        chosen = o.id
        now = now or datetime.now(CHINA_TZ)
        if chosen == AUTO:
            o = self._auto(o, pick, usable, now)
        direct = o.model
        sub = o.subagent or Subagent(model=direct, effort=o.effort)
        agent = self.free_ref(direct, usable, now) if free else direct
        if free:
            sub = sub.model_copy(update={"model": self.free_ref(sub.model, usable, now)})
        on = {"always": True, "never": False}.get(o.thinking, thinking if thinking is not None else True)
        return Plan(option=chosen, agent=ModelUse(ref=agent, thinking=on, effort=o.effort if on else ""),
                    subagent=ModelUse(ref=sub.model, thinking=on, effort=sub.effort if on else ""), pick=direct)

    def _auto(self, auto: Option, pick: str | None, usable: Callable[[str], bool], now: datetime) -> Option:
        """Auto 这一轮用的选项：pick 是 Auto 选项、备用的模型就是它们；是还能用的免费模型就照它现建一个（子 agent 和备用的一样）；
        别的（免费的到期了、用完了、没配 key 了，models.toml 里删了）改用备用。"""
        fb = self.fallback()
        if pick is None or pick == auto.model:
            return auto
        if pick == fb.model:
            return fb
        f = next((f for f in self.free if f.model == pick), None)
        if f is None or not self.free_ok(f, usable, now):
            return fb
        return Option(id=AUTO, name=self.names()[f.model]["name"], model=f.model, subagent=fb.subagent or Subagent(model=fb.model, effort=fb.effort),
                      access="guest")

    def pick(self, simple: bool, guest: bool, usable: Callable[[str], bool], now: datetime | None = None) -> str:
        """Auto 的对话第一句话判断完难易，定下用哪个模型（之后整段对话沿用，2026-11-08 Sean 定）：
        简单的按到期日从早到晚挑第一个还能用的免费模型（有 same_as 的不挑），都用不了用备用；复杂的用 Auto 选项的模型，游客用备用（游客不给 Pro）。"""
        if simple:
            now = now or datetime.now(CHINA_TZ)
            for f in sorted(self.free, key=lambda f: f.until):
                if not f.same_as and self.free_ok(f, usable, now):
                    return f.model
            return self.fallback().model
        return self.fallback().model if guest else self.options[0].model

    def free_ref(self, direct: str, usable: Callable[[str], bool], now: datetime | None = None) -> str:
        """直连的这个模型这次走哪个：还能用的免费额度（same_as 是它的，有几个按到期日早的先用），没有就是它自己。"""
        now = now or datetime.now(CHINA_TZ)
        return next((f.model for f in sorted(self.free, key=lambda f: f.until) if f.same_as == direct and self.free_ok(f, usable, now)), direct)

    def fallback(self) -> Option:
        return self.option(self.auto.fallback)  # type: ignore[return-value]  校验过一定有

    def free_ok(self, f: Free, usable: Callable[[str], bool], now: datetime) -> bool:
        """这个免费额度还能用：没到期、没报过用完、配了 key。"""
        return now.astimezone(CHINA_TZ).date() < f.until and f.model not in self._used_up and usable(f.model)

    def is_free(self, ref: str) -> bool:
        return any(f.model == ref for f in self.free)

    def same_as(self, ref: str) -> str:
        """免费额度对应的直连模型（[[free]] 的 same_as）；不是免费额度、没有对应选项的是空的。"""
        return next((f.same_as for f in self.free if f.model == ref), "")

    def used_up(self, ref: str) -> None:
        """这个免费模型报了额度用完：之后的对话不再挑它，正在用它的对话下一次调模型改用备用。"""
        self._used_up.add(ref)

    def names(self) -> dict[str, dict[str, str]]:
        """选项以外、页面上可能出现的模型（免费额度的）：名字和图标（厂商前缀）。"""
        by_model = {o.model: o for o in self.options if o.id != AUTO}
        return {f.model: {"name": f.name or by_model[f.same_as].name, "icon": f.icon or (f.same_as or f.model).partition(":")[0]} for f in self.free}

    def rate(self, ref: str, at: datetime, input_tokens: int) -> tuple[str, Rate] | None:
        """这个模型在 at 这一刻、输入这么多 token 的一次调用按哪组单价：返回（哪一种, 单价），没填价格的是 None。
        先看优惠（没过期的），再看峰谷，再看分档，都不是就是常规价。"""
        if self.is_free(ref):
            return FREE_TIER, ZERO
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

    def role_uses(self, name: str, usable: Callable[[str], bool], now: datetime | None = None) -> list[ModelUse]:
        """内部用途这一次按顺序试的：同一个模型还能用的免费额度（见 free_ref），出问题再直连；没有免费额度的只有直连。"""
        use = self.role(name)
        ref = self.free_ref(use.ref, usable, now)
        return [use.model_copy(update={"ref": ref}), use] if ref != use.ref else [use]


def load(path: Path = PATH) -> Catalog:
    """读模型选项，写错了（引用的模型格式不对、少了内部用途、第一个不是 auto……）直接报错。"""
    return Catalog.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))
