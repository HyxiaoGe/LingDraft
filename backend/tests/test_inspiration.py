"""今日灵感：从 Fusion 取、只留四类、缓存半小时、取不到时用旧的或报 502。网络用 MockTransport 模拟。"""
import httpx
from fastapi.testclient import TestClient

from app.api import main as api_main
from app.api.inspiration import FRESH, InspirationError, Inspirations, pick
from tests.research_helpers import run_async as run

URL = "https://fusion.test/api/prompts/examples"


def fusion(examples, calls: list[str], status: int = 200):
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        if isinstance(examples, Exception):
            raise examples
        return httpx.Response(status, json={"code": "SUCCESS", "data": {"examples": examples, "refreshed_at": "2026-10-31T05:00:00"}})
    return httpx.MockTransport(handler)


def ex(category: str, question: str) -> dict:
    return {"category": category, "question": question, "requires": None}


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_pick_keeps_the_four_kinds_and_drops_bad_items():
    got = pick({"data": {"examples": [
        ex("news", "币安被盗最新进展"), ex("image", "画一张中秋壁纸"), ex("map", "西安一日游怎么走"),
        ex("research", " 折叠屏和旗舰怎么选 "), ex("knowledge", "喝咖啡影响睡眠吗"), ex("weather", "上海明天冷不冷"),
        ex("document", "写一份周报"), ex("code", "解释闭包"),
        ex("news", "币安被盗最新进展"), ex("news", ""), ex("news", "长" * 41), {"category": "news"}, "坏数据",
    ]}})
    assert got == ["币安被盗最新进展", "折叠屏和旗舰怎么选", "喝咖啡影响睡眠吗", "上海明天冷不冷"]


def test_pick_rejects_wrong_shape():
    for bad in (None, [], {"data": None}, {"data": {"examples": "x"}}):
        try:
            pick(bad)
        except InspirationError:
            continue
        raise AssertionError(f"应该报错：{bad!r}")


def test_cached_for_half_an_hour_then_fetched_again():
    calls: list[str] = []
    clock = Clock()
    box = [ex("news", "第一批")]
    inspirations = Inspirations("https://fusion.test/", fusion(box, calls), clock)
    assert run(inspirations.get()) == ["第一批"]
    assert calls == [URL + "?limit=50"]
    clock.t += FRESH - 1
    box[0] = ex("news", "第二批")
    assert run(inspirations.get()) == ["第一批"]
    assert len(calls) == 1
    clock.t += 2
    assert run(inspirations.get()) == ["第二批"]
    assert len(calls) == 2


def test_failure_keeps_old_questions_and_waits_before_retrying():
    calls: list[str] = []
    clock = Clock()
    box: list = [ex("research", "存储行业现状")]
    ok = Inspirations("https://fusion.test", fusion(box, calls), clock)
    run(ok.get())
    ok._transport = fusion(httpx.ConnectError("down"), calls)
    clock.t += FRESH + 1
    assert run(ok.get()) == ["存储行业现状"]
    assert len(calls) == 2
    clock.t += 60  # 刚失败过：半小时内不再去试
    assert run(ok.get()) == ["存储行业现状"]
    assert len(calls) == 2


def test_endpoint_returns_questions_or_502():
    calls: list[str] = []
    try:
        api_main.set_inspirations(Inspirations("https://fusion.test", fusion([ex("weather", "广州今天下雨吗"), ex("image", "画个头像")], calls)))
        c = TestClient(api_main.app)
        r = c.get("/api/inspirations")
        assert r.status_code == 200 and r.json() == {"questions": ["广州今天下雨吗"]}

        api_main.set_inspirations(Inspirations("https://fusion.test", fusion([], calls, status=503)))
        r = c.get("/api/inspirations")
        assert r.status_code == 502 and "今日灵感没取到" in r.json()["detail"]
    finally:
        api_main.set_inspirations(None)
