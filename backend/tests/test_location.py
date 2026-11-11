"""用户的当前位置（app.tools.location）：模型选了 current_location 的先停下来要位置，给了的经 MCP 的 _meta 带给服务，
坐标只在内存里、按人留一小段时间（这段时间里再用到的不再要）；没给的工具不调。
只用假模型（“当前位置”，见 FakeToolModel._here）和假的天气、地图服务。"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app import aio
from app.api import main as api_main
from app.api.research_runner import ResearchRunner
from app.api.run_store import ASKED
from app.graph.research import history, prompts
from app.research_llm import FAKE_HERE, fake_weather_server
from app.tools import location, mcp
from app.tools.location import GEO_META_KEY, KEEP_SECONDS, Device, PlaceAnswer, Places, holder
from app.tools.maps_mcp import PLACE_TOOL, ROUTE_TOOL
from app.tools.weather_mcp import WEATHER_TOOL, WeatherTool
from tests.auth_helpers import ADMIN, USER
from tests.research_helpers import events, last, new, say, state

# 测试用的坐标：数字特别，方便在检查点里找（不该找到）
LNG, LAT = 114.057868, 22.543099
HERE = {"longitude": LNG, "latitude": LAT, "accuracy_m": 35}


class Clock:
    """测试用的钟：往前拨就过了时候。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def rr(tmp_path):
    runner = ResearchRunner(fake=True)
    runner.clock = Clock()
    runner.places = Places(clock=runner.clock)
    return runner


def _who(rr: ResearchRunner, tid: str) -> str:
    return holder(aio.run(rr.run_store.owner_of(tid)), tid)


@pytest.fixture
def client(rr):
    api_main.set_research_runner(rr)
    return TestClient(api_main.app, headers=USER)


def _answer(http: TestClient, tid: str, ask_id: str, **body):
    return http.post(f"/api/research/threads/{tid}/answer", json={"id": ask_id, **body})


def _steps(turn: dict, kind: str | None = None) -> list[dict]:
    return [i for i in turn["items"] if i["type"] == "step" and (kind is None or i["kind"] == kind)]


def _reply(turn: dict) -> str:
    return "".join(i["text"] for i in turn["items"] if i["type"] == "text")


def _stored(rr: ResearchRunner, tid: str) -> str:
    """这段对话存下来的所有东西（每个检查点和它的待写入）拼成一段字。"""
    async def dump() -> str:
        out = []
        async for tup in rr.graph.checkpointer.alist({"configurable": {"thread_id": tid}}):
            out.append(repr(tup.checkpoint) + repr(tup.metadata) + repr(tup.pending_writes))
        return "\n".join(out)
    return aio.run(dump())


# ---- 要位置、给了 ----


def test_current_location_pauses_for_the_position_and_uses_it(client, rr):
    ev = events(client.post("/api/research/threads", json={"text": "当前位置天气"}))  # 对话（Chat）里也会要
    st = last(ev, "state")
    tid = st["thread_id"]
    assert st["status"] == "asking" and st["error"] is None
    ask = st["ask"]
    assert ask["kind"] == "location" and ask["purpose"] == "weather" and ask["question"] == "" and ask["options"] == [] and ask["id"]
    (turn,) = st["turns"]
    (step,) = _steps(turn)  # 过程里是查天气那一步，还没做完；没有另外一条提问
    assert step["id"] == ask["call_id"] and step["kind"] == "weather" and step["text"] == "当前位置" and step["pending"]
    assert not [i for i in turn["items"] if i["type"] == "ask"]
    assert [(r["kind"], r["status"]) for r in client.get(f"/api/research/threads/{tid}/runs").json()["runs"]] == [("new", ASKED)]

    time.sleep(0.05)
    end = last(events(_answer(client, tid, ask["id"], location=HERE)), "state")
    assert end["status"] == "idle" and end["ask"] is None
    (turn,) = end["turns"]
    (step,) = _steps(turn, "weather")
    assert step["ok"] and step["text"] == "当前位置" and step["title"] == FAKE_HERE and step["weather"]["location"] == FAKE_HERE
    assert _reply(turn) == "查好了，看卡片。"
    assert turn["record"]["waited_ms"] >= 50  # 等位置的这段不算进这一轮的用时
    # 坐标没进检查点，也没给模型看；内存里记在这个人名下，为哪次调用要的这一轮完了就不记了
    stored = _stored(rr, tid)
    assert "current_location" in stored and str(LNG) not in stored and str(LAT) not in stored
    assert _who(rr, tid).startswith("owner:") and rr.places.get(_who(rr, tid)).longitude == LNG
    assert not rr.places.asked_for(tid, step["id"])


def test_the_position_goes_to_the_service_in_request_meta():
    seen = []
    server = fake_weather_server()

    async def spy(context, call_next):
        seen.append(context.fastmcp_context.request_context.meta)
        return await call_next(context)

    from fastmcp.server.middleware import Middleware

    class Spy(Middleware):
        async def on_call_tool(self, context, call_next):
            return await spy(context, call_next)

    server.add_middleware(Spy())
    tool = WeatherTool(lambda: server)
    aio.run(tool.connect())
    call = {"name": WEATHER_TOOL, "args": {"location": "current_location", "location_source": "current_location"}, "id": "c1", "type": "tool_call"}

    async def with_position():
        with mcp.request_meta(Device(**HERE).meta()):
            return await tool.tool.ainvoke(call)

    got = aio.run(with_position())
    assert got.artifact["structured_content"]["resolved_location"] == FAKE_HERE
    meta = seen[-1] if isinstance(seen[-1], dict) else seen[-1].model_dump()
    assert meta[GEO_META_KEY] == HERE
    # 没包着的照常走适配器：服务没收到位置
    plain = aio.run(tool.tool.ainvoke(call))
    assert plain.artifact["structured_content"]["error"]["code"] == "location_unavailable"
    assert json.dumps(call).find(str(LNG)) < 0


def test_two_calls_in_one_message_share_one_card(client, rr):
    st = new(client, "当前位置 两处")
    tid, ask = st["thread_id"], st["ask"]
    assert st["status"] == "asking" and ask["kind"] == "location" and ask["purpose"] == "weather"  # 先发出的那个
    end = last(events(_answer(client, tid, ask["id"], location=HERE)), "state")
    assert end["status"] == "idle"  # 一次都回了，没有第二张卡
    (turn,) = end["turns"]
    assert _steps(turn, "weather")[0]["ok"] and _steps(turn, "place")[0]["ok"]
    assert _steps(turn, "place")[0]["limits"] == ["当前位置附近"]


def test_later_call_in_the_same_turn_reuses_the_position(client, rr):
    st = new(client, "当前位置天气 再查")
    tid = st["thread_id"]
    end = last(events(_answer(client, tid, st["ask"]["id"], location=HERE)), "state")
    assert end["status"] == "idle"  # 第二次用位置不再停下来
    (turn,) = end["turns"]
    assert _steps(turn, "weather")[0]["ok"] and _steps(turn, "place")[0]["ok"]


def test_once_given_the_position_is_not_asked_for_again(client, rr):
    # 要过一次就不再要（2026-11-11 Sean 定）：下一轮、这个人的另一段对话再用到，都直接用，不停下来
    st = new(client, "当前位置天气")
    tid = st["thread_id"]
    assert last(events(_answer(client, tid, st["ask"]["id"], location=HERE)), "state")["status"] == "idle"
    again = last(events(say(client, tid, "当前位置附近")), "state")
    assert again["status"] == "idle" and again["ask"] is None
    assert _steps(again["turns"][1], "place")[0]["ok"]
    assert [r["status"] for r in client.get(f"/api/research/threads/{tid}/runs").json()["runs"]].count(ASKED) == 1
    other = new(client, "当前位置路线")  # 另一段对话
    assert other["status"] == "idle" and _steps(other["turns"][0], "route")[0]["ok"]
    assert str(LNG) not in _stored(rr, tid) + _stored(rr, other["thread_id"])
    # 别人的对话不沾：照样要
    theirs = last(events(client.post("/api/research/threads", json={"text": "当前位置天气"}, headers=ADMIN)), "state")
    assert theirs["status"] == "asking" and theirs["ask"]["kind"] == "location"


def test_the_position_is_asked_for_again_after_a_while(client, rr):
    st = new(client, "当前位置天气")
    tid = st["thread_id"]
    assert last(events(_answer(client, tid, st["ask"]["id"], location=HERE)), "state")["status"] == "idle"
    rr.clock.now += KEEP_SECONDS - 1
    assert last(events(say(client, tid, "当前位置附近")), "state")["status"] == "idle"
    rr.clock.now += 1  # 留的时候到了：人可能已经走开，重新要
    again = last(events(say(client, tid, "当前位置附近")), "state")
    assert again["status"] == "asking" and again["ask"]["kind"] == "location" and again["ask"]["purpose"] == "nearby"
    assert rr.places.get(_who(rr, tid)) is None


def test_route_from_current_location(client, rr):
    st = new(client, "当前位置路线")
    assert st["ask"]["purpose"] == "route"
    end = last(events(_answer(client, st["thread_id"], st["ask"]["id"], location=HERE)), "state")
    (step,) = _steps(end["turns"][0], "route")
    assert step["ok"] and step["text"] == "当前位置 → 外滩"


# ---- 没给 ----


@pytest.mark.parametrize(("body", "code", "message", "key"), [
    ({"skip": True}, "location_declined", "没用当前位置", "declined"),
    ({"unavailable": True}, "location_unavailable", "拿不到当前位置", "unavailable"),
])
def test_without_the_position_the_tool_is_not_run(client, rr, body, code, message, key):
    st = new(client, "当前位置天气")
    tid = st["thread_id"]
    end = last(events(_answer(client, tid, st["ask"]["id"], **body)), "state")
    assert end["status"] == "idle"
    (turn,) = end["turns"]
    (step,) = _steps(turn, "weather")
    assert not step["ok"] and step["message"] == message
    assert _reply(turn) == "没拿到你的位置，你在哪个城市？"
    result = next(m for m in aio.run(rr.graph.aget_state(rr._cfg(tid))).values["messages"] if isinstance(m, ToolMessage))
    assert result.status == "error" and result.content == prompts.render(f"tools.location.{key}")
    assert result.artifact["structured_content"]["error"]["code"] == code and result.artifact[history.LOCATED]["status"] == key


def test_asking_again_in_the_same_turn_does_not_pause_again(client, rr):
    st = new(client, "当前位置天气 硬要")
    tid = st["thread_id"]
    end = last(events(_answer(client, tid, st["ask"]["id"], skip=True)), "state")
    assert end["status"] == "idle"  # 没再出卡
    (turn,) = end["turns"]
    (place,) = _steps(turn, "place")
    assert not place["ok"] and place["message"] == "没用当前位置"
    results = [m for m in aio.run(rr.graph.aget_state(rr._cfg(tid))).values["messages"] if isinstance(m, ToolMessage)]
    assert results[-1].content == prompts.render("tools.location.again") and history.located_of(results[-1]) is None


def test_typing_a_place_instead(client, rr):
    st = new(client, "当前位置天气")
    tid = st["thread_id"]
    ev = events(_answer(client, tid, st["ask"]["id"], text="  广州  "))
    end = last(ev, "state")
    assert end["status"] == "idle"
    (turn,) = end["turns"]
    said = {"type": "ask", "kind": "location", "id": f"{st['ask']['call_id']}:where", "question": "", "options": [], "pending": False,
            "answer": "广州", "skipped": False, "muted": False}
    assert [i for i in turn["items"] if i["type"] == "ask"] == [said]  # 用户说的话留在过程里
    assert said in [d["ask"] for e, d in ev if e == "ask"]  # 运行中页面也收到这一条
    first, second = _steps(turn, "weather")
    assert not first["ok"] and first["message"] == "没用当前位置"
    assert second["ok"] and second["text"] == "广州" and second["title"] == "广东省·广州"  # 模型按用户说的地方查
    result = next(m for m in aio.run(rr.graph.aget_state(rr._cfg(tid))).values["messages"] if isinstance(m, ToolMessage))
    assert "<user_reply>\n广州\n</user_reply>" in result.content


def test_answers_that_do_not_fit(client, rr):
    st = new(client, "当前位置天气")
    tid, ask_id = st["thread_id"], st["ask"]["id"]
    assert _answer(client, tid, ask_id).status_code == 400  # 什么都没带
    assert _answer(client, tid, ask_id, location={"longitude": 200, "latitude": 22}).status_code == 422
    assert _answer(client, tid, ask_id, location={"longitude": 114}).status_code == 422
    assert _answer(client, tid, "nope", location=HERE).status_code == 409
    assert state(client, tid)["status"] == "asking" and rr.places.get(tid) is None  # 都没交上，还等着
    # 等着的是问题、不是位置：不收位置
    q = new(client, "问我 再做对比", work=True)
    assert "kind" not in q["ask"]
    assert _answer(client, q["thread_id"], q["ask"]["id"], location=HERE).status_code == 400
    assert rr.places.get(q["thread_id"]) is None


def test_stop_while_waiting_for_the_position(client, rr):
    st = new(client, "当前位置天气")
    tid = st["thread_id"]
    assert client.post(f"/api/research/threads/{tid}/stop").status_code == 200
    end = state(client, tid)
    assert end["status"] == "idle" and end["ask"] is None
    (step,) = _steps(end["turns"][0], "weather")
    assert not step["ok"] and step["message"] == "停止了，没有做完"


def test_stopping_the_turn_keeps_the_position_but_not_which_calls_asked(client, rr):
    st = new(client, "当前位置天气 再查")
    tid = st["thread_id"]
    rr.places.put(_who(rr, tid), Device(**HERE), tid, {"earlier"})  # 这一轮前面已经给过位置，后面又停下来（比如模型接着问了个问题）
    assert client.post(f"/api/research/threads/{tid}/stop").status_code == 200
    assert rr.places.get(_who(rr, tid)) is not None and not rr.places.asked_for(tid, "earlier")


# ---- 小件 ----


def test_wants():
    here = history.CURRENT_LOCATION
    assert location.wants(WEATHER_TOOL, {"location": here, "location_source": here}) == location.WEATHER
    assert location.wants(WEATHER_TOOL, {"location": "广州"}) is None
    assert location.wants(WEATHER_TOOL, {"location": here}) is None  # 没明确选：服务会让模型改
    assert location.wants(PLACE_TOOL, {"query": "咖啡", "anchor_source": here}) == location.NEARBY
    assert location.wants(PLACE_TOOL, {"query": "咖啡", "anchor_source": "named", "near": "外滩"}) is None
    assert location.wants(ROUTE_TOOL, {"origin_source": "named", "destination_source": here}) == location.ROUTE
    assert location.wants(ROUTE_TOOL, {"origin_source": "named", "destination_source": "named"}) is None
    assert location.wants("search_web", {"query": here, "anchor_source": here}) is None


def test_device_hides_the_position_and_checks_the_range():
    d = Device(**HERE)
    assert str(LNG) not in repr(d) and str(LAT) not in str(d)
    assert d.meta() == {GEO_META_KEY: HERE}
    assert Device(longitude=LNG, latitude=LAT).meta() == {GEO_META_KEY: {"longitude": LNG, "latitude": LAT}}
    for bad in ({"longitude": 181, "latitude": 0}, {"longitude": 0, "latitude": -91}, {"longitude": 0, "latitude": 0, "accuracy_m": -1},
                {"longitude": 0, "latitude": 0, "city": "深圳"}):
        with pytest.raises(ValueError) as e:
            Device(**bad)
        assert "181" not in str(e.value) and "input_value" not in str(e.value)


def test_places_keep_one_position_per_person_for_a_while():
    clock = Clock()
    p = Places(keep=60, clock=clock)
    assert p.get("a") is None and not p.asked_for("t", "c1")
    p.put("a", Device(**HERE), "t", {"c1"})
    p.put("a", Device(longitude=1, latitude=2), "t", {"c2"})  # 又给了一次：换成新的，重新算时候
    assert p.get("a").longitude == 1 and p.get("b") is None
    assert p.asked_for("t", "c1") and p.asked_for("t", "c2") and not p.asked_for("t", "c3") and not p.asked_for("u", "c1")
    assert str(LNG) not in repr(p._held) and "longitude" not in repr(p._held)
    p.end_turn("t")
    p.end_turn("t")
    assert not p.asked_for("t", "c1") and p.get("a") is not None  # 这一轮完了：位置还留着
    clock.now += 59
    assert p.get("a") is not None
    clock.now += 1
    assert p.get("a") is None and "a" not in p._held
    # 过了时候的，别人再给位置时顺手丢掉，不等那个人再来
    p.put("a", Device(**HERE), "t", set())
    clock.now += 61
    p.put("b", Device(**HERE), "u", set())
    assert set(p._held) == {"b"}


def test_holder():
    assert holder("u1", "t1") == "owner:u1" and holder(None, "t1") == "thread:t1" and holder("", "t1") == "thread:t1"


def test_not_given_before_looks_at_this_turn_only():
    def result(status: str | None, cid: str) -> ToolMessage:
        art = {history.LOCATED: PlaceAnswer(status=status).model_dump()} if status else {}
        return ToolMessage(content="x", tool_call_id=cid, name=WEATHER_TOOL, artifact=art)

    earlier = [HumanMessage("上一轮"), AIMessage("", tool_calls=[{"name": WEATHER_TOOL, "args": {}, "id": "a"}]), result("declined", "a")]
    now = [HumanMessage("这一轮"), AIMessage("", tool_calls=[{"name": WEATHER_TOOL, "args": {}, "id": "b"}])]
    assert not location.not_given_before(earlier + now)
    assert not location.not_given_before(earlier + now + [result("provided", "b")])
    assert not location.not_given_before(earlier + now + [result(None, "b")])
    for status in ("declined", "unavailable", "said"):
        assert location.not_given_before(earlier + now + [result(status, "b")])


def test_waiting_for_the_position_counts_as_waited():
    art = {history.LOCATED: {"status": "provided", "asked_at": "2026-11-11T10:00:00+00:00", "answered_at": "2026-11-11T10:00:07+00:00"}}
    assert history.waited_ms([ToolMessage(content="x", tool_call_id="a", name=WEATHER_TOOL, artifact=art)]) == 7000


def test_without_a_page_the_turn_does_not_pause(rr):
    # 命令行、评测直接跑一轮（run_turn）：没有页面可以要位置，不停下来，服务照没给位置回
    rr.accept("t1", "当前位置天气")
    assert rr.run_turn("t1") is None
    view = rr.state_view("t1")
    assert view["status"] == "idle" and view["ask"] is None
    (step,) = _steps(view["turns"][0], "weather")
    assert not step["ok"] and step["message"] == "拿不到当前位置"


def test_position_lost_before_the_tool_runs_counts_as_unavailable(client, rr):
    # 位置交回来了、工具还没调，进程重启过（坐标只在内存里）：交给 interrupt 的是“给了”，坐标没了，当取不到，不拿空的去调
    st = last(events(client.post("/api/research/threads", json={"text": "当前位置天气"})), "state")
    tid = st["thread_id"]
    resume = rr.answer_input(tid, st["ask"]["id"], location=Device(**HERE))
    rr.places = Places()  # 重启以后：位置和为哪次调用要的都没了
    assert aio.run(rr._run_turn(tid, resume, lambda ev: None, locate=True)) is None
    view = rr.state_view(tid)
    assert view["status"] == "idle" and view["ask"] is None
    (step,) = _steps(view["turns"][0], "weather")
    assert not step["ok"] and step["message"] == "拿不到当前位置"
    (result,) = [m for m in aio.run(rr.graph.aget_state(rr._cfg(tid))).values["messages"] if isinstance(m, ToolMessage)]
    assert result.content == prompts.render("tools.location.unavailable")
    assert str(LNG) not in _stored(rr, tid) and str(LAT) not in _stored(rr, tid)
