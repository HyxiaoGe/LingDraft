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


def test_search_tool_passes_time_and_site_limits():
    """recency_days 按搜索服务的四档向上取整；超出范围的数收进范围；没填就不传。"""
    bodies = []

    def handler(req: httpx.Request):
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json={"results": []})

    search = next(t for t in make_research_tools(_client(handler)) if t.name == "search_web")
    search.invoke({"query": "q", "recency_days": 30, "domains": ["sqlite.org", " ", "a.org", "b", "c", "d", "e"]})
    search.invoke({"query": "q", "count": 50, "recency_days": 90})
    search.invoke({"query": "q", "recency_days": 0})
    search.invoke({"query": "q"})
    assert bodies[0] == {"query": "q", "type": "web", "count": 5, "freshness": "pm", "domain_filters": ["sqlite.org", "a.org", "b", "c", "d"]}
    assert bodies[1] == {"query": "q", "type": "web", "count": 10, "freshness": "py"}
    assert bodies[2]["freshness"] == "pd"
    assert bodies[3] == {"query": "q", "type": "web", "count": 5}


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
    assert set(tools) == {"search_web", "read_page", "ask_user", "load_skill"}
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


def test_malformed_responses_are_failures():
    """200 但结构不对：按格式错处理，不当成没搜到，也不让坏数据流到后面；合法的空结果照常是空列表。"""
    for body in ([], {"results": ["bad"]}, {"results": 1}, {"results": [{"url": 123}]}, {"results": [{"url": "https://a", "title": 5}]}, {}):
        with pytest.raises(GatewayError) as e:
            _client(lambda req, body=body: httpx.Response(200, json=body)).search("q")
        assert e.value.kind == "bad_response" and not e.value.retryable, body
    assert _client(lambda req: httpx.Response(200, json={"results": []})).search("q") == []
    body = {"results": [{"url": None, "title": "无网址"}, {"url": "https://a", "title": None, "x": 1}]}
    (hit,) = _client(lambda req: httpx.Response(200, json=body)).search("q")  # 没有网址的条目跳过
    assert hit.to_dict() == {"title": "", "url": "https://a", "description": "", "content": "", "published_at": None}
    for body in ({"content": 7}, [], {"title": ["T"]}):
        r = _client(lambda req, body=body: httpx.Response(200, json=body)).read("https://a")
        assert not r.ok and r.kind == "bad_response" and not r.retryable, body
    tools = {t.name: t for t in make_research_tools(_client(lambda req: httpx.Response(200, json={"results": 1})))}
    msg = tools["search_web"].invoke({"name": "search_web", "args": {"query": "q"}, "id": "c1", "type": "tool_call"})
    assert msg.status == "error" and json.loads(msg.content)["kind"] == "bad_response"
