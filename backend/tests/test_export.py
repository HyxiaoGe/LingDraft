"""右栏“下载成 PDF / Word”（app.api.export，2026-11-12 Sean 定，见 docs/沙箱方案.md）：模型写的文档原样转一份，不经过模型、不扣额度。
转的内容和页面上下载的 Markdown 一样；用临时的工作区，不碰对话自己的；转过的再点直接给；一个人一天有上限；游客不能用。
对话里的 PPT 也从这里要 PDF（一张一页，右栏拿它看每一页的样子）。
只用假模型和假沙箱（见 FakeSandbox 的 convert-doc）。"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app import aio
from app.api import export
from app.api import main as api_main
from app.api.export import DOCX, PDF, Exporter, ExportFailed
from app.api.research_runner import ResearchRunner
from app.graph.research import history
from app.identity import ADMIN, USER as USER_TIER, Identity
from app.tools.gateway import SANDBOX_MAX_UPLOAD_BYTES, GatewayAuthError, GatewayLimitError, SandboxError
from tests.auth_helpers import USER, bearer, guest_token, user_token
from tests.research_helpers import SIMPLE, after_tool, events, gated_runner, last, new, run_async, say, state, wait_until
from tests.test_files import make_pptx

ME = Identity("u1", USER_TIER)


# ---- 转换本身：假的网关，看放了什么、跑了什么、留没留 ----


class Gateway:
    """只有导出用到的四个调用。run 是下一次跑命令回什么（字典），或者抛什么。"""

    def __init__(self) -> None:
        self.files: dict[str, dict[str, bytes]] = {}
        self.runs: list[tuple[str, str, int]] = []
        self.deleted: list[str] = []
        self.run: dict | Exception = {"exit_code": 0, "killed": None}
        self.gate: asyncio.Event | None = None

    async def sandbox_put(self, workspace: str, path: str, data: bytes) -> None:
        self.files.setdefault(workspace, {})[path] = data

    async def sandbox_run(self, workspace: str, command: str, timeout_seconds: int) -> dict:
        self.runs.append((workspace, command, timeout_seconds))
        if self.gate is not None:
            await self.gate.wait()
        if isinstance(self.run, Exception):
            raise self.run
        if self.run["exit_code"] == 0 and not self.run["killed"]:
            _, source, output = command.split()[:3]
            self.files[workspace][output] = b"converted:" + self.files[workspace][source]
        return self.run

    async def sandbox_get(self, workspace: str, path: str) -> bytes:
        return self.files[workspace][path]

    async def sandbox_delete(self, workspace: str) -> bool:
        self.deleted.append(workspace)
        return self.files.pop(workspace, None) is not None


def test_converts_in_a_temporary_workspace_and_keeps_the_result():
    async def go():
        gw = Gateway()
        ex = Exporter(gw)
        data = await ex.convert("file-0123456789abcdef", PDF, "# 报告\n", "季度 报告's", ME)
        assert data == "converted:# 报告\n".encode()
        ws = "export-0123456789abcdef-pdf"
        # 标题是用户的内容：整个当一个参数，引号转义好
        assert gw.runs == [(ws, """convert-doc uploads/document.md outputs/document.pdf --title='季度 报告'"'"'s'""", 120)]
        assert gw.deleted == [ws] and gw.files == {}
        # 再要同一版同一种格式：直接给，不再跑；换一种格式另转
        assert await ex.convert("file-0123456789abcdef", PDF, "# 报告\n", "季度 报告's", ME) == data and len(gw.runs) == 1
        await ex.convert("file-0123456789abcdef", DOCX, "# 报告\n", "季度 报告's", ME)
        assert gw.runs[1][0] == "export-0123456789abcdef-docx" and gw.runs[1][1].startswith("convert-doc uploads/document.md outputs/document.docx ")

    run_async(go())


def test_slides_are_laid_out_from_the_original_file(monkeypatch):
    async def go():
        gw = Gateway()
        ex = Exporter(gw)
        deck = b"PK\x03\x04 slides"
        assert await ex.slides("file-0123456789abcdef", deck, ME) == b"converted:" + deck
        ws = "export-0123456789abcdef-pdf"
        # 原件原样放进去，没有标题这个参数
        assert gw.runs == [(ws, "convert-doc uploads/slides.pptx outputs/slides.pdf", 120)]
        assert gw.deleted == [ws] and gw.files == {}
        assert await ex.slides("file-0123456789abcdef", deck, ME) == b"converted:" + deck and len(gw.runs) == 1
        # 放不进沙箱的：不去试，不算这个人今天的次数
        monkeypatch.setattr(export, "DAILY", 2)
        with pytest.raises(ExportFailed) as e:
            await ex.slides("file-big", b"x" * (SANDBOX_MAX_UPLOAD_BYTES + 1), ME)
        assert (e.value.status, e.value.code) == (413, export.TOO_BIG) and len(gw.runs) == 1
        await ex.slides("file-b", deck, ME)  # 今天的第 2 次：还能转
        with pytest.raises(ExportFailed) as e:
            await ex.slides("file-c", deck, ME)
        assert e.value.code == export.DAILY_LIMIT

    run_async(go())


def test_two_clicks_at_once_convert_once():
    async def go():
        gw = Gateway()
        gw.gate = asyncio.Event()
        ex = Exporter(gw)
        both = [asyncio.create_task(ex.convert("file-aa", PDF, "正文", "标题", ME)) for _ in range(2)]
        await asyncio.sleep(0.05)
        gw.gate.set()
        a, b = await asyncio.gather(*both)
        assert a == b and len(gw.runs) == 1

    run_async(go())


@pytest.mark.parametrize(("result", "status", "code"), [
    ({"exit_code": 1, "killed": None}, 502, export.FAILED),
    ({"exit_code": 137, "killed": "timeout"}, 502, export.TOO_BIG),
    ({"exit_code": 137, "killed": "memory"}, 502, export.TOO_BIG),
    (SandboxError("x", kind="sandbox_busy", status=503), 503, export.BUSY_NOW),
    (SandboxError("x", kind="workspace_busy", status=409), 503, export.BUSY_NOW),
    (SandboxError("x", kind="workspace_over_quota", status=413), 502, export.TOO_BIG),
    (SandboxError("x", kind="unavailable"), 503, export.UNAVAILABLE),
    (GatewayLimitError("x"), 429, export.DAILY_LIMIT),
    (GatewayAuthError("x"), 503, export.UNAVAILABLE),
])
def test_failures_are_named_cleaned_up_and_not_kept(result, status, code):
    async def go():
        gw = Gateway()
        ex = Exporter(gw)
        gw.run = result
        with pytest.raises(ExportFailed) as e:
            await ex.convert("file-aa", PDF, "正文", "标题", ME)
        assert (e.value.status, e.value.code) == (status, code)
        assert gw.deleted == ["export-aa-pdf"] and gw.files == {}
        # 没转成的不留：好了以后再点是重新转
        gw.run = {"exit_code": 0, "killed": None}
        assert await ex.convert("file-aa", PDF, "正文", "标题", ME) == "converted:正文".encode() and len(gw.runs) == 2

    run_async(go())


def test_daily_cap_counts_real_conversions_per_person(monkeypatch):
    monkeypatch.setattr(export, "DAILY", 2)

    async def go():
        now = [datetime(2026, 11, 12, 15, 0, tzinfo=UTC)]  # 北京时间 23:00
        gw = Gateway()
        ex = Exporter(gw, clock=lambda: now[0])
        await ex.convert("file-a", PDF, "一", "一", ME)
        await ex.convert("file-a", PDF, "一", "一", ME)  # 直接给的不算
        await ex.convert("file-b", PDF, "二", "二", ME)
        with pytest.raises(ExportFailed) as e:
            await ex.convert("file-c", PDF, "三", "三", ME)
        assert (e.value.status, e.value.code) == (429, export.DAILY_LIMIT) and len(gw.runs) == 2
        assert await ex.convert("file-b", PDF, "二", "二", ME)  # 到了上限，转过的照样给
        # 各人数各人的；管理员不限
        await ex.convert("file-c", PDF, "三", "三", Identity("u2", USER_TIER))
        boss = Identity("root", ADMIN)
        for i in range(3):
            await ex.convert(f"file-d{i}", PDF, "四", "四", boss)
        # 北京时间过了 0 点：重新数
        now[0] += timedelta(hours=2)
        await ex.convert("file-e", PDF, "五", "五", ME)
        assert list(ex._used) == ["u1"]

    run_async(go())


def test_kept_results_are_bounded_by_size(monkeypatch):
    monkeypatch.setattr(export, "CACHE_BYTES", 40)

    async def go():
        gw = Gateway()
        ex = Exporter(gw)
        boss = Identity("root", ADMIN)
        for name in ("a", "b", "c"):
            await ex.convert(f"file-{name}", PDF, "x" * 6, "t", boss)  # 每份 16 字节：放得下两份
        assert list(ex._kept) == [("file-b", PDF), ("file-c", PDF)]
        await ex.convert("file-b", PDF, "x" * 6, "t", boss)  # 用过的往后排
        await ex.convert("file-d", PDF, "x" * 6, "t", boss)
        assert list(ex._kept) == [("file-b", PDF), ("file-d", PDF)]
        await ex.convert("file-big", PDF, "x" * 60, "t", boss)  # 一份就超了的不留
        assert ("file-big", PDF) not in ex._kept and len(ex._kept) == 2

    run_async(go())


# ---- 经接口 ----


@pytest.fixture
def rr(tmp_path):
    return ResearchRunner(fake=True)


@pytest.fixture
def client(rr):
    api_main.set_research_runner(rr)
    return TestClient(api_main.app, headers=USER)


def _written(v: dict) -> dict:
    return next(i["file"] for i in v["turns"][-1]["items"] if i["type"] == "step" and i["kind"] == "write")


def _export(client, tid: str, fid: str, fmt: str | None = PDF):
    return client.post(f"/api/research/threads/{tid}/files/{fid}/export", params={"format": fmt} if fmt else None)


def _refused(resp) -> tuple[int, str]:
    return resp.status_code, resp.json()["detail"]["code"]


def test_export_gives_the_document_as_it_is_downloaded(rr, client):
    sb = rr.planner.sandbox
    tid = new(client, SIMPLE)["thread_id"]  # 搜、读过：文档里引用得到来源
    f = _written(last(events(say(client, tid, "写文档《调研报告》")), "state"))
    rows = len(aio.run(rr.files.store.of_workspace(tid)))
    r = _export(client, tid, f["id"])
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"] == "attachment; filename*=utf-8''" + quote("调研报告.pdf")
    assert r.headers["cache-control"] == "private, no-store"
    body = r.content.decode()
    # 交出去的样子：来源 id 换成了编号，文末附来源（和 sources.export_markdown 一样）
    assert body.startswith("%PDF-1.7\n# 调研报告\n\n## 一、背景\n\n第一段内容 [1]。") and "\n\n## 参考来源\n\n1. [" in body
    ws = f"export-{f['id'].removeprefix('file-')}-pdf"
    assert sb.runs == [(ws, "convert-doc uploads/document.md outputs/document.pdf --title='调研报告'", 120)]
    # 用完就删；对话自己的工作区没碰；转出来的不进文件表
    assert sb.deleted == [ws] and sb.workspaces == {} and len(aio.run(rr.files.store.of_workspace(tid))) == rows
    # 再点直接给；Word 另转
    assert _export(client, tid, f["id"]).content == r.content and len(sb.runs) == 1
    w = _export(client, tid, f["id"], DOCX)
    assert w.status_code == 200 and w.content.startswith(b"PK") and w.headers["content-type"] == export.MIME[DOCX]
    assert w.headers["content-disposition"].endswith(quote("调研报告.docx")) and len(sb.runs) == 2
    # 改过以后：旧的一版、新的一版各转各的
    f2 = _written(last(events(say(client, tid, "改文档把第一段内容改成开头")), "state"))
    assert "\n开头 [1]。" in _export(client, tid, f2["id"]).content.decode() and "第一段内容" in _export(client, tid, f["id"]).content.decode()
    # 重新回答把那一版删了：没有了
    events(client.post(f"/api/research/threads/{tid}/regenerate", json={}))
    assert _refused(_export(client, tid, f2["id"])) == (404, export.NOT_FOUND)


def test_export_only_for_written_documents_and_their_owner(rr, client):
    sb = rr.planner.sandbox
    v = new(client, "写文档《调研报告》", work=True)
    tid, doc = v["thread_id"], _written(v)
    assert _export(client, tid, doc["id"], "pptx").status_code == 422 and _export(client, tid, doc["id"], None).status_code == 422
    # 别人的对话：和别的接口一样 404
    assert _export(TestClient(api_main.app, headers=bearer(user_token("u2"))), tid, doc["id"]).status_code == 404
    # 网页、代码、跑命令产出的 .md、用户发的文件、别的对话的文档、没有的编号：都不转
    page = _written(last(events(say(client, tid, "写网页《首页》")), "state"))
    code = _written(last(events(say(client, tid, "写代码文件")), "state"))
    ran = last(events(say(client, tid, "跑命令：write outputs/笔记.md 内容")), "state")
    out = next(i for i in ran["turns"][-1]["items"] if i["type"] == "step" and i["kind"] == "command")["run"]["files"][0]
    up = client.post("/api/files", files={"file": ("说明.md", "# 说明\n".encode(), "text/markdown")}).json()
    aio.run(rr.files.settle())
    events(say(client, tid, "文件里写了什么", files=[up["id"]]))
    elsewhere = _written(new(client, "写文档《另一份》"))
    runs = len(sb.runs)
    for fid in (page["id"], code["id"], out["id"], up["id"], elsewhere["id"], "file-0000000000000000"):
        assert _refused(_export(client, tid, fid)) == (404, export.NOT_FOUND), fid
    # 游客：自己对话里的文档也不转
    guest = TestClient(api_main.app, headers=bearer(guest_token()))
    g = new(guest, "写文档《调研报告》")
    assert _refused(_export(guest, g["thread_id"], _written(g)["id"])) == (403, export.LOGIN_REQUIRED)
    assert len(sb.runs) == runs


def test_slides_of_the_conversation_come_back_as_pdf(rr, client, monkeypatch):
    """对话里的 PPT（用户发的、跑命令产出的）要 PDF：原件排成一张一页。别的格式没有；游客、别人的对话不给。"""
    sb = rr.planner.sandbox
    deck = make_pptx([("季度目标", "营收增长 30%", ""), ("路线图", "Q1 上线", "")])
    up = client.post("/api/files", files={"file": ("季度 汇报.pptx", deck, "application/octet-stream")}).json()
    aio.run(rr.files.settle())
    tid = new(client, "文件里写了什么", files=[up["id"]])["thread_id"]
    r = _export(client, tid, up["id"])
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.content == b"%PDF-1.7\n" + deck
    assert r.headers["content-disposition"] == "attachment; filename*=utf-8''" + quote("季度 汇报.pdf")
    ws = f"export-{up['id'].removeprefix('file-')}-pdf"
    assert sb.runs == [(ws, "convert-doc uploads/slides.pptx outputs/slides.pdf", 120)] and sb.deleted == [ws]
    # 再要直接给；PPT 转不成 Word
    assert _export(client, tid, up["id"]).content == r.content and len(sb.runs) == 1
    assert _refused(_export(client, tid, up["id"], DOCX)) == (404, export.NOT_FOUND)
    # 跑命令产出的也一样
    made = aio.run(rr.files.add_output(owner="u1", workspace_id=tid, turn_id="t1", path="outputs/结果.pptx", name="结果.pptx", data=deck))
    out = _export(client, tid, made.id)
    assert out.status_code == 200 and out.headers["content-disposition"].endswith(quote("结果.pdf")) and len(sb.runs) == 2
    # 别人的对话 404
    assert _export(TestClient(api_main.app, headers=bearer(user_token("u2"))), tid, made.id).status_code == 404
    # 游客发的 PPT：不排
    guest = TestClient(api_main.app, headers=bearer(guest_token()))
    theirs = guest.post("/api/files", files={"file": ("a.pptx", deck, "application/octet-stream")}).json()
    aio.run(rr.files.settle())
    g = new(guest, "文件里写了什么", files=[theirs["id"]])
    assert _refused(_export(guest, g["thread_id"], theirs["id"])) == (403, export.LOGIN_REQUIRED) and len(sb.runs) == 2
    # 放不进沙箱的（比网关收的上限大）：不去试
    big = aio.run(rr.files.add_output(owner="u1", workspace_id=tid, turn_id="t1", path="outputs/大.pptx", name="大.pptx", data=deck))
    monkeypatch.setattr("app.api.research_runner.SANDBOX_MAX_UPLOAD_BYTES", len(deck) - 1)
    assert _refused(_export(client, tid, big.id)) == (413, export.TOO_BIG) and len(sb.runs) == 2


def test_export_says_why_it_could_not(rr, client, monkeypatch):
    sb = rr.planner.sandbox
    v = new(client, "写文档《调研报告》")
    tid, fid = v["thread_id"], _written(v)["id"]
    for fail, want in (((503, "sandbox_busy"), (503, export.BUSY_NOW)), ((429, "daily_limit_exceeded"), (429, export.DAILY_LIMIT)),
                       ((401, "unauthorized"), (503, export.UNAVAILABLE)), ((500, "internal"), (503, export.UNAVAILABLE))):
        sb.fail = fail
        resp = _export(client, tid, fid)
        assert _refused(resp) == want and resp.json()["detail"]["message"] == api_main.EXPORT_MESSAGES[want[1]]
    sb.fail = None
    assert _export(client, tid, fid).status_code == 200
    # 一个人一天的上限：转过的照样给，新的不转了
    monkeypatch.setattr(export, "DAILY", 1)
    rr.exporter._used.clear()
    assert _export(client, tid, fid, DOCX).status_code == 200
    other = _written(last(events(say(client, tid, "重写文档")), "state"))
    assert _refused(_export(client, tid, other["id"])) == (429, export.DAILY_LIMIT)
    assert _export(client, tid, fid).status_code == 200 and _export(client, tid, fid, DOCX).status_code == 200


def test_export_waits_for_the_turn_that_wrote_the_document():
    """写这一版的那一轮还没答完：文件已经在右栏能看，但检查点里还没有这一轮（一轮结束才写），来源对不上，先不转。"""
    rr, gate = gated_runner(None, after_tool(history.CREATE_TOOL))  # 停在写完文件、写回复之前
    api_main.set_research_runner(rr)
    client = TestClient(api_main.app, headers=USER)
    tid = new(client, "直接答：先说一句")["thread_id"]
    done: dict = {}
    t = threading.Thread(target=lambda: done.setdefault("resp", say(client, tid, "写文档《调研报告》")))
    t.start()
    try:
        assert wait_until(lambda: aio.run(rr.files.store.of_workspace(tid)) and state(client, tid)["status"] == "running")
        (row,) = aio.run(rr.files.store.of_workspace(tid))
        assert _refused(_export(client, tid, row.id)) == (409, export.NOT_READY) and rr.planner.sandbox.runs == []
    finally:
        gate.set()
        t.join(30)
    events(done["resp"])
    assert _export(client, tid, row.id).status_code == 200


def test_export_when_the_sandbox_is_switched_off(monkeypatch):
    monkeypatch.setattr("app.config.SANDBOX_ENABLED", False)
    rr = ResearchRunner(fake=True)
    api_main.set_research_runner(rr)
    client = TestClient(api_main.app, headers=USER)
    v = new(client, "写文档《调研报告》")
    assert rr.exporter is None and _refused(_export(client, v["thread_id"], _written(v)["id"])) == (503, export.UNAVAILABLE)
