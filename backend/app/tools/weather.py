"""查天气：经网关取和风天气的城市、逐日和逐小时预报，整理成一份 Forecast，页面把它画成天气卡片；
模型看的是逐日预报加逐小时的摘要（见 summarize_hourly）。

和风的城市查询是模糊匹配，随便写什么都会返回几个城市。照 Fusion 的做法挑（见 select_city）：
只认名字出现在地名里的候选；名字相同的，用地名里写到的省、市、国家区分；还分不出就把候选交给模型，
它按候选的写法再查一次，或者问用户。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta, timezone

from app.graph.research.state import Forecast, WeatherDay, WeatherHour
from app.tools.gateway import City, Day, GatewayClient, GatewayError, Hour

FORECAST_DAYS = 4
HOURLY_POINTS = 24
MAX_CANDIDATES = 5
FOREIGN_MAX_RANK = 80  # 国外的地方 rank 大于这个就不算：乱写的地名、同名小地方实测都在 83～85
CACHE_SECONDS = 30 * 60
SEP = "·"
DOMESTIC = "中国"
CHINA_TZ = timezone(timedelta(hours=8))
# 只用来从上级行政区的名字里取出常写的简称（浙江省 → 浙江），不判断意思
_ADMIN_SUFFIXES = ("特别行政区", "维吾尔自治区", "壮族自治区", "回族自治区", "自治区", "省", "市")


class Unresolved(Exception):
    """地名对不上和风的城市。kind：location_not_found 没找到；ambiguous_location 对得上好几个，candidates 是它们的写法。"""

    def __init__(self, kind: str, candidates: list[str] | None = None) -> None:
        super().__init__(kind)
        self.kind = kind
        self.candidates = candidates or []


def city_label(c: City) -> str:
    """候选的写法：国家（国内不写）·省·市·区县，相邻重复的只留一个。"""
    parts = ([c.country] if c.country and c.country != DOMESTIC else []) + [c.adm1, c.adm2, c.name]
    out: list[str] = []
    for p in parts:
        if p and (not out or out[-1] != p):
            out.append(p)
    return SEP.join(out)


def _short(name: str) -> str:
    for suffix in _ADMIN_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return name


def _hinted(c: City, query: str) -> bool:
    """地名里写到了这个候选的省、市或国家。"""
    return any(len(s := _short(p)) >= 2 and s != c.name and s in query for p in (c.adm1, c.adm2, c.country))


def select_city(query: str, cities: list[City]) -> City:
    candidates = [c for c in cities
                  if not (c.country and c.country != DOMESTIC and c.rank is not None and c.rank > FOREIGN_MAX_RANK)]
    for c in candidates:  # 模型照候选的写法再查
        if city_label(c) == query:
            return c
    matched = [c for c in candidates if c.name in query]
    if not matched:
        raise Unresolved("location_not_found")
    matched = [c for c in matched if _hinted(c, query)] or matched
    # 地名一般从大写到小：取名字在地名里结束得最靠后的，再取名字最长的（更具体）
    end = max(query.rfind(c.name) + len(c.name) for c in matched)
    matched = [c for c in matched if query.rfind(c.name) + len(c.name) == end]
    longest = max(len(c.name) for c in matched)
    unique = list({c.location_id: c for c in matched if len(c.name) == longest}.values())
    labels = list(dict.fromkeys(city_label(c) for c in unique))
    if len(labels) == 1:  # 写法完全一样的重复条目分不出、也不用分，取第一个
        return unique[0]
    raise Unresolved("ambiguous_location", labels[:MAX_CANDIDATES])


def _num(s: str | None) -> float | None:
    try:
        return float(s) if s not in (None, "") else None
    except ValueError:
        return None


def _days(days: list[Day], today: str) -> list[WeatherDay]:
    out: list[WeatherDay] = []
    for d in days:
        high, low = _num(d.temp_max), _num(d.temp_min)
        try:
            on = date.fromisoformat(d.date).isoformat()
        except ValueError:
            continue
        if on < today or high is None or low is None or any(x.date == on for x in out):
            continue
        wind = " ".join(x for x in (d.wind_dir_day, f"{d.wind_scale_day}级" if d.wind_scale_day else "") if x)
        out.append(WeatherDay(date=on, day=d.text_day, night=d.text_night, high=high, low=low, wind=wind))
        if len(out) == FORECAST_DAYS:
            break
    return out


def _hours(hours: list[Hour]) -> list[WeatherHour]:
    out: list[WeatherHour] = []
    for h in hours[:HOURLY_POINTS]:
        temp, pop = _num(h.temp), _num(h.pop)
        try:
            at = datetime.fromisoformat(h.time)
        except ValueError:
            continue
        if temp is None or at.tzinfo is None or (out and at <= datetime.fromisoformat(out[-1].time)):
            continue
        out.append(WeatherHour(time=h.time, temp=temp, text=h.text, pop=int(pop) if pop is not None else None, precip=_num(h.precip)))
    return out


def summarize_hourly(hours: list[WeatherHour]) -> dict | None:
    """逐小时明细只给卡片画图，模型拿这份摘要（照 Fusion）：时间窗、气温范围、降水概率最高的那个小时、有降水的小时。
    24 条明细交给模型，它容易漏看降水概率的高峰，也容易逐条复述。"""
    if not hours:
        return None
    temps = [h.temp for h in hours]
    out: dict = {"window": f"{hours[0].time} ~ {hours[-1].time}", "temp_min": min(temps), "temp_max": max(temps)}
    with_pop = [h for h in hours if h.pop is not None]
    if with_pop:
        peak = max(with_pop, key=lambda h: h.pop)  # 一样高的取最早的
        out["max_pop"], out["max_pop_time"] = peak.pop, peak.time
    wet = [h for h in hours if (h.precip or 0) > 0]
    out["precip_hours"] = len(wet)
    if wet:
        out["first_precip_time"], out["last_precip_time"] = wet[0].time, wet[-1].time
        out["precip_total"] = round(sum(h.precip for h in wet), 1)
    return out


class WeatherService:
    """查城市，再取逐日和逐小时预报。预报按城市在内存里放半小时：同一个城市接着问，不重复花和风的调用。"""

    def __init__(self, client: GatewayClient, clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._client = client
        self._clock = clock
        self._now = now
        self._cache: dict[tuple[str, str], tuple[float, list]] = {}

    async def forecast(self, location: str) -> Forecast:
        # 模型照候选的写法（“江西省·南昌·西湖”）再查时只拿最后一段去查，再按整个写法对
        lookup = location.rsplit(SEP, 1)[-1].strip() or location
        city = select_city(location, await self._client.weather_cities(lookup, lang="en" if lookup.isascii() else "zh"))
        daily = await self._cached("daily", city.location_id, self._client.weather_daily)
        try:
            hourly = _hours(await self._cached("hourly", city.location_id, self._client.weather_hourly))
        except GatewayError:  # 逐小时只是卡片上的趋势图，取不到不耽误逐日预报
            hourly = []
        # 当地今天：逐小时从当地现在这个小时开始，带着当地时区；取不到就按北京时间
        today = (datetime.fromisoformat(hourly[0].time) if hourly else self._now().astimezone(CHINA_TZ)).date().isoformat()
        days = _days(daily, today)
        if not days:
            raise GatewayError("天气服务没给出能用的逐日预报", kind="bad_response")
        return Forecast(location=city_label(city), today=today, fetched_at=self._now().isoformat(timespec="seconds"),
                        days=days, hourly=hourly)

    async def _cached(self, what: str, location_id: str, fetch: Callable[[str], Awaitable[list]]) -> list:
        hit = self._cache.get((what, location_id))
        if hit and self._clock() - hit[0] < CACHE_SECONDS:
            return hit[1]
        values = await fetch(location_id)
        if values:
            now = self._clock()
            self._cache = {k: v for k, v in self._cache.items() if now - v[0] < CACHE_SECONDS}  # 过期的顺手清掉
            self._cache[(what, location_id)] = (now, values)
        return values
