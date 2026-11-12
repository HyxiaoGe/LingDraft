"""额度：每个人一段时间里能花多少 credits（1 credit = ¥0.001 的 API 成本，见 app.costs）。

2026-11-06 Sean 定（Fusion 的做法只作参考）：
- 窗口和 Fusion 一样：北京时间的自然日、自然周（周一起）。游客每天 1000；登录用户每天 10000、每周 50000（项目前期要用户量，给足，
  别让游客体验打折）；管理员不限。所有游客合计每天 50000（约 50 元）：防脚本批量领游客身份刷，配合每个地址一天 5 个游客身份
  （identity.GuestTokens）。数字在 config 的 CREDITS_*，环境变量能改。
- 怎么算：从账上（model_costs）把这个人在窗口里算给用户的几样（主 agent、子 agent 的模型调用，做成了的工具调用，知识库建好了的索引）加起来；
  加的是 billed（免费额度的按正常价，2026-11-09 Sean 定，见 app.costs），之前的行没有，用 yuan；
  出错、丢了的那一轮（运行记录里这一轮最后一次执行是 error/lost）不算：用户没拿到回答不该付钱（出错前调完的模型厂商照收，算灵稿的）。
  点了停止的照算。账上记的是对话归谁（owner），游客期间花的记在游客身份上，登录并进账号后不跟过去。
- 只在开始前查（和 Fusion 一样）：说话、排话、重新回答、改话、重试时额度用完了就拒（429，detail 里带 code、什么时候重置），
  正在跑的那一轮不管，跑完可能略超。页面在还剩两成时提醒。
- 管理员重置：uv run python -m app.quota reset <owner>，记一条，这个人从那一刻起重新数（窗口里只算这之后的）；show <owner> 看用了多少。
- 页面上叫“额度”，不露 credits，带“约可问 N 次”的换算（2026-11-07 批次 E，Sean 定按 config.CREDITS_PER_ASK 固定换算）：
  /api/credits 和 429 的 detail 都带 per_ask；游客的还带 login（登录后每天、每周多少），页面说“登录后会怎样”用它，不写死数字。
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from app import aio, config, costs
from app.api.run_store import ERROR, LOST, MemoryRunStore, PostgresRunStore, RunStore
from app.identity import ADMIN, GUEST, GUEST_PREFIX, Identity

log = logging.getLogger(__name__)

CHINA_TZ = ZoneInfo("Asia/Shanghai")
PURPOSES = (costs.AGENT, costs.SUBAGENT, costs.TOOL, costs.KB_INDEX)  # 算给用户的（起标题、推荐问题、压缩、整理记忆、知识库查询时的向量和重排不算）
FAILED = (ERROR, LOST)
DAY, WEEK, GUESTS = "day", "week", "guests"  # 窗口：这个人今天的、这个人这周的、所有游客今天合起来的
EXHAUSTED = "credits_exhausted"  # 429 的 detail.code
WARN_AT = 0.8  # 用了这么多页面开始提醒（页面也知道这个数，见 frontend/src/research.js）


def day_start(at: datetime) -> datetime:
    return at.astimezone(CHINA_TZ).replace(hour=0, minute=0, second=0, microsecond=0)


def week_start(at: datetime) -> datetime:
    d = day_start(at)
    return d - timedelta(days=d.weekday())


@dataclass(frozen=True)
class Limits:
    guest_daily: int
    user_daily: int
    user_weekly: int
    guests_daily: int

    @classmethod
    def from_config(cls) -> Limits:
        return cls(config.CREDITS_GUEST_DAILY, config.CREDITS_USER_DAILY, config.CREDITS_USER_WEEKLY, config.CREDITS_GUESTS_DAILY)


@dataclass(frozen=True)
class Window:
    kind: str  # DAY / WEEK / GUESTS
    limit: int | None  # credits；管理员不限是 None（只看用了多少）
    used: float  # credits
    resets_at: datetime  # 窗口什么时候重新开始（北京时间的下一个 0 点 / 下周一 0 点）

    @property
    def remaining(self) -> float:
        return float("inf") if self.limit is None else max(0.0, self.limit - self.used)

    @property
    def exhausted(self) -> bool:
        return self.limit is not None and self.used >= self.limit

    def view(self) -> dict[str, Any]:
        return {"kind": self.kind, "limit": self.limit, "used": round(self.used, 2), "resets_at": self.resets_at.isoformat()}


def asks(credits: float, per_ask: int) -> int:
    """这么多额度约可问几次（向下取整）；页面上的换算同一个算法（frontend/src/research.js 的 asksOf）。"""
    return int(credits // per_ask) if per_ask > 0 else 0


class Exhausted(Exception):
    """额度用完了：接口回 429，detail 是 self.detail()。login 是游客登录以后的额度（{"day", "week"}），登录用户是 None。
    message 只进日志和接口，页面按 detail 里的字段自己写（见 frontend/src/errors.js 的 quotaText）。"""

    def __init__(self, window: Window, guest: bool, per_ask: int = 0, login: dict[str, int] | None = None) -> None:
        self.window, self.guest, self.per_ask, self.login = window, guest, per_ask, login
        super().__init__(self.message())

    def message(self) -> str:
        when = self.window.resets_at.astimezone(CHINA_TZ)
        more = f"；登录后每天 {self.login['day']} 额度，约可问 {asks(self.login['day'], self.per_ask)} 次" if self.login else ""
        if self.window.kind == GUESTS:
            return f"今天所有游客合用的额度用完了，{when:%m-%d} 0 点恢复{more or '；登录后用自己的额度'}"
        span = "这周" if self.window.kind == WEEK else "今天"
        reset = f"{'下周一' if self.window.kind == WEEK else '明天'}（{when:%m-%d}）0 点恢复"
        return f"{span}的额度用完了（共 {self.window.limit} 额度），{reset}{more}"

    def detail(self) -> dict[str, Any]:
        d = {"code": EXHAUSTED, "message": self.message(), "guest": self.guest, "per_ask": self.per_ask, **self.window.view()}
        return {**d, "login": self.login} if self.login else d


# ---- 从账上算 ----


class Usage(Protocol):
    """账上的数：钱数都是元。"""

    async def setup(self) -> None: ...
    async def spent(self, owner: str, since: datetime) -> float: ...  # 这个人从 since 起算给用户的花费（出错、丢了的轮不算）
    async def spent_by_guests(self, since: datetime) -> float: ...  # 所有游客的
    async def last_reset(self, owner: str) -> datetime | None: ...
    async def reset(self, owner: str, by: str, at: datetime) -> None: ...


class MemoryUsage:
    """测试、没配库时用：账本的行交给 put（和 costs.LEDGER 一样的接口），运行记录看内存的那份。"""

    def __init__(self, runs: MemoryRunStore | None = None) -> None:
        self.rows: list[dict] = []
        self.resets: dict[str, datetime] = {}
        self._runs = runs

    def put(self, row: dict) -> None:
        self.rows.append(row)

    async def setup(self) -> None:
        return None

    def _failed(self, turn_id: str | None) -> bool:
        if self._runs is None or turn_id is None:
            return False
        last = [r for r in self._runs._rows.values() if r["turn_id"] == turn_id]
        return bool(last) and last[-1]["status"] in FAILED

    def _sum(self, rows: list[dict], since: datetime) -> float:
        return sum(r["yuan"] if r.get("billed") is None else r["billed"] for r in rows
                   if r["yuan"] is not None and r["purpose"] in PURPOSES and r["started_at"] >= since and not self._failed(r["turn_id"]))

    async def spent(self, owner: str, since: datetime) -> float:
        return self._sum([r for r in self.rows if r.get("owner") == owner], since)

    async def spent_by_guests(self, since: datetime) -> float:
        return self._sum([r for r in self.rows if (r.get("owner") or "").startswith(GUEST_PREFIX)], since)

    async def last_reset(self, owner: str) -> datetime | None:
        return self.resets.get(owner)

    async def reset(self, owner: str, by: str, at: datetime) -> None:
        self.resets[owner] = at


_SPENT = """
SELECT coalesce(sum(coalesce(c.billed, c.yuan)), 0) AS yuan FROM model_costs c
WHERE {owner} AND c.started_at >= %s AND c.purpose = ANY(%s) AND c.yuan IS NOT NULL
  AND coalesce((SELECT r.status FROM runs r WHERE r.turn_id = c.turn_id ORDER BY r.seq DESC LIMIT 1), '') <> ALL(%s)
"""
_DDL = ("CREATE TABLE IF NOT EXISTS quota_resets (owner text NOT NULL, at timestamptz NOT NULL, by text NOT NULL)",
        "CREATE INDEX IF NOT EXISTS quota_resets_owner ON quota_resets (owner, at)")


class PostgresUsage:
    """从 model_costs、runs 表算（见 app.costs、api.run_store），重置记在 quota_resets。exec 是跑 SQL 的（PostgresRunStore._exec，共用对话的连接池）。"""

    def __init__(self, exec_: Callable[[str, tuple], Awaitable[list[dict]]]) -> None:
        self._exec = exec_

    async def setup(self) -> None:
        for sql in (*costs._DDL, *_DDL):  # model_costs 本来在第一次记账时才建：先建好，查的时候就不用管有没有
            await self._exec(sql, ())

    async def _spent(self, where: str, params: tuple, since: datetime) -> float:
        rows = await self._exec(_SPENT.format(owner=where), (*params, since, list(PURPOSES), list(FAILED)))
        return float(rows[0]["yuan"])

    async def spent(self, owner: str, since: datetime) -> float:
        return await self._spent("c.owner = %s", (owner,), since)

    async def spent_by_guests(self, since: datetime) -> float:
        return await self._spent("c.owner LIKE %s", (GUEST_PREFIX + "%",), since)

    async def last_reset(self, owner: str) -> datetime | None:
        rows = await self._exec("SELECT max(at) AS at FROM quota_resets WHERE owner = %s", (owner,))
        return rows[0]["at"] if rows else None

    async def reset(self, owner: str, by: str, at: datetime) -> None:
        await self._exec("INSERT INTO quota_resets (owner, at, by) VALUES (%s, %s, %s)", (owner, at, by))


def usage_for(run_store: RunStore) -> Usage:
    """对话的运行记录存在哪，账就从哪算：Postgres 的查库，内存的只在测试里用（行要自己 put 进去）。"""
    if isinstance(run_store, PostgresRunStore):
        return PostgresUsage(run_store._exec)
    return MemoryUsage(run_store if isinstance(run_store, MemoryRunStore) else None)


# ---- 准入 ----


class Quota:
    """接口开始一轮之前调 admit；页面显示用 view。同步方法在接口的线程里调，活交给对话的事件循环（见 app.aio）。"""

    def __init__(self, usage: Usage, limits: Limits | None = None, clock: Callable[[], datetime] = costs.now) -> None:
        self.usage, self.limits, self.clock = usage, limits or Limits.from_config(), clock
        # 每个人最近一次来查额度时是哪一档（只在内存里）：对话里建定时任务时记到任务上，到点发起时没有令牌，按任务上记的查（见 app.scheduled）
        self.tiers: dict[str, str] = {}
        aio.run(usage.setup())

    async def windows(self, who: Identity) -> list[Window]:
        """这个人现在的几个窗口；管理员也有日、周两个，只是 limit 是 None（不限，只看用了多少）。重置过的，窗口里只算重置之后的。"""
        now = self.clock()
        reset = await self.usage.last_reset(who.sub)
        day, week = day_start(now), week_start(now)

        async def own(kind: str, limit: int | None, start: datetime, resets_at: datetime) -> Window:
            return Window(kind, limit, await self.usage.spent(who.sub, max(start, reset) if reset else start) * costs.CREDITS_PER_YUAN, resets_at)
        if who.tier == GUEST:
            return [await own(DAY, self.limits.guest_daily, day, day + timedelta(days=1)),
                    Window(GUESTS, self.limits.guests_daily, await self.usage.spent_by_guests(day) * costs.CREDITS_PER_YUAN, day + timedelta(days=1))]
        admin = who.tier == ADMIN
        return [await own(DAY, None if admin else self.limits.user_daily, day, day + timedelta(days=1)),
                await own(WEEK, None if admin else self.limits.user_weekly, week, week + timedelta(days=7))]

    def login_limits(self, who: Identity) -> dict[str, int] | None:
        """游客登录以后每天、每周多少（页面告诉他登录后会怎样）；已经登录的是 None。"""
        return {"day": self.limits.user_daily, "week": self.limits.user_weekly} if who.tier == GUEST else None

    async def check(self, who: Identity) -> None:
        self.tiers[who.sub] = who.tier
        for w in await self.windows(who):
            if w.exhausted:
                log.info("额度用完：%s %s 用了 %.0f / %d", who.sub, w.kind, w.used, w.limit)
                raise Exhausted(w, who.tier == GUEST, config.CREDITS_PER_ASK, self.login_limits(who))

    def admit(self, who: Identity) -> None:
        aio.run(self.check(who))

    def view(self, who: Identity) -> dict[str, Any]:
        """GET /api/credits：{"unlimited": 管理员, "warn_at", "per_ask": 一次约多少, "windows": [...]}，游客多一个 "login"（登录后每天、每周多少）。
        游客总额那个窗口也在 windows 里（kind 是 guests），页面只显示自己的；管理员的窗口 limit 是 null。"""
        windows = aio.run(self.windows(who))
        v = {"unlimited": who.tier == ADMIN, "warn_at": WARN_AT, "per_ask": config.CREDITS_PER_ASK, "windows": [w.view() for w in windows]}
        login = self.login_limits(who)
        return {**v, "login": login} if login else v

    def reset(self, owner: str, by: str) -> None:
        aio.run(self.usage.reset(owner, by, self.clock()))


# ---- 命令行 ----


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="额度：看一个人用了多少、给他重置（读 LINGDRAFT_DATABASE_URL 那个库）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    show = sub.add_parser("show", help="这个人现在几个窗口各用了多少")
    show.add_argument("owner", help="登录用户的 id 或 guest:xxx（thread_owners 里的 owner）")
    reset = sub.add_parser("reset", help="从现在起重新数")
    reset.add_argument("owner")
    reset.add_argument("--by", default="cli", help="谁重置的，记进 quota_resets")
    args = ap.parse_args(argv)
    if not config.DATABASE_URL:
        print("没配 LINGDRAFT_DATABASE_URL")
        return 1
    from app.api.research_runner import open_storage

    _, _, runs = aio.run(open_storage(config.DATABASE_URL))
    try:
        q = Quota(usage_for(runs))
        who = Identity(args.owner, GUEST if args.owner.startswith(GUEST_PREFIX) else "user")
        if args.cmd == "reset":
            q.reset(args.owner, args.by)
            print(f"已重置 {args.owner}，从现在起重新数")
        for w in aio.run(q.windows(who)):
            print(f"{w.kind:<6} 用了 {w.used:>9.1f} / {w.limit} credits，{w.resets_at.astimezone(CHINA_TZ):%m-%d %H:%M} 重置")
    finally:
        aio.run(runs.pool.close())  # 不关，退出时池子的后台任务会报 pending
    return 0


if __name__ == "__main__":
    sys.exit(main())
