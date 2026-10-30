"""调研场景接口：建线程、接着说、回答提问、任务列表，以及几种拒绝（400 / 404 / 409）。只用假模型。"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.api import main as api_main
from app.api.research_runner import ResearchRunner
from app.graph.research.graph import memory_checkpointer, open_sqlite_checkpointer, thread_ids


@pytest.fixture
def client(tmp_path):
    api_main.set_research_runner(ResearchRunner(fake=True, data_dir=tmp_path / "data", out_dir=tmp_path / "out"))
    return TestClient(api_main.app)


def _events(resp) -> list[tuple[str, dict]]:
    assert resp.status_code == 200, resp.text
    out, ev = [], None
    for line in resp.text.splitlines():
        if line.startswith("event: "):
            ev = line[7:]
        elif line.startswith("data: "):
            out.append((ev, json.loads(line[6:])))
    return out


def _last(events, name):
    return [d for e, d in events if e == name][-1]


def _new(client, text: str) -> list[tuple[str, dict]]:
    return _events(client.post("/api/research/threads", json={"text": text}))


def _say(client, tid: str, text: str = "", **kw):
    return client.post(f"/api/research/threads/{tid}/messages", json={"text": text, **kw})


def _resume(client, st: dict, payload: dict):
    return client.post(f"/api/research/threads/{st['thread_id']}/resume", json={"interrupt_id": st["interrupt"]["id"], "payload": payload})


def test_conversation_over_http(client):
    ev = _new(client, "调研一下局部修改")
    names = [e for e, _ in ev]
    assert names[:2] == ["thread", "started"] and names[-2:] == ["state", "end"]
    assert {"title", "step", "delta", "writing", "node"} <= set(names)
    st = _last(ev, "state")
    tid = st["thread_id"]
    assert st["status"] == "idle" and st["report"]["version"] == 1 and st["title"] == _last(ev, "title")["title"]
    assert client.get(f"/api/research/threads/{tid}/state").json() == st  # 刷新后看到的和刚才一样

    ev = _events(_say(client, tid, "直接答：为什么不稳定？"))
    st = _last(ev, "state")
    assert len(st["turns"]) == 2 and st["turns"][1]["record"]["version"] is None and "title" not in [e for e, _ in ev]

    src = next(s["id"] for s in st["sources"].values() if s["status"] == "adopted")
    st = _last(_events(_say(client, tid, action={"type": "exclude_source", "source_id": src})), "state")
    assert st["report"]["version"] == 2 and [x["n"] for x in st["versions"]] == [1, 2]


def test_ask_over_http(client):
    st = _last(_new(client, "问一下 直接答"), "state")
    assert st["status"] == "waiting" and st["interrupt"]["type"] == "research.ask"
    tid = st["thread_id"]
    assert _say(client, tid, "先别问").status_code == 409  # 先回答或跳过
    assert _resume(client, st, {"answer": "包含", "skip": True}).status_code == 400
    r = client.post(f"/api/research/threads/{tid}/resume", json={"interrupt_id": "old", "payload": {"answer": "包含"}})
    assert r.status_code == 409
    end = _last(_events(_resume(client, st, {"answer": "包含"})), "state")
    assert end["status"] == "idle" and end["interrupt"] is None
    assert _resume(client, st, {"answer": "包含"}).status_code == 409  # 卡已经答过了
    assert _say(client, tid, "直接答：好").status_code == 200


def test_bad_messages_rejected(client):
    assert client.post("/api/research/threads", json={"text": "  "}).status_code == 400
    assert client.post("/api/research/threads", json={"request": "旧字段"}).status_code == 422
    tid = _last(_new(client, "调研一下局部修改"), "state")["thread_id"]
    assert _say(client, tid, "  ").status_code == 400
    assert _say(client, tid, "看看", quote={"text": "x", "claim_ids": ["k99"], "section_ids": []}).status_code == 400
    assert _say(client, tid, action={"type": "rewrite_section", "section_id": "c99"}).status_code == 400
    assert _say(client, tid, "x", extra=1).status_code == 422


def test_unknown_thread_404(client):
    for r in (client.get("/api/research/threads/nope0000/state"), _say(client, "nope0000", "x"),
              client.post("/api/research/threads/nope0000/retry"), client.get("/api/research/threads/nope0000/events")):
        assert r.status_code == 404


def test_list_threads_with_status(client):
    waiting = _last(_new(client, "问一下 直接答"), "state")
    done = _last(_new(client, "调研一下局部修改"), "state")
    rows = client.get("/api/research/threads").json()["threads"]
    by_id = {r["thread_id"]: r for r in rows}
    assert set(by_id) == {waiting["thread_id"], done["thread_id"]}
    assert rows[0]["thread_id"] == done["thread_id"]  # 最近有动静的在前
    w, d = by_id[waiting["thread_id"]], by_id[done["thread_id"]]
    assert w["status"] == "waiting" and w["version"] is None and w["title"] == waiting["title"] and w["updated_at"]
    assert d["status"] == "idle" and d["version"] == 1 and d["running_since"] is None


def test_thread_ids_without_reading_checkpoints(tmp_path):
    assert thread_ids(open_sqlite_checkpointer(tmp_path / "c.sqlite")) == []  # 表还没建
    assert thread_ids(memory_checkpointer()) == []


# ---- 真实模型的适配（不发请求） ----


@pytest.fixture
def deepseek(monkeypatch):
    from app import config
    from app.research_llm import make_chat_model

    monkeypatch.setattr(config, "DEEPSEEK_API_KEY", "k")
    return make_chat_model("deepseek", "m", thinking=True)


def test_deepseek_passes_back_only_this_turns_reasoning(deepseek):
    """同一轮里的工具调用要把思考原样传回；之前几轮的不传（传了接口也不用，白占上下文）。"""
    call = [{"name": "search_web", "args": {"query": "q"}, "id": "c1", "type": "tool_call"}]
    msgs = [HumanMessage("第一轮"), AIMessage(content="", tool_calls=call, additional_kwargs={"reasoning_content": "旧思考"}),
            ToolMessage(content="[]", tool_call_id="c1"), AIMessage(content="答", additional_kwargs={"reasoning_content": "旧结论"}),
            HumanMessage("第二轮"), AIMessage(content="", tool_calls=[{**call[0], "id": "c2"}], additional_kwargs={"reasoning_content": "新思考"}),
            ToolMessage(content="[]", tool_call_id="c2")]
    payload = deepseek._get_request_payload(msgs)
    assert [m.get("reasoning_content") for m in payload["messages"]] == [None, None, None, None, None, "新思考", None]


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
    v = Chat(tmp_path, p).say("直接答：好")
    thinking = next(i for i in v["turns"][0]["items"] if i["type"] == "thinking")
    assert isinstance(thinking["ms"], int)
