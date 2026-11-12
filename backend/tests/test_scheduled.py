"""定时任务（2026-11-12 Sean 定，见 app.scheduled）：什么时候跑的规则，建改删列，到点发起（额度用完、那段对话在跑、卡着、没跑成以后、
服务重启打断、对话删了），没人在场不出问题卡，模型的工具，接口。只用假模型；时间用假的钟。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import Field

from app import aio
from app.api import main as api_main
from app.api import research_runner
from app.api.research_runner import BUSY, SCHEDULED, ResearchRunner
from app.api.runner import LocalEventLog, LocalRunRegistry
from app.graph.research import history
from app.identity import USER as USER_TIER
from app.quota import Limits
from app.research_llm import FakeResearchPlanner, FakeToolModel
from app.scheduled import rule
from app.scheduled import service as sv
from app.scheduled.notice import opening
from app.scheduled.rule import CHINA_TZ, BadRule
from app.scheduled.service import ScheduledTasks, TaskError
from app.scheduled.store import MemoryTaskStore
from app.tools.schedule import with_run_notes
from tests.auth_helpers import USER, bearer, guest_token, user_token
from tests.research_helpers import events, flaky_runner, hold_model, last, new, say, state, wait_until

T0 = datetime(2026, 11, 13, 6, 0, tzinfo=CHINA_TZ)  # 周五早上 6 点
DAILY = "22 7 * * *"
NOTE = "[Scheduled run]"


def at(day: int, hour: int, minute: int = 0, month: int = 11) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=CHINA_TZ)


# ---- 什么时候跑 ----


@pytest.mark.parametrize(("expr", "nxt", "says"), [
    ("22 7 * * *", at(13, 7, 22), "每天 · 07:22"),
    ("0 9 * * 1-5", at(13, 9), "工作日 · 09:00"),
    ("30 18 * * 5", at(13, 18, 30), "每周五 · 18:30"),
    ("0 9 * * 0", at(15, 9), "每周日 · 09:00"),
    ("0 9 * * 7", at(15, 9), "每周日 · 09:00"),
    ("0 9 1,15 * *", at(15, 9), "每月 1、15 号 · 09:00"),
    ("5 * * * *", at(13, 6, 5), "每小时 · 第 5 分"),
    ("0 5 * * *", at(14, 5), "每天 · 05:00"),  # 今天的已经过了
    # 少见的写法（模型、表单里“自定义”建的）也说成中文，不把表达式给人看
    ("0 */2 * * *", at(13, 8), "每 2 小时 · 00:00 起"),
    ("30 9-18 * * 1-5", at(13, 9, 30), "工作日 · 09:30–18:30 每小时"),
    ("0 8-20/4 * * *", at(13, 8), "每天 · 08:00–20:00 每 4 小时"),
    ("0 8,9,13,20 * * 6,0", at(14, 8), "周末 · 08:00、09:00、13:00、20:00"),
    ("15 * * * 1", at(16, 0, 15), "每周一每小时 · 第 15 分"),
    ("0 9 1 1,7 *", datetime(2027, 1, 1, 9, tzinfo=CHINA_TZ), "每年 1、7 月 1 号 · 09:00"),
    ("0 9 * 6 1-5", datetime(2027, 6, 1, 9, tzinfo=CHINA_TZ), "每年 6 月的工作日 · 09:00"),
    ("0 9 * 12 *", at(1, 9, month=12), "每年 12 月每天 · 09:00"),
    ("0 9 15 * 5", at(13, 9), "每月 15 号或每周五 · 09:00"),
    ("0 9 1-31 * 5", at(13, 9), "每天 · 09:00"),  # 日和周都写了、日那一段是全的：对上一个就算，就是每天
])
def test_cron_next_run_and_description(expr, nxt, says):
    assert rule.next_run(expr, T0) == nxt
    assert rule.describe(expr, None) == says


def test_cron_day_of_month_and_weekday_match_either():
    # 日和周都写了：对上一个就算（cron 的老规矩）。11-13 是周五，11-15 是 15 号
    assert rule.next_run("0 9 15 * 5", T0) == at(13, 9)
    assert rule.next_run("0 9 15 * 5", at(13, 10)) == at(15, 9)


def test_cron_is_normalized_and_next_run_is_strictly_later():
    assert rule.normalize("  22   7 *  * * ") == DAILY
    assert rule.next_run(DAILY, at(13, 7, 22)) == at(14, 7, 22)  # 正好到点：算下一次
    assert rule.next_run("0 0 29 2 *", T0) == datetime(2028, 2, 29, tzinfo=CHINA_TZ)  # 隔几年才有一次的也找得到


@pytest.mark.parametrize(("expr", "code"), [
    ("*/5 * * * *", rule.TOO_OFTEN),
    ("0,30 9 * * *", rule.TOO_OFTEN),
    ("* * * * *", rule.TOO_OFTEN),
    ("0 9 31 2 *", rule.NEVER),
    ("0 9 * *", rule.BAD_CRON),
    ("0 25 * * *", rule.BAD_CRON),
    ("0 9 * * MON", rule.BAD_CRON),
    ("每天九点", rule.BAD_CRON),
])
def test_bad_cron_says_why(expr, code):
    with pytest.raises(BadRule) as e:
        rule.normalize(expr)
    assert e.value.code == code


def test_run_once_time_is_beijing_unless_it_says_otherwise():
    assert rule.parse_once("2026-11-13 17:00") == at(13, 17)
    assert rule.parse_once("2026-11-13T09:00:00+00:00") == at(13, 17)
    assert rule.describe(None, at(13, 17)) == "仅一次 · 2026-11-13 17:00"
    with pytest.raises(BadRule) as e:
        rule.parse_once("明天下午")
    assert e.value.code == rule.BAD_TIME


# ---- 建、改、删、列，和每次跑完怎么记 ----


@pytest.fixture
def clock() -> list[datetime]:
    return [T0]


def service(clock, **kw) -> ScheduledTasks:
    return ScheduledTasks(MemoryTaskStore(), clock=lambda: clock[0], **kw)


def make(tasks: ScheduledTasks, owner: str = "u1", **kw):
    kw = {"title": "每日简报", "prompt": "查今天的新闻"} | ({} if "once_at" in kw else {"cron": DAILY}) | kw
    return aio.run(tasks.create(owner, USER_TIER, **kw))


def rejected(coro) -> str:
    with pytest.raises(TaskError) as e:
        aio.run(coro)
    return e.value.code


def test_create_validates_and_computes_the_first_run(clock):
    tasks = service(clock)
    t = make(tasks, title="  每日   简报 ", prompt="  查今天的新闻\n", cron=" 22  7 * * *")
    assert (t.title, t.prompt, t.cron, t.next_at, t.status, t.tier) == ("每日 简报", "查今天的新闻", DAILY, at(13, 7, 22), sv.ACTIVE, USER_TIER)
    once = make(tasks, once_at="2026-11-13 17:00")
    assert (once.cron, once.once_at, once.next_at) == (None, at(13, 17), at(13, 17))
    for kw, code in (({"title": " "}, sv.BAD_TITLE), ({"title": "长" * (sv.TITLE_MAX + 1)}, sv.BAD_TITLE),
                     ({"prompt": ""}, sv.BAD_PROMPT), ({"prompt": "长" * (sv.PROMPT_MAX + 1)}, sv.BAD_PROMPT),
                     ({"cron": None}, sv.NO_SCHEDULE), ({"cron": DAILY, "once_at": "2026-11-14 09:00"}, sv.NO_SCHEDULE),
                     ({"cron": "*/5 * * * *"}, rule.TOO_OFTEN), ({"cron": None, "once_at": "2026-11-13 05:00"}, sv.IN_THE_PAST),
                     ({"cron": None, "once_at": "不知道"}, rule.BAD_TIME)):
        assert rejected(tasks.create("u1", USER_TIER, **({"title": "每日简报", "prompt": "查", "cron": DAILY} | kw))) == code, kw
    assert len(aio.run(tasks.of_owner("u1"))) == 2  # 没收的都没存


def test_each_user_has_a_cap_and_finished_one_offs_do_not_count(clock):
    tasks = service(clock, max_tasks=2)
    once = make(tasks, once_at="2026-11-13 17:00")
    make(tasks)
    assert rejected(tasks.create("u1", USER_TIER, title="三", prompt="查", cron=DAILY)) == sv.TOO_MANY
    make(tasks, owner="u2")  # 别人的不占
    aio.run(tasks.launched(once, "thread-1"))  # 只跑一次的跑过了：不占位置
    assert aio.run(tasks.get("u1", once.id)).status == sv.DONE
    third = make(tasks, title="三")
    # 满了以后，跑完了的想再开要先腾位置
    assert rejected(tasks.update("u1", once.id, once_at="2026-11-14 09:00")) == sv.TOO_MANY
    aio.run(tasks.delete("u1", third.id))
    again = aio.run(tasks.update("u1", once.id, once_at="2026-11-14 09:00"))
    assert (again.status, again.next_at) == (sv.ACTIVE, at(14, 9))


def test_tasks_belong_to_their_owner(clock):
    tasks = service(clock)
    t = make(tasks)
    for coro in (tasks.get("u2", t.id), tasks.update("u2", t.id, title="改"), tasks.delete("u2", t.id), tasks.get("u1", "task-none")):
        assert rejected(coro) == sv.NOT_FOUND
    assert aio.run(tasks.of_owner("u2")) == [] and aio.run(tasks.get("u1", t.id)).title == "每日简报"


def test_update_changes_only_what_is_given_and_reschedules(clock):
    tasks = service(clock)
    t = make(tasks)
    same = aio.run(tasks.update("u1", t.id))
    assert same == t
    renamed = aio.run(tasks.update("u1", t.id, title="早报", prompt="查早上的新闻"))
    assert (renamed.title, renamed.prompt, renamed.cron, renamed.next_at) == ("早报", "查早上的新闻", DAILY, t.next_at)
    weekly = aio.run(tasks.update("u1", t.id, cron="30 18 * * 5"))
    assert (weekly.cron, weekly.once_at, weekly.next_at) == ("30 18 * * 5", None, at(13, 18, 30))
    once = aio.run(tasks.update("u1", t.id, once_at="2026-11-20 08:00"))  # 重复的改成只跑一次
    assert (once.cron, once.once_at, once.next_at) == (None, at(20, 8), at(20, 8))
    assert rejected(tasks.update("u1", t.id, cron="* * * * *")) == rule.TOO_OFTEN
    assert rejected(tasks.update("u1", t.id, status="done")) == sv.BAD_STATUS
    assert aio.run(tasks.get("u1", t.id)) == once  # 没收的改动没存


def test_pause_and_resume(clock):
    tasks = service(clock)
    t = make(tasks)
    paused = aio.run(tasks.update("u1", t.id, status=sv.PAUSED))
    assert (paused.status, paused.paused_by, paused.next_at) == (sv.PAUSED, sv.BY_USER, None)
    assert aio.run(tasks.store.due(at(20, 0))) == []  # 停着的到点了也不跑
    clock[0] = at(13, 8)
    resumed = aio.run(tasks.update("u1", t.id, status=sv.ACTIVE))
    assert (resumed.status, resumed.paused_by, resumed.next_at) == (sv.ACTIVE, "", at(14, 7, 22))  # 从现在往后算，停着时错过的不补
    # 只跑一次的：停着时时间过了，恢复要一起给新的时间
    once = make(tasks, once_at="2026-11-13 17:00")
    aio.run(tasks.update("u1", once.id, status=sv.PAUSED))
    clock[0] = at(13, 18)
    assert rejected(tasks.update("u1", once.id, status=sv.ACTIVE)) == sv.IN_THE_PAST
    back = aio.run(tasks.update("u1", once.id, status=sv.ACTIVE, once_at="2026-11-14 09:00"))
    assert (back.status, back.next_at) == (sv.ACTIVE, at(14, 9))


def test_launching_moves_to_the_next_time_and_missed_ones_collapse(clock):
    tasks = service(clock)
    t = make(tasks)
    clock[0] = at(16, 10)  # 服务停了三天：错过三次
    assert [d.id for d in aio.run(tasks.store.due(clock[0]))] == [t.id]
    ran = aio.run(tasks.launched(t, "thread-1"))
    assert (ran.thread_id, ran.last_status, ran.last_at, ran.next_at) == ("thread-1", sv.RUNNING, at(16, 10), at(17, 7, 22))
    assert aio.run(tasks.store.due(clock[0])) == []  # 只补一次
    by_hand = aio.run(tasks.launched(ran, "thread-1", manual=True))
    assert by_hand.next_at == at(17, 7, 22)  # 立即执行不动原来定的下一次
    skipped = aio.run(tasks.skipped(t, sv.NO_CREDITS))
    assert (skipped.last_status, skipped.next_at, skipped.failures) == (sv.NO_CREDITS, at(17, 7, 22), 0)


def test_repeated_failures_pause_the_task(clock):
    tasks = service(clock, max_failures=3)
    t = make(tasks)
    assert aio.run(tasks.finished(t.id, sv.FAILED)).failures == 1
    ok = aio.run(tasks.finished(t.id, sv.OK))
    assert (ok.failures, ok.status) == (0, sv.ACTIVE)  # 中间跑成一次就重新数
    aio.run(tasks.finished(t.id, sv.FAILED))
    still = aio.run(tasks.finished(t.id, sv.FAILED))
    assert (still.failures, still.status, still.next_at) == (2, sv.ACTIVE, t.next_at)
    stopped = aio.run(tasks.finished(t.id, sv.FAILED))
    assert (stopped.status, stopped.paused_by, stopped.next_at, stopped.last_status) == (sv.PAUSED, sv.BY_FAILURES, None, sv.FAILED)
    resumed = aio.run(tasks.update("u1", t.id, status=sv.ACTIVE))
    assert (resumed.status, resumed.failures, resumed.paused_by) == (sv.ACTIVE, 0, "")
    assert aio.run(tasks.finished("task-none", sv.OK)) is None  # 跑着的时候任务被删了


def test_interrupted_run_is_tried_again_right_away(clock):
    tasks = service(clock)
    once = make(tasks, once_at="2026-11-13 17:00")
    clock[0] = at(13, 17)
    aio.run(tasks.launched(once, "thread-1"))  # 只跑一次的：交出去就算完了
    lost = aio.run(tasks.finished(once.id, sv.LOST))
    assert (lost.status, lost.next_at, lost.failures, lost.last_status) == (sv.ACTIVE, at(13, 17), 1, sv.LOST)  # 被重启打断：再来一次
    # 用户停掉的不因为被打断又开起来
    t = make(tasks)
    aio.run(tasks.update("u1", t.id, status=sv.PAUSED))
    assert aio.run(tasks.finished(t.id, sv.LOST)).status == sv.PAUSED


def test_view_for_the_page(clock):
    tasks = service(clock)
    v = tasks.view(make(tasks))
    assert v == {"id": v["id"], "title": "每日简报", "prompt": "查今天的新闻", "cron": DAILY, "once_at": None, "schedule": "每天 · 07:22",
                 "status": "active", "paused_by": "", "thread_id": None, "work": False, "model": None, "next_at": "2026-11-12T23:22:00+00:00",
                 "last_at": None, "last_status": "", "created_at": "2026-11-12T22:00:00+00:00", "updated_at": "2026-11-12T22:00:00+00:00"}
    assert v["id"].startswith("task-")


# ---- 表（内存、Postgres 各跑一遍，见 conftest） ----


def test_store_roundtrip(clock):
    rr = ResearchRunner(fake=True)
    store, tasks = rr.tasks.store, ScheduledTasks(rr.tasks.store, clock=lambda: clock[0])
    a = aio.run(tasks.create("u1", USER_TIER, title="甲", prompt="查甲", cron=DAILY, thread_id="thread-a", work=True, option="pro"))
    clock[0] = T0 + timedelta(seconds=1)
    b = aio.run(tasks.create("u1", USER_TIER, title="乙", prompt="查乙", once_at="2026-11-13 06:30"))
    c = aio.run(tasks.create("u2", USER_TIER, title="丙", prompt="查丙", cron="0 9 * * *", thread_id="thread-a"))
    assert aio.run(store.get(a.id)) == a and aio.run(store.get(b.id)) == b and aio.run(store.get("task-none")) is None
    assert [t.id for t in aio.run(store.of_owner("u1"))] == [a.id, b.id]  # 先建的在前
    assert aio.run(store.due(T0)) == [] and [t.id for t in aio.run(store.due(at(13, 8)))] == [b.id, a.id]  # 先到点的在前
    got = aio.run(store.patch(a.id, title="改了", next_at=None, failures=2))
    assert (got.title, got.next_at, got.failures, got.prompt, got.work, got.option) == ("改了", None, 2, "查甲", True, "pro")
    assert aio.run(store.patch("task-none", title="x")) is None
    aio.run(store.patch(b.id, last_status=sv.RUNNING))
    assert [t.id for t in aio.run(store.running())] == [b.id]
    aio.run(store.detach("thread-a"))  # 这段对话删了：两个人挂在上面的都摘下来
    assert aio.run(store.get(a.id)).thread_id is None and aio.run(store.get(c.id)).thread_id is None
    assert aio.run(store.delete(a.id)) and not aio.run(store.delete(a.id))
    assert [t.id for t in aio.run(store.of_owner("u1"))] == [b.id]


# ---- 到点发起 ----


class Recording(FakeToolModel):
    """记下主 agent 每次调模型发出去的消息、带的工具。bind_tools 复制后还是同一个 list。"""

    sent: list = Field(default_factory=list)

    def _message(self, messages):
        self.sent.append((list(messages), list(self.bound)))
        return super()._message(messages)


def _api(clock, runner: ResearchRunner | None = None, **headers) -> tuple[ResearchRunner, TestClient]:
    rr = runner or ResearchRunner(fake=True)
    rr.tasks.clock = lambda: clock[0]
    api_main.set_research_runner(rr)
    return rr, TestClient(api_main.app, headers=headers or USER)


def _recording(clock) -> tuple[ResearchRunner, TestClient, Recording]:
    p = FakeResearchPlanner()
    p._model = Recording()
    rr, client = _api(clock, ResearchRunner(planner=p))
    return rr, client, p._model


def _create(client: TestClient, **kw) -> dict:
    body = {"title": "每日简报", "prompt": "直接答今天的新闻"} | ({} if "once_at" in kw else {"cron": DAILY}) | kw
    r = client.post("/api/scheduled-tasks", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _task(client: TestClient, task_id: str) -> dict:
    return next(t for t in client.get("/api/scheduled-tasks").json()["tasks"] if t["id"] == task_id)


def _settled(client: TestClient, task_id: str) -> dict:
    """等任务这一次跑完、那段对话的占位也退了。"""
    assert wait_until(lambda: _task(client, task_id)["last_status"] != sv.RUNNING)
    t = _task(client, task_id)
    assert wait_until(lambda: state(client, t["thread_id"])["status"] not in ("queued", "running"))
    return t


def _fire(rr: ResearchRunner, client: TestClient, task_id: str) -> dict:
    assert rr.fire_due() == [(task_id, sv.RUNNING)]
    return _settled(client, task_id)


def _reply(turn: dict) -> str:
    return "".join(i["text"] for i in turn["items"] if i["type"] == "text")


def _steps(turn: dict) -> list[dict]:
    return [i for i in turn["items"] if i["type"] == "step" and i["kind"] == "schedule"]


def _runs(client: TestClient, tid: str) -> list[tuple[str, str]]:
    return [(r["kind"], r["status"]) for r in reversed(client.get(f"/api/research/threads/{tid}/runs").json()["runs"])]


def test_due_task_runs_in_a_new_conversation_named_after_it(clock):
    rr, client, model = _recording(clock)
    t = _create(client)
    assert (t["thread_id"], t["next_at"], t["schedule"]) == (None, "2026-11-12T23:22:00+00:00", "每天 · 07:22")
    assert rr.fire_due() == []  # 还没到点
    clock[0] = at(13, 7, 22)
    done = _fire(rr, client, t["id"])
    assert (done["last_status"], done["status"], done["next_at"]) == (sv.OK, "active", "2026-11-13T23:22:00+00:00")
    tid = done["thread_id"]
    st = state(client, tid)
    assert (st["status"], st["title"], st["work"]) == ("idle", "每日简报", False)  # 标题就是任务的名字，不让模型按那份交代再起
    turn = st["turns"][0]
    assert turn["scheduled"] == {"task_id": t["id"], "title": "每日简报"} and turn["user"] == "直接答今天的新闻"
    assert _reply(turn) == "直接回答：这个问题不用查。"
    assert rr.owner_of(tid) == "u1" and tid in [x["thread_id"] for x in client.get("/api/research/threads").json()["threads"]]
    assert _runs(client, tid) == [(SCHEDULED, "success")]
    # 发给模型的：交代前面有一段说明；存下来的那句话里没有
    said = next(m for m in model.sent[0][0] if isinstance(m, HumanMessage))
    assert said.content.startswith(NOTE) and '"每日简报"' in said.content and said.content.endswith("Instructions:\n直接答今天的新闻")
    stored = rr.graph.get_state(rr._cfg(tid)).values["messages"][0]
    assert stored.content == "直接答今天的新闻" and history.scheduled_of(stored) == {"task_id": t["id"], "title": "每日简报"}
    assert rr.fire_due() == []  # 这一次跑过了
    clock[0] = at(14, 7, 22)
    again = _fire(rr, client, t["id"])
    assert again["thread_id"] == tid and len(state(client, tid)["turns"]) == 2  # 下一次落在同一段对话里
    assert [bool(x["scheduled"]) for x in state(client, tid)["turns"]] == [True, True]


def test_task_made_in_a_conversation_runs_there(clock):
    rr, client = _api(clock)
    st = new(client, "建定时任务：直接答今天的新闻")
    tid = st["thread_id"]
    [t] = client.get("/api/scheduled-tasks").json()["tasks"]
    assert (t["thread_id"], t["title"], t["prompt"], t["cron"], t["work"]) == (tid, "每日简报", "直接答今天的新闻", DAILY, False)
    [step] = _steps(st["turns"][0])
    assert (step["text"], step["action"], step["limits"], step["ok"], step["pending"]) == ("每日简报", "create", ["每天 · 07:22"], True, False)
    assert _reply(st["turns"][0]) == f"建好了：{t['id']}"
    assert st["turns"][0]["scheduled"] is None
    clock[0] = at(13, 7, 22)
    _fire(rr, client, t["id"])
    turns = state(client, tid)["turns"]
    assert len(turns) == 2 and turns[1]["scheduled"]["task_id"] == t["id"] and turns[1]["user"] == "直接答今天的新闻"
    # 用户接着在这段对话里说话：照常的一轮
    end = last(events(say(client, tid, "直接答：谢谢")), "state")
    assert len(end["turns"]) == 3 and end["turns"][2]["scheduled"] is None and end["status"] == "idle"


def test_one_off_task_runs_once(clock):
    rr, client = _api(clock)
    t = _create(client, once_at="2026-11-13 17:00")
    assert (t["cron"], t["once_at"], t["schedule"]) == (None, "2026-11-13T09:00:00+00:00", "仅一次 · 2026-11-13 17:00")
    clock[0] = at(13, 17, 0)
    done = _fire(rr, client, t["id"])
    assert (done["status"], done["next_at"], done["last_status"]) == ("done", None, sv.OK)
    clock[0] = at(20, 17, 0)
    assert rr.fire_due() == []


def test_scheduled_turn_never_asks_the_user(clock):
    """工作任务里平时会停下来问的，定时任务触发的这一轮没人在场：不给问的工具，照着做完。"""
    rr, client, model = _recording(clock)
    asked = new(client, "问我用途再做对比", work=True)
    assert asked["status"] == "asking" and history.ASK_TOOL in model.sent[-1][1]
    t = _create(client, prompt="问我用途再做对比", work=True)
    clock[0] = at(13, 7, 22)
    done = _fire(rr, client, t["id"])
    st = state(client, done["thread_id"])
    assert (st["status"], st["work"], st["ask"]) == ("idle", True, None) and done["last_status"] == sv.OK
    assert not [i for i in st["turns"][0]["items"] if i["type"] == "ask"]
    sent, bound = model.sent[-1]
    assert history.ASK_TOOL not in bound and history.CREATE_TASK_TOOL in bound
    assert history.unattended({"messages": sent}) and not history.unattended({"messages": [HumanMessage("问我")]})


def test_scheduled_turn_can_manage_tasks_itself(clock):
    rr, client = _api(clock)
    t = _create(client, prompt="删定时任务")
    clock[0] = at(13, 7, 22)
    assert rr.fire_due() == [(t["id"], sv.RUNNING)]
    assert wait_until(lambda: client.get("/api/scheduled-tasks").json()["tasks"] == [])  # 这一轮里把自己删了
    [thread] = client.get("/api/research/threads").json()["threads"]
    assert wait_until(lambda: state(client, thread["thread_id"])["status"] == "idle")
    turn = state(client, thread["thread_id"])["turns"][0]
    assert [(s["action"], s["text"], s["limits"]) for s in _steps(turn)] == [("delete", "每日简报", [])] and _reply(turn) == "删了。"
    assert rr._scheduled == {}  # 结果记不上（任务没了）也不留着


def test_no_credits_skips_this_time(clock):
    rr, client = _api(clock)
    t = _create(client)
    rr.quota.limits = Limits(guest_daily=10, user_daily=0, user_weekly=0, guests_daily=50)
    clock[0] = at(13, 7, 22)
    assert rr.fire_due() == [(t["id"], sv.NO_CREDITS)]
    got = _task(client, t["id"])
    assert (got["last_status"], got["thread_id"], got["next_at"], got["status"]) == (sv.NO_CREDITS, None, "2026-11-13T23:22:00+00:00", "active")
    assert client.get("/api/research/threads").json()["threads"] == [] and rr.fire_due() == []


def test_blocked_conversation_skips_until_next_time(clock):
    """那段对话上一轮是用户自己的、出错了没重试：这一次不跑，等下一次；用户重试好了以后照常。"""
    rr = flaky_runner(None, lambda messages: any("会出错" in str(m.content) for m in messages if isinstance(m, HumanMessage)))
    rr, client = _api(clock, rr)
    tid = new(client, "建定时任务：直接答今天的新闻")["thread_id"]
    [t] = client.get("/api/scheduled-tasks").json()["tasks"]
    assert last(events(say(client, tid, "直接答：会出错")), "state")["status"] == "failed"
    clock[0] = at(13, 7, 22)
    assert rr.fire_due() == [(t["id"], sv.BLOCKED)]
    got = _task(client, t["id"])
    assert (got["last_status"], got["next_at"], got["status"]) == (sv.BLOCKED, "2026-11-13T23:22:00+00:00", "active")
    assert len(state(client, tid)["turns"]) == 2 and state(client, tid)["status"] == "failed"  # 什么都没动
    stuck = client.post(f"/api/scheduled-tasks/{t['id']}/run")  # 手动跑也一样，而且不记到任务上
    assert stuck.status_code == 409 and stuck.json()["detail"] == {"code": "task_rejected", "reason": "thread_stuck", "message": "这段对话上一轮没跑完，先去对话里看看"}
    assert _task(client, t["id"])["last_at"] == got["last_at"] and rr.runs.get(rr._run_key(tid)) is None
    assert last(events(client.post(f"/api/research/threads/{tid}/retry")), "state")["status"] == "idle"
    clock[0] = at(14, 7, 22)
    _fire(rr, client, t["id"])
    assert len(state(client, tid)["turns"]) == 3


def _scheduled_call(messages) -> bool:
    return any(NOTE in str(m.content) for m in messages if isinstance(m, HumanMessage))


def _stored(rr: ResearchRunner, tid: str) -> list:
    return rr.graph.get_state(rr._cfg(tid)).values["messages"]


def test_failed_run_is_redone_in_the_same_turn_next_time(clock, monkeypatch):
    monkeypatch.setattr(research_runner, "now", lambda: clock[0].astimezone(UTC).isoformat(timespec="seconds"))  # 那句话的发送时间也按假的钟
    rr = flaky_runner(None, _scheduled_call)  # 定时触发的这一轮第一次调模型就断了
    rr, client = _api(clock, rr)
    t = _create(client)
    clock[0] = at(13, 7, 22)
    failed = _fire(rr, client, t["id"])
    tid = failed["thread_id"]
    assert (failed["last_status"], failed["status"]) == (sv.FAILED, "active") and state(client, tid)["status"] == "failed"
    first = _stored(rr, tid)[0]
    assert rr.fire_due() == []  # 等下一次到点
    client.patch(f"/api/scheduled-tasks/{t['id']}", json={"title": "早报", "prompt": "直接答今天早上的新闻"})  # 这期间用户改了任务
    clock[0] = at(14, 7, 22)
    done = _fire(rr, client, t["id"])
    st = state(client, tid)
    assert done["last_status"] == sv.OK and st["status"] == "idle" and len(st["turns"]) == 1  # 那一轮重来，不另起一轮
    assert _reply(st["turns"][0]) == "直接回答：这个问题不用查。" and st["turns"][0]["record"]["outcome"] == "ok"
    assert _runs(client, tid) == [(SCHEDULED, "error"), (SCHEDULED, "success")]
    # 那句话换成任务现在的交代，触发时间记成这一次的（发给模型的说明里的时间按它写）
    again = _stored(rr, tid)[0]
    assert (again.id, again.content, history.scheduled_of(again)) == (first.id, "直接答今天早上的新闻", {"task_id": t["id"], "title": "早报"})
    assert (first.additional_kwargs["sent_at"], again.additional_kwargs["sent_at"]) == ("2026-11-12T23:22:00+00:00", "2026-11-13T23:22:00+00:00")
    assert "at 2026-11-14 07:22 (Saturday)" in with_run_notes([again])[0].content
    assert st["turns"][0]["user"] == "直接答今天早上的新闻" and st["turns"][0]["scheduled"]["title"] == "早报"


def test_failed_midway_starts_over_from_scratch(clock):
    """做到一半出错的：当时查到的到下一次已经旧了，那一轮从头重来，之前搜到的不留。"""
    rr = flaky_runner(None, lambda messages: _scheduled_call(messages) and isinstance(messages[-1], ToolMessage))
    rr, client = _api(clock, rr)
    t = _create(client, prompt="查一下局部修改")
    clock[0] = at(13, 7, 22)
    failed = _fire(rr, client, t["id"])
    tid = failed["thread_id"]
    assert failed["last_status"] == sv.FAILED and state(client, tid)["status"] == "failed"
    assert [type(m) for m in _stored(rr, tid)] == [HumanMessage, AIMessage, ToolMessage]  # 停在搜索之后
    old = {m.id for m in _stored(rr, tid)[1:]}
    clock[0] = at(14, 7, 22)
    done = _fire(rr, client, t["id"])
    st = state(client, tid)
    assert done["last_status"] == sv.OK and st["status"] == "idle" and len(st["turns"]) == 1 and "结论" in _reply(st["turns"][0])
    assert not old & {m.id for m in _stored(rr, tid)}  # 上一次的搜索没留着：这一次重新搜的
    assert [i["kind"] for i in st["turns"][0]["items"] if i["type"] == "step"] == ["search", "read"]


def test_scheduled_turns_are_left_out_of_memory(clock):
    """定时任务触发的一轮不拿去整理记忆：那句话是任务里的交代，不是这个人说的。"""
    from app.graph.research.memory import conversation_of
    rr, client = _api(clock)
    tid = new(client, "建定时任务：直接答今天的新闻")["thread_id"]
    [t] = client.get("/api/scheduled-tasks").json()["tasks"]
    clock[0] = at(13, 7, 22)
    _fire(rr, client, t["id"])
    conv = conversation_of(tid, rr.graph.get_state(rr._cfg(tid)).values, {})
    assert [x.user for x in conv.new] == ["建定时任务：直接答今天的新闻"] and len(state(client, tid)["turns"]) == 2


def test_three_failures_in_a_row_pause_the_task(clock):
    class Down(FakeToolModel):
        def _message(self, messages):
            if _scheduled_call(messages):
                raise RuntimeError("假模型：一直连不上")
            return super()._message(messages)

    p = FakeResearchPlanner()
    p._model = Down()
    rr, client = _api(clock, ResearchRunner(planner=p))
    t = _create(client)
    for day in (13, 14, 15):
        clock[0] = at(day, 7, 22)
        got = _fire(rr, client, t["id"])
    assert (got["status"], got["paused_by"], got["next_at"], got["last_status"]) == ("paused", "failures", None, sv.FAILED)
    clock[0] = at(16, 7, 22)
    assert rr.fire_due() == []
    assert len(state(client, got["thread_id"])["turns"]) == 1  # 三次都是同一轮重来
    # 用户恢复：重新数
    r = client.patch(f"/api/scheduled-tasks/{t['id']}", json={"status": "active"})
    assert (r.json()["status"], r.json()["paused_by"], r.json()["next_at"]) == ("active", "", "2026-11-16T23:22:00+00:00")


def test_run_cut_off_by_a_restart_is_picked_up_again(clock):
    one, client = _api(clock)
    t = _create(client)
    clock[0] = at(13, 7, 22)
    one._spawn = lambda work: work.close()  # 交出去了、还没跑，进程就停了
    assert one.fire_due() == [(t["id"], sv.RUNNING)]
    assert _task(client, t["id"])["last_status"] == sv.RUNNING
    fresh = ResearchRunner(fake=True)
    fresh.events, fresh.runs = LocalEventLog(), LocalRunRegistry()  # 重启后进程里什么都没有，只剩库里的
    fresh, client = _api(clock, fresh)
    fresh.recover()
    lost = _task(client, t["id"])
    assert (lost["last_status"], lost["status"], lost["next_at"]) == (sv.LOST, "active", "2026-11-12T23:22:00+00:00")  # 马上再来一次
    done = _fire(fresh, client, t["id"])
    st = state(client, done["thread_id"])
    assert done["last_status"] == sv.OK and done["next_at"] == "2026-11-13T23:22:00+00:00"
    assert st["status"] == "idle" and len(st["turns"]) == 1 and _reply(st["turns"][0]) == "直接回答：这个问题不用查。"


def test_deleting_the_conversation_keeps_the_task(clock):
    rr, client = _api(clock)
    tid = new(client, "建定时任务：直接答今天的新闻")["thread_id"]
    [t] = client.get("/api/scheduled-tasks").json()["tasks"]
    assert client.delete(f"/api/research/threads/{tid}").status_code == 200
    kept = _task(client, t["id"])
    assert (kept["thread_id"], kept["status"], kept["next_at"]) == (None, "active", t["next_at"])
    clock[0] = at(13, 7, 22)
    done = _fire(rr, client, t["id"])
    assert done["thread_id"] not in (None, tid) and state(client, done["thread_id"])["title"] == "每日简报"  # 新开了一段


def test_task_changed_while_being_fired_is_left_alone(clock):
    """查额度、占位的工夫用户把任务停了、改了时间：这一次不跑。"""
    rr, client = _api(clock)
    t = _create(client)
    clock[0] = at(13, 7, 22)
    [due] = aio.run(rr.tasks.store.due(clock[0]))
    held = rr.runs.busy()  # 占位的登记是进程里共用的，别的测试可能留着：只看这里有没有多占
    client.patch(f"/api/scheduled-tasks/{t['id']}", json={"status": "paused"})
    assert aio.run(rr._afire(due)) == "gone"
    assert client.get("/api/research/threads").json()["threads"] == [] and rr.runs.busy() == held
    client.patch(f"/api/scheduled-tasks/{t['id']}", json={"cron": "0 9 * * *"})  # 恢复并改了时间：按新的来
    client.patch(f"/api/scheduled-tasks/{t['id']}", json={"status": "active"})
    assert aio.run(rr._afire(due)) == "gone" and _task(client, t["id"])["next_at"] == "2026-11-13T01:00:00+00:00"
    client.delete(f"/api/scheduled-tasks/{t['id']}")
    assert aio.run(rr._afire(due)) == "gone" and rr.runs.busy() == held


def test_conversation_deleted_while_being_fired_waits_for_the_next_tick(clock):
    """到点的那一下任务挂着的对话刚被删了：不拿删掉的那个编号再开一段，下一拍照新的来（新开一段）。"""
    rr, client = _api(clock)
    tid = new(client, "建定时任务：直接答今天的新闻")["thread_id"]
    clock[0] = at(13, 7, 22)
    [due] = aio.run(rr.tasks.store.due(clock[0]))
    assert client.delete(f"/api/research/threads/{tid}").status_code == 200
    assert aio.run(rr._afire(due)) == "gone" and not rr.state_view(tid)["exists"]
    done = _fire(rr, client, due.id)
    assert done["thread_id"] != tid and done["last_status"] == sv.OK


# ---- 模型的工具 ----


def test_model_can_pause_and_delete_tasks(clock):
    rr, client = _api(clock)
    st = new(client, "建定时任务：直接答今天的新闻")
    paused = last(events(say(client, st["thread_id"], "停定时任务")), "state")
    [t] = client.get("/api/scheduled-tasks").json()["tasks"]
    assert (t["status"], t["paused_by"], t["next_at"]) == ("paused", "user", None) and _reply(paused["turns"][1]) == "停了。"
    assert [(s["action"], s["text"], s["limits"]) for s in _steps(paused["turns"][1])] == [("update", "每日简报", ["每天 · 07:22"])]  # 列任务那一步不显示
    # 换一段对话也能删：任务认人不认对话
    gone = new(client, "删定时任务")
    assert client.get("/api/scheduled-tasks").json()["tasks"] == [] and _reply(gone["turns"][0]) == "删了。"
    assert [(s["action"], s["text"], s["ok"]) for s in _steps(gone["turns"][0])] == [("delete", "每日简报", True)]
    assert _reply(new(client, "删定时任务")["turns"][0]) == "没有定时任务。"


def test_tool_tells_the_model_what_was_wrong(clock):
    rr, client = _api(clock)
    st = new(client, "建定时任务：太密 直接答今天的新闻")
    assert client.get("/api/scheduled-tasks").json()["tasks"] == []
    [step] = _steps(st["turns"][0])
    assert (step["action"], step["ok"], step["text"]) == ("create", False, "每日简报")
    assert "at most once an hour" in _reply(st["turns"][0])
    # 满了
    rr.tasks.max_tasks = 1
    new(client, "建定时任务：直接答今天的新闻")
    assert "which is the limit" in _reply(new(client, "建定时任务：直接答明天的新闻")["turns"][0])
    assert len(client.get("/api/scheduled-tasks").json()["tasks"]) == 1


def test_one_off_task_from_a_conversation(clock):
    rr, client = _api(clock)
    new(client, "建定时任务：一次性 直接答今天的新闻")
    [t] = client.get("/api/scheduled-tasks").json()["tasks"]
    assert (t["cron"], t["once_at"], t["schedule"]) == (None, "2099-01-01T01:00:00+00:00", "仅一次 · 2099-01-01 09:00")


def test_guests_do_not_get_the_tools(clock):
    p = FakeResearchPlanner()
    p._model = Recording()
    rr, client = _api(clock, ResearchRunner(planner=p), **bearer(guest_token()))
    st = new(client, "建定时任务：直接答今天的新闻")
    assert _reply(st["turns"][0]) == "这里不能用定时任务。" and aio.run(rr.tasks.store.due(at(20, 0))) == []
    assert not set(history.TASK_TOOLS) & set(p._model.sent[-1][1])


def test_tool_result_says_whether_the_next_run_is_today(clock):
    """模型只知道今天几号、不知道几点：结果里写明现在几点、下一次是今天还是明天，不让它自己算。"""
    from app.tools.schedule import for_model, how_far
    tasks = service(clock)
    clock[0] = at(13, 0, 18)
    t = make(tasks, cron="0 8 * * *")
    got = for_model(t, clock[0], "thread-1")
    assert got == {"task_id": t.id, "title": "每日简报", "status": "active", "cron": "0 8 * * *", "next_run": "2026-11-13 08:00 (Friday)",
                   "next_run_is": "today", "runs_in_this_conversation": False}
    assert how_far(at(14, 8), at(13, 9)) == "tomorrow" and how_far(at(16, 8), at(13, 9)) == "in 3 days" and how_far(None, at(13, 9)) is None
    assert how_far(datetime(2026, 11, 13, 17, 0, tzinfo=UTC), at(13, 23, 30)) == "tomorrow"  # 按北京时间的日子算：UTC 17 点是北京第二天 1 点
    paused = aio.run(tasks.update("u1", t.id, status=sv.PAUSED))
    assert "next_run_is" not in for_model(paused, clock[0]) and for_model(paused, clock[0])["next_run"] is None


def test_run_note_is_added_only_to_scheduled_messages():
    mark = {history.SCHEDULED: {"task_id": "task-1", "title": "每日简报"}, "sent_at": "2026-11-12T23:22:05+00:00"}
    msgs = [HumanMessage("你好", id="a"), AIMessage("在", id="b"), HumanMessage("查今天的新闻", id="c", additional_kwargs=mark)]
    out = with_run_notes(msgs)
    assert out[:2] == msgs[:2] and msgs[2].content == "查今天的新闻"  # 原来的消息不动
    assert out[2].content.startswith(f'{NOTE}\nThis turn was started automatically by the scheduled task "每日简报" at 2026-11-13 07:22 (Friday)')
    assert out[2].content.endswith("Instructions:\n查今天的新闻") and out[2].id == "c" and history.scheduled_of(out[2])
    assert with_run_notes(out[:2]) == out[:2]
    assert with_run_notes(msgs)[2].content == out[2].content  # 每次拼出来的都一样：前缀缓存接得上


# ---- 接口 ----


def test_api_create_list_update_delete(clock):
    rr, client = _api(clock)
    assert client.get("/api/scheduled-tasks").json() == {"available": True, "tasks": [], "limit": 5}
    a = _create(client)
    clock[0] = T0 + timedelta(seconds=1)  # 同一刻建的两个先后不定（按编号排）：假的钟要自己走
    b = _create(client, title="周报", prompt="总结这一周", cron="30 18 * * 5", work=True)
    assert [t["id"] for t in client.get("/api/scheduled-tasks").json()["tasks"]] == [a["id"], b["id"]]
    assert (b["work"], b["model"], b["schedule"], b["status"]) == (True, None, "每周五 · 18:30", "active")
    r = client.patch(f"/api/scheduled-tasks/{a['id']}", json={"title": "早报", "cron": "0 8 * * 1-5"})
    assert (r.json()["title"], r.json()["prompt"], r.json()["schedule"], r.json()["next_at"]) == ("早报", "直接答今天的新闻", "工作日 · 08:00", "2026-11-13T00:00:00+00:00")
    r = client.patch(f"/api/scheduled-tasks/{a['id']}", json={"status": "paused"})
    assert (r.json()["status"], r.json()["paused_by"], r.json()["next_at"]) == ("paused", "user", None)
    assert client.delete(f"/api/scheduled-tasks/{a['id']}").json() == {"ok": True}
    assert [t["id"] for t in client.get("/api/scheduled-tasks").json()["tasks"]] == [b["id"]]


@pytest.mark.parametrize(("body", "status", "reason", "message"), [
    ({"title": " "}, 422, "bad_title", "任务名称不能为空，最多 40 个字"),
    ({"prompt": ""}, 422, "bad_prompt", "要做的事不能为空，最多 4000 个字"),
    ({"cron": None}, 422, "no_schedule", "还没选什么时候运行"),
    ({"cron": "*/10 * * * *"}, 422, "too_often", "最密每小时运行一次"),
    ({"cron": "0 9 31 2 *"}, 422, "never_runs", "这个时间永远到不了，换一个"),
    ({"cron": "九点"}, 422, "bad_cron", "运行时间的写法不对"),
    ({"cron": None, "once_at": "2026-11-13 05:00"}, 422, "in_the_past", "这个时间已经过了，选一个以后的时间"),
    ({"cron": None, "once_at": "明天"}, 422, "bad_time", "运行时间的写法不对"),
])
def test_api_says_what_is_wrong_in_plain_words(clock, body, status, reason, message):
    rr, client = _api(clock)
    r = client.post("/api/scheduled-tasks", json={"title": "每日简报", "prompt": "查", "cron": DAILY} | body)
    assert r.status_code == status and r.json()["detail"] == {"code": "task_rejected", "reason": reason, "message": message}
    assert client.get("/api/scheduled-tasks").json()["tasks"] == []


def test_api_cap_other_users_and_guests(clock):
    rr, client = _api(clock)
    rr.tasks.max_tasks = 1
    t = _create(client)
    full = client.post("/api/scheduled-tasks", json={"title": "二", "prompt": "查", "cron": DAILY})
    assert full.status_code == 409 and full.json()["detail"] == {"code": "task_rejected", "reason": "too_many", "message": "最多 1 个定时任务，先删掉一个", "limit": 1}
    other = TestClient(api_main.app, headers=bearer(user_token("u2")))
    assert other.get("/api/scheduled-tasks").json()["tasks"] == []
    for r in (other.patch(f"/api/scheduled-tasks/{t['id']}", json={"title": "改"}), other.delete(f"/api/scheduled-tasks/{t['id']}"),
              other.post(f"/api/scheduled-tasks/{t['id']}/run"), client.post("/api/scheduled-tasks/task-none/run")):
        assert r.status_code == 404 and r.json()["detail"]["message"] == "没有这个定时任务"
    guest = TestClient(api_main.app, headers=bearer(guest_token()))
    assert guest.get("/api/scheduled-tasks").json() == {"available": False, "tasks": [], "limit": 0}
    for r in (guest.post("/api/scheduled-tasks", json={"title": "二", "prompt": "查", "cron": DAILY}), guest.post(f"/api/scheduled-tasks/{t['id']}/run"),
              guest.patch(f"/api/scheduled-tasks/{t['id']}", json={"title": "改"}), guest.delete(f"/api/scheduled-tasks/{t['id']}")):
        assert r.status_code == 403 and r.json()["detail"]["reason"] == "login_required"
    assert _task(client, t["id"])["title"] == "每日简报"
    assert client.post("/api/scheduled-tasks", json={"title": "二", "prompt": "查", "cron": DAILY, "model": "没有这个"}).status_code == 400


def test_api_run_now_does_not_move_the_schedule(clock):
    rr, client = _api(clock)
    t = _create(client)
    r = client.post(f"/api/scheduled-tasks/{t['id']}/run")
    assert r.status_code == 200 and r.json()["thread_id"] and r.json()["last_status"] == sv.RUNNING
    done = _settled(client, t["id"])
    assert (done["last_status"], done["next_at"], done["thread_id"]) == (sv.OK, t["next_at"], r.json()["thread_id"])
    assert state(client, done["thread_id"])["turns"][0]["scheduled"]["task_id"] == t["id"]
    # 停着的也能手动跑，跑完还是停着
    client.patch(f"/api/scheduled-tasks/{t['id']}", json={"status": "paused"})
    assert client.post(f"/api/scheduled-tasks/{t['id']}/run").status_code == 200
    again = _settled(client, t["id"])
    assert (again["status"], again["next_at"], again["last_status"]) == ("paused", None, sv.OK) and len(state(client, done["thread_id"])["turns"]) == 2
    # 额度用完了不跑
    rr.quota.limits = Limits(guest_daily=10, user_daily=0, user_weekly=0, guests_daily=50)
    r = client.post(f"/api/scheduled-tasks/{t['id']}/run")
    assert r.status_code == 429 and r.json()["detail"]["code"] == "credits_exhausted"


def test_running_conversation_makes_the_task_wait(clock):
    rr, client = _api(clock)
    t = _create(client)
    gate = hold_model(rr.planner)
    try:
        first = client.post(f"/api/scheduled-tasks/{t['id']}/run").json()
        busy = client.post(f"/api/scheduled-tasks/{t['id']}/run")
        assert busy.status_code == 409 and busy.json()["detail"] == {"code": "task_rejected", "reason": "thread_busy", "message": "这段对话正在回答，等它答完再运行"}
        clock[0] = at(13, 7, 22)
        assert rr.fire_due() == [(t["id"], BUSY)]  # 到点了那段对话还在跑：下一拍再试，什么都不记
        assert _task(client, t["id"])["next_at"] == t["next_at"]
    finally:
        gate.set()
    _settled(client, t["id"])
    _fire(rr, client, t["id"])  # 下一拍：跑了
    assert len(state(client, first["thread_id"])["turns"]) == 2


# ---- 跑完以后的未读和通知 ----


def _row(client: TestClient, tid: str, **headers) -> dict:
    return next(r for r in client.get("/api/research/threads", **({"headers": headers} if headers else {})).json()["threads"] if r["thread_id"] == tid)


def test_list_row_says_which_conversations_are_scheduled(clock):
    """左栏用钟的图标的那些：在里面建成过定时任务的、定时任务在里面跑过的。只看对话本身，任务删了也还算。"""
    rr, client = _api(clock)
    plain = new(client, "直接答：你好")["thread_id"]
    assert _row(client, plain)["scheduled"] is False
    # 在对话里建的：还没跑过也算
    made = new(client, "建定时任务：直接答今天的新闻")["thread_id"]
    [t] = client.get("/api/scheduled-tasks").json()["tasks"]
    assert _row(client, made)["scheduled"] is True and _row(client, plain)["scheduled"] is False
    assert client.patch(f"/api/research/threads/{made}", json={"title": "早报"}).json()["scheduled"] is True  # 单取一行的也带着
    client.delete(f"/api/scheduled-tasks/{t['id']}")
    assert _row(client, made)["scheduled"] is True
    # 接口建的：到点新开的那段对话
    t = _create(client)
    clock[0] = at(13, 7, 22)
    ran = _fire(rr, client, t["id"])["thread_id"]
    assert _row(client, ran)["scheduled"] is True
    client.delete(f"/api/scheduled-tasks/{t['id']}")
    assert _row(client, ran)["scheduled"] is True


def test_conversation_where_making_a_task_failed_is_not_scheduled(clock):
    rr, client = _api(clock)
    tid = new(client, "建定时任务：太密 直接答今天的新闻")["thread_id"]  # 工具报了错，结果里没有任务
    assert client.get("/api/scheduled-tasks").json()["tasks"] == [] and _row(client, tid)["scheduled"] is False


def test_finished_run_leaves_an_unread_notice(clock):
    rr, client = _api(clock)
    t = _create(client)
    clock[0] = at(13, 7, 22)
    tid = _fire(rr, client, t["id"])["thread_id"]
    row = _row(client, tid)
    assert row["unread"] == 1
    n = row["notice"]
    assert (n["title"], n["text"], n["ok"]) == ("每日简报", "直接回答：这个问题不用查。", True) and n["at"]
    client.patch(f"/api/scheduled-tasks/{t['id']}", json={"title": "早报"})
    clock[0] = at(14, 7, 22)
    _fire(rr, client, t["id"])
    again = _row(client, tid)
    assert again["unread"] == 2 and again["notice"]["title"] == "早报" and again["notice"]["at"] > n["at"]
    # 看过了：清零。别人的对话 404，不清
    assert client.post(f"/api/research/threads/{tid}/read", headers=bearer(user_token("u2"))).status_code == 404
    assert _row(client, tid)["unread"] == 2
    read = client.post(f"/api/research/threads/{tid}/read").json()
    assert (read["thread_id"], read["unread"], read["notice"]) == (tid, 0, None) and _row(client, tid)["unread"] == 0
    # 用户自己说的一轮不算未读
    assert last(events(say(client, tid, "直接答：好")), "state")["status"] == "idle"
    assert _row(client, tid)["unread"] == 0


def test_failed_run_is_unread_too_and_a_stopped_one_is_not(clock):
    rr = flaky_runner(None, _scheduled_call)  # 定时触发的这一轮第一次调模型就断了
    rr, client = _api(clock, rr)
    t = _create(client)
    clock[0] = at(13, 7, 22)
    tid = _fire(rr, client, t["id"])["thread_id"]
    row = _row(client, tid)
    assert row["unread"] == 1 and row["notice"] == {"title": "每日简报", "text": "", "ok": False, "at": row["notice"]["at"]}

    rr, client = _api(clock)
    t = _create(client)
    gate = hold_model(rr.planner)
    try:
        tid = client.post(f"/api/scheduled-tasks/{t['id']}/run").json()["thread_id"]
        assert client.post(f"/api/research/threads/{tid}/stop").status_code == 200
    finally:
        gate.set()
    assert _settled(client, t["id"])["last_status"] == sv.STOPPED
    assert _row(client, tid)["unread"] == 0  # 用户自己点了停止：人就在场


def test_notice_text_is_the_plain_opening_of_the_reply():
    reply = """## 今日要点

- **OpenAI** 发布了新模型[s3fa9c1]，详见[官方公告](https://example.com/a)。
- 价格下调 *20%*[s0b1c2d3e4f]

| 模型 | 价格 |
| --- | --- |
| A | 1 |

```python
print("hi")
```
"""
    assert opening(reply) == '今日要点 OpenAI 发布了新模型，详见官方公告。 价格下调 20% 模型 价格 A 1 print("hi")'
    assert opening("1. 第一条\n2. 第二条\n\n---\n> 引用的话") == "1. 第一条 2. 第二条 引用的话"
    assert opening("a * b * c，snake_case_name 不动") == "a * b * c，snake_case_name 不动"
    assert opening("长" * 200) == "长" * 120 + "…" and opening("") == ""


# ---- 表单里“自定义”的时间：一句话转成什么时候跑 ----


def test_api_reads_a_sentence_into_a_schedule(clock):
    """模型转出来的照样过校验，返回页面要的那几样；不存任务。"""
    rr, client = _api(clock)
    r = client.post("/api/scheduled-tasks/schedule", json={"text": " 每个月 1 号和 15 号早上 9 点半 "})
    assert r.status_code == 200 and r.json() == {"cron": "30 9 1,15 * *", "once_at": None, "schedule": "每月 1、15 号 · 09:30",
                                                 "next_at": "2026-11-15T01:30:00+00:00"}
    once = client.post("/api/scheduled-tasks/schedule", json={"text": "2099 年元旦早上九点"}).json()
    assert (once["cron"], once["once_at"], once["next_at"]) == (None, "2099-01-01T01:00:00+00:00", "2099-01-01T01:00:00+00:00")
    assert [c for c in rr.planner.calls if c[0] == "when"] == [("when", "每个月 1 号和 15 号早上 9 点半"), ("when", "2099 年元旦早上九点")]
    assert client.get("/api/scheduled-tasks").json()["tasks"] == []


def test_api_says_so_when_the_sentence_is_not_a_time(clock):
    rr, client = _api(clock)
    for text, reason, message in (("随便什么时候", "unclear_time", "没看懂这个时间，换个说法试试"), ("每 5 分钟", "too_often", "最密每小时运行一次"),
                                  ("   ", "unclear_time", "没看懂这个时间，换个说法试试")):
        r = client.post("/api/scheduled-tasks/schedule", json={"text": text})
        assert r.status_code == 422 and r.json()["detail"] == {"code": "task_rejected", "reason": reason, "message": message}, text
    rr.planner.fail_when = True  # 模型这会儿不行：不说用户写得不对
    r = client.post("/api/scheduled-tasks/schedule", json={"text": "每个月 1 号和 15 号早上 9 点半"})
    assert r.status_code == 503 and r.json()["detail"]["message"] == "这会儿没认出来，再试一次"
    guest = TestClient(api_main.app, headers=bearer(guest_token()))
    assert guest.post("/api/scheduled-tasks/schedule", json={"text": "每天九点"}).status_code == 403
    assert client.post("/api/scheduled-tasks/schedule", json={"text": "长" * 201}).status_code == 422
