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
from app.graph.research.graph import memory_checkpointer, open_sqlite_checkpointer, thread_ids
from tests.research_helpers import events, last, say


@pytest.fixture
def client(tmp_path):
    api_main.set_research_runner(ResearchRunner(fake=True, data_dir=tmp_path / "data"))
    return TestClient(api_main.app)


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
    assert set(st) == {"thread_id", "exists", "scenario", "status", "error", "stopping", "title", "updated_at", "running_since", "turns", "sources",
                       "queued_message", "guided_message"}
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
    assert d == {"thread_id": done["thread_id"], "title": done["title"], "status": "idle", "updated_at": d["updated_at"], "running_since": None}
    assert d["updated_at"]


def test_thread_ids_without_reading_checkpoints(tmp_path):
    assert aio.run(thread_ids(aio.run(open_sqlite_checkpointer(tmp_path / "c.sqlite")))) == []  # 表还没建
    assert aio.run(thread_ids(memory_checkpointer())) == []


# ---- 真实模型的适配（不发请求） ----


@pytest.fixture
def deepseek(monkeypatch):
    from app import config
    from app.research_llm import make_chat_model

    monkeypatch.setattr(config, "DEEPSEEK_API_KEY", "k")
    return make_chat_model("deepseek", "m", thinking=True)


def test_deepseek_passes_back_all_reasoning(deepseek):
    """思考原样传回：同一轮里的不传接口报 400；之前几轮的也传，下一轮发出去的开头和上一轮一样，前缀缓存才接得上。"""
    call = [{"name": "search_web", "args": {"query": "q"}, "id": "c1", "type": "tool_call"}]
    msgs = [HumanMessage("第一轮"), AIMessage(content="", tool_calls=call, additional_kwargs={"reasoning_content": "旧思考"}),
            ToolMessage(content="[]", tool_call_id="c1"), AIMessage(content="答", additional_kwargs={"reasoning_content": "旧结论"}),
            HumanMessage("第二轮"), AIMessage(content="", tool_calls=[{**call[0], "id": "c2"}], additional_kwargs={"reasoning_content": "新思考"}),
            ToolMessage(content="[]", tool_call_id="c2")]
    payload = deepseek._get_request_payload(msgs)
    assert [m.get("reasoning_content") for m in payload["messages"]] == [None, "旧思考", None, "旧结论", None, "新思考", None]


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
