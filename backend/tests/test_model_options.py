"""选模型：models.toml 的选项、每轮定下用哪个模型、对话定下选项后不换、思考开关（见 app.model_catalog、agent.TurnMiddleware、agent.ModelSelect）。只用假模型。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from pydantic import Field, ValidationError

from app import aio, config, model_catalog
from app.api import main as api_main
from app.api.research_runner import ResearchRunner
from app.graph.research import history
from app.graph.research.agent import _others_without_thinking
from app.graph.research.graph import make_serde
from app.graph.research.state import Turn
from app.model_catalog import AUTO, LEGACY_MODEL, Catalog, ModelUse
from app.research_llm import FakeResearchPlanner, FakeToolModel
from tests.research_helpers import events, last, say, state
from tests.auth_helpers import USER, bearer, guest_token

PRO, FLASH = "deepseek:deepseek-v4-pro", "deepseek:deepseek-flash"
# Auto 判为复杂（没判断的也当复杂）现在先走的那个：TokenHub 上的免费 Pro 还能用时是它，到期了是直连的 Pro（[[free]] 的 same_as）
AUTO_REF = model_catalog.load().plan(None, None, lambda ref: True).agent.ref
FLASH_REF = model_catalog.load().free_ref(FLASH, lambda ref: True)  # Flash 先走同一个模型的免费额度（[[free]] 的 same_as），按今天能用的
DOUBAO, LITE = "ark:doubao-seed-2-1-pro-260915", "ark:doubao-seed-2-1-lite-260915"


def _always(_ref: str) -> bool:
    return True


def _with(status: dict[str, str]) -> Catalog:
    """真的那份选项，把几个选项改成停用、藏起来。"""
    c = model_catalog.load()
    return c.model_copy(update={"options": [o.model_copy(update={"status": status.get(o.id, o.status)}) for o in c.options]})


class Seeing(FakeToolModel):
    """记下每次调模型收到的消息。"""

    seen: list = Field(default_factory=list)  # bind_tools 复制后还是同一个 list

    def _message(self, messages: list[BaseMessage]) -> AIMessage:
        self.seen.append(list(messages))
        return super()._message(messages)


def _client(p: FakeResearchPlanner) -> tuple[TestClient, ResearchRunner]:
    rr = ResearchRunner(planner=p)
    api_main.set_research_runner(rr)
    return TestClient(api_main.app, headers=USER), rr


def _create(http: TestClient, text: str, **kw) -> dict:
    return last(events(http.post("/api/research/threads", json={"text": text, **kw})), "state")


def _records(st: dict) -> list[dict]:
    return [t["record"] for t in st["turns"]]


# ---- 选项本身 ----


def test_models_toml_is_valid():
    """仓库里那份：Auto 在第一个，内部用途齐全，用到的厂商都认得，现在的 Auto 就是以前的默认组合（V4 Pro 思考 high + Flash 子 agent）。"""
    from app.research_llm import PROVIDERS

    c = model_catalog.load()
    assert c.options[0].id == AUTO and set(c.roles) == set(model_catalog.ROLES)
    assert {r.partition(":")[0] for r in c.refs()} <= set(PROVIDERS)
    auto = c.plan(None, None, _always, free=False)  # 不走免费额度时
    assert auto == model_catalog.Plan(option=AUTO, agent=ModelUse(ref=PRO, thinking=True, effort="high"),
                                      subagent=ModelUse(ref=FLASH, thinking=True, effort="high"), pick=PRO)
    assert auto.agent == model_catalog.LEGACY_USE  # 以前没记模型的轮也是它答的


def test_catalog_rejects_mistakes():
    good = model_catalog.load().model_dump()
    for broken, why in [
        ({**good, "options": [good["options"][1], good["options"][0]]}, "第一个选项要是 auto"),
        ({**good, "options": good["options"] + [good["options"][1]]}, "id 重复"),
        ({**good, "roles": {k: v for k, v in good["roles"].items() if k != "judge"}}, "内部用途"),
        ({**good, "options": [*good["options"], {**good["options"][1], "id": "x", "model": "deepseek-v4-pro"}]}, "厂商:模型"),
        ({**good, "options": [{**good["options"][0], "status": "retired"}, *good["options"][1:]]}, "不能停用"),
        ({**good, "options": [{**good["options"][0], "access": "user"}, *good["options"][1:]]}, "游客也能用"),
        ({**good, "options": [*good["options"][:-1], {**good["options"][-1], "access": "vip"}]}, "access"),
    ]:
        with pytest.raises(ValidationError, match=why):
            Catalog.model_validate(broken)


def test_guests_get_auto_and_the_light_models():
    """游客能选 Auto 和轻量模型（2026-11-06 Sean 定），别的要登录；现在没有只给管理员的。"""
    c = model_catalog.load()
    assert {o.id for o in c.offered(_always) if o.allows("guest")} == {
        AUTO, "deepseek-flash", "doubao-seed-2-1-lite", "qwen3-8-flash", "mimo-v2-6-flash", "glm-5-3-flash", "hy3", "minimax-m3-1-flash"}
    assert all(o.allows("user") and o.allows("admin") for o in c.options)


def test_plan_falls_back_to_auto():
    """选项不在了、厂商停了、没配 key 的改用 Auto；只是不再提供（hidden）的照常用，但新对话里没有它。"""
    c = _with({"deepseek-flash": "hidden", "doubao-seed-2-1-lite": "retired"})
    assert c.plan("deepseek-flash", None, _always).agent.ref == FLASH_REF
    assert "deepseek-flash" not in {o.id for o in c.offered(_always)}
    assert c.plan("doubao-seed-2-1-lite", None, _always).option == AUTO
    assert c.plan("nope", None, _always).option == AUTO
    only_deepseek = lambda ref: ref.startswith("deepseek:")  # noqa: E731
    assert c.plan("doubao-seed-2-1-pro", None, only_deepseek).option == AUTO
    assert [o.id for o in c.offered(only_deepseek)] == [AUTO, "deepseek-v4-pro"]


def test_plan_thinking_switch():
    """思考开关：没给是开；关了主 agent、子 agent 都不思考（强度也不传）。always、never 的不看开关。"""
    c = model_catalog.load()
    off = c.plan("doubao-seed-2-1-pro", False, _always)
    assert (off.agent, off.subagent) == (ModelUse(ref=DOUBAO, thinking=False), ModelUse(ref=LITE, thinking=False))
    assert c.plan("deepseek-flash", None, _always).agent == ModelUse(ref=FLASH_REF, thinking=True, effort="high")
    fixed = c.model_copy(update={"options": [o.model_copy(update={"thinking": "always" if o.id == "deepseek-flash" else "never"})
                                             for o in c.options]})
    assert fixed.plan("deepseek-flash", False, _always).agent.thinking is True
    assert fixed.plan(AUTO, True, _always).agent.thinking is False


def test_live_planner_builds_each_model_once(monkeypatch):
    """同样的（模型、思考开关、强度）共用一个模型；思考关了不传强度，DeepSeek 不开思考时 temperature 0。没配 key 的那家不能选。"""
    from app.research_llm import LiveResearchPlanner, make_chat_model, usable

    for name, value in (("DEEPSEEK_API_KEY", "k"), ("AGENT_TOOLS_API_KEY", "k"), ("ARK_API_KEY", None)):
        monkeypatch.setattr(config, name, value)
    p = LiveResearchPlanner()
    on, off = ModelUse(ref=FLASH, thinking=True, effort="high"), ModelUse(ref=FLASH, thinking=False)
    assert p.chat_model(on) is p.chat_model(on) is p.chat_model(on, sub=True)
    assert p.chat_model(off) is not p.chat_model(on)
    assert p.chat_model(on).reasoning_effort == "high" and p.chat_model(on).extra_body == {"thinking": {"type": "enabled"}}
    assert p.chat_model(off).reasoning_effort is None and p.chat_model(off).temperature == 0
    assert p.chat_model(off).extra_body == {"thinking": {"type": "disabled"}}
    assert not usable(DOUBAO) and usable(PRO) and not usable("openai:gpt-x")
    assert [o["id"] for o in ResearchRunner.models(type("R", (), {"planner": p})(), "user")] == [AUTO, "deepseek-v4-pro", "deepseek-flash"]
    with pytest.raises(ValueError, match="未知的模型提供方"):
        make_chat_model("openai:gpt-x", thinking=False)


def test_turn_with_model_survives_checkpoint():
    """每轮记的模型跟着检查点存取（ModelUse 在反序列化的白名单里）。"""
    serde = make_serde()
    t = Turn(id="h1", started_at="2026-11-05T00:00:00+00:00", option="doubao-seed-2-1-pro",
             model=ModelUse(ref=DOUBAO, thinking=False), subagent_model=ModelUse(ref=LITE, thinking=False))
    assert serde.loads_typed(serde.dumps_typed([t])) == [t]
    assert Turn.model_validate({"id": "h0", "started_at": "x"}).model is None  # 以前的轮没有这几项


def test_other_models_thinking_is_dropped():
    """发给这个模型时，别的模型答的那几轮不带思考（没记模型的是 LEGACY_MODEL），同一个模型答的原样。插进来的话不算新的一轮。"""
    think = {history.REASONING: "想", history.ENCRYPTED: "密"}
    msgs = [HumanMessage("一", id="h1"), AIMessage("答一", additional_kwargs=think),
            HumanMessage("二", id="h2"), AIMessage("答二", additional_kwargs=think),
            HumanMessage("插话", id="g", additional_kwargs={history.GUIDE: True}), AIMessage("还是二", additional_kwargs=think),
            HumanMessage("三", id="h3"), AIMessage("答三", additional_kwargs=think)]
    turns = [Turn(id="h1", started_at="x"), Turn(id="h2", started_at="x", model=ModelUse(ref=DOUBAO, thinking=True)),
             Turn(id="h3", started_at="x", model=ModelUse(ref=PRO, thinking=True))]
    kept = [bool(m.additional_kwargs) for m in _others_without_thinking(msgs, turns, ModelUse(ref=DOUBAO, thinking=True)) if isinstance(m, AIMessage)]
    assert kept == [False, True, True, False]
    kept = [bool(m.additional_kwargs) for m in _others_without_thinking(msgs, turns, ModelUse(ref=LEGACY_MODEL, thinking=False)) if isinstance(m, AIMessage)]
    assert kept == [True, False, False, True]


# ---- 经过运行器、接口 ----


def test_models_endpoint_and_default_auto():
    """/api/models 给新对话能选的；没选模型的新对话是 Auto，每轮记下用的模型。"""
    http, rr = _client(FakeResearchPlanner(catalog=_with({"deepseek-flash": "hidden"})))
    models = http.get("/api/models").json()["models"]
    assert [m["id"] for m in models] == [o.id for o in model_catalog.load().options if o.id != "deepseek-flash" and not o.status]
    assert models[0] == {"id": AUTO, "name": "自动", "description": models[0]["description"], "thinking": "toggle", "model": PRO, "locked": False}
    assert not any(m["locked"] for m in models)  # 现在没有登录，默认都算登录用户
    st = _create(http, "直接答：你好")
    assert st["model"] == AUTO
    rec = _records(st)[0]
    assert rec["option"] == AUTO and rec["model"] == {"ref": AUTO_REF, "thinking": True, "effort": "high"}
    assert rec["subagent_model"] == {"ref": FLASH_REF, "thinking": True, "effort": "high"}


def test_bad_model_is_rejected():
    """没有的、不再提供的模型开不了新对话（400）；已经开始的对话不收 model。"""
    http, _ = _client(FakeResearchPlanner(catalog=_with({"deepseek-flash": "hidden"})))
    for bad in ("nope", "deepseek-flash"):
        r = http.post("/api/research/threads", json={"text": "你好", "model": bad})
        assert r.status_code == 400 and "没有这个模型" in r.json()["detail"]
    assert http.get("/api/research/threads").json()["threads"] == []  # 没留下半截的对话
    tid = _create(http, "直接答：你好")["thread_id"]
    assert say(http, tid, "再说", model="deepseek-v4-pro").status_code == 422


def test_guest_sees_locked_models_and_cannot_pick_them():
    """游客：要登录的模型也列出来，标着 locked；直接调接口选它开不了对话（403，不留下对话）。游客能选的、不选（Auto）照常开；
    不再提供的照常是 400。"""
    http, _ = _client(FakeResearchPlanner(catalog=_with({"kimi-k3-1-pro": "hidden"})))
    http.headers.update(bearer(guest_token()))  # 游客的令牌
    locked = {m["id"]: m["locked"] for m in http.get("/api/models").json()["models"]}
    assert not locked[AUTO] and not locked["deepseek-flash"] and locked["deepseek-v4-pro"] and locked["qwen3-8-max"]
    r = http.post("/api/research/threads", json={"text": "你好", "model": "deepseek-v4-pro"})
    assert r.status_code == 403 and "登录" in r.json()["detail"]
    r = http.post("/api/research/threads", json={"text": "你好", "model": "kimi-k3-1-pro"})
    assert r.status_code == 400 and "没有这个模型" in r.json()["detail"]
    assert http.get("/api/research/threads").json()["threads"] == []
    assert _create(http, "直接答：你好", model="deepseek-flash")["model"] == "deepseek-flash"
    assert _create(http, "直接答：你好")["model"] == AUTO


def test_conversation_keeps_its_model_and_thinking_per_turn():
    """对话定下的模型每轮都用它；思考开关每句话各自算，子 agent 跟着关。"""
    p = FakeResearchPlanner()
    http, rr = _client(p)
    st = _create(http, "直接答：你好", model="doubao-seed-2-1-pro", thinking=False)
    tid = st["thread_id"]
    assert st["model"] == "doubao-seed-2-1-pro"
    p.model_uses.clear()
    st = last(events(say(http, tid, "子任务：查一下", thinking=True)), "state")
    second = last(events(say(http, tid, "子任务：再查", thinking=False)), "state")
    recs = _records(second)
    assert [r["option"] for r in recs] == ["doubao-seed-2-1-pro"] * 3
    assert [(r["model"]["ref"], r["model"]["thinking"]) for r in recs] == [(DOUBAO, False), (DOUBAO, True), (DOUBAO, False)]
    assert [r["subagent_model"]["thinking"] for r in recs] == [False, True, False]
    assert (ModelUse(ref=LITE, thinking=True), True) in p.model_uses and (ModelUse(ref=LITE, thinking=False), True) in p.model_uses
    assert {u.ref for u, sub in p.model_uses if not sub} == {DOUBAO} and {u.ref for u, sub in p.model_uses if sub} == {LITE}
    assert st["model"] == second["model"] == "doubao-seed-2-1-pro"


def test_regenerate_and_edit_take_a_new_thinking_switch():
    """重新回答可以换思考开关（不给按原来的），改了话没给开关的也按原来的；模型照对话定下的。"""
    http, rr = _client(FakeResearchPlanner())
    tid = _create(http, "直接答：你好", model="deepseek-flash", thinking=False)["thread_id"]
    base = f"/api/research/threads/{tid}"
    st = last(events(http.post(f"{base}/regenerate")), "state")
    assert _records(st)[0]["model"]["thinking"] is False
    st = last(events(http.post(f"{base}/regenerate", json={"thinking": True})), "state")
    assert _records(st)[0]["model"] == {"ref": FLASH_REF, "thinking": True, "effort": "high"}
    st = last(events(http.post(f"{base}/edit", json={"text": "直接答：改一下"})), "state")
    assert _records(st)[0]["model"]["thinking"] is True and st["turns"][0]["user"] == "直接答：改一下"
    st = last(events(http.post(f"{base}/edit", json={"text": "直接答：再改", "thinking": False})), "state")
    assert _records(st)[0]["model"]["thinking"] is False and len(st["turns"]) == 1


def test_retired_model_switches_to_auto_without_its_thinking():
    """厂商停了的模型：老对话下一轮起改用 Auto，之后都是 Auto；发给新模型的之前几轮不带原来那个模型的思考。只是不再提供的照常用。"""
    p = FakeResearchPlanner()
    p._model = Seeing()
    http, rr = _client(p)
    tid = _create(http, "直接答：你好", model="doubao-seed-2-1-pro")["thread_id"]
    last(events(say(http, tid, "直接答：还是豆包")), "state")
    assert [history.thinking_of(m) for m in p._model.seen[-1] if isinstance(m, AIMessage)] == ["（假思考）先想一想。"]  # 同一个模型带着
    p.catalog = _with({"doubao-seed-2-1-pro": "hidden"})
    assert state(http, tid)["model"] == "doubao-seed-2-1-pro"
    last(events(say(http, tid, "直接答：藏起来了")), "state")
    p.catalog = _with({"doubao-seed-2-1-pro": "retired"})
    assert state(http, tid)["model"] == AUTO  # 页面上下一轮起就是 Auto
    st = last(events(say(http, tid, "直接答：停了")), "state")
    recs = _records(st)
    assert [r["option"] for r in recs] == ["doubao-seed-2-1-pro"] * 3 + [AUTO]
    assert recs[-1]["model"]["ref"] == AUTO_REF and st["model"] == AUTO
    sent = [m for m in p._model.seen[-1] if isinstance(m, AIMessage)]
    assert len(sent) == 3 and not any(history.thinking_of(m) for m in sent)
    p.catalog = model_catalog.load()  # 又不停了：对话已经是 Auto，不回去
    assert _records(last(events(say(http, tid, "直接答：恢复了")), "state"))[-1]["option"] == AUTO
    values = aio.run(rr.graph.aget_state(rr._cfg(tid))).values
    assert values["option"] == AUTO


def test_old_conversation_becomes_auto():
    """2026-11-05 之前的对话没定选项，下一轮起是 Auto（还是原来那个模型），以前的轮不记模型。"""
    p = FakeResearchPlanner()
    http, rr = _client(p)
    tid = _create(http, "直接答：你好")["thread_id"]
    cfg = rr._cfg(tid)
    old = aio.run(rr.graph.aget_state(cfg)).values["turns"]
    legacy = [t.model_copy(update={"option": "", "model": None, "subagent_model": None}) for t in old]
    aio.run(rr.graph.aupdate_state(cfg, {"turns": legacy, "option": ""}, as_node="TurnMiddleware.after_agent"))
    st = state(http, tid)
    assert st["model"] == AUTO and _records(st)[0]["model"] is None
    st = last(events(say(http, tid, "直接答：接着说")), "state")
    assert [r["option"] for r in _records(st)] == ["", AUTO] and _records(st)[1]["model"]["ref"] == AUTO_REF
