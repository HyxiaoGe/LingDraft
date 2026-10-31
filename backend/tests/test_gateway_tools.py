from __future__ import annotations

import json

import httpx
import pytest

from langchain.tools import ToolRuntime

from app.tools.gateway import GatewayAuthError, GatewayClient, GatewayError, tidy_title
from app.tools.research_tools import make_research_tools


def _rt(messages=()) -> ToolRuntime:
    """工具执行时 ToolNode 注入的运行时：搜索、读页从里面的消息查已有的来源 id。"""
    return ToolRuntime(state={"messages": list(messages)}, context=None, config={}, stream_writer=lambda _: None,
                       tool_call_id="c1", store=None)


def _call(name: str, **args) -> dict:
    return {"name": name, "args": {**args, "runtime": _rt()}, "id": "c1", "type": "tool_call"}


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
    """recency_days 按搜索服务的四档向上取整；没填就不传；空白的站点去掉。"""
    bodies = []

    def handler(req: httpx.Request):
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json={"results": []})

    search = next(t for t in make_research_tools(_client(handler)) if t.name == "search_web")
    search.invoke(_call("search_web", query="q", recency_days=30, domains=["sqlite.org", " ", "a.org"]))
    search.invoke(_call("search_web", query="q", count=10, recency_days=90))
    search.invoke(_call("search_web", query="q", recency_days=1))
    search.invoke(_call("search_web", query="q"))
    assert bodies[0] == {"query": "q", "type": "web", "count": 5, "freshness": "pm", "domain_filters": ["sqlite.org", "a.org"]}
    assert bodies[1] == {"query": "q", "type": "web", "count": 10, "freshness": "py"}
    assert bodies[2]["freshness"] == "pd"
    assert bodies[3] == {"query": "q", "type": "web", "count": 5}


@pytest.mark.parametrize(("args", "said"), [
    ({"count": 50}, "count 要在 1～10 之间，传的是 50"),
    ({"count": 0}, "count 要在 1～10 之间，传的是 0"),
    ({"recency_days": 0}, "recency_days 要在 1～365 之间"),
    ({"recency_days": 400}, "recency_days 要在 1～365 之间"),
    ({"domains": ["a", "b", "c", "d", "e", "f"]}, "domains 最多 5 个，传了 6 个"),
])
def test_search_args_out_of_range_go_back_to_the_agent(args, said):
    """超出搜索服务支持的范围：不替模型改成别的数去搜，告诉它哪里不对，它自己重发。"""
    sent = []
    search = next(t for t in make_research_tools(_client(lambda req: sent.append(req) or httpx.Response(200, json={"results": []})))
                  if t.name == "search_web")
    msg = search.invoke(_call("search_web", query="q", **args))
    data = json.loads(msg.content)
    assert msg.status == "error" and data["kind"] == "bad_arguments" and said in data["message"] and not data["retryable"]
    assert sent == []


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
    assert set(tools) == {"search_web", "read_page", "get_weather", "generate_image"}
    hit = json.loads(tools["search_web"].invoke(_call("search_web", query="q")).content)[0]
    assert hit["url"] == "https://a" and hit["id"].startswith("s")
    page = tools["read_page"].invoke(_call("read_page", url="https://a"))
    assert json.loads(page.content)["content"] == "body" and json.loads(page.content)["id"] == hit["id"]  # 同一个网址同一个 id
    assert page.artifact["retrieved_at"]


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
    call = _call("search_web", query="q")
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
    # 读页成功一定带正文（reader-service 读不到正文时自己按 empty_content 失败返回）：200 却没有正文也是格式错
    for body in ({"content": 7}, [], {"title": ["T"]}, {"error": "upstream failed"}, {"title": "T"}, {"content": " \n"}):
        r = _client(lambda req, body=body: httpx.Response(200, json=body)).read("https://a")
        assert not r.ok and r.kind == "bad_response" and not r.retryable, body
    tools = {t.name: t for t in make_research_tools(_client(lambda req: httpx.Response(200, json={"results": 1})))}
    msg = tools["search_web"].invoke(_call("search_web", query="q"))
    assert msg.status == "error" and json.loads(msg.content)["kind"] == "bad_response"


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


def test_titles_tidied_on_search_and_on_old_sources():
    """网页标题重复一遍的，搜索结果里理掉；以前存下的来源打开时（从检查点还原会重新校验）也理掉。"""
    from app.graph.research.state import Source

    def handler(req: httpx.Request):
        return httpx.Response(200, json={"results": [{"title": "A 文章 - A 文章", "url": "https://a"}]})

    assert _client(handler).search("q")[0].title == "A 文章"
    assert Source(id="s1", url="https://a", title="A 文章 - A 文章").title == "A 文章"
