"""画图：agent-tools 的 MCP 工具 generate_image（app.tools.image_mcp）。改图只认这段对话的图、每轮上限、失败的说法、
经网关调（401/429/连不上）、停止、删对话和重新回答时删图、取图接口、以前的图。
服务用进程内的假 image-service（research_llm.FakeImageService）；经 HTTP 的用 ASGI 模拟网关。只用假模型。"""

from __future__ import annotations

import threading
import time
import uuid
from types import SimpleNamespace

import httpx
import httpx2
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app import aio
from app.api import main as api_main
from app.api.research_runner import ResearchRunner
from app.graph.research import history, prompts
from app.research_llm import FAKE_IMAGE_FILTERED, FakeImageService, FakeResearchPlanner, FakeToolModel
from app.tools import image_mcp
from app.tools.gateway import GatewayAuthError
from app.tools.image_mcp import ImageMiddleware, ImageTool, attempts_before, gateway_target, placeholder
from tests.research_helpers import after_tool, events, gated_runner, new, say, state, wait_until
from tests.test_research import Chat, _answered, _kinds
from tests.auth_helpers import USER

SHOWN = prompts.render("tools.generate_image.shown")


def _planner(model: FakeToolModel | None = None) -> FakeResearchPlanner:
    p = FakeResearchPlanner()
    if model is not None:
        p._model = model
    return p


@pytest.fixture
def chat() -> Chat:
    return Chat(planner=_planner())


def _svc(c: Chat) -> FakeImageService:
    return c.planner.images


def _image_steps(turn: dict) -> list[dict]:
    return [i for i in turn["items"] if i["type"] == "step" and i["kind"] == "image"]


def _results(chat: Chat) -> list[ToolMessage]:
    return [m for m in chat.values()["messages"] if isinstance(m, ToolMessage) and m.name == "generate_image"]


class Calls(FakeToolModel):
    """每一轮第一步发 calls 里的这些工具调用（同一条消息里并行），之后回复。"""

    calls: list[dict] = []

    def _decide(self, messages):
        turn = messages[history.last_human(messages) + 1:]
        if not any(isinstance(m, ToolMessage) for m in turn):
            return "", [{**c, "id": f"call_{len(messages)}_{k}"} for k, c in enumerate(self.calls)]
        return "好了。", []


# ---- 画新图 ----


def test_draw_keeps_only_the_id_and_the_card_details(chat):
    v = chat.say("画一只猫")
    (turn,) = v["turns"]
    assert _kinds(turn) == ["thinking", "step:image", "thinking", "text"] and turn["items"][-1]["text"] == "画好了，看看要不要调整。"
    (step,) = _image_steps(turn)
    img = step["image"]
    assert step["ok"] and step["text"] == "画一只猫" and step["aspect_ratio"] == "1:1" and not step["edit"] and not step["message"]
    assert (img["width"], img["height"], img["mime_type"], img["model"]) == (512, 512, "image/png", "doubao-seedream-5-0-flash")
    assert uuid.UUID(img["id"]) and img["file"] is None and img["edited_from"] is None and img["prompt"] == "画一只猫"
    assert img["id"] in _svc(chat).made and _svc(chat).calls == [{"prompt": "画一只猫"}]
    # 模型看的是服务的那段英文（有编号、没有地址），后面接一句卡片上已经标了模型
    (result,) = _results(chat)
    assert result.content.startswith(f"The image was generated. Image id: {img['id']}.") and result.content.endswith(SHOWN)
    assert "http" not in result.content and result.artifact == {"attempted": True, "image": img, "reference_search_status": None}
    assert turn["record"]["usage"]["images"] == 1 and turn["record"]["usage"]["searches"] == 0


def test_arguments_go_to_the_service_as_the_model_gave_them():
    model = Calls(calls=[{"name": "generate_image", "args": {"prompt": "海报", "aspect_ratio": "9:16", "quality": "high", "style": "水彩",
                                                             "regenerate": True, "reference_subject": "某型号手机"}}])
    c = Chat(planner=_planner(model))
    _svc(c).reference_status = "none_accepted"
    (step,) = _image_steps(c.say("做张海报")["turns"][0])
    assert _svc(c).calls == [{"prompt": "海报", "aspect_ratio": "9:16", "quality": "high", "style": "水彩", "regenerate": True,
                              "reference_subject": "某型号手机"}]
    assert (step["image"]["width"], step["image"]["height"], step["image"]["model"], step["aspect_ratio"]) == (288, 512, "doubao-seedream-5-0-pro", "9:16")
    assert _results(c)[0].artifact["reference_search_status"] == "none_accepted"


# ---- 改图 ----


def test_edit_sends_the_id_and_the_service_keeps_the_shape(chat):
    first = _image_steps(chat.say("画一只横的猫")["turns"][0])[0]["image"]
    assert (first["width"], first["height"], first["aspect_ratio"]) == (512, 288, "16:9")
    v = chat.say("改图：背景换成海边")
    (step,) = _image_steps(v["turns"][1])
    img = step["image"]
    assert step["ok"] and step["edit"] and img["edited_from"] == first["id"] and img["id"] != first["id"]
    assert step["aspect_ratio"] == "16:9" and (img["width"], img["height"]) == (512, 288)  # 没给比例：服务按原图的形状画，卡片按实际形状占位
    assert _svc(chat).calls[1] == {"prompt": "改图：背景换成海边", "source_image_id": first["id"]}
    assert _results(chat)[1].content.startswith("The image was edited.")


def test_edit_of_an_image_not_in_this_conversation_is_not_sent():
    """编号不是这段对话里画的、还在的图：编的、别的对话的（服务里有）、以前灵稿自己存的，都不交给服务，不算次数，照找不到原图说。"""
    other = Chat(planner=_planner(), tid="other")
    other.say("画一只猫")
    elsewhere = _image_steps(other.view()["turns"][0])[0]["image"]["id"]
    ids = [str(uuid.uuid4()), "img_55d0d6a946b5e", elsewhere, "0123456789ab"]
    model = Calls(calls=[{"name": "generate_image", "args": {"prompt": "改背景", "source_image_id": sid}} for sid in ids])
    c = Chat(planner=_planner(model))
    _svc(c).made[elsewhere] = other.planner.images.made[elsewhere]  # 同一个服务里有这张，但不是这段对话的
    v = c.say("把那张图改一下")
    steps = _image_steps(v["turns"][0])
    assert [s["message"] for s in steps] == ["这张图片无法直接编辑"] * 4 and all(s["edit"] and not s["ok"] for s in steps)
    assert _svc(c).calls == []
    for r in _results(c):
        assert r.status == "error" and r.artifact == {"attempted": False, "error_code": "source_image_unavailable"}
        assert r.content == prompts.render("tools.generate_image.source_image_unavailable")


def test_attempts_before_counts_this_turn_only():
    def calls(*ids):
        return AIMessage(content="", tool_calls=[{"name": "generate_image", "args": {"prompt": "p"}, "id": i} for i in ids])

    def res(i, attempted):
        return ToolMessage(content="", tool_call_id=i, name="generate_image", artifact={"attempted": attempted})

    msgs = [HumanMessage("一"), calls("a"), res("a", True), HumanMessage("二"), calls("b", "c"), res("b", True), res("c", False),
            calls("d", "e", "f")]
    assert attempts_before(msgs, "d") == 1  # 上一轮的不算，没去画的 c 不算
    assert attempts_before(msgs, "f") == 3  # 同一条消息里排在前面、还没结果的算去画了


# ---- 上限、失败 ----


def test_at_most_three_images_a_turn():
    model = Calls(calls=[{"name": "generate_image", "args": {"prompt": f"图{k}"}} for k in range(4)])
    c = Chat(planner=_planner(model))
    v = c.say("画四张")
    steps = _image_steps(v["turns"][0])
    assert [s["ok"] for s in steps] == [True, True, True, False] and steps[3]["message"] == "这一轮画的图已经到上限"
    assert sorted(a["prompt"] for a in _svc(c).calls) == ["图0", "图1", "图2"]
    assert _results(c)[3].content == prompts.render("tools.generate_image.run_image_limit_reached", max_per_turn=3)
    assert v["turns"][0]["record"]["usage"]["images"] == 3
    v = c.say("再来四张")  # 下一轮重新算
    assert [s["ok"] for s in _image_steps(v["turns"][1])] == [True, True, True, False] and len(_svc(c).calls) == 6


def test_content_filtered_keeps_the_service_text_and_counts():
    model = Calls(calls=[{"name": "generate_image", "args": {"prompt": f"{FAKE_IMAGE_FILTERED}{k}"}} for k in range(3)]
                  + [{"name": "generate_image", "args": {"prompt": "正常的图"}}])
    c = Chat(planner=_planner(model))
    v = c.say("画")
    steps = _image_steps(v["turns"][0])
    assert [s["message"] for s in steps] == ["图片内容未通过模型审核"] * 3 + ["这一轮画的图已经到上限"]  # 被拒的也算画过（可能已经花了钱）
    first = _results(c)[0]
    assert first.status == "error" and first.artifact == {"attempted": True, "error_code": "content_filtered"}
    assert first.content.startswith("The image model refused this request") and v["turns"][0]["record"]["usage"]["images"] == 0


def test_bad_arguments_go_back_to_the_model():
    """比例不在服务的可选值里：服务的参数校验不过（MCP 的 isError），没去画、不算次数，服务的说明照给模型。"""
    model = Calls(calls=[{"name": "generate_image", "args": {"prompt": "猫", "aspect_ratio": "7:3"}}])
    c = Chat(planner=_planner(model))
    (step,) = _image_steps(c.say("画")["turns"][0])
    (bad,) = _results(c)
    assert step["message"] == "图片生成失败" and _svc(c).calls == []
    assert bad.status == "error" and "aspect_ratio" in bad.content and bad.artifact == {"attempted": False, "error_code": "invalid_arguments"}


@pytest.mark.parametrize(("code", "label", "attempted"), [
    ("upstream_timeout", "生成超时", True),
    ("upstream_error", "图片生成失败", True),
    ("source_image_unavailable", "这张图片无法直接编辑", False),  # 这段对话里的图，服务里却没有了（比如删过）
    ("invalid_params", "图片生成失败", False),
])
def test_service_failures(code, label, attempted):
    c = Chat(planner=_planner())
    _svc(c).fail = {"code": code, "message": "x", "retryable": code == "upstream_timeout"}
    (step,) = _image_steps(c.say("画一只猫")["turns"][0])
    (result,) = _results(c)
    assert (step["ok"], step["message"]) == (False, label)
    assert result.artifact == {"attempted": attempted, "error_code": code} and result.content == f"Image generation failed (error code: {code})."


def test_unexpected_result_counts_as_drawn():
    tool = ImageTool(FakeImageService().server)
    aio.run(tool.connect())

    async def handler(_):
        return ToolMessage(content="ok", tool_call_id="c1", name="generate_image",
                           artifact={"structured_content": {"ok": True, "image": {"id": "x"}}})

    out = aio.run(ImageMiddleware().awrap_tool_call(SimpleNamespace(tool_call=_call(), state={"messages": []}), handler))
    assert out.status == "error" and out.artifact == {"attempted": True, "error_code": "invalid_response"}


# ---- 没连上、经网关调（HTTP） ----


def _call(cid: str = "c1", **args) -> dict:
    return {"name": "generate_image", "args": {"prompt": "猫", **args}, "id": cid, "type": "tool_call"}


async def _through(tool, call: dict, messages=()) -> ToolMessage:
    req = SimpleNamespace(tool_call=call, state={"messages": list(messages)})
    return await ImageMiddleware().awrap_tool_call(req, lambda r: tool.ainvoke(call))


def test_connect_takes_the_service_tool_and_adds_the_lingdraft_note():
    tool = ImageTool(FakeImageService().server)
    assert tool.current().description.endswith(prompts.render("tools.generate_image.lingdraft"))  # 没连上：占位
    aio.run(tool.connect())
    real = tool.current()
    assert real is tool.tool and real.description.startswith("Generate a new image")
    assert {"prompt", "aspect_ratio", "source_image_id", "regenerate"} <= set(real.args_schema["properties"])


def test_placeholder_until_connected_tells_the_model_the_service_is_down():
    out = aio.run(_through(placeholder(), _call(aspect_ratio="16:9")))
    assert out.status == "error" and out.content == prompts.render("tools.generate_image.unavailable")
    assert out.artifact == {"attempted": False, "error_code": "unavailable"}  # 没发出去，不算次数


class _Gateway:
    """模拟网关：/image/v2/mcp 转给假 image-service 的 MCP，记下请求头；status 设了就直接回这个状态。"""

    def __init__(self) -> None:
        self.service = FakeImageService()
        self.app = self.service.server().http_app(path="/image/v2/mcp", stateless_http=True)
        self.status: int | None = None
        self.headers: list[dict] = []

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            self.headers.append({k.decode(): v.decode() for k, v in scope["headers"]})
            if self.status:
                await send({"type": "http.response.start", "status": self.status, "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"error":"x"}'})
                return
        await self.app(scope, receive, send)


def test_calls_go_through_the_gateway():
    gw = _Gateway()

    async def go():
        async with gw.app.lifespan(gw.app):
            tool = ImageTool(lambda: gateway_target("http://gw/", "k1", scene="research_eval", transport=httpx2.ASGITransport(app=gw)))
            await tool.connect()
            out = {"ok": await _through(tool.tool, _call())}
            for status in (429, 502):
                gw.status = status
                out[status] = await _through(tool.tool, _call())
            gw.status = 401
            with pytest.raises(GatewayAuthError) as e:
                await _through(tool.tool, _call())
            out["auth"] = e.value
            return out

    out = aio.run(go())
    assert out["ok"].status == "success" and out["ok"].artifact["image"]["id"] in gw.service.made
    assert gw.headers[0]["authorization"] == "Bearer k1" and gw.headers[0]["x-caller-scene"] == "research_eval"
    assert out[429].content == prompts.render("tools.generate_image.quota_exhausted")
    assert out[429].artifact == {"attempted": False, "error_code": "quota_exhausted"}  # 网关拦下的没去画
    assert out[502].content == prompts.render("tools.generate_image.unavailable")
    assert out[502].artifact == {"attempted": True, "error_code": "unavailable"}  # 发出去了没拿到结果：服务那边可能已经画了
    assert out["auth"].kind == "http_401"  # 密钥不对、网关没放行：让这次运行失败
    assert image_mcp.CALL_TIMEOUT > 135  # 比 image-service 整条主备链的限时长


# ---- 停止 ----


def test_stop_while_drawing_does_not_wait_for_the_image():
    gate = threading.Event()
    c = Chat(planner=_planner())
    _svc(c).block = gate
    t0 = time.monotonic()
    try:
        v = c.say("画一只猫", stop_on=lambda ev: ev["event"] == "step" and ev["data"]["step"]["kind"] == "image")
        elapsed = time.monotonic() - t0
    finally:
        gate.set()
    (turn,) = v["turns"]
    (step,) = _image_steps(turn)
    assert elapsed < 3 and turn["record"]["outcome"] == "stopped" and v["status"] == "idle"
    assert not step["ok"] and step["message"] == "已停止生成" and step["aspect_ratio"] == ""
    assert _answered(c.values()["messages"]) and _results(c)[0].artifact.get("image") is None
    assert c.say("直接答：好")["turns"][-1]["record"]["outcome"] == "ok"


# ---- 重新回答、删对话 ----


def test_regenerate_and_delete_remove_the_images_from_the_service(chat):
    chat.say("画一只猫")
    old = _image_steps(chat.view()["turns"][0])[0]["image"]["id"]
    v = chat.regenerate()
    new_id = _image_steps(v["turns"][0])[0]["image"]["id"]
    assert _svc(chat).deleted == [old] and new_id != old and set(_svc(chat).made) == {new_id}
    chat.r.delete(chat.tid)
    assert _svc(chat).deleted == [old, new_id] and _svc(chat).made == {}


def test_regenerate_button_redraws_the_same_description(chat):
    """卡片上的“重新生成”发的是一句话（用同样的描述再生成一张），模型照上次的参数再画、带上 regenerate。"""
    chat.say("画一只猫")
    v = chat.say("用同样的描述再生成一张")
    (step,) = _image_steps(v["turns"][1])
    assert step["ok"] and _svc(chat).calls[1] == {"prompt": "画一只猫", "regenerate": True}


# ---- 以前的图 ----


OLD_IMAGE = {"id": "0123456789ab", "file": "0123456789ab.png", "mime_type": "image/png", "width": 512, "height": 288, "prompt": "猫",
             "aspect_ratio": "16:9", "model": "doubao-seedream-5-0-flash", "edited_from": None}


def test_old_images_say_they_are_gone():
    """2026-11-04 以前灵稿自己存的图不搬（Sean 定）：卡片说这张图已经不在了，过程里照样是画成了；删对话不去服务里删。"""
    call = {"name": "generate_image", "args": {"prompt": "猫"}, "id": "c1"}
    old = ToolMessage(content="", tool_call_id="c1", name="generate_image", artifact={"attempted": True, "image": OLD_IMAGE,
                                                                                       "source_url": "http://image-service/static/images/a.png"})
    step = history.step_of(call, old)
    assert step.ok and step.image is not None and step.message == history.IMAGE_GONE and step.aspect_ratio == "16:9"
    assert history.stored_images([old]) == [] and history.usage_of([old]).images == 1


def test_image_of_ignores_other_tools_and_broken_records():
    assert history.image_of(ToolMessage(content="", tool_call_id="a", name="read_page", artifact={"image": OLD_IMAGE})) is None
    assert history.image_of(ToolMessage(content="", tool_call_id="a", name="generate_image", artifact={"image": {"id": "x"}})) is None
    assert history.image_of(ToolMessage(content="", tool_call_id="a", name="generate_image", artifact=None)) is None


# ---- 取图接口 ----


def test_image_route():
    rr = ResearchRunner(fake=True)
    api_main.set_research_runner(rr)
    client = TestClient(api_main.app, headers=USER)
    v = new(client, "画一只猫")
    tid = v["thread_id"]
    img = next(i["image"] for t in v["turns"] for i in t["items"] if i["type"] == "step" and i.get("image"))
    r = client.get(f"/api/research/threads/{tid}/images/{img['id']}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content.startswith(b"\x89PNG")
    assert r.headers["x-content-type-options"] == "nosniff" and "immutable" in r.headers["cache-control"]
    service = rr.planner.images
    # 服务里没有的、以前灵稿自己存的（编号不是 uuid）、编号格式不对、线程 id 格式不对
    for path in (f"{tid}/images/{uuid.uuid4()}", f"{tid}/images/0123456789ab", f"{tid}/images/..%2Fx", f"bad%20id/images/{img['id']}"):
        assert client.get(f"/api/research/threads/{path}").status_code == 404, path
    assert client.delete(f"/api/research/threads/{tid}").status_code == 200
    assert img["id"] in service.deleted and client.get(f"/api/research/threads/{tid}/images/{img['id']}").status_code == 404


def test_image_shows_while_the_turn_is_still_running():
    """图画好、页面马上来取，这时模型还在写回复，检查点里还没有这张图（运行结束才写）：照样取得到。
    2026-11-04 上线后 dev 上踩到：页面取图回 404，显示图片加载失败。"""
    rr, gate = gated_runner(None, after_tool("generate_image"))  # 停在画完图、写回复之前
    api_main.set_research_runner(rr)
    client = TestClient(api_main.app, headers=USER)
    tid = new(client, "直接答：先说一句")["thread_id"]
    done: dict = {}
    t = threading.Thread(target=lambda: done.setdefault("resp", say(client, tid, "画一只猫")))
    t.start()
    try:
        assert wait_until(lambda: rr.planner.images.made and state(client, tid)["status"] == "running")
        (iid,) = rr.planner.images.made
        assert not any(history.image_of(m) for m in rr.graph.get_state(rr._cfg(tid)).values["messages"])  # 检查点里还没有
        r = client.get(f"/api/research/threads/{tid}/images/{iid}")
        assert r.status_code == 200 and r.content.startswith(b"\x89PNG")
    finally:
        gate.set()
        t.join(30)
    events(done["resp"])


def test_image_route_when_the_service_is_down(monkeypatch):
    rr = ResearchRunner(fake=True)
    api_main.set_research_runner(rr)
    client = TestClient(api_main.app, headers=USER)
    v = new(client, "画一只猫")
    img = next(i["image"] for t in v["turns"] for i in t["items"] if i["type"] == "step" and i.get("image"))

    async def down(req):
        return httpx.Response(502)

    monkeypatch.setattr(rr.planner.images, "handle", down)
    rr.images = rr.planner.images.gateway_client()  # 新的客户端用换掉的处理
    assert client.get(f"/api/research/threads/{v['thread_id']}/images/{img['id']}").status_code == 502
