"""网关客户端（传图、按编号取图、删图）和灵稿自己的工具；搜索、读页、天气、画图是 MCP 工具，见 test_web_tools、test_weather、test_images。"""

from __future__ import annotations

import httpx
import pytest

from app.graph.research.state import Source, tidy_title
from app.tools.gateway import GatewayAuthError, GatewayClient, GatewayError, ImageRejected
from app.tools.research_tools import make_research_tools
from tests.research_helpers import blocking

IMAGE_ID = "3f0e8a4c-0000-4000-8000-000000000000"


def test_own_tools():
    assert [t.name for t in make_research_tools()] == ["get_current_time"]


def test_image_fetch_and_delete():
    """按编号取图、删图（经网关 /image/v1/images/{id}）：服务里没有是 None；类型不对、太大、网关出错抛 GatewayError；删不掉只记日志。"""
    seen: list[tuple[str, str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        name = req.url.path.rsplit("/", 1)[-1]
        seen.append((req.method, req.url.path, req.headers["authorization"]))
        if req.method == "DELETE":
            return httpx.Response(204 if name == IMAGE_ID else 502)
        return {
            IMAGE_ID: httpx.Response(200, content=b"img", headers={"content-type": "image/webp"}),
            "gone": httpx.Response(404, json={"error": "image_not_found"}),
            "page": httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"}),
            "big": httpx.Response(200, content=b"x" * (20 * 1024 * 1024 + 1), headers={"content-type": "image/png"}),
            "auth": httpx.Response(401),
        }[name]

    client = blocking(GatewayClient("http://gw", "k1", scene="research", transport=httpx.MockTransport(handler)))
    assert client.image(IMAGE_ID) == (b"img", "image/webp")
    assert seen[0] == ("GET", f"/image/v1/images/{IMAGE_ID}", "Bearer k1")
    assert client.image("gone") is None
    for name, kind in (("page", "http_200"), ("big", "image_too_large")):
        with pytest.raises(GatewayError) as e:
            client.image(name)
        assert e.value.kind == kind
    with pytest.raises(GatewayAuthError):
        client.image("auth")
    client.delete_images([IMAGE_ID, "broken"])  # 删不掉的不抛
    assert [s[:2] for s in seen[-2:]] == [("DELETE", f"/image/v1/images/{IMAGE_ID}"), ("DELETE", "/image/v1/images/broken")]


def test_image_upload():
    """传用户发的图（POST /image/v1/images）：原样传字节；服务不收的（413、415）抛 ImageRejected，密钥不对、别的问题照常抛。"""
    seen: list[tuple[str, str, bytes]] = []
    replies = iter([
        httpx.Response(201, json={"id": IMAGE_ID, "mime_type": "image/png", "width": 4, "height": 3, "bytes": 3, "model": "upload"}),
        httpx.Response(413, json={"error": "image_too_large"}),
        httpx.Response(415, json={"error": "unsupported_image"}),
        httpx.Response(401),
        httpx.Response(502),
    ])

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path, req.content))
        return next(replies)

    client = blocking(GatewayClient("http://gw", "k1", scene="research", transport=httpx.MockTransport(handler)))
    assert client.upload_image(b"png")["id"] == IMAGE_ID
    assert seen == [("POST", "/image/v1/images", b"png")]
    for kind in ("image_too_large", "unsupported_image"):
        with pytest.raises(ImageRejected) as e:
            client.upload_image(b"x")
        assert e.value.kind == kind
    with pytest.raises(GatewayAuthError):
        client.upload_image(b"x")
    with pytest.raises(GatewayError) as e:
        client.upload_image(b"x")
    assert e.value.retryable and not isinstance(e.value, ImageRejected)


def test_missing_key():
    with pytest.raises(GatewayError):
        GatewayClient("http://gw", "")


@pytest.mark.parametrize(("raw", "want"), [
    ("DeepSeek 隐私政策 - DeepSeek 隐私政策", "DeepSeek 隐私政策"),
    ("Wi-Fi 7 有多快 | Wi-Fi 7 有多快 - 某站", "Wi-Fi 7 有多快 - 某站"),  # 站名留着
    ("  大模型“独角兽”亟须差异化应对价格冲击 - 大模型“独角兽”亟须差异化应对价格冲击 ", "大模型“独角兽”亟须差异化应对价格冲击"),
    ("Wi-Fi 7 - 评测", "Wi-Fi 7 - 评测"),  # 不是重复不动
    ("FAQ - FAQ", "FAQ"),
    ("首页_首页", "首页"),
    ("1 - 1", "1 - 1"),  # 一个字的不算
    ("10-10-2024 发布会", "10-10-2024 发布会"),  # 横线两边没空格的不是分隔符
    ("A 与 B - A 与 BC", "A 与 B - A 与 BC"),  # 只认一字不差的重复
])
def test_tidy_title(raw, want):
    assert tidy_title(raw) == want


def test_old_source_titles_are_tidied():
    """以前存下的来源打开时（从检查点还原会重新校验）标题理掉重复；新的搜索结果搜索服务已经理过了。"""
    assert Source(id="s1", url="https://a", title="A 文章 - A 文章").title == "A 文章"
