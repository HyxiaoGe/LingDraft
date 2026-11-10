"""模型问用户（ask_user，人介入）：这一轮停下来等回答，回答或跳过以后同一轮接着跑；等的时候哪些能做、哪些先拒掉。
只用假模型（“问我”“边搜边问”“问两个”“一直问”，见 FakeToolModel）。能问的都是 Work 模式下发的话（work=True）；Chat 的见最后一节。"""

from __future__ import annotations

import time
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage

from app.api import main as api_main
from app.api import research_runner
from app.api.research_runner import ResearchRunner
from app.api.run_store import ASKED
from app.api.runner import LocalEventLog, LocalRunRegistry
from app.graph.research import history, prompts
from app.research_llm import FakeToolModel
from app.tools.ask_user import MAX_ANSWER, AskAnswer, make_ask_tool
from tests.auth_helpers import USER
from tests.research_helpers import events, last, new, say, state, wait_until
from tests.test_research_queue import _gate, _guide, _queue, _Running

QUESTION = "这份对比是用来选型，还是做分享？"
OPTIONS = ["选型", "分享"]  # 假模型给的四个里有一个重复、一个空的，工具整理掉了


@pytest.fixture
def rr(tmp_path):
    return ResearchRunner(fake=True)


@pytest.fixture
def client(rr):
    return _client(rr)


def _client(runner: ResearchRunner) -> TestClient:
    api_main.set_research_runner(runner)
    return TestClient(api_main.app, headers=USER)


def _answer(http: TestClient, tid: str, ask_id: str, text: str = "", skip: bool = False):
    return http.post(f"/api/research/threads/{tid}/answer", json={"id": ask_id, "text": text, "skip": skip})


def _asking(http: TestClient, text: str) -> dict:
    st = new(http, text, work=True)
    assert st["status"] == "asking" and st["ask"], st
    return st


def _asks(turn: dict) -> list[dict]:
    return [i for i in turn["items"] if i["type"] == "ask"]


def _reply(turn: dict) -> str:
    return "".join(i["text"] for i in turn["items"] if i["type"] == "text")


def _runs(http: TestClient, tid: str) -> list[tuple[str, str]]:
    """这个对话的每次执行（怎么来的，结果），先后顺序。"""
    return [(r["kind"], r["status"]) for r in reversed(http.get(f"/api/research/threads/{tid}/runs").json()["runs"])]


# ---- 问、回答、跳过 ----


def test_ask_pauses_the_turn_and_the_answer_continues_it(client, rr):
    ev = events(client.post("/api/research/threads", json={"text": "问我 再做对比", "work": True}))
    st = last(ev, "state")
    tid = st["thread_id"]
    # 停下来了：不是失败，也没在跑；这次执行记成 asked，运行位退了
    assert st["status"] == "asking" and st["error"] is None and not st["stopping"]
    assert rr.runs.get(rr._run_key(tid)) is None and _runs(client, tid) == [("new", ASKED)]
    ask = st["ask"]
    assert ask["question"] == QUESTION and ask["options"] == OPTIONS and ask["id"]
    (turn,) = st["turns"]
    (item,) = _asks(turn)
    assert item == {"type": "ask", "id": ask["call_id"], "question": QUESTION, "options": OPTIONS, "pending": True, "answer": "", "skipped": False}
    assert _reply(turn) == "" and ("phase", {"phase": "suggest"}) not in ev  # 这一轮还没完：没有回答，不起推荐问题
    (live,) = [d["ask"] for e, d in ev if e == "ask"]  # 运行中页面先从事件里拿到这张卡
    assert live == item
    assert state(client, tid)["ask"] == ask  # 重新打开页面还在
    assert client.get("/api/research/threads").json()["threads"][0]["status"] == "asking"

    time.sleep(0.05)
    ev = events(_answer(client, tid, ask["id"], "  选型  "))
    end = last(ev, "state")
    assert end["status"] == "idle" and end["ask"] is None
    (turn,) = end["turns"]  # 还是同一轮
    (item,) = _asks(turn)
    assert (item["pending"], item["answer"], item["skipped"]) == (False, "选型", False)
    assert _reply(turn) == "收到回答：选型。"
    (live,) = [d["ask"] for e, d in ev if e == "ask"]
    assert live == item
    rec = turn["record"]
    assert rec["outcome"] == "ok" and rec["waited_ms"] >= 50
    took = (datetime.fromisoformat(rec["ended_at"]) - datetime.fromisoformat(rec["started_at"])).total_seconds() * 1000
    assert rec["waited_ms"] < took + 1000  # 等回答的那段在这一轮的起止之内（起止只记到秒），页面算用时的时候去掉
    assert _runs(client, tid) == [("new", ASKED), ("answer", "success")]
    assert wait_until(lambda: state(client, tid)["turns"][0]["record"]["suggestions"])  # 答完了照常起推荐问题
    # 模型看到的：那次工具调用的结果是用户的回答
    msgs = rr.graph.get_state(rr._cfg(tid)).values["messages"]
    (result,) = [m for m in msgs if isinstance(m, ToolMessage) and m.name == history.ASK_TOOL]
    assert result.content == prompts.render("tools.ask_user.answered", answer="选型")
    assert say(client, tid, "直接答：接着说").status_code == 200


def test_skip_lets_the_model_go_on_with_its_own_assumption(client, rr):
    st = _asking(client, "问我 再做对比")
    end = last(events(_answer(client, st["thread_id"], st["ask"]["id"], skip=True)), "state")
    (turn,) = end["turns"]
    (item,) = _asks(turn)
    assert end["status"] == "idle" and (item["pending"], item["answer"], item["skipped"]) == (False, "", True)
    assert _reply(turn) == "你跳过了，我按选型做。" and turn["record"]["outcome"] == "ok"
    (result,) = [m for m in rr.graph.get_state(rr._cfg(st["thread_id"])).values["messages"] if isinstance(m, ToolMessage)]
    assert result.content == prompts.render("tools.ask_user.skipped")


def test_answers_that_do_not_fit(client, rr):
    st = _asking(client, "问我 再做对比")
    tid, ask_id = st["thread_id"], st["ask"]["id"]
    assert _answer(client, tid, ask_id, "   ").status_code == 400  # 空的
    assert _answer(client, tid, ask_id, "长" * (MAX_ANSWER + 1)).status_code == 400
    r = _answer(client, tid, "nope", "选型")  # 不是现在等着的那个问题
    assert r.status_code == 409 and r.json()["detail"] == "这个问题已经不用回答了"
    assert client.post(f"/api/research/threads/{tid}/answer", json={"id": ask_id, "text": "选型", "more": 1}).status_code == 422
    assert state(client, tid)["status"] == "asking" and state(client, tid)["ask"]["id"] == ask_id  # 都没动它，还等着
    assert rr.runs.get(rr._run_key(tid)) is None
    assert last(events(_answer(client, tid, ask_id, "分享")), "state")["status"] == "idle"
    assert _answer(client, tid, ask_id, "选型").status_code == 409  # 答过了
    idle = new(client, "直接答：没问问题")
    assert _answer(client, idle["thread_id"], ask_id, "选型").status_code == 409


def test_at_most_two_questions_a_turn(client, rr):
    """一轮最多问 MAX_ASKS 次：再问的不停下来，工具直接让模型按自己的判断做。"""
    st = _asking(client, "一直问")
    tid = st["thread_id"]
    for n in range(history.MAX_ASKS - 1):
        st = last(events(_answer(client, tid, st["ask"]["id"], f"回答{n}")), "state")
        assert st["status"] == "asking"
    end = last(events(_answer(client, tid, st["ask"]["id"], "最后一个")), "state")
    (turn,) = end["turns"]
    assert end["status"] == "idle" and len(_asks(turn)) == history.MAX_ASKS  # 没问出去的那次不在过程里
    assert _reply(turn) == "收到回答：回答0；收到回答：最后一个；没问成。"
    (refused,) = [m for m in rr.graph.get_state(rr._cfg(tid)).values["messages"]
                  if isinstance(m, ToolMessage) and m.name == history.ASK_TOOL and history.ask_answer(m) is None]
    assert refused.content == prompts.render("tools.ask_user.limit", max_asks=history.MAX_ASKS)


def test_two_questions_in_one_message_are_answered_one_at_a_time(client, rr):
    st = _asking(client, "问两个")
    tid = st["thread_id"]
    first = st["ask"]
    assert [i["pending"] for i in _asks(st["turns"][0])] == [True, True] and first["call_id"] == _asks(st["turns"][0])[0]["id"]
    st = last(events(_answer(client, tid, first["id"], "选型")), "state")
    second = st["ask"]
    assert st["status"] == "asking" and second["id"] != first["id"] and second["call_id"] == _asks(st["turns"][0])[1]["id"]
    assert [(i["pending"], i["answer"]) for i in _asks(st["turns"][0])] == [(False, "选型"), (True, "")]
    assert _answer(client, tid, first["id"], "分享").status_code == 409  # 前一个答过了
    end = last(events(_answer(client, tid, second["id"], skip=True)), "state")
    assert end["status"] == "idle" and _reply(end["turns"][0]) == "收到回答：选型；你跳过了，我按选型做。"


def test_search_sent_with_the_question_is_kept(client, rr):
    """同一条消息里又搜又问：搜索照常做完，回答以后不重搜。"""
    ev = events(client.post("/api/research/threads", json={"text": "边搜边问", "work": True}))
    st = last(ev, "state")
    tid = st["thread_id"]
    assert st["status"] == "asking"
    steps = [i for i in st["turns"][0]["items"] if i["type"] == "step"]
    assert [(s["kind"], s["pending"], s["ok"]) for s in steps] == [("search", False, True)] and len(st["sources"]) == 3
    ev = events(_answer(client, tid, st["ask"]["id"], "选型"))
    end = last(ev, "state")
    assert end["status"] == "idle" and [e for e, _ in ev if e == "step"] == [] and len(end["sources"]) == 3


# ---- 等回答的时候 ----


def test_while_asking_say_retry_and_rename_wait_for_the_answer(client, rr):
    st = _asking(client, "问我 再做对比")
    tid = st["thread_id"]
    r = say(client, tid, "直接答：先说别的")
    assert r.status_code == 409 and r.json()["detail"] == "先回答或跳过上面的问题"
    assert _queue(client, tid, "直接答：排一句").status_code == 409
    assert client.post(f"/api/research/threads/{tid}/retry").status_code == 409
    r = client.patch(f"/api/research/threads/{tid}", json={"title": "新名字"})
    assert r.status_code == 409 and r.json()["detail"].startswith("先回答或跳过上面的问题")
    now = state(client, tid)
    assert now["status"] == "asking" and now["ask"] == st["ask"] and len(now["turns"]) == 1  # 问题还在
    assert rr.runs.get(rr._run_key(tid)) is None
    assert last(events(_answer(client, tid, st["ask"]["id"], "选型")), "state")["status"] == "idle"


def test_stop_while_asking_ends_the_turn_unanswered(client, rr):
    st = _asking(client, "边搜边问")
    tid = st["thread_id"]
    assert client.post(f"/api/research/threads/{tid}/stop").status_code == 200
    end = state(client, tid)
    (turn,) = end["turns"]
    (item,) = _asks(turn)
    assert end["status"] == "idle" and end["ask"] is None and not end["stopping"]
    assert (item["pending"], item["answer"], item["skipped"]) == (False, "", False)  # 问过、没回答
    rec = turn["record"]
    assert rec["outcome"] == "stopped" and rec["waited_ms"] == 0
    assert [(s["kind"], s["ok"]) for s in turn["items"] if s["type"] == "step"] == [("search", True)] and len(end["sources"]) == 3  # 做完的搜索还在
    assert _answer(client, tid, st["ask"]["id"], "选型").status_code == 409  # 停了就不用回答了
    assert client.post(f"/api/research/threads/{tid}/stop").status_code == 409  # 没在跑、也没在等
    again = last(events(say(client, tid, "直接答：接着说")), "state")
    assert again["status"] == "idle" and [t["record"]["outcome"] for t in again["turns"]] == ["stopped", "ok"]


def test_stopped_turn_ends_when_the_question_was_asked(client, rr):
    """等了多久再点停止，这一轮的用时都只算到问出来的时候。"""
    st = _asking(client, "问我 再做对比")
    time.sleep(0.3)
    asked = datetime.fromisoformat(rr.graph.get_state(rr._cfg(st["thread_id"])).created_at)
    client.post(f"/api/research/threads/{st['thread_id']}/stop")
    rec = state(client, st["thread_id"])["turns"][0]["record"]
    assert datetime.fromisoformat(rec["ended_at"]) == asked


def test_regenerate_and_edit_while_asking_start_the_turn_over(client, rr):
    st = _asking(client, "边搜边问")
    tid = st["thread_id"]
    again = last(events(client.post(f"/api/research/threads/{tid}/regenerate")), "state")
    (turn,) = again["turns"]
    assert again["status"] == "asking" and again["ask"]["id"] != st["ask"]["id"]  # 重来一遍又问了：是新的一次提问
    assert len(_asks(turn)) == 1 and len([i for i in turn["items"] if i["type"] == "step"]) == 1  # 上一遍的不留
    assert _answer(client, tid, st["ask"]["id"], "选型").status_code == 409
    edited = last(events(client.post(f"/api/research/threads/{tid}/edit", json={"text": "直接答：不用问了"})), "state")
    (turn,) = edited["turns"]
    assert edited["status"] == "idle" and edited["ask"] is None and _asks(turn) == [] and turn["user"] == "直接答：不用问了"
    assert turn["record"]["outcome"] == "ok" and turn["record"]["waited_ms"] == 0


def test_question_survives_restart(tmp_path):
    one = ResearchRunner(fake=True)
    st = _asking(_client(one), "问我 再做对比")
    tid = st["thread_id"]
    fresh = ResearchRunner(fake=True)  # 像服务重启：等着的问题在检查点里
    fresh.events, fresh.runs = LocalEventLog(), LocalRunRegistry()
    http = _client(fresh)
    assert state(http, tid)["status"] == "asking" and state(http, tid)["ask"] == st["ask"]
    end = last(events(_answer(http, tid, st["ask"]["id"], "分享")), "state")
    assert end["status"] == "idle" and _reply(end["turns"][0]) == "收到回答：分享。"


# ---- 排着的话、插进这一轮的话 ----


def test_queued_message_waits_through_the_question(client, rr):
    """答着的时候排了一句，模型接着停下来问了：排着的话留着（不放回输入框、也不发），回答完这一轮结束才发。"""
    tid = new(client, "直接答：先说一句")["thread_id"]
    with _Running(client, rr, tid, "问我 再做对比", work=True) as run:
        assert _queue(client, tid, "直接答：排着的").status_code == 200
    st = last(run.evs, "state")
    assert st["status"] == "asking" and st["queued_message"]["text"] == "直接答：排着的" and len(st["turns"]) == 2
    events(_answer(client, tid, st["ask"]["id"], "选型"))
    assert wait_until(lambda: (s := state(client, tid))["status"] == "idle" and len(s["turns"]) == 3)
    end = state(client, tid)
    assert end["queued_message"] is None and end["turns"][2]["user"] == "直接答：排着的"
    assert _reply(end["turns"][1]) == "收到回答：选型。"


def test_stop_while_asking_sends_the_queued_message(client, rr):
    tid = new(client, "直接答：先说一句")["thread_id"]
    with _Running(client, rr, tid, "问我 再做对比", work=True) as run:
        _queue(client, tid, "直接答：排着的")
    assert last(run.evs, "state")["status"] == "asking"
    assert client.post(f"/api/research/threads/{tid}/stop").status_code == 200
    assert wait_until(lambda: (s := state(client, tid))["status"] == "idle" and len(s["turns"]) == 3)
    end = state(client, tid)
    assert [t["record"]["outcome"] for t in end["turns"]] == ["ok", "stopped", "ok"] and end["turns"][2]["user"] == "直接答：排着的"


def test_guided_message_goes_in_after_the_answer(client, rr):
    """插进这一轮的话还没放进去，模型就停下来问了：等着，回答以后下一次调模型之前放进对话。"""
    tid = new(client, "直接答：先说一句")["thread_id"]
    gate = _gate(rr.planner)
    with _Running(client, rr, tid, "问我 再做对比", gate, work=True) as run:
        assert gate.waiting.wait(10)
        _queue(client, tid, "只要国产的")
        assert _guide(client, tid).json()["guided"]["text"] == "只要国产的"
    st = last(run.evs, "state")
    assert st["status"] == "asking" and st["guided_message"]["text"] == "只要国产的"
    end = last(events(_answer(client, tid, st["ask"]["id"], "选型")), "state")
    turn = end["turns"][1]
    assert end["status"] == "idle" and end["guided_message"] is None and len(end["turns"]) == 2
    assert [i["type"] for i in turn["items"] if i["type"] in ("ask", "guide")] == ["ask", "guide"]
    assert _reply(turn) == "收到：只要国产的。收到回答：选型。"


# ---- 状态怎么认、记录怎么算 ----


def _snap(messages: list, interrupts: list, errors: tuple = ()) -> SimpleNamespace:
    return SimpleNamespace(values={"messages": messages}, next=("tools",), interrupts=interrupts,
                           tasks=[SimpleNamespace(error=e) for e in errors])


def _interrupt(call_id: str, kind: str = "ask") -> SimpleNamespace:
    return SimpleNamespace(id=f"int-{call_id}", value={"type": kind, "call_id": call_id, "question": "问什么", "options": []})


def _call(cid: str, name: str = history.ASK_TOOL) -> dict:
    return {"name": name, "args": {"question": "问什么"}, "id": cid}


def test_status_is_asking_only_for_an_unanswered_question(rr):
    ai = AIMessage(content="", tool_calls=[_call("c1"), _call("c2")])
    assert rr._status(_snap([ai], [_interrupt("c1"), _interrupt("c2")]), None)[0] == "asking"
    # 答了一个的：答过的那次提问可能还留在检查点上，不算
    answered = ToolMessage(content="ok", name=history.ASK_TOOL, tool_call_id="c1", artifact={"ask": {"answer": "a"}})
    left = _snap([ai, answered], [_interrupt("c1"), _interrupt("c2")])
    assert rr._status(left, None)[0] == "asking"
    assert [a["call_id"] for a in research_runner._asks(left)] == ["c2"]
    # 同一步里别的工具抛了错：算失败，重试时问题会再问出来
    assert rr._status(_snap([ai], [_interrupt("c1")], errors=("RuntimeError('x')",)), None)[0] == "failed"
    # 不是在问用户的中断不认；在跑的还是在跑
    assert rr._status(_snap([ai], [_interrupt("c1", kind="other")]), None)[0] == "failed"
    assert rr._status(_snap([ai], [_interrupt("c1")]), {"queued": False})[0] == "running"


def test_waited_ms_is_the_union_of_the_waits():
    def result(cid: str, asked: str, answered: str) -> ToolMessage:
        got = AskAnswer(answer="a", asked_at=asked, answered_at=answered).model_dump()
        return ToolMessage(content="ok", name=history.ASK_TOOL, tool_call_id=cid, artifact={"ask": got})

    t = "2026-11-10T08:00:{:02d}+00:00".format
    assert history.waited_ms([]) == 0
    assert history.waited_ms([result("a", t(0), t(10))]) == 10_000
    # 一条消息里问了两个：等第二个的时间从同一刻算起，重叠的那段只算一次
    assert history.waited_ms([result("a", t(0), t(10)), result("b", t(0), t(25))]) == 25_000
    assert history.waited_ms([result("a", t(0), t(10)), result("b", t(20), t(25))]) == 15_000
    # 时间不对的（没填、倒着的）不算
    assert history.waited_ms([result("a", "", t(10)), result("b", t(10), t(5)), result("c", "x", "y")]) == 0


def test_ask_items_and_counting():
    call = _call("c1")
    assert history.ask_of(call, None)["pending"] is True
    assert history.ask_of({**call, "args": {"question": "  "}}, None) is None  # 没有问题的不列
    assert history.ask_of(_call("c1", "search_web"), None) is None
    refused = ToolMessage(content="不能再问了", name=history.ASK_TOOL, tool_call_id="c1")
    assert history.ask_of(call, refused) is None  # 没问出去的不列
    stopped = ToolMessage(content="停了", name=history.ASK_TOOL, tool_call_id="c1", artifact={"stopped": True})
    assert history.ask_of(call, stopped) == {"type": "ask", "id": "c1", "question": "问什么", "options": [], "pending": False, "answer": "",
                                             "skipped": False}
    assert history.ask_args({"question": " 问 ", "options": ["a", " a ", "", 3, "b", "c", "d", "e"]}) == ("问", ["a", "b", "c", "d"])
    assert history.ask_args({"question": None, "options": "a"}) == ("", [])
    # 数这次提问之前问出去了几次：问出去的（等着的、答了的、跳过的）算，没问出去的（超了上限、问题是空的）不算
    answered = ToolMessage(content="ok", name=history.ASK_TOOL, tool_call_id="c1", artifact={"ask": {"skip": True}})
    msgs = [AIMessage(content="", tool_calls=[_call("c1")]), answered, AIMessage(content="", tool_calls=[_call("c2")]),
            ToolMessage(content="不能再问了", name=history.ASK_TOOL, tool_call_id="c2"),
            AIMessage(content="", tool_calls=[_call("c3"), _call("c4")])]
    assert [history.asks_before(msgs, c) for c in ("c1", "c2", "c3", "c4")] == [0, 1, 1, 2]


def test_subagents_cannot_ask():
    """只给主 agent：子 agent 的工具是从 SUBAGENT_TOOLS 里挑的，里面没有它。"""
    from app.graph.research.subagent import SUBAGENT_TOOLS

    assert history.ASK_TOOL not in SUBAGENT_TOOLS and make_ask_tool().name == history.ASK_TOOL


# ---- Chat 和 Work：只有 Work 模式下发的话能问（2026-11-10 Sean 定） ----


def _watch(rr: ResearchRunner) -> list[tuple[str, list[str]]]:
    """换掉主 agent 的假模型：记下每次调它时的系统提示词、给了哪些工具；用户的话里有“硬问”的，没给 ask_user 也照调。"""
    seen: list[tuple[str, list[str]]] = []

    class Watching(FakeToolModel):
        def _decide(self, messages):
            seen.append((messages[0].content, list(self.bound)))
            if "硬问" in history.text_of(messages[history.last_human(messages)]):
                if isinstance(messages[-1], ToolMessage):
                    return "照自己的判断做。", []
                return "", [{"name": history.ASK_TOOL, "args": {"question": "做什么用？"}, "id": "call_forced"}]
            return super()._decide(messages)

    rr.planner._model = Watching()
    return seen


def _modes(st: dict) -> list[bool]:
    return [t["record"]["work"] for t in st["turns"]]


def test_only_work_messages_get_the_work_prompt_and_the_question_tool(client, rr):
    """按每句话算，不按对话：Work 下发的话，系统提示词多 Work 那一段、给问用户的工具；Chat 下发的两样都没有，同一句话也不问。"""
    seen = _watch(rr)
    st = new(client, "问我 再做对比")
    tid = st["thread_id"]
    assert st["status"] == "idle" and st["ask"] is None and _asks(st["turns"][0]) == [] and _modes(st) == [False]
    assert seen and all("[Work mode]" not in sys and history.ASK_TOOL not in tools for sys, tools in seen)
    seen.clear()
    st = last(events(say(client, tid, "问我 再做对比", work=True)), "state")
    assert st["status"] == "asking" and _modes(st) == [False, True]
    end = last(events(_answer(client, tid, st["ask"]["id"], "选型")), "state")
    assert end["status"] == "idle" and len(seen) == 2  # 问之前一次、回答之后一次，都是 Work 的
    assert all("[Work mode]" in sys and history.ASK_TOOL in tools for sys, tools in seen)
    assert seen[0][0].index("[Work mode]") < seen[0][0].index("[Visible response language]")
    seen.clear()
    st = last(events(say(client, tid, "问我 又来一遍", work=False)), "state")  # 下一句切回 Chat：之前问过也不给了
    assert st["status"] == "idle" and _modes(st) == [False, True, False] and _asks(st["turns"][2]) == []
    assert seen and all("[Work mode]" not in sys and history.ASK_TOOL not in tools for sys, tools in seen)


def test_question_tool_called_in_a_chat_turn_does_not_pause(client, rr):
    """Chat 的轮里模型硬调了 ask_user（没给它，照着前几轮的样子调的）：不停下来，工具让它在正文里问、或者按自己的判断做。"""
    _watch(rr)
    st = new(client, "硬问 做个对比")
    (turn,) = st["turns"]
    assert st["status"] == "idle" and st["ask"] is None and _asks(turn) == [] and _reply(turn) == "照自己的判断做。"
    (result,) = [m for m in rr.graph.get_state(rr._cfg(st["thread_id"])).values["messages"] if isinstance(m, ToolMessage)]
    assert result.content == prompts.render("tools.ask_user.chat") and history.ask_answer(result) is None
    # 同样硬调，Work 的轮里是真问
    assert new(client, "硬问 做个对比", work=True)["status"] == "asking"


def test_queued_edited_and_regenerated_messages_keep_their_mode(client, rr):
    """排着的话带着发的时候的模式，轮到它时照那个模式跑；改写、重新生成没带 work 的照原来那句，带了的按带的。"""
    tid = new(client, "直接答：先说一句")["thread_id"]
    with _Running(client, rr, tid, "直接答：再说一句"):
        assert client.post(f"/api/research/threads/{tid}/queue", json={"text": "问我 排着的", "work": True}).status_code == 200
        assert state(client, tid)["queued_message"]["work"] is True
    assert wait_until(lambda: state(client, tid)["status"] == "asking")
    assert _modes(state(client, tid)) == [False, False, True]
    edited = last(events(client.post(f"/api/research/threads/{tid}/edit", json={"text": "问我 换个说法"})), "state")
    assert edited["status"] == "asking" and _modes(edited) == [False, False, True]  # 改写没带：还是 Work，又问了
    chat = last(events(client.post(f"/api/research/threads/{tid}/regenerate", json={"work": False})), "state")
    assert chat["status"] == "idle" and _modes(chat) == [False, False, False] and _asks(chat["turns"][2]) == []
    kept = last(events(client.post(f"/api/research/threads/{tid}/regenerate")), "state")
    assert kept["status"] == "idle" and _modes(kept) == [False, False, False]  # 没带：照上一遍的
    back = last(events(client.post(f"/api/research/threads/{tid}/regenerate", json={"work": True})), "state")
    assert back["status"] == "asking" and _modes(back) == [False, False, True]
