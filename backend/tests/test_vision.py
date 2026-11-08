"""用户发的图怎么给模型（2026-11-08 Sean 定）：看得见图的模型直接看（最近 NATIVE_IMAGES 张），看不见的调看图工具 view_image，
由 [roles] 的 vision 看图回答；对话里不换模型。见 app.tools.vision。"""

from __future__ import annotations


import io
import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage, ToolMessage
from PIL import Image

from app import costs, model_catalog
from app.api import main as api_main
from app.api.research_runner import ResearchRunner
from app.graph.research import history, prompts
from app.model_guard import ModelUnavailable
from app.research_llm import FailingModel
from app.tools.vision import NATIVE_IMAGES, ImageUris, with_images
from tests.auth_helpers import USER
from tests.research_helpers import events, last, run_async

PRO, FLASH = "deepseek:deepseek-v4-pro", "deepseek:deepseek-flash"


def _photo(width: int = 64, height: int = 48) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, "PNG")
    return buf.getvalue()


def _send(client, path: str, text: str, n: int, **body):
    files = [("image", (f"p{k}.png", _photo(40 + k, 30), "image/png")) for k in range(n)]
    return client.post(path, data={"body": json.dumps({"text": text, **body})}, files=files)


def _api() -> tuple[ResearchRunner, TestClient]:
    rr = ResearchRunner(fake=True)
    api_main.set_research_runner(rr)
    return rr, TestClient(api_main.app, headers=USER)


def _steps(turn: dict, kind: str) -> list[dict]:
    return [i for i in turn["items"] if i["type"] == "step" and i["kind"] == kind]


def _reply(turn: dict) -> str:
    return next(i["text"] for i in reversed(turn["items"]) if i["type"] == "text")


def _pro_refs(rr: ResearchRunner) -> set[str]:
    return {PRO, *(f.model for f in rr.planner.catalog.free if f.same_as == PRO)}


def _messages(rr: ResearchRunner, tid: str) -> list:
    return rr.graph.get_state(rr._cfg(tid)).values["messages"]


# ---- 哪些模型看得见图 ----


def test_models_toml_vision_flags_follow_the_2026_11_08_probe():
    """实测（一张写着数字的图问是几）：V4 Pro 把图丢了编一个数，GLM-5.3/5.2 报 400，混元 Hy3 说看不到；别的都读对了。
    免费额度没写的跟 same_as 的选项；Auto 用的 K3 自己写。"""
    c = model_catalog.load()
    blind = {o.id for o in c.options[1:] if not c.sees(o.model)}
    assert blind == {"deepseek-v4-pro", "glm-5-3", "glm-5-2", "hy3"}
    assert not c.sees(c.options[0].model)  # Auto 判为复杂的用的 V4 Pro
    for f in c.free:
        assert c.sees(f.model) == (c.sees(f.same_as) if f.same_as else True), f.model
    assert c.sees(c.role("vision").ref) and costs.tool_price(history.VIEW_TOOL) == 0.005
    assert not c.sees("deepseek:no-such-model")


# ---- 给模型的那份 ----


def _att(iid: str) -> dict:
    return {"id": iid, "mime_type": "image/png", "width": 64, "height": 48}


async def _uri(iid: str) -> str | None:
    return None if iid.startswith("gone") else f"data:image/png;base64,{iid}"


def _blocks(m, kind: str) -> list:
    return [b for b in m.content if isinstance(b, dict) and b.get("type") == kind] if isinstance(m.content, list) else []


def test_blind_models_get_a_note_with_ids_and_no_images():
    m = HumanMessage(content="图里写的什么", additional_kwargs={history.ATTACHED: [_att("a1"), _att("b2")]})
    plain = HumanMessage(content="你好")
    out = run_async(with_images([plain, m], False, _uri))
    assert out[0] is plain and _blocks(out[1], "image_url") == []
    edit = prompts.render("tools.generate_image._edit")
    assert out[1].content == "图里写的什么\n\n" + prompts.render("tools.generate_image.attached_unseen", ids=["a1", "b2"], edit=edit)
    assert "view_image" in out[1].content and m.content == "图里写的什么"  # 存着的那条不变


def test_seeing_models_get_the_latest_images_and_notes_for_the_rest():
    """看得见的：最近 NATIVE_IMAGES 张接在那句话后面（按发的先后），更早的、服务里没了的换成说明（编号照给，还能改图）。
    11-08 以前存的一张（不是列表）一样认。"""
    old = HumanMessage(content="第一张", additional_kwargs={history.ATTACHED: _att("old0")})
    many = HumanMessage(content="一堆", additional_kwargs={history.ATTACHED: [_att(f"m{k}") for k in range(NATIVE_IMAGES)]})
    last_one = HumanMessage(content="这张", additional_kwargs={history.ATTACHED: [_att("gone1"), _att("z9")]})
    out = run_async(with_images([old, many, last_one], True, _uri))
    urls = [[b["image_url"]["url"].split(",")[1] for b in _blocks(m, "image_url")] for m in out]
    assert urls == [[], [f"m{k}" for k in range(2, NATIVE_IMAGES)], ["z9"]]  # 最近 10 张（gone1 算一张，取不到）
    assert "can no longer be shown" in history.text_of(out[0]) and "old0" in history.text_of(out[0])
    assert "m0, m1" in history.text_of(out[1]) and "shown above" in history.text_of(out[1])
    assert "gone1" in history.text_of(out[2]) and "z9" in history.text_of(out[2])
    assert history.text_of(out[2]).startswith("这张")


def test_image_uris_cache_by_id_and_drop_the_oldest():
    class Gw:
        calls: list[str] = []

        async def image(self, iid):
            self.calls.append(iid)
            return None if iid == "none" else (b"x" * 30, "image/png")

    gw = Gw()
    uris = ImageUris(gw, limit=130)  # 一张的 data URI 62 字：放得下两张
    a = run_async(uris.get("a"))
    assert a == "data:image/png;base64," + "eHh4" * 10 and run_async(uris.get("a")) == a and gw.calls == ["a"]
    run_async(uris.get("b"))
    run_async(uris.get("c"))  # 超了：丢最久没用的 a
    run_async(uris.get("a"))
    assert gw.calls == ["a", "b", "c", "a"] and run_async(uris.get("none")) is None


# ---- 整条链路（假模型：问“图里”有什么，有看图工具就调，没有就说收到了几张图） ----


def test_pro_cannot_see_so_it_calls_view_image_and_the_model_is_not_switched():
    rr, client = _api()
    v = last(events(_send(client, "/api/research/threads", "图里有什么", 2, model="deepseek-v4-pro")), "state")
    (turn,) = v["turns"]
    second = rr.planner.images.uploaded[1]
    (step,) = _steps(turn, "view")
    assert step["ok"] and step["text"] == "图里有什么"
    assert _reply(turn) == "看了：（看图）image/png：图里有什么"  # 看的是这句话带的最后一张（假看图回 MIME 类型）
    assert ("view_image", "图里有什么") in rr.planner.calls
    assert {u.ref for u, sub in rr.planner.model_uses if not sub} <= _pro_refs(rr)  # 没换模型（免费额度上的 Pro 也是 Pro）
    msgs = _messages(rr, v["thread_id"])
    call = next(tc for m in msgs for tc in getattr(m, "tool_calls", []) if tc["name"] == history.VIEW_TOOL)
    assert call["args"]["image_id"] == second
    result = next(m for m in msgs if isinstance(m, ToolMessage) and m.name == history.VIEW_TOOL)
    assert result.response_metadata.get(costs.COST) == 0.005  # 看成了按次收钱


def test_flash_sees_the_images_and_has_no_view_tool():
    rr, client = _api()
    v = last(events(_send(client, "/api/research/threads", "图里有什么", 3, model="deepseek-flash")), "state")
    (turn,) = v["turns"]
    assert _reply(turn) == "看到了 3 张图。" and _steps(turn, "view") == []
    assert ("view_image", "图里有什么") not in rr.planner.calls
    assert len(turn["images"]) == 3 and [i["width"] for i in turn["images"]] == [40, 41, 42]


def test_auto_simple_uses_k3_which_sees_and_auto_complex_uses_pro_with_the_tool():
    rr, client = _api()
    v = last(events(_send(client, "/api/research/threads", "简单问题：图里有什么", 1)), "state")
    assert _reply(v["turns"][0]) == "看到了 1 张图。"
    v = last(events(_send(client, "/api/research/threads", "复杂的：图里有什么", 1)), "state")
    assert _steps(v["turns"][0], "view")[0]["ok"]


def test_view_image_only_looks_at_images_of_this_conversation():
    """编号不对（编的、别的对话的）：不去看，给模型一句说明，页面上“没看成”，不收钱。看图的模型出问题也一样。"""
    rr, client = _api()
    other = last(events(_send(client, "/api/research/threads", "直接答：先放一张", 1)), "state")
    theirs = other["turns"][0]["images"][0]["id"]
    v = last(events(_send(client, "/api/research/threads", f"看编号{theirs} 图里有什么", 1, model="deepseek-v4-pro")), "state")
    (step,) = _steps(v["turns"][0], "view")
    assert not step["ok"] and step["message"] == "没看成" and _reply(v["turns"][0]) == "没看成。"
    result = next(m for m in _messages(rr, v["thread_id"]) if isinstance(m, ToolMessage) and m.name == history.VIEW_TOOL)
    assert result.status == "error" and theirs in result.content and costs.COST not in result.response_metadata
    assert not any(c[0] == "view_image" for c in rr.planner.calls)
    rr.planner.fail_view = True
    v = last(events(_send(client, "/api/research/threads", "图里有什么", 1, model="deepseek-v4-pro")), "state")
    assert not _steps(v["turns"][0], "view")[0]["ok"]


def test_switching_to_the_backup_mid_turn_reorganizes_the_images():
    """Pro 坏了换备用 Flash（看得见图）：按换上的重新组织，图给它、看图工具拿掉。"""
    rr, client = _api()
    for ref in _pro_refs(rr):
        rr.planner.broken[ref] = FailingModel(error=ModelUnavailable("模型服务暂时不可用（服务端出错），稍后再试"))
    v = last(events(_send(client, "/api/research/threads", "图里有什么", 2, model="deepseek-v4-pro")), "state")
    assert _reply(v["turns"][0]) == "看到了 2 张图。" and _steps(v["turns"][0], "view") == []
    assert rr.planner.model_uses[-1][0].ref in {FLASH, *(f.model for f in rr.planner.catalog.free if f.same_as == FLASH)}


def test_at_most_four_images_per_message():
    rr, client = _api()
    r = _send(client, "/api/research/threads", "图里有什么", history.MAX_ATTACHED + 1)
    assert (r.status_code, r.json()["detail"]) == (400, f"一次最多带 {history.MAX_ATTACHED} 张图")
    assert rr.planner.images.uploaded == []


def test_one_rejected_image_discards_the_ones_already_uploaded():
    rr, client = _api()
    good = _photo()
    files = [("image", ("a.png", good, "image/png")), ("image", ("b.gif", b"GIF89a" + b"\0" * 20, "image/gif"))]
    r = client.post("/api/research/threads", data={"body": json.dumps({"text": "图里有什么"})}, files=files)
    assert r.status_code == 415
    service = rr.planner.images
    assert len(service.uploaded) == 1 and service.deleted == service.uploaded


@pytest.mark.parametrize("n", [1, 2])
def test_regenerate_and_edit_keep_all_images(n):
    rr, client = _api()
    v = last(events(_send(client, "/api/research/threads", "直接答：看看", n)), "state")
    ids = [i["id"] for i in v["turns"][0]["images"]]
    v = last(events(client.post(f"/api/research/threads/{v['thread_id']}/edit", json={"text": "直接答：再看看"})), "state")
    assert [i["id"] for i in v["turns"][0]["images"]] == ids
