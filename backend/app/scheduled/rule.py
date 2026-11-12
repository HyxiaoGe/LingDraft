"""定时任务什么时候跑。都按北京时间（没有夏令时，按天数着算就行）。

重复的：5 段的 cron 表达式（分 时 日 月 周），认 *、单个数、a-b、a,b、*/n、a-b/n；周是 0–7（0 和 7 都是周日）。
日和周都写了的，照 cron 的老规矩：对上一个就算。不认 L、W、#、月份和星期的英文名。
分钟那一段只能是一个数：一小时里最多跑一次（2026-11-12 定最密每小时一次）。
只跑一次的：一个时间点（没带时区的按北京时间）。

页面上怎么说（describe）：都写成中文（“每天 · 07:22”“工作日 · 09:00–18:00 每小时”），不把表达式给人看。
没用现成的 cron 库：要的只是“下一次是什么时候”，几十行；依赖里没有（croniter 这类），不为这个加一个。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

CHINA_TZ = ZoneInfo("Asia/Shanghai")
BAD_CRON, TOO_OFTEN, NEVER, BAD_TIME = "bad_cron", "too_often", "never_runs", "bad_time"
_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_LOOKAHEAD_DAYS = 366 * 8 + 2  # 2 月 29 日最远隔 8 年（跨过不闰的整百年）
WEEKDAYS = "一二三四五六日"


class BadRule(ValueError):
    """规则写得不对：code 是哪一种（BAD_CRON、TOO_OFTEN、NEVER、BAD_TIME），调用的地方按它换成给人、给模型看的话。"""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass(frozen=True)
class Cron:
    minute: int
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 周一是 0（和 date.weekday() 一样）
    any_day: bool  # 日那一段是 *
    any_weekday: bool  # 周那一段是 *

    def on(self, d: date) -> bool:
        if d.month not in self.months:
            return False
        if self.any_day or self.any_weekday:
            return (self.any_day or d.day in self.days) and (self.any_weekday or d.weekday() in self.weekdays)
        return d.day in self.days or d.weekday() in self.weekdays


def _field(text: str, lo: int, hi: int) -> frozenset[int]:
    out: set[int] = set()
    for part in text.split(","):
        body, _, step = part.partition("/")
        if "/" in part and not step.isdigit():
            raise BadRule(BAD_CRON, part)
        n = int(step) if step else 1
        if body == "*":
            a, b = lo, hi
        else:
            first, dash, last = body.partition("-")
            if not first.isdigit() or (dash and not last.isdigit()):
                raise BadRule(BAD_CRON, part)
            a = int(first)
            b = int(last) if dash else (hi if step else a)
        if n < 1 or not lo <= a <= b <= hi:
            raise BadRule(BAD_CRON, part)
        out.update(range(a, b + 1, n))
    return frozenset(out)


def parse_cron(expr: str) -> Cron:
    parts = expr.split()
    if len(parts) != 5:
        raise BadRule(BAD_CRON, expr)
    if not parts[0].isdigit():
        if set(parts[0]) <= set("0123456789*,-/"):  # 写法是对的，只是一小时里不止一次
            _field(parts[0], *_RANGES[0])
            raise BadRule(TOO_OFTEN, parts[0])
        raise BadRule(BAD_CRON, parts[0])
    minute, hours, days, months, weekdays = (_field(p, lo, hi) for p, (lo, hi) in zip(parts, _RANGES, strict=True))
    return Cron(next(iter(minute)), hours, days, months, frozenset((d - 1) % 7 for d in weekdays), parts[2] == "*", parts[4] == "*")


def normalize(expr: str) -> str:
    """存进库的写法：段之间一个空格。写得不对抛 BadRule；永远对不上日子的（比如 2 月 31 日）也算不对。"""
    expr = " ".join(expr.split())
    if next_run(expr, datetime.now(CHINA_TZ)) is None:
        raise BadRule(NEVER, expr)
    return expr


def next_run(expr: str, after: datetime) -> datetime | None:
    """after 之后（不含）下一次是什么时候；永远对不上的是 None。"""
    cron = parse_cron(expr)
    start = after.astimezone(CHINA_TZ).replace(second=0, microsecond=0) + timedelta(minutes=1)
    day = start.date()
    for _ in range(_LOOKAHEAD_DAYS):
        if cron.on(day):
            for h in sorted(cron.hours):
                at = datetime.combine(day, time(h, cron.minute), CHINA_TZ)
                if at >= start:
                    return at
        day += timedelta(days=1)
    return None


def parse_once(text: str) -> datetime:
    """只跑一次的时间点：2026-11-13 09:00、2026-11-13T09:00:00+08:00 这些；没带时区的按北京时间。"""
    try:
        at = datetime.fromisoformat(text.strip())
    except ValueError:
        raise BadRule(BAD_TIME, text) from None
    return (at.replace(tzinfo=CHINA_TZ) if at.tzinfo is None else at.astimezone(CHINA_TZ)).replace(second=0, microsecond=0)


def _hhmm(h: int, m: int) -> str:
    return f"{h:02d}:{m:02d}"


def _weekly(c: Cron) -> str:
    if c.weekdays == frozenset(range(5)):
        return "工作日"
    if c.weekdays == frozenset({5, 6}):
        return "周末"
    return "每周" + "、".join(WEEKDAYS[d] for d in sorted(c.weekdays))


def _days(c: Cron) -> str:
    """哪些日子：每天、工作日、每周一、每月 1、15 号、每年 6 月的工作日……日和周都写了的是“或”（对上一个就算）。"""
    by_day, by_week = not c.any_day and len(c.days) < 31, not c.any_weekday and len(c.weekdays) < 7
    if not (c.any_day or c.any_weekday) and not (by_day and by_week):
        by_day = by_week = False  # 两段都写了、其中一段是全的：对上一个就算，也就是每天
    nums = "、".join(str(d) for d in sorted(c.days))
    if len(c.months) == 12:
        return f"每月 {nums} 号或{_weekly(c)}" if by_day and by_week else f"每月 {nums} 号" if by_day else _weekly(c) if by_week else "每天"
    year = f"每年 {'、'.join(str(m) for m in sorted(c.months))} 月"
    if by_day:
        return f"{year} {nums} 号" + (f"或{_weekly(c)}" if by_week else "")
    return f"{year}的{_weekly(c)}" if by_week else f"{year}每天"


def _times(c: Cron) -> tuple[str, str]:
    """一天里的哪些时间：（接在日子后面的“每小时”“每 2 小时”，没有是空的；点后面的那一截）。"""
    hours = sorted(c.hours)
    first, last = _hhmm(hours[0], c.minute), _hhmm(hours[-1], c.minute)
    steps = {b - a for a, b in zip(hours, hours[1:], strict=False)}
    if len(hours) == 24:
        return "每小时", f"第 {c.minute} 分"
    if len(hours) > 3 and len(steps) == 1:  # 隔得一样远的一串
        n = steps.pop()
        if n > 1 and hours[0] < n and hours[-1] + n > 23:  # 一整天都在跑
            return f"每 {n} 小时", f"{first} 起"
        return "", f"{first}–{last} 每小时" if n == 1 else f"{first}–{last} 每 {n} 小时"
    return "", "、".join(_hhmm(h, c.minute) for h in hours)


def describe(cron: str | None, once_at: datetime | None) -> str:
    """页面上那一行“每天 · 07:22”：都说成中文，不把表达式给人看（模型、表单里“自定义”建的可以是任何写法）。"""
    if cron is None:
        return f"仅一次 · {once_at.astimezone(CHINA_TZ):%Y-%m-%d %H:%M}" if once_at else ""
    c = parse_cron(cron)
    days, (often, times) = _days(c), _times(c)
    if often:
        return f"{'' if days == '每天' else days}{often} · {times}"
    return f"{days} · {times}"
