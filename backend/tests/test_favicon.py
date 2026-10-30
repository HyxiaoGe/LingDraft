"""来源网站图标：读首页找声明的图标、退到 /favicon.ico、按域名缓存、不碰内网地址。网络用 MockTransport 模拟。"""
import asyncio
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import main as api_main
from app.api.favicon import RETRY_AFTER, Favicons, icon_links

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
ICO = b"\x00\x00\x01\x00" + b"\x00" * 20
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


def site(routes: dict[str, httpx.Response], calls: list[str]):
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        return routes.get(str(req.url), httpx.Response(404, text="<html>not found</html>"))
    return httpx.MockTransport(handler)


def public(ips: dict[str, str] | None = None):
    async def resolve(host: str) -> list[str]:
        return [(ips or {}).get(host, "93.184.216.34")]
    return resolve


def get(f: Favicons, host: str):
    return asyncio.run(f.get(host))


def test_icon_declared_in_page_is_used_and_cached(tmp_path):
    calls: list[str] = []
    routes = {
        "https://a.test/": httpx.Response(200, html='<head><link rel="mask-icon" href="/m.svg"><link rel="shortcut icon" href="/static/i.png"></head>'),
        "https://a.test/static/i.png": httpx.Response(200, content=PNG),
    }
    f = Favicons(tmp_path, site(routes, calls), public())
    assert get(f, "A.test") == (PNG, "image/png")
    assert calls == ["https://a.test/", "https://a.test/static/i.png"]  # mask-icon 是蒙版，不要
    again = Favicons(tmp_path, site(routes, calls), public())  # 换个实例（比如重启后）也走磁盘缓存
    assert get(again, "a.test") == (PNG, "image/png") and len(calls) == 2


def test_falls_back_to_favicon_ico_and_html_is_not_an_icon(tmp_path):
    calls: list[str] = []
    routes = {
        "https://b.test/": httpx.Response(200, html='<head><link rel="icon" href="http://[bad/i.png"></head>'),  # 写坏的地址跳过
        "https://b.test/favicon.ico": httpx.Response(200, content=ICO),
        "https://c.test/": httpx.Response(200, html="<head></head>"),
        "https://c.test/favicon.ico": httpx.Response(200, html="<html>首页</html>"),  # 不存在也回 200 的站
    }
    f = Favicons(tmp_path, site(routes, calls), public())
    assert get(f, "b.test") == (ICO, "image/x-icon")
    assert get(f, "c.test") is None


def test_miss_is_remembered_then_retried_after_a_week(tmp_path):
    calls: list[str] = []
    clock = [time.time()]
    f = Favicons(tmp_path, site({}, calls), public(), now=lambda: clock[0])
    assert get(f, "d.test") is None
    n = len(calls)
    assert get(f, "d.test") is None and len(calls) == n  # 记着取不到，不再去取
    clock[0] += RETRY_AFTER + 1
    assert get(f, "d.test") is None and len(calls) > n


def test_inline_data_icon(tmp_path):
    import base64

    href = "data:image/png;base64," + base64.b64encode(PNG).decode()
    routes = {"https://e.test/": httpx.Response(200, html=f'<link rel="icon" href="{href}">')}
    assert get(Favicons(tmp_path, site(routes, []), public()), "e.test") == (PNG, "image/png")


def test_same_host_fetched_once_when_asked_together(tmp_path):
    calls: list[str] = []
    routes = {"https://f.test/": httpx.Response(200, html='<link rel="icon" href="/i.png">'), "https://f.test/i.png": httpx.Response(200, content=PNG)}
    f = Favicons(tmp_path, site(routes, calls), public())

    async def both():
        return await asyncio.gather(f.get("f.test"), f.get("f.test"))

    assert asyncio.run(both()) == [(PNG, "image/png")] * 2 and calls.count("https://f.test/") == 1


def test_private_addresses_are_never_fetched(tmp_path):
    calls: list[str] = []
    routes = {
        "https://jump.test/": httpx.Response(302, headers={"location": "http://10.0.0.8/"}),
        "https://port.test/": httpx.Response(302, headers={"location": "https://port.test:6379/"}),
        "https://inner.test/": httpx.Response(200, html='<link rel="icon" href="https://intranet.test/i.png">'),
    }
    f = Favicons(tmp_path, site(routes, calls), public({"lan.test": "192.168.1.11", "intranet.test": "127.0.0.1"}))
    assert get(f, "lan.test") is None  # 域名解析到内网：一个请求都不发
    assert get(f, "jump.test") is None  # 跳转到内网 IP：不跟
    assert get(f, "port.test") is None  # 非默认端口：不连
    assert get(f, "inner.test") is None  # 页面声明的图标在内网：不取
    hosts = {httpx.URL(u).host for u in calls}
    assert not hosts & {"lan.test", "10.0.0.8", "intranet.test"} and all(httpx.URL(u).port is None for u in calls)


@pytest.mark.parametrize("bad", ["localhost", "127.0.0.1", "a..test", "-a.test", "a.test/x", "a_b.test", "x"])
def test_malformed_host_rejected(tmp_path, bad):
    with pytest.raises(ValueError):
        get(Favicons(tmp_path, site({}, []), public()), bad)


def test_icon_links_order():
    html = '<link rel="apple-touch-icon" href="/a.png"><link href="/i.ico" rel="icon"><link rel=icon href=/x.svg>'
    assert icon_links(html, "https://g.test/p/") == ["https://g.test/i.ico", "https://g.test/x.svg", "https://g.test/a.png"]


def test_endpoint(tmp_path):
    routes = {
        "https://h.test/": httpx.Response(200, html='<link rel="icon" href="/i.svg">'),
        "https://h.test/i.svg": httpx.Response(200, content=SVG),
    }
    api_main.set_favicons(Favicons(tmp_path, site(routes, []), public()))
    try:
        c = TestClient(api_main.app)
        r = c.get("/api/favicon/h.test")
        assert r.status_code == 200 and r.headers["content-type"] == "image/svg+xml"
        assert "default-src 'none'" in r.headers["content-security-policy"]  # 直接打开 SVG 也跑不了里面的脚本
        assert c.get("/api/favicon/none.test").status_code == 404
        assert c.get("/api/favicon/localhost").status_code == 400
    finally:
        api_main.set_favicons(None)


def test_slow_site_answers_later_from_cache(tmp_path):
    """等不及的先告诉调用方还没好，后台接着取；取完下次直接从缓存给。"""
    async def handler(req: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.2)
        return httpx.Response(200, content=PNG) if req.url.path == "/favicon.ico" else httpx.Response(200, html="<head></head>")

    f = Favicons(tmp_path, httpx.MockTransport(handler), public())

    async def run():
        with pytest.raises(TimeoutError):
            await f.get("slow.test", wait=0.05)
        await asyncio.sleep(0.6)
        return await f.get("slow.test", wait=0.05)

    assert asyncio.run(run()) == (PNG, "image/png")
