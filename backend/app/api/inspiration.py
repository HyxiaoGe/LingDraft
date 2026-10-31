"""新对话页的“今日灵感”：从 Fusion 的公开接口取它每天出的示例问题，只留这边答得了的几类。

Fusion 每天凌晨用辅助模型按功能目录出题（新闻、调研题照当天热榜出），首页从 /api/prompts/examples 拿，
不用登录，每次随机抽一批，最多 50 条。这边只会搜、读、在对话里回答：生图、地图题答不了，文档、编程题显不出
搜索和标来源，所以只留新闻、调研、常识、天气四类（天气只能靠搜，不如 Fusion 的天气工具准）（2026-10-31 Sean 定）。
取到的在内存里放半小时；再取没取到，先接着用旧的，过半小时再试；一次都没取到就报错，页面显示固定的几个例子。
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable

import httpx

KEEP = {"news", "research", "knowledge", "weather"}
LIMIT = 50  # Fusion 一次最多给这么多
FRESH = 30 * 60
TIMEOUT = 5
MAX_CHARS = 40  # Fusion 出题限 25 字以内，太长的多半是坏数据

log = logging.getLogger(__name__)


class InspirationError(Exception):
    """Fusion 连不上、或者给的东西用不了。"""


def pick(payload: object) -> list[str]:
    """Fusion 返回 {data: {examples: [{question, category}]}}：留下要的几类，去掉空的、太长的和重复的。"""
    data = payload.get("data") if isinstance(payload, dict) else None
    examples = data.get("examples") if isinstance(data, dict) else None
    if not isinstance(examples, list):
        raise InspirationError("Fusion 返回的格式不对")
    out: list[str] = []
    for e in examples:
        if not isinstance(e, dict) or e.get("category") not in KEEP:
            continue
        q = str(e.get("question") or "").strip()
        if q and len(q) <= MAX_CHARS and q not in out:
            out.append(q)
    return out


class Inspirations:
    def __init__(self, fusion_url: str, transport: httpx.AsyncBaseTransport | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._url = fusion_url.rstrip("/") + "/api/prompts/examples"
        self._transport = transport
        self._clock = clock
        self._questions: list[str] | None = None
        self._at = 0.0

    async def get(self) -> list[str]:
        if self._questions is not None and self._clock() - self._at < FRESH:
            return self._questions
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=TIMEOUT) as client:
                resp = await client.get(self._url, params={"limit": LIMIT})
                resp.raise_for_status()
                questions = pick(resp.json())
        except (httpx.HTTPError, ValueError, InspirationError) as e:
            if self._questions is None:
                raise InspirationError(f"今日灵感没取到：{type(e).__name__}") from e
            log.warning("今日灵感没取到，接着用上次的：%s", type(e).__name__)
            self._at = self._clock()
            return self._questions
        self._questions, self._at = questions, self._clock()
        return questions
