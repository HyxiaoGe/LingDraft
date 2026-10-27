"""M2：接口层把 interrupt / resume 搬到 SSE 上，状态查询与导出可用。"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

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
    assert [e for e, _ in ev][:2] == ["node", "interrupt"]
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
