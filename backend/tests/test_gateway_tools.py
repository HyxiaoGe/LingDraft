from __future__ import annotations

import json

import httpx
import pytest

from app.tools.gateway import GatewayAuthError, GatewayClient, GatewayError
from app.tools.research_tools import make_research_tools


def _client(handler):
    return GatewayClient("http://gw", "k1", scene="research", transport=httpx.MockTransport(handler))


def test_search_sends_auth_and_parses():
    seen = {}

    def handler(req: httpx.Request):
        seen["auth"] = req.headers.get("authorization")
        seen["scene"] = req.headers.get("x-caller-scene")
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": "t", "url": "https://a", "description": "d", "content": "long", "published_at": "2026-01-01"}
                ]
            },
        )

    hits = _client(handler).search("q", count=3)
    assert seen == {
        "auth": "Bearer k1",
        "scene": "research",
        "path": "/search/search",
        "body": {"query": "q", "type": "web", "count": 3},
    }
    assert hits[0].to_dict() == {
        "title": "t",
        "url": "https://a",
        "description": "d",
        "content": "long",
        "published_at": "2026-01-01",
    }


def test_search_error_raises():
    c = _client(lambda req: httpx.Response(401, json={"error": "unauthorized"}))
    with pytest.raises(GatewayError):
        c.search("q")


def test_read_ok_truncates():
    c = _client(lambda req: httpx.Response(200, json={"title": "T", "content": "x" * 50, "provider": "direct"}))
    r = c.read("https://a", max_chars=10)
    assert r.ok and r.truncated and r.content == "x" * 10 and r.title == "T"


def test_read_structured_failure():
    detail = {"kind": "page_not_found", "message": "gone", "retryable": False, "upstream_status": 404}
    c = _client(lambda req: httpx.Response(404, json={"detail": detail}))
    r = c.read("https://a")
    assert r.to_dict() == {
        "ok": False,
        "url": "https://a",
        "kind": "page_not_found",
        "message": "gone",
        "retryable": False,
    }


def test_read_gateway_failure():
    c = _client(lambda req: httpx.Response(504, json={"error": "upstream_timeout", "message": "slow"}))
    r = c.read("https://a")
    assert not r.ok and r.kind == "upstream_timeout" and r.retryable


def test_tools_wrap_client():
    def handler(req: httpx.Request):
        if req.url.path == "/search/search":
            return httpx.Response(200, json={"results": [{"title": "t", "url": "https://a"}]})
        return httpx.Response(200, json={"title": "T", "content": "body", "provider": "direct"})

    tools = {t.name: t for t in make_research_tools(_client(handler))}
    assert set(tools) == {"search_web", "read_page", "ask_user"}
    assert json.loads(tools["search_web"].invoke({"query": "q"}))[0]["url"] == "https://a"
    assert json.loads(tools["read_page"].invoke({"url": "https://a"}))["content"] == "body"
    assert "拦截" in tools["ask_user"].invoke({"question": "?", "options": ["a"], "why": "w"})


def test_missing_key():
    with pytest.raises(GatewayError):
        GatewayClient("http://gw", "")


def test_search_failures_are_typed():
    """搜索失败带类型和能不能重试；密钥问题单独一类。"""
    cases = [
        (lambda req: httpx.Response(502, text="bad gateway"), GatewayError, "http_502", True),
        (lambda req: httpx.Response(400, json={"error": "bad query"}), GatewayError, "http_400", False),
        (lambda req: httpx.Response(200, text="<html>"), GatewayError, "bad_response", True),
        (lambda req: httpx.Response(403, json={"error": "forbidden"}), GatewayAuthError, "http_403", False),
    ]
    for handler, exc, kind, retryable in cases:
        with pytest.raises(exc) as e:
            _client(handler).search("q")
        assert e.value.kind == kind and e.value.retryable is retryable

    def timeout(req):
        raise httpx.ReadTimeout("slow", request=req)

    with pytest.raises(GatewayError) as e:
        _client(timeout).search("q")
    assert e.value.kind == "timeout" and e.value.retryable


def test_read_non_json_is_a_failed_read():
    r = _client(lambda req: httpx.Response(200, text="<html>")).read("https://a")
    assert not r.ok and r.kind == "bad_response" and r.retryable


def test_search_tool_hands_service_errors_to_agent():
    """搜索服务出错变成给 agent 的工具结果（status=error），不让整次调研失败；密钥问题照常抛出。"""
    call = {"name": "search_web", "args": {"query": "q"}, "id": "c1", "type": "tool_call"}
    tools = {t.name: t for t in make_research_tools(_client(lambda req: httpx.Response(503, text="down")))}
    msg = tools["search_web"].invoke(call)
    assert msg.status == "error"
    data = json.loads(msg.content)
    assert data["ok"] is False and data["kind"] == "http_503" and data["retryable"] is True
    tools = {t.name: t for t in make_research_tools(_client(lambda req: httpx.Response(401, json={"error": "unauthorized"})))}
    with pytest.raises(GatewayAuthError):
        tools["search_web"].invoke(call)
