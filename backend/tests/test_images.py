"""画图：经网关调 image-service、存图、改图、每轮上限、失败的说法、停止、删对话和重新生成时清图、取图接口。只用假模型和假网关。"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app import config
from app.api import main as api_main
from app.api.research_runner import ResearchRunner
from app.graph.research import history, prompts
from app.research_llm import FAKE_IMAGE_FILTERED, FakeResearchPlanner, FakeToolModel, _fake_image_service
from app.tools import images
from app.tools.gateway import GatewayAuthError, GatewayClient, ImageError
from app.tools.research_tools import make_research_tools
from tests.research_helpers import blocking, new
from tests.test_research import Chat, _answered, _kinds


class Recorded:
    """假 image-service，记下生图请求（参数和场景）。block 在出图前停住，模拟生图很慢。"""

    def __init__(self, block: threading.Event | None = None) -> None:
        self.generate: list[tuple[dict, str]] = []
        self.block = block
        self._images = _fake_image_service()

    async def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.url.path == "/image/v1/generate":
            self.generate.append((json.loads(req.content), req.headers["x-caller-scene"]))
            if self.block is not None:
                await asyncio.to_thread(self.block.wait, 10)  # 在线程里等，不卡对话的事件循环（点了停止，这里跟着取消）
            return await self._images(req)
        if req.url.path.startswith("/image/static/"):
            return await self._images(req)
        return httpx.Response(404)


def _planner(handler, model: FakeToolModel | None = None) -> FakeResearchPlanner:
    p = FakeResearchPlanner()
    client = GatewayClient("http://gw", "k", scene="research", transport=httpx.MockTransport(handler))
    p._gateway, p._tools = client, make_research_tools(client)
    if model is not None:
        p._model = model
    return p


@pytest.fixture
def gw() -> Recorded:
    return Recorded()


@pytest.fixture
def chat(tmp_path, gw) -> Chat:
    return Chat(tmp_path, planner=_planner(gw))


def _image_steps(turn: dict) -> list[dict]:
    return [i for i in turn["items"] if i["type"] == "step" and i["kind"] == "image"]


def _results(chat: Chat) -> list[ToolMessage]:
    return [m for m in chat.values()["messages"] if isinstance(m, ToolMessage) and m.name == "generate_image"]


def _files(tmp_path, tid: str = "t1") -> list[str]:
    d = tmp_path / "data" / "images" / tid
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


class Calls(FakeToolModel):
    """每一轮第一步发 calls 里的这些工具调用（同一条消息里并行），之后回复。"""

    calls: list[dict] = []

    def _decide(self, messages):
        turn = messages[history.last_human(messages) + 1:]
        if not any(isinstance(m, ToolMessage) for m in turn):
            return "", [{**c, "id": f"call_{len(messages)}_{k}"} for k, c in enumerate(self.calls)]
        return "好了。", []


# ---- 画新图 ----


def test_draw_saves_the_image_and_tells_the_model_only_its_id(chat, gw, tmp_path):
    v = chat.say("画一只猫")
    (turn,) = v["turns"]
    assert _kinds(turn) == ["thinking", "step:image", "thinking", "text"] and turn["items"][-1]["text"] == "画好了，看看要不要调整。"
    (step,) = _image_steps(turn)
    img = step["image"]
    assert step["ok"] and step["text"] == "画一只猫" and step["aspect_ratio"] == "1:1" and not step["edit"]
    assert (img["width"], img["height"], img["mime_type"], img["model"]) == (512, 512, "image/png", "doubao-seedream-5-0-flash")
    assert img["file"] == f"{img['id']}.png" and img["edited_from"] is None and _files(tmp_path) == [img["file"]]
    assert gw.generate == [({"prompt": "画一只猫", "aspect_ratio": "1:1", "grounding": True}, "chat_image")]
    # 模型只拿到一句话和编号：不给图片地址，也不把参数之外的东西交回给它
    (result,) = _results(chat)
    assert result.content == prompts.render("tools.generate_image.success", image_id=img["id"], reference_subject=None,
                                            reference_search_status="skipped", edited=False)
    assert f"Image id: {img['id']}." in result.content and "static" not in result.content and "http" not in result.content
    assert turn["record"]["usage"]["images"] == 1 and turn["record"]["usage"]["searches"] == 0


def test_draw_passes_ratio_quality_style_and_regenerate(tmp_path, gw):
    model = Calls(calls=[{"name": "generate_image", "args": {"prompt": " 海报 ", "aspect_ratio": "9:16", "quality": "high", "style": " 水彩 ",
                                                             "regenerate": True, "reference_subject": "  "}}])
    v = Chat(tmp_path, planner=_planner(gw, model)).say("做张海报")
    (step,) = _image_steps(v["turns"][0])
    assert gw.generate == [({"prompt": "海报", "aspect_ratio": "9:16", "grounding": True, "no_cache": True, "quality": "high", "style": "水彩"},
                            "chat_image")]
    assert (step["image"]["width"], step["image"]["height"], step["image"]["model"]) == (288, 512, "doubao-seedream-5-0-pro")


def test_reference_photo_not_found_is_told_to_the_model(tmp_path):
    service = _fake_image_service()

    async def handler(req: httpx.Request) -> httpx.Response:
        res = await service(req)
        if req.url.path == "/image/v1/generate":
            return httpx.Response(200, json={**res.json(), "reference_search": {"status": "none_accepted"}})
        return res

    c = Chat(tmp_path, planner=_planner(handler, Calls(calls=[{"name": "generate_image", "args": {"prompt": "手机", "reference_subject": "某型号手机"}}])))
    c.say("画这台手机")
    (result,) = _results(c)
    assert "No usable reference photo of 某型号手机 was found" in result.content
    assert result.artifact["reference_search_status"] == "none_accepted"


# ---- 改图 ----


def test_edit_hands_back_the_original_and_keeps_its_shape(chat, gw, tmp_path):
    first = _image_steps(chat.say("画一只横的猫")["turns"][0])[0]["image"]
    assert (first["width"], first["height"], first["aspect_ratio"]) == (512, 288, "16:9")
    v = chat.say("改图：背景换成海边")
    (step,) = _image_steps(v["turns"][1])
    img = step["image"]
    assert step["ok"] and step["edit"] and img["edited_from"] == first["id"] and img["id"] != first["id"]
    assert step["aspect_ratio"] == "16:9"  # 没给比例：用最接近原图的
    payload, scene = gw.generate[1]
    assert scene == "image_edit" and payload["prompt"] == "改图：背景换成海边" and payload["aspect_ratio"] == "16:9"
    assert payload["reference_images"] == [config.IMAGE_SERVICE_URL + "/static/images/fake1.png"] and "source_image_id" not in payload
    assert _results(chat)[1].content == prompts.render("tools.generate_image.success", image_id=img["id"], reference_subject=None,
                                                       reference_search_status="skipped", edited=True)
    assert _files(tmp_path) == sorted([first["file"], img["file"]])


def test_edit_of_an_unknown_image_is_not_sent(tmp_path, gw):
    """编号不是这段对话里画的图：不去画、不算次数，模型被告知这张图改不了。编号是编的、格式都不对也一样。"""
    model = Calls(calls=[{"name": "generate_image", "args": {"prompt": "改背景", "source_image_id": sid}} for sid in ("0123456789ab", "fake1")])
    c = Chat(tmp_path, planner=_planner(gw, model))
    v = c.say("把那张图改一下")
    steps = _image_steps(v["turns"][0])
    assert [s["message"] for s in steps] == ["这张图片无法直接编辑"] * 2 and all(s["edit"] and not s["ok"] for s in steps)
    assert gw.generate == []
    assert all(r.status == "error" and r.artifact == {"attempted": False, "error_code": "source_image_unavailable"} for r in _results(c))
    assert _results(c)[0].content == prompts.render("tools.generate_image.failed", error_code="source_image_unavailable", retryable=False,
                                                    fallback_from=None)


def test_attempts_before_counts_this_turn_only():
    def calls(*ids):
        return AIMessage(content="", tool_calls=[{"name": "generate_image", "args": {"prompt": "p"}, "id": i} for i in ids])

    def res(i, attempted):
        return ToolMessage(content="", tool_call_id=i, name="generate_image", artifact={"attempted": attempted})

    msgs = [HumanMessage("一"), calls("a"), res("a", True), HumanMessage("二"), calls("b", "c"), res("b", True), res("c", False),
            calls("d", "e", "f")]
    assert images.attempts_before(msgs, "d") == 1  # 上一轮的不算，没去画的 c 不算
    assert images.attempts_before(msgs, "f") == 3  # 同一条消息里排在前面、还没结果的算去画了


# ---- 上限、失败 ----


def test_at_most_three_images_a_turn(tmp_path, gw):
    model = Calls(calls=[{"name": "generate_image", "args": {"prompt": f"图{k}"}} for k in range(4)])
    c = Chat(tmp_path, planner=_planner(gw, model))
    v = c.say("画四张")
    steps = _image_steps(v["turns"][0])
    assert [s["ok"] for s in steps] == [True, True, True, False] and steps[3]["message"] == "这一轮画的图已经到上限"
    assert sorted(p["prompt"] for p, _ in gw.generate) == ["图0", "图1", "图2"]
    assert v["turns"][0]["record"]["usage"]["images"] == 3 and len(_files(tmp_path)) == 3
    v = c.say("再来四张")  # 下一轮重新算
    assert [s["ok"] for s in _image_steps(v["turns"][1])] == [True, True, True, False] and len(gw.generate) == 6


def test_content_filtered_is_said_plainly_and_counts(tmp_path, gw):
    model = Calls(calls=[{"name": "generate_image", "args": {"prompt": f"{FAKE_IMAGE_FILTERED}{k}"}} for k in range(3)]
                  + [{"name": "generate_image", "args": {"prompt": "正常的图"}}])
    c = Chat(tmp_path, planner=_planner(gw, model))
    v = c.say("画")
    steps = _image_steps(v["turns"][0])
    assert [s["message"] for s in steps] == ["图片内容未通过模型审核"] * 3 + ["这一轮画的图已经到上限"]  # 被拒的也算画过（可能已经花了钱）
    first = _results(c)[0]
    assert first.status == "error" and first.artifact == {"attempted": True, "error_code": "content_filtered"}
    assert first.content == prompts.render("tools.generate_image.failed", error_code="content_filtered", retryable=False, fallback_from=None)
    assert "content safety filter" in first.content and _files(tmp_path) == [] and v["turns"][0]["record"]["usage"]["images"] == 0


def test_bad_arguments_go_back_to_the_model(tmp_path, gw):
    model = Calls(calls=[{"name": "generate_image", "args": {"prompt": "猫", "aspect_ratio": "7:3"}},
                         {"name": "generate_image", "args": {"prompt": "   "}}])
    c = Chat(tmp_path, planner=_planner(gw, model))
    v = c.say("画")
    assert [s["message"] for s in _image_steps(v["turns"][0])] == ["图片生成失败"] * 2 and gw.generate == []
    bad, blank = _results(c)
    assert bad.status == "error" and "aspect_ratio" in bad.content and blank.artifact == {"attempted": False, "error_code": "invalid_arguments"}


def test_failures_of_the_image_service(tmp_path):
    """image-service 的失败码照原样交给模型（照 Fusion 的说法），主模型被拒、备用模型也没成时带上 fallback_from；取不到图也算画过。"""
    cases = [
        (httpx.Response(504, json={"error": "upstream_timeout", "message": "t", "retryable": True}), "upstream_timeout", True, None, "生成超时"),
        (httpx.Response(422, json={"error": "content_filtered", "retryable": False, "fallback_from": {"code": "content_filtered"}}),
         "content_filtered", False, "content_filtered", "图片内容未通过模型审核"),
        (httpx.Response(502, json={"error": "upstream_error", "retryable": True, "fallback_from": {"code": "content_filtered"}}),
         "upstream_error", True, "content_filtered", "图片生成失败"),
        (httpx.Response(502, text="<html>bad gateway</html>"), "invalid_response", True, None, "图片生成失败"),
        (httpx.Response(200, json={"image_url": "https://evil.test/x.png"}), "invalid_response", False, None, "图片生成失败"),
        (httpx.Response(200, json={"image_url": "/static/images/missing.png", "model": "m"}), "image_download_failed", True, None, "图片生成失败"),
    ]
    for k, (resp, code, retryable, fallback, label) in enumerate(cases):
        def handler(req: httpx.Request, resp=resp) -> httpx.Response:
            return resp if req.url.path == "/image/v1/generate" else httpx.Response(404)

        c = Chat(tmp_path, planner=_planner(handler), tid=f"f{k}")
        (step,) = _image_steps(c.say("画一只猫")["turns"][0])
        (result,) = _results(c)
        assert (step["ok"], step["message"]) == (False, label), code
        assert result.artifact == {"attempted": True, "error_code": code}, code
        assert result.content == prompts.render("tools.generate_image.failed", error_code=code, retryable=retryable, fallback_from=fallback)


def test_unauthorized_fails_the_run(tmp_path):
    """网关没放行生图、image-service 没登记灵稿：配置问题，这次运行失败，不当成没画成交给模型。"""
    with pytest.raises(GatewayAuthError):
        Chat(tmp_path, planner=_planner(lambda req: httpx.Response(403, json={"error": "x"}))).say("画一只猫")


def test_image_file_checks_type_and_size():
    def handler(req: httpx.Request) -> httpx.Response:
        name = req.url.path.rsplit("/", 1)[-1]
        if name == "big.png":
            return httpx.Response(200, content=b"x" * (10 * 1024 * 1024 + 1), headers={"content-type": "image/png"})
        if name == "page.png":
            return httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})
        return httpx.Response(200, content=b"img", headers={"content-type": "image/webp; q=1"})

    client = blocking(GatewayClient("http://gw", "k", scene="research", transport=httpx.MockTransport(handler)))
    assert client.image_file("/static/images/ok.webp") == (b"img", "image/webp")
    for path, kind in (("/static/images/big.png", "image_too_large"), ("/static/images/page.png", "image_download_failed"),
                       ("/static/../secret.png", "invalid_response")):
        with pytest.raises(ImageError) as e:
            client.image_file(path)
        assert e.value.kind == kind


def test_closest_aspect_ratio():
    assert images.closest_aspect_ratio(512, 288) == "16:9"
    assert images.closest_aspect_ratio(1000, 1010) == "1:1"
    assert images.closest_aspect_ratio(600, 2000) == "9:16"
    assert images.closest_aspect_ratio(3000, 1000) == "21:9"
    assert images.closest_aspect_ratio(0, 10) == "1:1"


# ---- 停止 ----


def test_stop_while_drawing_does_not_wait_for_the_image(tmp_path):
    gate = threading.Event()
    gw = Recorded(block=gate)
    c = Chat(tmp_path, planner=_planner(gw))
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
    assert _answered(c.values()["messages"]) and _files(tmp_path) == []
    time.sleep(0.3)  # 停止后那张图出来了也不存
    assert _files(tmp_path) == []
    assert c.say("直接答：好")["turns"][-1]["record"]["outcome"] == "ok"


# ---- 重新生成、删对话 ----


def test_regenerate_and_delete_remove_the_images(chat, gw, tmp_path):
    chat.say("画一只猫")
    old = _files(tmp_path)
    v = chat.regenerate()
    (step,) = _image_steps(v["turns"][0])
    assert len(old) == 1 and _files(tmp_path) == [step["image"]["file"]] and old != _files(tmp_path)
    chat.r.delete(chat.tid)
    assert not (tmp_path / "data" / "images" / "t1").exists()


def test_regenerate_button_redraws_the_same_description(chat, gw):
    """卡片上的“重新生成”发的是一句话（用同样的描述再生成一张），模型照上次的参数再画、带上 regenerate。"""
    chat.say("画一只猫")
    v = chat.say("用同样的描述再生成一张")
    (step,) = _image_steps(v["turns"][1])
    assert step["ok"] and gw.generate[1] == ({"prompt": "画一只猫", "aspect_ratio": "1:1", "grounding": True, "no_cache": True}, "chat_image")


# ---- 取图接口 ----


def test_image_route(tmp_path):
    api_main.set_research_runner(ResearchRunner(fake=True, data_dir=tmp_path / "data"))
    client = TestClient(api_main.app)
    v = new(client, "画一只猫")
    img = _image(v)
    r = client.get(f"/api/research/threads/{v['thread_id']}/images/{img['file']}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content.startswith(b"\x89PNG")
    assert r.headers["x-content-type-options"] == "nosniff" and "immutable" in r.headers["cache-control"]
    for path in (f"other/images/{img['file']}", f"{v['thread_id']}/images/0123456789ab.png", f"{v['thread_id']}/images/..%2Fx.png",
                 f"{v['thread_id']}/images/{img['id']}.svg"):
        assert client.get(f"/api/research/threads/{path}").status_code == 404, path
    assert client.delete(f"/api/research/threads/{v['thread_id']}").status_code == 200
    assert client.get(f"/api/research/threads/{v['thread_id']}/images/{img['file']}").status_code == 404


def _image(view: dict) -> dict:
    return next(i["image"] for t in view["turns"] for i in t["items"] if i["type"] == "step" and i.get("image"))


def test_image_of_ignores_other_tools_and_broken_records():
    assert history.image_of(ToolMessage(content="", tool_call_id="a", name="read_page", artifact={"image": {"id": "x"}})) is None
    assert history.image_of(ToolMessage(content="", tool_call_id="a", name="generate_image", artifact={"image": {"id": "x"}})) is None
    assert history.image_of(ToolMessage(content="", tool_call_id="a", name="generate_image", artifact=None)) is None
