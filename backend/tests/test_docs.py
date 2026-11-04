"""查文档：Context7 的 resolve-library-id、query-docs（app.tools.docs_mcp）。取工具、经网关调、每段文档带来源 id、没做成的三种、
过程记录、来源、整轮对话、压缩后取回、内容审核去掉。服务用进程内的假 Context7（research_llm.fake_docs_server）；经 HTTP 的用 ASGI 模拟网关。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx2
import pytest
from fastmcp import FastMCP
from langchain_core.messages import ToolMessage

from app import aio
from app.graph.research import compaction, history, prompts
from app.graph.research.agent import _withdrawn
from app.graph.research.sources import DOCS_TOOL, collect_sources, doc_snippets, source_id
from app.research_llm import FAKE_DOCS, FAKE_LIBRARY_ID, FakeResearchPlanner, fake_docs_server
from app.tools.docs_mcp import LIBRARY_TOOL, DocsMiddleware, docs_tools, gateway_target, with_ids
from app.tools.gateway import GatewayAuthError
from tests.test_research import Chat

MEMORY_URL = "https://docs.langchain.com/oss/python/langchain/short-term-memory"
CUSTOM_URL = "https://docs.langchain.com/oss/python/langchain/middleware/custom"


def _call(name: str, cid: str = "c1", **args) -> dict:
    return {"name": name, "args": args, "id": cid, "type": "tool_call"}


def _library_call(name: str = "LangChain") -> dict:
    return _call(LIBRARY_TOOL, libraryName=name, query="custom middleware")


def _docs_call(library: str = FAKE_LIBRARY_ID) -> dict:
    return _call(DOCS_TOOL, libraryId=library, query="custom middleware before the model call")


def _connected(target=fake_docs_server) -> dict:
    tools = docs_tools(target)
    for t in tools:
        aio.run(t.connect())
    return {t.name: t.tool for t in tools}


async def _through(tools: dict, call: dict, state: dict | None = None):
    request = SimpleNamespace(tool_call=call, state=state or {"messages": []})
    return await DocsMiddleware().awrap_tool_call(request, lambda req: tools[call["name"]].ainvoke(call))


def _ask(tools: dict, call: dict, state: dict | None = None) -> ToolMessage:
    return aio.run(_through(tools, call, state))


def _answered(handler_result) -> ToolMessage:
    """中间件拿到的是这个结果（或者抛这个错）时交给模型的。"""

    async def handler(_):
        if isinstance(handler_result, BaseException):
            raise handler_result
        return handler_result

    return aio.run(DocsMiddleware().awrap_tool_call(SimpleNamespace(tool_call=_docs_call(), state={"messages": []}), handler))


# ---- 取工具 ----


def test_connect_takes_both_tools_and_adds_lingdraft_notes():
    tools = _connected()
    assert list(tools) == [LIBRARY_TOOL, DOCS_TOOL]
    # 说明是 Context7 的，后面接上灵稿自己的一段
    assert tools[LIBRARY_TOOL].description.startswith("Resolves a package/product name")
    assert tools[LIBRARY_TOOL].description.endswith(prompts.render("tools.resolve-library-id.lingdraft"))
    assert tools[DOCS_TOOL].description.endswith(prompts.render("tools.query-docs.lingdraft"))
    assert set(tools[LIBRARY_TOOL].args_schema["properties"]) == {"query", "libraryName"}
    assert set(tools[DOCS_TOOL].args_schema["required"]) == {"libraryId", "query"}

    with pytest.raises(RuntimeError, match="没有 resolve-library-id"):
        aio.run(docs_tools(lambda: FastMCP("empty"))[0].connect())


def test_planner_has_docs_tools_only_after_connecting():
    p = FakeResearchPlanner()
    assert LIBRARY_TOOL not in [t.name for t in p.tools()]
    aio.run(p.connect_tools())
    assert [t.name for t in p.tools()][-2:] == [LIBRARY_TOOL, DOCS_TOOL]


# ---- 经网关调（HTTP） ----


class _Gateway:
    """模拟网关：转给假 Context7 的 MCP，记下请求头；status 设了就直接回这个状态（429 到了每日上限、401 密钥不对）。"""

    def __init__(self) -> None:
        self.mcp = fake_docs_server().http_app(path="/context7/mcp", stateless_http=True)
        self.status: int | None = None
        self.headers: list[dict] = []

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            self.headers.append({k.decode(): v.decode() for k, v in scope["headers"]})
            if self.status:
                await send({"type": "http.response.start", "status": self.status, "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"error":"daily_limit_exceeded"}'})
                return
        await self.mcp(scope, receive, send)


def test_calls_go_through_the_gateway_and_auth_errors_fail_the_run():
    gw = _Gateway()

    async def go():
        async with gw.mcp.lifespan(gw.mcp):
            tools = docs_tools(lambda: gateway_target("http://gw/", "k1", transport=httpx2.ASGITransport(app=gw)))
            for t in tools:
                await t.connect()
            by_name = {t.name: t.tool for t in tools}
            ok = await _through(by_name, _docs_call())
            gw.status = 429
            limited = await _through(by_name, _docs_call())
            gw.status = 403
            try:
                await _through(by_name, _library_call())
            except GatewayAuthError as e:
                return ok, limited, e
            return ok, limited, None

    ok, limited, auth = aio.run(go())
    assert ok.status == "success" and len(doc_snippets(ok)) == 3
    assert gw.headers[0]["authorization"] == "Bearer k1" and gw.headers[0]["x-caller-scene"] == "research"
    # 网关今天的上限到了：告诉模型今天别再查、改用搜索
    assert limited.status == "error" and limited.content == prompts.render("tools.docs.quota_exhausted")
    assert limited.artifact == {"error": {"code": "quota_exhausted"}}
    # 密钥不对、网关没放行：让这次运行失败，把配置问题亮出来
    assert auth is not None and auth.kind == "http_403"


# ---- 每段文档带来源 id ----


def test_each_snippet_gets_the_source_id_of_its_page():
    res = _ask(_connected(), _docs_call())
    memory, custom = source_id(MEMORY_URL, {}), source_id(CUSTOM_URL, {})
    assert res.status == "success" and res.artifact["retrieved_at"]
    # 模型看的是原来的 Markdown（代码不转义），每段的 Source 那行下面加一行来源 id
    assert f"Source: {MEMORY_URL}\nSource id: {memory}\n" in res.content
    assert res.content.count(f"Source id: {custom}") == 2  # 同一页的两段是同一个 id
    assert "```python\n@before_model" in res.content
    # 带来源的几段另外记在 artifact 里，内容不带标题和出处那两行
    snippets = res.artifact["snippets"]
    assert [(s["id"], s["url"]) for s in snippets] == [(memory, MEMORY_URL), (custom, CUSTOM_URL), (custom, CUSTOM_URL)]
    assert snippets[0]["title"] == "Trim messages using @before_model middleware"
    assert snippets[0]["content"].startswith("Keep only the last few messages") and "Source" not in snippets[0]["content"]


def test_a_page_found_earlier_keeps_its_id():
    earlier = ToolMessage(json.dumps({"ok": True, "query": "q", "results": [{"id": "s0a0a0a", "url": CUSTOM_URL, "title": "Custom"}]}),
                          tool_call_id="c0", name="search_web")
    res = _ask(_connected(), _docs_call(), {"messages": [earlier]})
    assert {s["id"] for s in res.artifact["snippets"] if s["url"] == CUSTOM_URL} == {"s0a0a0a"}


def test_snippets_without_a_web_source_stay_as_they_are():
    text = "### No source here\n\nJust text.\n\n--------------------------------\n\n### Local\n\nSource: file:///tmp/x.md\n\nLocal file."
    body, snippets = with_ids(text, {})
    assert body == text and snippets == []
    body, snippets = with_ids(FAKE_DOCS, {})
    assert len(snippets) == 3 and body.count("--------------------------------") == FAKE_DOCS.count("--------------------------------")


# ---- 没做成的 ----


def test_library_not_found_passes_context7_words_to_the_model():
    res = _ask(_connected(), _library_call("Qzxw"))
    assert res.status == "error" and res.content == 'No libraries found for "Qzxw". Try a different search term.'
    assert res.artifact == {"error": {"code": "tool_error"}}
    missing = _ask(_connected(), _docs_call("/nobody/nothing"))
    assert missing.status == "error" and missing.content.startswith('Library "/nobody/nothing" not found')


def test_rate_limited_and_unreachable_get_lingdraft_notes():
    # context7 服务认出 Context7 限流时 structuredContent 里是 rate_limited（见 agent-tools 的 services/context7）
    limited = _answered(ToolMessage("Rate limited: too many requests", tool_call_id="c1", name=DOCS_TOOL, status="error",
                                    artifact={"structured_content": {"error": {"code": "rate_limited", "tool": DOCS_TOOL}}}))
    assert limited.status == "error" and limited.content == prompts.render("tools.docs.rate_limited")
    assert limited.artifact == {"error": {"code": "rate_limited"}}
    down = _answered(httpx2.ConnectError("connection refused"))
    assert down.status == "error" and down.content == prompts.render("tools.docs.unavailable") and down.artifact["error"]["code"] == "unavailable"


# ---- 过程记录、来源 ----


def test_steps_count_libraries_and_snippets_and_say_why_it_failed():
    tools = _connected()
    lib = history.step_of(_library_call(), _ask(tools, _library_call()))
    assert (lib.kind, lib.text, lib.hits, lib.ok) == ("library", "LangChain", 2, True)
    docs = history.step_of(_docs_call(), _ask(tools, _docs_call()))
    assert (docs.kind, docs.hits, docs.limits) == ("docs", 3, [FAKE_LIBRARY_ID])
    assert docs.source_ids == [source_id(MEMORY_URL, {}), source_id(CUSTOM_URL, {})]
    failed = history.step_of(_library_call("Qzxw"), _ask(tools, _library_call("Qzxw")))
    assert not failed.ok and failed.message == "Context7 没给出结果"
    limited = history.step_of(_docs_call(), ToolMessage("x", tool_call_id="c1", name=DOCS_TOOL, status="error",
                                                        artifact={"error": {"code": "rate_limited"}}))
    assert limited.message == history.DOCS_FAILURES["rate_limited"]
    assert history.step_of(_docs_call(), None).pending


def test_each_documentation_page_is_one_source():
    res = _ask(_connected(), _docs_call())
    sources = collect_sources([res])
    assert [(s.url, s.title) for s in sources] == [(MEMORY_URL, "Trim messages using @before_model middleware"),
                                                    (CUSTOM_URL, "Create decorator-based middleware with @before_model and @wrap_model_call")]
    assert sources[0].excerpt.startswith("Keep only the last few messages") and sources[0].retrieved_at == res.artifact["retrieved_at"]
    assert {s.excerpt_kind for s in sources} == {"docs"}


def test_docs_excerpt_replaces_a_search_snippet_but_not_a_read_page():
    memory = source_id(MEMORY_URL, {})
    searched = ToolMessage(json.dumps({"ok": True, "query": "q", "results": [{"id": memory, "url": MEMORY_URL, "title": "Short-term memory",
                                                                               "snippet": "snippet"}]}), tool_call_id="c0", name="search_web")
    res = _ask(_connected(), _docs_call())
    after_search = collect_sources([searched, res])[0]
    assert (after_search.excerpt_kind, after_search.title) == ("docs", "Short-term memory")  # 标题沿用搜索时的
    assert after_search.excerpt.startswith("Keep only the last few messages")
    read = ToolMessage(json.dumps({"ok": True, "id": memory, "url": MEMORY_URL, "title": "Short-term memory", "content": "Page body."}),
                       tool_call_id="c2", name="read_page")
    assert collect_sources([read, res])[0].excerpt == "Page body."
    assert collect_sources([res, read])[0].excerpt_kind == "body"


# ---- 整轮 ----


def test_docs_turn_resolves_the_library_queries_and_cites_the_page():
    chat = Chat()
    v = chat.say("LangChain 的文档里，自定义 middleware 怎么在调模型前改消息")
    turn = v["turns"][0]
    steps = [i for i in turn["items"] if i["type"] == "step"]
    assert [(s["kind"], s["ok"]) for s in steps] == [("library", True), ("docs", True)]
    memory = source_id(MEMORY_URL, {})
    assert f"[{memory}]" in turn["items"][-1]["text"]  # 回答引用查到的第一段
    assert memory in v["sources"] and v["sources"][memory]["url"] == MEMORY_URL
    assert turn["record"]["usage"]["docs"] == 2


def test_docs_turn_for_an_unknown_library_says_so():
    chat = Chat()
    v = chat.say("Qzxw 的文档怎么用")
    steps = [i for i in v["turns"][0]["items"] if i["type"] == "step"]
    assert [(s["kind"], s["ok"], s["message"]) for s in steps] == [("library", False, "Context7 没给出结果")]


# ---- 压缩后取回、内容审核 ----


def test_cleared_docs_list_their_pages_and_can_be_recalled():
    res = _ask(_connected(), _docs_call()).model_copy(update={"id": "r1"})
    note = compaction._cleared(res).content
    listed = json.loads(note.split("\n", 1)[1])
    assert [s["url"] for s in listed] == [MEMORY_URL, CUSTOM_URL]  # 同一页只列一次
    back = json.loads(compaction.recall_source([res], source_id(CUSTOM_URL, {})))
    assert back["url"] == CUSTOM_URL and [d["title"] for d in back["documentation"]] == [
        "Create decorator-based middleware with @before_model and @wrap_model_call", "Create middleware > Decorator-based middleware"]


def test_withdrawn_docs_leave_no_sources():
    res = _ask(_connected(), _docs_call())
    gone = _withdrawn(res)
    assert gone.status == "error" and gone.content == prompts.render("agent.material_blocked") and doc_snippets(gone) == []
    assert collect_sources([gone]) == []
    assert history.step_of(_docs_call(), gone).message == history.BLOCKED_NOTE
