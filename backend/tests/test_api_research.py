"""调研场景接口：SSE 事件、动态提问的整形与回传、文件与导出。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.api import main as api_main
from app.api.research_runner import ResearchRunner


@pytest.fixture
def client(tmp_path):
    api_main.set_research_runner(ResearchRunner(fake=True, data_dir=tmp_path / "data", out_dir=tmp_path / "out"))
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


def test_research_flow_over_http(client):
    ev = _events(client.post("/api/research/threads", json={"request": "AI 海报工具在局部修改上的现状（2026）"}))
    kinds = [e for e, _ in ev]
    assert kinds[0] == "progress" and kinds[-3:] == ["node", "interrupt", "state"]
    st = _last(ev, "state")
    tid = st["thread_id"]
    assert st["scenario"] == "research" and st["interrupt"]["type"] == "research.scope"

    values = {f["key"]: f["value"] for f in st["interrupt"]["fields"]}
    ev = _events(client.post(f"/api/research/threads/{tid}/resume", json={"payload": {"values": values}}))
    ask = _last(ev, "interrupt")
    assert ask["type"] == "research.ask" and ask["options"] == ["包含", "不包含"] and ask["why"]
    st = _last(ev, "state")
    assert st["interrupt"]["type"] == "research.ask" and st["progress"] == {"researched": 0, "total": 2}

    ev = _events(client.post(f"/api/research/threads/{tid}/resume", json={"payload": {"answer": "包含"}}))
    outline = _last(ev, "interrupt")
    assert outline["type"] == "research.outline" and [n["id"] for n in outline["nodes"]] == ["c1", "c2"]
    sections = [d for e, d in ev if e == "node" and d["node"] == "research_section"]
    assert [s["section"] for s in sections] == ["局部修改机制", "事实准确性"]
    assert _last(ev, "state")["progress"] == {"researched": 2, "total": 2}

    ev = _events(client.post(f"/api/research/threads/{tid}/resume", json={"payload": {"action": "approve"}}))
    review = _last(ev, "interrupt")
    assert review["type"] == "research.review" and review["version"] == 1 and review["md_url"].endswith("/files/report_v1.md")
    assert any(e == "node" and d["node"] == "write" and d["version"] == 1 for e, d in ev)
    md = client.get(review["md_url"])
    assert md.status_code == 200 and md.text.startswith("# ")

    sid = review["claims"][0]["source_ids"][0]
    ev = _events(client.post(f"/api/research/threads/{tid}/resume", json={"payload": {"action": "exclude_source", "target": sid}}))
    assert _last(ev, "interrupt")["version"] == 2
    st = client.get(f"/api/research/threads/{tid}/state").json()
    assert st["next"] == ["review"] and len(st["versions"]) == 2

    ev = _events(client.post(f"/api/research/threads/{tid}/resume", json={"payload": {"action": "approve"}}))
    exported = _last(ev, "state")["exported"]
    assert exported["version"] == 2
    assert client.get(exported["items_url"]).json()[0]["kind"] == "scope"
    assert client.get(exported["zip_url"]).headers["content-type"] == "application/zip"


def test_invalid_answer_reports_error(client):
    st = _last(_events(client.post("/api/research/threads", json={"request": "x"})), "state")
    tid = st["thread_id"]
    ev = _events(client.post(f"/api/research/threads/{tid}/resume", json={"payload": {"values": {"question": ""}}}))
    assert any(e == "error" and "研究问题" in d["message"] for e, d in ev)
    assert _last(ev, "state")["interrupt"]["type"] == "research.scope"


def test_unknown_research_thread_404(client):
    assert client.get("/api/research/threads/nope/state").status_code == 404
    assert client.post("/api/research/threads", json={"request": "  "}).status_code == 400
