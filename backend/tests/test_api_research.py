"""对话场景接口：建线程、接着说、线程列表，以及几种拒绝（400 / 404 / 409）。只用假模型。"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app import aio
from app.api import main as api_main
from app.api.research_runner import ResearchRunner
from app.graph.research.graph import memory_checkpointer, thread_ids
from tests.research_helpers import events, last, say
from tests.auth_helpers import USER, bearer, user_token


@pytest.fixture
def client(tmp_path):
    api_main.set_research_runner(ResearchRunner(fake=True))
    return TestClient(api_main.app, headers=USER)


def _new(client, text: str) -> list[tuple[str, dict]]:
    return events(client.post("/api/research/threads", json={"text": text}))


def test_conversation_over_http(client):
    ev = _new(client, "查一下局部修改")
    names = [e for e, _ in ev]
    assert names[:2] == ["thread", "started"] and names[-2:] == ["state", "end"]
    assert {"title", "step", "delta", "node"} <= set(names)
    st = last(ev, "state")
    tid = st["thread_id"]
    assert str(uuid.UUID(tid)) == tid == last(ev, "thread")["thread_id"]  # 新对话的 id 是完整的 UUID，地址栏 /chat/<id> 用它
    assert st["status"] == "idle" and st["title"] == last(ev, "title")["title"] and len(st["sources"]) == 3
    assert set(st) == {"thread_id", "exists", "scenario", "status", "error", "error_kind", "model", "model_ref", "stopping", "suggesting", "title", "updated_at", "running_since",
                       "turns", "sources", "queued_message", "guided_message", "ask", "asks_off"}
    assert st["model"] == "auto"  # 没选模型的新对话是 Auto
    assert client.get(f"/api/research/threads/{tid}/state").json() == st  # 刷新后看到的和刚才一样

    ev = events(say(client, tid, "直接答：为什么不稳定？"))
    st = last(ev, "state")
    assert len(st["turns"]) == 2 and st["turns"][1]["user"] == "直接答：为什么不稳定？" and "title" not in [e for e, _ in ev]


def test_bad_messages_rejected(client):
    assert client.post("/api/research/threads", json={"text": "  "}).status_code == 400
    assert client.post("/api/research/threads", json={"request": "旧字段"}).status_code == 422
    tid = last(_new(client, "直接答：好"), "state")["thread_id"]
    assert say(client, tid, "  ").status_code == 400
    assert say(client, tid, "看看", quote={"text": "x"}).status_code == 422  # 引用、按钮都拿掉了
    assert say(client, tid, "x", extra=1).status_code == 422
    assert client.post(f"/api/research/threads/{tid}/resume", json={"interrupt_id": "x", "payload": {}}).status_code == 404  # 没有这个接口了


def test_unknown_thread_404(client):
    for r in (client.get("/api/research/threads/nope0000/state"), say(client, "nope0000", "x"),
              client.post("/api/research/threads/nope0000/retry"), client.get("/api/research/threads/nope0000/events"),
              client.post("/api/research/threads/nope0000/stop"), client.post("/api/research/threads/nope0000/regenerate")):
        assert r.status_code == 404


def test_list_threads_with_status(client):
    first = last(_new(client, "直接答：好"), "state")
    done = last(_new(client, "查一下局部修改"), "state")
    rows = client.get("/api/research/threads").json()["threads"]
    by_id = {r["thread_id"]: r for r in rows}
    assert set(by_id) == {first["thread_id"], done["thread_id"]}
    assert rows[0]["thread_id"] == done["thread_id"]  # 最近有动静的在前
    d = by_id[done["thread_id"]]
    assert d == {"thread_id": done["thread_id"], "title": done["title"], "status": "idle", "updated_at": d["updated_at"], "running_since": None,
                 "pinned_at": None}
    assert d["updated_at"]


def test_pin_threads(client):
    """置顶存后端（批次 D）：再置顶一次不改时间；列表里每行带 pinned_at；取消置顶；别人的、没有的 404；删了对话置顶一起没。"""
    a = last(_new(client, "直接答：好"), "state")["thread_id"]
    b = last(_new(client, "直接答：再来"), "state")["thread_id"]
    row = client.post(f"/api/research/threads/{a}/pin").json()
    assert row["thread_id"] == a and row["title"] and row["pinned_at"]
    assert client.post(f"/api/research/threads/{a}/pin").json()["pinned_at"] == row["pinned_at"]
    by_id = {t["thread_id"]: t for t in client.get("/api/research/threads").json()["threads"]}
    assert by_id[a]["pinned_at"] == row["pinned_at"] and by_id[b]["pinned_at"] is None
    other = bearer(user_token("u2"))
    assert client.post(f"/api/research/threads/{a}/pin", headers=other).status_code == 404
    assert client.delete(f"/api/research/threads/{a}/pin", headers=other).status_code == 404
    assert client.post("/api/research/threads/nope0000/pin").status_code == 404
    assert client.delete(f"/api/research/threads/{a}/pin").json()["pinned_at"] is None
    assert [t["pinned_at"] for t in client.get("/api/research/threads").json()["threads"]] == [None, None]
    client.post(f"/api/research/threads/{b}/pin")
    assert client.delete(f"/api/research/threads/{b}").status_code == 200
    assert aio.run(api_main.research().run_store.pins_of("u1")) == {}


def test_thread_ids_without_reading_checkpoints(tmp_path):
    assert aio.run(thread_ids(memory_checkpointer())) == []
    rr = ResearchRunner(fake=True)  # 存储见 conftest 的 _storage：设了测试库时是 Postgres
    assert aio.run(thread_ids(rr.graph.checkpointer)) == []
    rr.accept("t1", "直接答：你好")
    rr.run_turn("t1")
    assert aio.run(thread_ids(rr.graph.checkpointer)) == ["t1"]


# ---- 真实模型的适配（不发请求） ----


@pytest.fixture
def deepseek(monkeypatch):
    from app import config
    from app.research_llm import make_chat_model

    monkeypatch.setattr(config, "DEEPSEEK_API_KEY", "k")
    return make_chat_model("deepseek:m", thinking=True, effort="high")


def test_deepseek_passes_back_all_reasoning(deepseek):
    """思考原样传回：同一轮里的不传接口报 400；之前几轮的也传，下一轮发出去的开头和上一轮一样，前缀缓存才接得上。"""
    call = [{"name": "search_web", "args": {"query": "q"}, "id": "c1", "type": "tool_call"}]
    msgs = [HumanMessage("第一轮"), AIMessage(content="", tool_calls=call, additional_kwargs={"reasoning_content": "旧思考"}),
            ToolMessage(content="[]", tool_call_id="c1"), AIMessage(content="答", additional_kwargs={"reasoning_content": "旧结论"}),
            HumanMessage("第二轮"), AIMessage(content="", tool_calls=[{**call[0], "id": "c2"}], additional_kwargs={"reasoning_content": "新思考"}),
            ToolMessage(content="[]", tool_call_id="c2")]
    payload = deepseek._get_request_payload(msgs)
    assert [m.get("reasoning_content") for m in payload["messages"]] == [None, "旧思考", None, "旧结论", None, "新思考", None]


def _compat(monkeypatch, provider, handler, **kw):
    """OpenAI 兼容接口的一家（见 research_llm.COMPAT），换成假的 http 客户端，不发真请求。"""
    import openai

    from app import config
    from app.research_llm import COMPAT, make_chat_model

    monkeypatch.setattr(config, COMPAT[provider].key_name, "k")
    llm = make_chat_model(f"{provider}:m", **{"thinking": True, **kw})
    transport = httpx.MockTransport(handler)
    base = "http://compat.test/v1"
    llm.root_client = openai.OpenAI(api_key="k", base_url=base, http_client=httpx.Client(transport=transport))
    llm.root_async_client = openai.AsyncOpenAI(api_key="k", base_url=base, http_client=httpx.AsyncClient(transport=transport))
    llm.client, llm.async_client = llm.root_client.chat.completions, llm.root_async_client.chat.completions
    return llm


def _ark(monkeypatch, handler):
    return _compat(monkeypatch, "ark", handler)


def _ark_reply(stream: bool) -> httpx.Response:
    """豆包 2.x 思考时的返回：reasoning_content 是思考摘要，encrypted_content 是加密的思考原文。"""
    if not stream:
        msg = {"role": "assistant", "content": "明天上海小雨。", "reasoning_content": "先查预报。", "encrypted_content": "djF+密文"}
        return httpx.Response(200, json={"id": "x", "object": "chat.completion", "created": 0, "model": "doubao",
                                         "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

    def sse(delta: dict, finish: str | None = None) -> str:
        body = {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "doubao",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        return f"data: {json.dumps(body, ensure_ascii=False)}\n\n"

    body = (sse({"role": "assistant", "content": "", "reasoning_content": "先查"}) + sse({"reasoning_content": "预报。"})
            + sse({"encrypted_content": "djF+密文"}) + sse({"content": "明天上海小雨。"}) + sse({}, "stop") + "data: [DONE]\n\n")
    return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})


def test_ark_keeps_thinking_summary_and_encrypted_original(monkeypatch):
    """ChatOpenAI 不取豆包的思考：补上以后整条、流式都留着摘要和加密原文，content_blocks 读得到摘要（标成 openai 就读不到）。"""
    from langchain_core.messages import AIMessageChunk

    from app.graph.research import history
    from app.research_llm import ARK_PROVIDER

    llm = _ark(monkeypatch, lambda req: _ark_reply(json.loads(req.content).get("stream", False)))
    whole = llm.invoke("上海明天下雨吗")
    parts: list[AIMessageChunk] = list(llm.stream("上海明天下雨吗"))
    streamed = parts[0]
    for c in parts[1:]:
        streamed += c
    for m in (whole, streamed):
        assert m.additional_kwargs["reasoning_content"] == "先查预报。" and m.additional_kwargs["encrypted_content"] == "djF+密文"
        assert m.response_metadata["model_provider"] == ARK_PROVIDER
        assert history.thinking_of(m) == "先查预报。" and history.text_of(m) == "明天上海小雨。"
    assert [history.thinking_of(c) for c in parts if history.thinking_of(c)] == ["先查", "预报。"]  # 页面逐段显示


def test_ark_passes_back_thinking(monkeypatch):
    """摘要和加密原文都原样传回：带工具的多轮对话不传加密原文推理效果会下降；之前几轮的也传，下一轮发出去的开头不变。"""
    llm = _ark(monkeypatch, lambda req: httpx.Response(500))
    call = [{"name": "search_web", "args": {"query": "q"}, "id": "c1", "type": "tool_call"}]
    msgs = [HumanMessage("第一轮"), AIMessage(content="答", additional_kwargs={"reasoning_content": "旧摘要", "encrypted_content": "旧密文"}),
            HumanMessage("第二轮"), AIMessage(content="", tool_calls=call, additional_kwargs={"reasoning_content": "新摘要", "encrypted_content": "新密文"}),
            ToolMessage(content="[]", tool_call_id="c1"), AIMessage(content="停下时的半截", additional_kwargs={"reasoning_content": "只有摘要"})]
    sent = llm._get_request_payload(msgs)["messages"]
    assert [(m.get("reasoning_content"), m.get("encrypted_content")) for m in sent] == [
        (None, None), ("旧摘要", "旧密文"), (None, None), ("新摘要", "新密文"), (None, None), ("只有摘要", None)]


NEW_PROVIDERS = ["moonshot", "xai", "dashscope", "mimo", "zhipuai", "hunyuan", "minimax"]


@pytest.mark.parametrize("provider", NEW_PROVIDERS)
def test_new_providers_keep_thinking_like_ark(monkeypatch, provider):
    """2026-11-05 新接的几家和豆包同一个类：整条、流式都读得到 reasoning_content，标成这一家（不是 openai），发出去时原样传回。"""
    from langchain_core.messages import AIMessageChunk

    from app.graph.research import history
    from app.research_llm import COMPAT

    llm = _compat(monkeypatch, provider, lambda req: _ark_reply(json.loads(req.content).get("stream", False)))
    whole = llm.invoke("上海明天下雨吗")
    parts: list[AIMessageChunk] = list(llm.stream("上海明天下雨吗"))
    streamed = parts[0]
    for c in parts[1:]:
        streamed += c
    for m in (whole, streamed):
        assert m.response_metadata["model_provider"] == COMPAT[provider].label != "openai"
        assert history.thinking_of(m) == "先查预报。" and history.text_of(m) == "明天上海小雨。"
    sent = llm._get_request_payload([HumanMessage("问"), whole, HumanMessage("再问")])["messages"]
    assert [m.get("reasoning_content") for m in sent] == [None, "先查预报。", None]


@pytest.mark.parametrize("provider", ["ark", *NEW_PROVIDERS])
def test_thinking_switch_uses_each_providers_own_params(monkeypatch, provider):
    """开关思考按各家实测的参数发（见 COMPAT）；关不掉的（off 是 None）要了关也不发；思考强度只在开思考、这家认的时候带。"""
    from app.research_llm import COMPAT

    spec = COMPAT[provider]
    on = _compat(monkeypatch, provider, lambda req: httpx.Response(500), effort="high")._get_request_payload([HumanMessage("问")])
    off = _compat(monkeypatch, provider, lambda req: httpx.Response(500), thinking=False, effort="high")._get_request_payload([HumanMessage("问")])
    assert on.get("extra_body") == (spec.on or None)
    assert off.get("extra_body") == (spec.off or None)
    assert on.get("reasoning_effort") == ("high" if spec.effort else None) and "reasoning_effort" not in off


def test_compat_without_key_says_which_env(monkeypatch):
    from app import config
    from app.research_llm import make_chat_model

    monkeypatch.setattr(config, "MOONSHOT_API_KEY", "")
    with pytest.raises(RuntimeError, match="MOONSHOT_API_KEY"):
        make_chat_model("moonshot:kimi", thinking=True)


def test_thinking_is_read_the_same_way_whoever_wrote_it():
    """读思考、正文按 content blocks：DeepSeek、豆包放在 additional_kwargs 里，Claude 这类放在 content 里。"""
    from langchain_core.messages import AIMessageChunk

    from app.api.research_runner import StreamBuffer
    from app.graph.research import history

    side = AIMessage(content="答", additional_kwargs={"reasoning_content": "想"}, response_metadata={"model_provider": "deepseek"})
    inline = AIMessage(content=[{"type": "thinking", "thinking": "想", "signature": "s"}, {"type": "text", "text": "答"}],
                       response_metadata={"model_provider": "anthropic"})
    for m in (side, inline):
        assert (history.thinking_of(m), history.text_of(m)) == ("想", "答")
    buf = StreamBuffer()
    buf.add(AIMessageChunk(content=[{"type": "thinking", "thinking": "想", "index": 0}], id="m1", response_metadata={"model_provider": "anthropic"}))
    buf.add(AIMessageChunk(content=[{"type": "text", "text": "答", "index": 1}], id="m1", response_metadata={"model_provider": "anthropic"}))
    p = buf.partial()
    assert (p.additional_kwargs["reasoning_content"], p.content) == ("想", "答")


def test_reply_without_structured_output_raises():
    """模型直接回一段文字、没调结构化输出的工具：要报错交给调用方，不能返回 None。"""
    from langchain_deepseek import ChatDeepSeek

    from app.research_llm import Suggestions, _structured

    def reply(content: str | None, tool_args: dict | None = None):
        msg = {"role": "assistant", "content": content}
        if tool_args is not None:
            msg["tool_calls"] = [{"id": "t1", "type": "function", "function": {"name": "Suggestions", "arguments": json.dumps(tool_args)}}]
        return {"id": "x", "object": "chat.completion", "created": 0, "model": "m",
                "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if tool_args else "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}

    answers = [reply("好的。"), reply(None, {"questions": ["a", "b", "c"]})]
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=answers.pop(0)))
    llm = ChatDeepSeek(model="m", api_key="k", api_base="http://model.test/v1", max_retries=0, http_client=httpx.Client(transport=transport))
    chain = _structured(llm, Suggestions)
    with pytest.raises(RuntimeError, match="没有按结构返回"):
        chain.invoke("推荐")
    assert chain.invoke("推荐") == Suggestions(questions=["a", "b", "c"])


def test_thinking_time_is_stored_with_the_message(tmp_path):
    from app.research_llm import FakeResearchPlanner, FakeToolModel, ThinkingTimer
    from tests.test_research import Chat

    p = FakeResearchPlanner()
    p._model = FakeToolModel(callbacks=[ThinkingTimer()])
    v = Chat(planner=p).say("直接答：好")
    thinking = next(i for i in v["turns"][0]["items"] if i["type"] == "thinking")
    assert isinstance(thinking["ms"], int)
