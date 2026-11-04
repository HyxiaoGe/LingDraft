"""长期记忆：每天整理一次（做梦），整份放进主 agent 的系统提示词。只用假模型（FakeResearchPlanner.dream 的说明）。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.store.memory import InMemoryStore

from app.api import main as api_main
from app.api.research_runner import ResearchRunner
from app.graph.research import memory as memory_mod
from app.graph.research.agent import system_prompt
from app.graph.research.memory import CHINA_TZ, DreamSchedule, MemoryBook, conversation_of, due_at, next_at, parse_ops
from app.graph.research.state import Turn
from app.model_guard import ContentBlocked
from app.research_llm import FAKE_BLOCKED, FakeResearchPlanner, FakeToolModel
from tests.test_research import Chat


def _ops(*ops: dict) -> str:
    return json.dumps({"operations": list(ops)}, ensure_ascii=False)


def _texts(r: ResearchRunner) -> list[str]:
    return [m["text"] for m in r.memory.items()]


def _dream_prompts(p: FakeResearchPlanner) -> list[str]:
    return [x for k, x in p.calls if k == "dream"]


def test_dream_remembers_and_the_next_turn_sees_it():
    """整理一次记下用户说的事；之后每次调主 agent，系统提示词里都有这份记忆（在回答语言那段前面）。"""
    seen: list[list] = []

    class Recording(FakeToolModel):
        def _message(self, messages):
            seen.append(messages)
            return super()._message(messages)

    p = FakeResearchPlanner()
    p._model = Recording()
    chat = Chat(planner=p)
    chat.say("直接答 记住：用户住在杭州")
    assert "[What you remember about the user]" not in seen[-1][0].content  # 还没整理，没有这一段
    run = chat.r.dream()
    assert run["threads"] == 1 and run["added"] == 1 and run["ended_at"]
    assert _texts(chat.r) == ["用户住在杭州"]
    chat.say("直接答 今天吃什么")
    sys = seen[-1][0]
    assert isinstance(sys, SystemMessage)
    assert "<memory>\n- 用户住在杭州\n</memory>" in sys.content
    assert sys.content.index("[What you remember about the user]") < sys.content.index("[Visible response language]")


def test_only_new_turns_are_dreamed_and_earlier_ones_are_context():
    """整理过的对话没有新的一轮就不再交给模型；有了新的，之前的几轮只带用户的话。"""
    chat = Chat()
    chat.say("直接答 记住：用户是后端工程师")
    chat.r.dream()
    assert chat.r.dream()["threads"] == 0
    assert len(_dream_prompts(chat.planner)) == 1
    chat.say("直接答 记住：用户常用 Python")
    run = chat.r.dream()
    assert run["threads"] == 1 and _texts(chat.r) == ["用户是后端工程师", "用户常用 Python"]
    prompt = _dream_prompts(chat.planner)[-1]
    earlier = prompt.split("<earlier_turns")[1].split("</earlier_turns>")[0]
    assert "用户是后端工程师" in earlier and "<assistant>" not in earlier
    assert "[" + chat.r.memory.items()[0]["id"] + "] 用户是后端工程师" in prompt  # 现有的记忆带着 id，模型改、删用它


def test_update_and_delete_use_the_ids_and_bad_ones_are_skipped():
    chat = Chat()
    chat.say("直接答 记住：用户住在杭州")
    chat.r.dream()
    mid = chat.r.memory.items()[0]["id"]
    created = chat.r.memory.items()[0]["created_at"]
    chat.say("直接答 我搬到上海了")
    chat.planner.dream_replies = [_ops({"action": "update", "id": mid, "text": "用户住在上海（2026-11 起）"},
                                       {"action": "update", "id": "nope", "text": "x"}, {"action": "add", "text": "  "})]
    run = chat.r.dream()
    assert (run["updated"], run["skipped"]) == (1, 2)
    [m] = chat.r.memory.items()
    assert (m["id"], m["text"], m["created_at"]) == (mid, "用户住在上海（2026-11 起）", created)
    chat.say("直接答 忘掉我住哪儿")
    chat.planner.dream_replies = [_ops({"action": "delete", "id": mid})]
    assert chat.r.dream()["deleted"] == 1 and chat.r.memory.items() == []


def test_a_failed_conversation_is_tried_again_next_time():
    """模型的结果两次都解析不了：这段算没整理，下次再来；别的对话照常整理。"""
    chat = Chat()
    chat.say("直接答 记住：甲")
    chat.planner.dream_replies = ["不是 json", "也不是"]
    run = chat.r.dream()
    assert (run["failed"], run["threads"]) == (1, 0) and _texts(chat.r) == []
    run = chat.r.dream()
    assert run["threads"] == 1 and _texts(chat.r) == ["甲"]


def test_content_review_counts_as_dreamed(monkeypatch):
    """整理时被内容审核拦下：记成整理过，不然每天都会再拦一次。"""
    chat = Chat()
    chat.say("直接答 记住：甲")

    def blocked(prompt):
        raise ContentBlocked(output=False)

    monkeypatch.setattr(chat.planner, "dream", blocked)
    assert chat.r.dream()["threads"] == 1
    assert chat.r.dream()["threads"] == 0


def test_blocked_and_unfinished_turns_are_left_out():
    """被内容审核拦下的一轮、还没结束的一轮不交给模型。"""
    chat = Chat()
    chat.say(f"直接答 记住：乙 {FAKE_BLOCKED}")
    assert chat.r.dream()["threads"] == 0
    v = {"messages": [HumanMessage("记住：丙", id="h1")], "turns": [Turn(id="h1", started_at="2026-11-03T01:00:00+00:00")]}
    assert conversation_of("t", v, {}) is None


def test_a_changed_turn_is_looked_at_again():
    """改过话、重新回答的一轮内容变了，下次整理再看一遍（模型对照已有的记忆，不会重复记）；回答一字不差就不用再看。"""
    chat = Chat()
    chat.say("直接答 记住：丁")
    chat.r.dream()
    chat.regenerate()
    assert chat.r.dream()["threads"] == 0  # 假模型重新回答还是那几个字
    chat.r.regenerate_input(chat.tid, "直接答 记住：庚")
    chat.run()
    chat.planner.dream_replies = [_ops()]
    assert chat.r.dream()["threads"] == 1 and _texts(chat.r) == ["丁"]
    assert "记住：庚" in _dream_prompts(chat.planner)[-1]


def test_later_conversations_are_dreamed_last():
    """几段对话按新的几轮结束的先后整理，后说的改得了先说的。"""
    book = MemoryBook(InMemoryStore())

    def conv(tid, text, ended):
        return tid, {"messages": [HumanMessage(text, id=tid + "h"), AIMessage("好")],
                     "turns": [Turn(id=tid + "h", started_at=ended, ended_at=ended)]}

    order = [c.thread_id for c in book.pending([conv("late", "b", "2026-11-03T05:00:00+00:00"), conv("early", "a", "2026-11-02T05:00:00+00:00")])]
    assert order == ["early", "late"]


def test_deleting_a_conversation_keeps_its_memories():
    chat = Chat()
    chat.say("直接答 记住：戊")
    chat.r.dream()
    chat.r.delete(chat.tid)
    assert _texts(chat.r) == ["戊"]
    assert chat.r.memory.store.get(memory_mod.THREADS, chat.tid) is None


def test_only_one_dream_at_a_time():
    chat = Chat()
    chat.r.memory._dreaming.acquire()
    try:
        assert chat.r.dream() is None
    finally:
        chat.r.memory._dreaming.release()


def test_parse_accepts_fenced_json_and_rejects_the_rest():
    assert [o.action for o in parse_ops('```json\n{"operations": [{"action": "add", "text": "x"}]}\n```')] == ["add"]
    for bad in ("", "{}", '{"operations": [{"action": "move"}]}'):
        try:
            parse_ops(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_memory_section_only_when_there_are_memories():
    at = datetime(2026, 11, 3, 1, tzinfo=UTC)
    assert "[What you remember about the user]" not in system_prompt(at)
    assert "<memory>\n- 甲\n</memory>" in system_prompt(at, memory="- 甲")


def test_schedule_is_three_in_the_morning_beijing_time():
    assert due_at(datetime(2026, 11, 3, 18, 59, tzinfo=UTC), 3) == datetime(2026, 11, 3, 3, tzinfo=CHINA_TZ)  # 北京 11-04 02:59
    assert due_at(datetime(2026, 11, 3, 19, 0, tzinfo=UTC), 3) == datetime(2026, 11, 4, 3, tzinfo=CHINA_TZ)
    assert next_at(datetime(2026, 11, 3, 19, 0, tzinfo=UTC), 3) == datetime(2026, 11, 5, 3, tzinfo=CHINA_TZ)


def test_schedule_catches_up_after_a_missed_night_but_not_on_the_first_start():
    now = datetime.now(UTC)
    slot = due_at(now, 3)
    last: dict | None = None
    s = DreamSchedule(lambda: None, lambda: last, 3)
    assert not s.due(now)  # 从来没整理过：等到点
    assert s.due(now + timedelta(days=1))
    last = {"started_at": (slot - timedelta(minutes=1)).isoformat()}
    assert s.due(now)  # 上次整理早于最近一次该整理的时间：补
    last = {"started_at": (slot + timedelta(minutes=1)).isoformat()}
    assert not s.due(now)


def test_memory_page_over_http(tmp_path):
    """记忆页的接口：全部记忆、上次整理、下次整理的时间。存起来的 Store（见 conftest 的 _storage）。"""
    r = ResearchRunner(fake=True)
    api_main.set_research_runner(r)
    client = TestClient(api_main.app)
    body = client.get("/api/memory").json()
    assert body["memories"] == [] and body["last_dream"] is None
    r.accept("t1", "直接答 记住：己")
    r.run_turn("t1")
    r.dream()
    body = client.get("/api/memory").json()
    assert [m["text"] for m in body["memories"]] == ["己"] and set(body["memories"][0]) == {"id", "text", "created_at", "updated_at"}
    assert body["last_dream"]["threads"] == 1
    reopened = ResearchRunner(fake=True)  # 存在库里，重启还在
    assert _texts(reopened) == ["己"]
