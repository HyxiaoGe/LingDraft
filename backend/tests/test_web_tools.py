"""搜索、读页：agent-tools 的 MCP 工具 search_web、read_page（app.tools.web_mcp）。取工具、没连上时的占位、中间件带来源 id 和整理失败、
经网关调（401/429/连不上）、只连上一部分。服务用进程内的假服务（research_llm.fake_search_server 等）；经 HTTP 的用 ASGI 模拟网关。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx2
import pytest
from fastmcp import FastMCP
from langchain_core.messages import ToolMessage

from app import aio
from app.api import research_runner
from app.graph.research import history, prompts
from app.research_llm import (
    FakeImageService,
    FakeResearchPlanner,
    McpTools,
    fake_docs_server,
    fake_flyai_server,
    fake_maps_server,
    fake_read_server,
    fake_search_server,
    fake_weather_server,
)
from app.tools.gateway import GatewayAuthError
from app.tools.web_mcp import READ_TOOL, SEARCH_TOOL, WebTool, WebToolsMiddleware, gateway_target, placeholder
from tests.test_research import Chat


def _call(name: str, cid: str = "c1", **args) -> dict:
    return {"name": name, "args": args, "id": cid, "type": "tool_call"}


async def _through(tool, call: dict, messages=()) -> ToolMessage:
    req = SimpleNamespace(tool_call=call, state={"messages": list(messages)})
    return await WebToolsMiddleware().awrap_tool_call(req, lambda r: tool.ainvoke(call))


def _connected(name: str, target) -> WebTool:
    t = WebTool(name, target)
    aio.run(t.connect())
    return t


# ---- 取工具、占位 ----


def test_connect_takes_the_service_tools_and_adds_lingdraft_notes():
    search = _connected(SEARCH_TOOL, fake_search_server).current()
    read = _connected(READ_TOOL, fake_read_server).current()
    assert search.description.endswith(prompts.render("tools.search_web.lingdraft")) and search.description.startswith("Search the web")
    assert read.description.endswith(prompts.render("tools.read_page.lingdraft"))
    assert {"query", "count", "recency_days", "domains"} <= set(search.args_schema["properties"])
    with pytest.raises(RuntimeError, match="没有 search_web"):
        aio.run(WebTool(SEARCH_TOOL, lambda: FastMCP("empty")).connect())


def test_placeholder_until_connected_tells_the_model_the_service_is_down():
    """没连上：同名的占位工具，调了说服务暂时连不上（模型照实说），页面上这一步显示连不上。"""
    tool = placeholder(SEARCH_TOOL)
    assert tool.name == SEARCH_TOOL and tool.description.endswith(prompts.render("tools.search_web.lingdraft"))
    call = _call(SEARCH_TOOL, query="q", count=5)  # 真工具的参数照样能传
    out = aio.run(_through(tool, call))
    assert out.status == "error" and out.content == prompts.render("tools.search_web.unavailable") and out.tool_call_id == "c1"
    assert history.step_of(call, out).message == history.SEARCH_FAILURES["unavailable"]
    read = aio.run(_through(placeholder(READ_TOOL), _call(READ_TOOL, url="https://a.test")))
    assert read.status == "error" and read.content == prompts.render("tools.read_page.unavailable")


# ---- 中间件 ----


def test_search_results_get_source_ids_and_meta_stays_out_of_the_model_text():
    hits = [{"title": "A", "url": "https://a.test/1", "snippet": "摘要"}, {"title": "A again", "url": "https://A.test/1#x"},
            {"title": "B", "url": "https://b.test/2", "published_at": "2026-11-03"}]
    tool = _connected(SEARCH_TOOL, lambda: fake_search_server(hits)).current()
    out = aio.run(_through(tool, _call(SEARCH_TOOL, query="q")))
    seen = json.loads(out.content)
    assert out.status == "success" and ", " not in out.content and "meta" not in seen  # 不带空格的 JSON（省 token），meta 不给模型
    ids = [h["id"] for h in seen["results"]]
    assert ids[0] == ids[1] != ids[2] and seen["results"][2]["published_at"] == "2026-11-03"
    assert out.artifact["meta"]["provider"] == "fake" and out.artifact["retrieved_at"]
    step = history.step_of(_call(SEARCH_TOOL, query="q"), out)
    assert step.ok and step.hits == 3 and step.source_ids == ids
    # 已经见过的网址沿用原来的 id
    again = aio.run(_through(tool, _call(SEARCH_TOOL, "c2", query="q2"), [out]))
    assert [h["id"] for h in json.loads(again.content)["results"]] == ids


def test_read_gets_the_id_of_the_searched_url_and_failures_are_explained():
    search = _connected(SEARCH_TOOL, fake_search_server).current()
    read = _connected(READ_TOOL, fake_read_server).current()
    found = aio.run(_through(search, _call(SEARCH_TOOL, query="q")))
    first, _, gone = json.loads(found.content)["results"]
    page = aio.run(_through(read, _call(READ_TOOL, "c2", url=first["url"]), [found]))
    body = json.loads(page.content)
    assert page.status == "success" and body["id"] == first["id"] and body["content"].startswith("正文：") and "meta" not in body
    step = history.step_of(_call(READ_TOOL, "c2", url=first["url"]), page)
    assert step.ok and step.body and step.source_ids == [first["id"]]

    # 服务说没读成：服务的那句英文给模型，错误码在 artifact 里，页面上是中文
    miss = aio.run(_through(read, _call(READ_TOOL, "c3", url=gone["url"]), [found]))
    assert miss.status == "error" and miss.content.startswith("The page does not exist")
    assert miss.artifact == {"error": {"code": "page_not_found", "retryable": False, "status": 404}}
    assert history.step_of(_call(READ_TOOL, "c3", url=gone["url"]), miss).message == "网页不存在"  # 状态码（404）不上页面


def test_service_side_errors_keep_the_service_text():
    """服务那边抛了没接住的错（MCP 的 isError，比如参数类型不对没过校验）：服务给的说明照给模型，这一步显示没调成。"""
    tool = _connected(SEARCH_TOOL, fake_search_server).current()
    call = _call(SEARCH_TOOL, query="q", count="很多")
    out = aio.run(_through(tool, call))
    assert out.status == "error" and "count" in out.content and out.artifact["error"]["code"] == "tool_error"
    assert history.step_of(call, out).message == history.SEARCH_FAILURES["tool_error"]


def test_old_stored_results_still_show():
    """11-05 以前存下的搜索、读页结果（一串条目；失败是带 kind、message 的 JSON）照常显示；原因按 kind 对表，message 里的 HTTP 状态码不上页面。"""
    old = ToolMessage(content=json.dumps([{"id": "s111111", "url": "https://a.test", "title": "A", "description": "d", "content": "c"}]),
                      name=SEARCH_TOOL, tool_call_id="c1")
    assert history.step_of(_call(SEARCH_TOOL, query="q"), old).source_ids == ["s111111"]
    failed = ToolMessage(content=json.dumps({"ok": False, "kind": "http_502", "message": "搜索服务出错：HTTP 502"}), name=SEARCH_TOOL,
                         tool_call_id="c1", status="error")
    step = history.step_of(_call(SEARCH_TOOL, query="q"), failed)
    assert not step.ok and step.message == ""  # 页面上是“没搜成：出错了”
    unread = ToolMessage(content=json.dumps({"ok": False, "url": "https://a.test", "kind": "page_not_found", "message": "网页不存在"}),
                         name=READ_TOOL, tool_call_id="c2")
    assert history.step_of(_call(READ_TOOL, "c2", url="https://a.test"), unread).message == "网页不存在"


@pytest.mark.parametrize("artifact,content,want", [
    ({"error": {"code": "page_not_found", "retryable": False, "status": 404}}, "The page does not exist.", "网页不存在"),  # 表里有：中文，不带状态码
    ({"error": {"code": "brand_new_code", "retryable": True, "status": 502}}, "Upstream said: boom", "没有读到正文"),  # 表里没有：不露码、不露英文
    ({"error": {"code": "brand_new_code", "retryable": True}}, "Upstream said: boom", "没有读到正文"),
    (None, "Error: Traceback ... KeyError('x')", "没有读到正文"),  # 不是结构化的结果：不露原文
    (None, json.dumps({"ok": False, "kind": "content_blocked"}), history.BLOCKED_NOTE),  # 内容审核：我们自己的说明
])
def test_read_failure_shows_only_our_own_words(artifact, content, want):
    """第 18 条（11-07 Sean 定，照 #79 的做法）：过程里没做成的原因只显示我们自己写的说法，其余换通用说法，HTTP 码和英文原文都不露。"""
    out = ToolMessage(content=content, name=READ_TOOL, tool_call_id="c1", status="error", artifact=artifact)
    step = history.step_of(_call(READ_TOOL, url="https://a.test"), out)
    assert not step.ok and step.message == want
    assert "HTTP" not in step.message and not any(w in step.message for w in ("Upstream", "Traceback", "brand_new_code"))


def test_search_failure_with_unknown_code_or_raw_text_falls_back_to_generic():
    call = _call(SEARCH_TOOL, query="q")
    for out in (ToolMessage(content="Upstream exploded", name=SEARCH_TOOL, tool_call_id="c1", status="error",
                            artifact={"error": {"code": "brand_new_code", "retryable": True, "status": 503}}),
                ToolMessage(content="Error: something in English", name=SEARCH_TOOL, tool_call_id="c1", status="error")):
        step = history.step_of(call, out)
        assert not step.ok and step.message == ""  # 页面上是“没搜成：出错了”
    limited = ToolMessage(content="Rate limited", name=SEARCH_TOOL, tool_call_id="c1", status="error",
                          artifact={"error": {"code": "rate_limited", "retryable": True, "status": 429}})
    assert history.step_of(call, limited).message == history.SEARCH_FAILURES["rate_limited"]


# ---- 经网关调（HTTP） ----


class _Gateway:
    """模拟网关：/search/mcp、/reader/mcp 转给假服务的 MCP，记下请求头；status 设了就直接回这个状态（429 到了每日上限、401 密钥不对……）。"""

    def __init__(self) -> None:
        self.apps = {"/search/mcp": fake_search_server().http_app(path="/search/mcp", stateless_http=True),
                     "/reader/mcp": fake_read_server().http_app(path="/reader/mcp", stateless_http=True)}
        self.status: int | None = None
        self.headers: list[dict] = []

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            self.headers.append({k.decode(): v.decode() for k, v in scope["headers"]})
            if self.status:
                await send({"type": "http.response.start", "status": self.status, "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"error":"x"}'})
                return
        await self.apps[scope["path"].rstrip("/")](scope, receive, send)


def test_calls_go_through_the_gateway():
    gw = _Gateway()

    async def go():
        search_app, read_app = gw.apps.values()
        async with search_app.lifespan(search_app), read_app.lifespan(read_app):
            def target(name):
                return lambda: gateway_target(name, "http://gw/", "k1", scene="research_eval", transport=httpx2.ASGITransport(app=gw))

            search, read = WebTool(SEARCH_TOOL, target(SEARCH_TOOL)), WebTool(READ_TOOL, target(READ_TOOL))
            await search.connect()
            await read.connect()
            out = {"search": await _through(search.tool, _call(SEARCH_TOOL, query="q")),
                   "read": await _through(read.tool, _call(READ_TOOL, url="https://a.test/1"))}
            for status in (429, 502):
                gw.status = status
                out[status] = (await _through(search.tool, _call(SEARCH_TOOL, query="q")), await _through(read.tool, _call(READ_TOOL, url="https://a.test/1")))
            gw.status = 401
            with pytest.raises(GatewayAuthError) as e:
                await _through(search.tool, _call(SEARCH_TOOL, query="q"))
            out["auth"] = e.value
            return out

    out = aio.run(go())
    assert out["search"].status == "success" and out["read"].status == "success"
    assert gw.headers[0]["authorization"] == "Bearer k1" and gw.headers[0]["x-caller-scene"] == "research_eval"
    # 网关今天的上限到了：和服务的额度用完一样，告诉模型今天别再搜、别再读
    limited_search, limited_read = out[429]
    assert limited_search.content == prompts.render("tools.search_web.quota_exhausted") and limited_search.artifact["error"]["code"] == "quota_exhausted"
    assert limited_read.content == prompts.render("tools.read_page.quota_exhausted") and limited_read.status == "error"
    # 网关、服务出问题：算没调成，可以再试一次
    down_search, down_read = out[502]
    assert down_search.content == prompts.render("tools.search_web.unavailable") and down_search.artifact["error"] == {"code": "unavailable", "retryable": True}
    assert down_read.content == prompts.render("tools.read_page.unavailable")
    # 密钥不对、网关没放行：让这次运行失败，把配置问题亮出来
    assert out["auth"].kind == "http_401"


# ---- 只连上一部分 ----


class _Flaky:
    """前 failures 次连不上，之后是真的假服务。"""

    def __init__(self, server, failures: int) -> None:
        self.server, self.failures = server, failures

    def __call__(self):
        if self.failures:
            self.failures -= 1
            return gateway_target(SEARCH_TOOL, "http://down", "k", transport=httpx2.MockTransport(lambda req: httpx2.Response(503)))
        return self.server()


def test_connect_keeps_what_connected_and_reports_what_did_not():
    tools = McpTools(_Flaky(fake_search_server, 1), fake_read_server, fake_weather_server, FakeImageService().server, fake_maps_server,
                     fake_flyai_server, fake_docs_server)
    maps = ["place_search", "route_compare", "search_flights", "search_trains"]
    assert [t.name for t in tools.tools([])] == [SEARCH_TOOL, READ_TOOL, "generate_image", *maps]  # 还没连：搜索、读页、画图、地图、航班火车是占位，天气、查文档先不带
    with pytest.raises(RuntimeError, match="没连上 search_web"):
        aio.run(tools.connect())
    names = [t.name for t in tools.tools([])]
    assert names == [SEARCH_TOOL, READ_TOOL, "generate_image", *maps, "get_weather", "resolve-library-id", "query-docs"] and tools.web[0].tool is None and tools.web[1].tool is not None
    assert all(t.tool is not None for t in (*tools.maps, *tools.travel))
    assert tools.image.tool is not None
    aio.run(tools.connect())  # 再连只连没连上的
    assert all(t.tool is not None for t in tools.web)


def test_runner_starts_with_placeholders_and_swaps_in_each_tool_once_connected(monkeypatch):
    monkeypatch.setattr(research_runner, "TOOLS_STARTUP_S", 0.2)
    monkeypatch.setattr(research_runner, "TOOLS_STARTUP_RETRY_S", 0.05)
    monkeypatch.setattr(research_runner, "TOOLS_RETRY_S", 0.05)
    flaky = _Flaky(fake_search_server, 8)  # 起服务时那几次、后台头几次都连不上
    chat = Chat(planner=FakeResearchPlanner(search=flaky))
    first = chat.r.graph
    assert chat.r._tools_retry is not None and chat.planner._mcp.web[0].tool is None
    assert chat.planner._mcp.web[1].tool is not None and chat.planner._mcp.weather.tool is not None  # 别的照常连上
    chat.r._tools_retry.result(timeout=10)
    assert chat.r.graph is not first and chat.planner._mcp.web[0].tool is not None
    v = chat.say("比较两款笔记软件")
    assert [i["ok"] for i in v["turns"][0]["items"] if i["type"] == "step"] == [True, True] and v["sources"]
