"""M2：接口层把 interrupt / resume 搬到 SSE 上，状态查询与导出可用。"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config
from app.api import main as api_main
from app.api.runner import GraphRunner


@pytest.fixture
def client(tmp_path):
    api_main.set_runner(GraphRunner(fake=True, data_dir=tmp_path / "data", out_dir=tmp_path / "out"))
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


def test_full_flow_over_http(client):
    ev = _events(client.post("/api/threads", data={"request": "某品牌蓝牙耳机黑五预热，5 折，11.20 到 11.27"}))
    kinds = [e for e, _ in ev]
    assert kinds[:3] == ["thread", "started", "progress"] and "stage" in ev[2][1]
    assert kinds[-4:] == ["node", "interrupt", "state", "end"]
    state = _last(ev, "state")
    tid = state["thread_id"]
    assert state["interrupt"]["type"] == "confirm_facts"

    values = {f["key"]: f["value"] for f in state["interrupt"]["fields"]}
    ev = _events(client.post(f"/api/threads/{tid}/resume", json={"payload": {"values": values}}))
    assert _last(ev, "interrupt")["type"] == "choose_style"

    ev = _events(client.post(f"/api/threads/{tid}/resume", json={"payload": {"style_id": "s1"}}))
    review = _last(ev, "interrupt")
    assert review["type"] == "review" and review["png_url"].endswith("/files/v1.png")
    assert any(e == "node" and d.get("node") == "render" and d["version"] == 1 for e, d in ev)
    assert client.get(review["png_url"]).headers["content-type"] == "image/png"

    ev = _events(client.post(f"/api/threads/{tid}/resume", json={"payload": {"action": "edit", "target": "headline", "instruction": "更紧迫"}}))
    assert _last(ev, "interrupt")["version"] == 2

    st = client.get(f"/api/threads/{tid}/state").json()
    assert st["next"] == ["review"] and len(st["versions"]) == 2 and st["interrupt"]["type"] == "review"

    ev = _events(client.post(f"/api/threads/{tid}/resume", json={"payload": {"action": "approve"}}))
    exported = _last(ev, "state")["exported"]
    assert exported["version"] == 2 and exported["zip_url"] == f"/api/threads/{tid}/export"
    z = client.get(exported["zip_url"])
    assert z.status_code == 200 and z.headers["content-type"] == "application/zip"
    assert client.get(exported["brief_url"]).json()["facts"]["offer"]["value"] == "5 折"


def test_invalid_resume_reports_error_and_keeps_interrupt(client):
    ev = _events(client.post("/api/threads", data={"request": "黑五海报"}))
    tid = _last(ev, "state")["thread_id"]
    ev = _events(client.post(f"/api/threads/{tid}/resume", json={"payload": {"values": {"product_name": None, "offer": None, "date_range": None}}}))
    assert any(e == "error" and "必填事实缺失" in d["message"] for e, d in ev)
    assert client.get(f"/api/threads/{tid}/state").json()["interrupt"]["type"] == "confirm_facts"


def test_unknown_thread_404(client):
    assert client.get("/api/threads/nope/state").status_code == 404
    assert client.post("/api/threads/nope/resume", json={"payload": {}}).status_code == 404
    assert client.get("/api/threads/nope/export").status_code == 404


def test_health(client, monkeypatch):
    assert client.get("/api/health").json() == {"ok": True, "version": None, "busy": 0}
    monkeypatch.setattr(config, "VERSION", "aca72b2")
    assert client.get("/api/health").json() == {"ok": True, "version": "aca72b2", "busy": 0}


def test_pages_fall_back_to_index_but_not_for_api_or_assets(tmp_path):
    """部署时后端出前端页面：/chat/<id> 这类地址给 index.html（不让直接用缓存）；/api 下没有的接口、缺了的资源文件都是 404。"""
    (tmp_path / "index.html").write_text("<p>页面</p>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app-1a2b.js").write_text("js")
    app = FastAPI()
    app.get("/api/health")(lambda: {"ok": True})
    app.mount("/", api_main.Pages(directory=tmp_path, html=True))
    c = TestClient(app)
    for path in ("/", "/chat/new", "/chat/9b259891-c7f8-4c51-9688-ee938444cb8d"):
        r = c.get(path)
        assert (r.status_code, r.text, r.headers["cache-control"]) == (200, "<p>页面</p>", "no-cache"), path
    r = c.get("/assets/app-1a2b.js")
    assert r.text == "js" and "cache-control" not in r.headers
    assert c.get("/api/health").json() == {"ok": True}
    assert c.get("/api/nope").status_code == 404 and c.get("/api").status_code == 404
    assert c.get("/assets/app-old.js").status_code == 404
