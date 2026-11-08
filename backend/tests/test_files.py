"""用户发的文件（2026-11-08 Sean 定，见 docs/图片和文件理解方案.md 的“二、文件”）：先传上来、后台解析，发消息时绑到对话；
短的整篇给模型、长的给目录和开头，模型用 read_file 按段读、在文件里找；跟着对话删，没发出去的过一天清掉。"""

from __future__ import annotations

import io
import threading
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage, ToolMessage

from app import aio, config
from app.api import main as api_main
from app.api.research_runner import ResearchRunner
from app.files import parse, service as file_service
from app.files.parse import Part, Unreadable
from app.files.service import FileService, NotSendable, UploadRejected, isolated_parse
from app.files.store import FAILED, PARSING, READY, FileRow, MemoryFileStore
from app.graph.research import compaction, history, prompts
from app.graph.research.state import AttachedFile
from app.tools import files as file_tools
from tests.auth_helpers import USER, bearer, guest_token, user_token
from tests.research_helpers import events, last, run_async

# ---- 造文件 ----


def make_pdf(pages: list[str]) -> bytes:
    """最小的 PDF：每页几行 ASCII 字（Helvetica），空字符串是没有字的页（像扫描版）。"""
    objs: dict[int, bytes] = {1: b"<< /Type /Catalog /Pages 2 0 R >>", 3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"}
    kids = []
    for i, text in enumerate(pages):
        pid, cid = 4 + 2 * i, 5 + 2 * i
        kids.append(f"{pid} 0 R")
        stream = ("BT /F1 12 Tf 72 720 Td 14 TL " + " ".join(f"({line}) Tj T*" for line in text.split("\n")) + " ET") if text else ""
        objs[pid] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> "
                     f"/Contents {cid} 0 R >>").encode()
        objs[cid] = f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream".encode()
    objs[2] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>".encode()
    out, offsets = b"%PDF-1.4\n", {}
    for k in sorted(objs):
        offsets[k] = len(out)
        out += f"{k} 0 obj\n".encode() + objs[k] + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offsets[k]:010d} 00000 n \n".encode() for k in sorted(objs))
    return out + f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()


def make_docx(blocks: list) -> bytes:
    """blocks：("h1", 文字) / ("p", 文字) / ("table", [[...], ...])。"""
    from docx import Document

    d = Document()
    for kind, value in blocks:
        if kind == "table":
            t = d.add_table(rows=len(value), cols=len(value[0]))
            for r, row in enumerate(value):
                for c, v in enumerate(row):
                    t.cell(r, c).text = v
        elif kind.startswith("h"):
            d.add_heading(value, int(kind[1:]))
        else:
            d.add_paragraph(value)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def make_xlsx(sheets: dict[str, list[list]]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for r in rows:
            ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def make_pptx(slides: list[tuple[str, str, str]]) -> bytes:
    """slides：(标题, 正文, 备注)。"""
    from pptx import Presentation

    p = Presentation()
    for title, body, notes in slides:
        s = p.slides.add_slide(p.slide_layouts[1])
        s.shapes.title.text = title
        s.placeholders[1].text = body
        if notes:
            s.notes_slide.notes_text_frame.text = notes
    buf = io.BytesIO()
    p.save(buf)
    return buf.getvalue()


def parsed(data: bytes, name: str) -> parse.Parsed:
    return parse.parse_bytes(data, name)


# ---- 认类型、解析 ----


def test_type_is_sniffed_from_the_content_not_the_extension():
    assert parse.sniff(make_docx([("p", "x")]), "看起来是.txt") == parse.DOCX
    assert parse.sniff(make_xlsx({"a": [["x"]]}), "a.docx") == parse.XLSX
    assert parse.sniff(make_pptx([("t", "b", "")]), "a.pdf") == parse.PPTX
    assert parse.sniff(make_pdf(["hi"]), "a.bin") == parse.PDF
    assert parse.sniff("名字,城市\n".encode("gbk"), "a.csv") == parse.CSV
    assert parse.sniff(b"# t", "README.md") == parse.MARKDOWN
    assert parse.sniff(b"print(1)", "a.py") == parse.CODE and parse.sniff(b"FROM x", "Dockerfile") == parse.CODE
    assert parse.sniff(b"hello", "notes") == parse.TEXT


@pytest.mark.parametrize("data, name, code", [
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 64, "a.doc", parse.LEGACY),
    (b"\x89PNG\r\n\x1a\n" + b"\0" * 64, "a.png", parse.IMAGE),
    (b"\x01\x02\x00\x03" * 64, "a.bin", parse.UNSUPPORTED),
    (b"PK\x03\x04broken", "a.docx", parse.BROKEN),
])
def test_unsupported_files_are_rejected_with_a_plain_reason(data, name, code):
    with pytest.raises(Unreadable) as e:
        parse.sniff(data, name)
    assert e.value.code == code and e.value.message and "Error" not in e.value.message


def test_a_plain_zip_and_a_zip_bomb_are_rejected(monkeypatch):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("a.txt", "x" * 100_000)
    with pytest.raises(Unreadable) as e:
        parse.sniff(buf.getvalue(), "a.zip")
    assert e.value.code == parse.UNSUPPORTED
    monkeypatch.setattr(parse, "MAX_UNZIPPED", 50_000)
    with pytest.raises(Unreadable) as e:
        parse.sniff(buf.getvalue(), "a.zip")
    assert e.value.code == parse.TOO_LARGE


def test_pdf_one_part_per_page_and_scanned_pdfs_are_unreadable():
    p = parsed(make_pdf(["Page one: parties and the start date", "", "Page three: payment terms and deposit"]), "a.pdf")
    assert [x.title for x in p.parts] == ["第 1 页", "第 2 页", "第 3 页"] and p.meta["pages"] == 3
    assert p.parts[0].text == "Page one: parties and the start date" and p.parts[1].text == "" and "payment terms" in p.parts[2].text
    with pytest.raises(Unreadable) as e:
        parsed(make_pdf(["", "", "x"]), "scan.pdf")
    assert e.value.code == parse.SCANNED
    deck = parsed(make_pdf(["Q1 Plan", "Team", "Goals"]), "deck.pdf")  # 每页只有个标题的（配图为主）照样能读
    assert [x.text for x in deck.parts] == ["Q1 Plan", "Team", "Goals"]
    with pytest.raises(Unreadable) as e:
        parsed(b"%PDF-1.4\nnot really", "bad.pdf")
    assert e.value.code == parse.BROKEN


def test_docx_headings_tables_and_splitting(monkeypatch):
    monkeypatch.setattr(parse, "PART_CHARS", 200)
    data = make_docx([("h1", "一、总则"), ("p", "甲" * 120), ("table", [["项目", "金额"], ["服务费", "12800"]]),
                      ("h1", "二、付款"), ("p", "乙" * 150), ("h2", "细则"), ("p", "丙" * 30)])
    p = parsed(data, "合同.docx")
    assert p.meta["outline"] == ["一、总则", "二、付款", "  细则"]
    assert p.parts[0].title == "一、总则" and "| 项目 | 金额 |\n|---|---|\n| 服务费 | 12800 |" in p.parts[0].text
    assert p.parts[1].title == "二、付款" and p.parts[1].text.startswith("# 二、付款")
    assert "".join(x.text for x in p.parts).count("丙") == 30


def test_xlsx_sheets_repeat_the_header_and_stop_at_max_rows(monkeypatch):
    monkeypatch.setattr(parse, "PART_CHARS", 300)
    monkeypatch.setattr(parse, "MAX_ROWS", 50)
    rows = [["日期", "门店", "金额"], *([datetime(2026, 1, 1 + i % 28), f"店{i}", 100 + i * 0.5] for i in range(80))]
    p = parsed(make_xlsx({"销售": rows, "说明": [["字段", "含义"], ["金额", "含税"]]}), "a.xlsx")
    assert p.meta["sheets"] == [{"name": "销售", "rows": 50, "cut": True}, {"name": "说明", "rows": 1, "cut": False}]
    sales = [x for x in p.parts if x.title.startswith("销售")]
    assert len(sales) > 1 and all(x.text.startswith("| 日期 | 门店 | 金额 |") for x in sales)
    assert sales[0].title.startswith("销售（第 1–") and "| 2026-01-01 | 店0 | 100 |" in sales[0].text
    assert sales[-1].title.endswith("–50 行）") and p.parts[-1].title == "说明（第 1–1 行）"


def test_pptx_one_part_per_slide_with_notes():
    p = parsed(make_pptx([("季度目标", "营收增长 30%", "强调第二点"), ("路线图", "Q1 上线", "")]), "a.pptx")
    assert [x.title for x in p.parts] == ["第 1 张：季度目标", "第 2 张：路线图"] and p.meta["outline"] == ["1. 季度目标", "2. 路线图"]
    assert "营收增长 30%" in p.parts[0].text and "备注：强调第二点" in p.parts[0].text


def test_gbk_csv_and_long_text_by_lines(monkeypatch):
    p = parsed("姓名,城市\n张三,杭州\n李四,上海\n".encode("gbk"), "a.csv")
    assert p.parts[0].text == "| 姓名 | 城市 |\n|---|---|\n| 张三 | 杭州 |\n| 李四 | 上海 |" and p.meta["rows"] == 2
    monkeypatch.setattr(parse, "PART_CHARS", 100)
    p = parsed("\n".join(f"line {i}" for i in range(1, 61)).encode(), "a.log")
    assert p.parts[0].title.startswith("第 1–") and p.parts[-1].title.endswith("–60 行") and p.meta["lines"] == 60


def test_empty_files_and_the_character_cap(monkeypatch):
    with pytest.raises(Unreadable) as e:
        parsed(b"  \n\n ", "a.txt")
    assert e.value.code == parse.EMPTY
    monkeypatch.setattr(parse, "MAX_CHARS", 500)
    p = parsed((("x" * 99 + "\n") * 20).encode(), "a.txt")
    assert p.chars <= 500 and p.meta["truncated"]


# ---- 文件服务 ----


def _service(tmp_path: Path, store=None, **kw) -> FileService:
    return FileService(store or MemoryFileStore(), tmp_path / "files", isolated=False, **kw)


async def _ready(svc: FileService, owner: str, name: str, data: bytes) -> FileRow:
    row = await svc.upload(owner, "user", name, data)
    await svc.settle()
    got = await svc.get(row.id, owner)
    assert got is not None
    return got


def test_upload_parses_in_the_background_and_rejects_early(tmp_path, monkeypatch):
    async def go():
        svc = _service(tmp_path)
        await svc.start()
        row = await svc.upload("u1", "user", "../../周报.docx", make_docx([("p", "本周完成了上传。")]))
        assert row.status == PARSING and row.name == "周报.docx" and row.type == parse.DOCX
        assert (svc.root / row.storage).read_bytes()[:2] == b"PK"
        await svc.settle()
        ready = await svc.get(row.id, "u1")
        assert ready.status == READY and ready.meta["chars"] > 0 and await svc.get(row.id, "u2") is None
        with pytest.raises(UploadRejected) as e:
            await svc.upload("u1", "user", "a.png", b"\x89PNG\r\n\x1a\n" + b"\0" * 64)
        assert (e.value.status, e.value.code) == (415, parse.IMAGE)
        bad = await svc.upload("u1", "user", "scan.pdf", make_pdf(["", ""]))
        await svc.settle()
        failed = await svc.get(bad.id, "u1")
        assert failed.status == FAILED and failed.error == "这个 PDF 是扫描的图片，读不出文字"
        monkeypatch.setattr(config, "FILE_MAX_BYTES", 10)
        with pytest.raises(UploadRejected) as e:
            await svc.upload("u1", "user", "a.txt", b"x" * 11)
        assert (e.value.status, e.value.code) == (413, "too_large")

    run_async(go())


def test_daily_upload_limit_per_tier(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FILE_UPLOADS_DAILY", {"guest": 2, "user": 3})

    async def go():
        svc = _service(tmp_path)
        await svc.start()
        for _ in range(2):
            await svc.upload("g", "guest", "a.txt", b"hi")
        with pytest.raises(UploadRejected) as e:
            await svc.upload("g", "guest", "a.txt", b"hi")
        assert (e.value.status, e.value.code, e.value.extra) == (429, "daily_limit", {"limit": 2, "guest": True, "login_limit": 3})
        for _ in range(5):
            await svc.upload("root", "admin", "a.txt", b"hi")  # 管理员不限

    run_async(go())


def test_bind_checks_every_file_before_binding_any(tmp_path):
    async def go():
        svc = _service(tmp_path)
        await svc.start()
        good = await _ready(svc, "u1", "a.txt", b"hello")
        bad = await _ready(svc, "u1", "b.txt", b"   ")
        other = await _ready(svc, "u2", "c.txt", b"hi")
        for ids, reason in (([good.id, bad.id], "unreadable"), ([good.id, other.id], "expired"), (["file-nope"], "expired"),
                            ([good.id] * 2, "too_many"), ([good.id] * 6, "too_many")):
            with pytest.raises(NotSendable) as e:
                await svc.bind(ids, "u1", "t1")
            assert e.value.code == reason
            assert (await svc.get(good.id, "u1")).workspace_id is None
        (att,) = await svc.bind([good.id], "u1", "t1")
        assert att == AttachedFile(id=good.id, name="a.txt", type="text", size=5, pages=None, chars=5)
        assert (await svc.bind([good.id], "u1", "t1"))[0].id == good.id  # 同一段对话里再发一次可以
        with pytest.raises(NotSendable):
            await svc.bind([good.id], "u1", "t2")  # 发在别的对话里的不行
        assert not await svc.remove(good.id, "u1")  # 发出去的不在这里删

    run_async(go())


def test_parsing_files_cannot_be_sent(tmp_path, monkeypatch):
    gate = threading.Event()
    real = file_service.direct_parse

    def slow(*a):
        gate.wait(10)
        return real(*a)
    monkeypatch.setattr(file_service, "direct_parse", slow)

    async def go():
        svc = _service(tmp_path)
        await svc.start()
        row = await svc.upload("u1", "user", "a.txt", b"hello")
        with pytest.raises(NotSendable) as e:
            await svc.bind([row.id], "u1", "t1")
        assert e.value.code == "parsing"
        gate.set()
        await svc.settle()
        assert len(await svc.bind([row.id], "u1", "t1")) == 1

    run_async(go())


def test_sweep_removes_unsent_files_after_a_day_and_restart_requeues_parsing(tmp_path):
    async def go():
        store = MemoryFileStore()
        svc = _service(tmp_path, store)
        await svc.start()
        kept = await _ready(svc, "u1", "a.txt", b"a")
        await svc.bind([kept.id], "u1", "t1")
        old = await _ready(svc, "u1", "b.txt", b"b")
        assert await svc.sweep() == 0
        assert await svc.sweep(datetime.now(UTC) + timedelta(days=1, minutes=1)) == 1
        assert await store.get(old.id) is None and not (svc.root / old.storage).exists()
        assert await store.get(kept.id) is not None and (svc.root / kept.storage).exists()
        # 服务重启时还在解析的：重新排上
        (svc.root / "zz").mkdir(parents=True)
        (svc.root / "zz/file-x").write_bytes(b"left over")
        await store.add(FileRow(id="file-x", owner="u1", name="c.txt", type="text", size=9, storage="zz/file-x"))
        again = _service(tmp_path, store)
        await again.start()
        await again.settle()
        assert (await store.get("file-x")).status == READY

    run_async(go())


def test_isolated_parse_runs_in_a_child_process_and_times_out(tmp_path):
    path = tmp_path / "a.docx"
    path.write_bytes(make_docx([("h1", "标题"), ("p", "正文")]))
    out = isolated_parse(path, "a.docx", 60)
    assert out[0] == "ok" and out[1].parts[0].title == "标题"
    bad = tmp_path / "scan.pdf"
    bad.write_bytes(make_pdf([""]))
    assert isolated_parse(bad, "scan.pdf", 60)[:2] == ("unreadable", parse.SCANNED)
    assert isolated_parse(path, "a.docx", 0.001) == ("timeout",)


# ---- 给模型的那份 ----


class _Files:
    """假的文件服务：编号 → (那一行, 段落)。"""

    def __init__(self, items: dict[str, tuple[FileRow, list[parse.Part]]]):
        self.items = items

    async def content(self, fid):
        return self.items.get(fid)


def _file(fid: str, chars: int, n_parts: int = 1, name: str | None = None, **meta) -> tuple[AttachedFile, tuple[FileRow, list[parse.Part]]]:
    per = chars // n_parts
    parts = [parse.Part(f"第 {i + 1} 页", f"P{i + 1}" + "字" * (per - len(f"P{i + 1}"))) for i in range(n_parts)]
    row = FileRow(id=fid, owner="u1", name=name or f"{fid}.pdf", type="pdf", size=1, storage="x", status=READY, meta=meta)
    return AttachedFile(id=fid, name=row.name, type="pdf", size=1, pages=n_parts, chars=chars), (row, parts)


def _human(text: str, files: list[AttachedFile], **kw) -> HumanMessage:
    return HumanMessage(content=text, id="h1", additional_kwargs={history.FILES: [f.model_dump() for f in files], **kw})


def test_short_files_go_in_full_and_long_ones_as_outline_and_beginning():
    a, ac = _file("file-a", 3000, 3)
    b, bc = _file("file-b", file_tools.INLINE_FILE_CHARS + 1, 40, outline=["第一章（第 1 页）", "第二章（第 20 页）"])
    (m,) = run_async(file_tools.with_files([_human("总结一下", [a, b])], _Files({"file-a": ac, "file-b": bc})))
    text = m.content
    assert text.endswith("\n\n总结一下") and text.index('id="file-a"') < text.index('id="file-b"')
    full = text[text.index('<file id="file-a"'):text.index("</file>")]
    assert 'parts="3">' in full and "[part 2: 第 2 页]\nP2" in full and "[part 3: 第 3 页]" in full
    brief = text[text.index('<file id="file-b"'):]
    assert 'shown="outline and beginning"' in brief and "Outline:\n第一章（第 1 页）\n第二章（第 20 页）" in brief
    assert "[part 1: 第 1 页]" in brief and "[part 4:" in brief and "[part 5:" not in brief  # 开头 HEAD_CHARS 字，每段 500
    assert "Call read_file" in text


def test_the_inline_budget_is_per_message_in_the_order_sent():
    fs = [_file(f"file-{k}", 18_000) for k in range(4)]
    files = _Files({a.id: c for a, c in fs})
    (m,) = run_async(file_tools.with_files([_human("看看", [a for a, _ in fs])], files))
    shown = [seg.split('"')[0] for seg in m.content.split('<file id="')[1:]]
    assert shown == ["file-0", "file-1", "file-2", "file-3"]
    assert [('shown="outline' in seg) for seg in m.content.split('<file id="')[1:]] == [False, False, True, True]
    assert file_tools.full_ids([a for a, _ in fs], brief=False) == {"file-0", "file-1"}


def test_missing_files_and_no_service_say_the_file_cannot_be_read():
    a, _ = _file("file-a", 100)
    for svc in (None, _Files({})):
        (m,) = run_async(file_tools.with_files([_human("看看", [a])], svc))
        assert "[This file can no longer be read.]" in m.content


def test_compaction_counts_file_text_and_old_turns_get_only_the_beginning():
    a, ac = _file("file-a", 10_000)
    h = _human("看看", [a])
    plain = HumanMessage(content="看看", id="h1")
    assert compaction.message_tokens(h) - compaction.message_tokens(plain) == pytest.approx(10_000 * compaction.FILE_TOKENS_PER_CHAR)
    cleared = compaction._cleared(h)
    assert cleared.additional_kwargs[history.FILES_BRIEF] and compaction._cleared(plain) is plain
    assert compaction.message_tokens(cleared) < compaction.message_tokens(h) / 2
    (m,) = run_async(file_tools.with_files([cleared], _Files({"file-a": ac})))
    assert 'shown="outline and beginning"' in m.content and "shortened to save space" in m.content
    note = compaction._cleared(ToolMessage(content="x" * 5000, tool_call_id="c1", name=history.FILE_TOOL))
    assert note.content == prompts.render("compact.file_cleared")


def test_read_file_parts_cut_and_search():
    parts = [parse.Part(f"第 {i} 页", f"page {i} " + ("付款方式按月结算。" if i in (3, 7) else "其他内容。") * 40) for i in range(1, 11)]
    out = file_tools.read_parts(parts, file_tools._wanted("2-3", 10))
    assert out.startswith("[part 2: 第 2 页]\npage 2") and "[part 3: 第 3 页]" in out
    assert file_tools._wanted("2,7, 9", 10) == [2, 7, 9] and file_tools._wanted("8-20", 10) == [8, 9, 10]
    assert file_tools._wanted("第三页", 10) is None and file_tools._wanted("11", 10) == []
    big = [parse.Part(f"第 {i} 页", "x" * 3000) for i in range(1, 6)]
    out = file_tools.read_parts(big, [1, 2, 3, 4, 5])
    assert "[part 2:" in out and "[part 3:" not in out and 'parts="3-5"' in out
    found, n = file_tools.search("合同.pdf", parts, "付款方式")
    assert n == 2 and found.startswith('2 parts of 合同.pdf match "付款方式"') and "[part 3: 第 3 页]" in found and "[part 7: 第 7 页]" in found
    assert file_tools.search("合同.pdf", parts, "违约金")[1] == 0 and "No part of 合同.pdf contains" in file_tools.search("合同.pdf", parts, "违约金")[0]


# ---- 经接口：传、发、读、删 ----


def _api() -> tuple[ResearchRunner, TestClient]:
    rr = ResearchRunner(fake=True)
    api_main.set_research_runner(rr)
    return rr, TestClient(api_main.app, headers=USER)


def _upload(rr: ResearchRunner, client: TestClient, name: str, data: bytes, ready: bool = True) -> dict:
    r = client.post("/api/files", files={"file": (name, data, "application/octet-stream")})
    assert r.status_code == 200, r.text
    got = r.json()
    if ready:
        aio.run(rr.files.settle())
        got = client.get(f"/api/files/{got['id']}").json()
        assert got["status"] == READY, got
    return got


def _new(client: TestClient, text: str, files: list[str], **body) -> dict:
    return last(events(client.post("/api/research/threads", json={"text": text, "files": files, **body})), "state")


def _reply(turn: dict) -> str:
    return next(i["text"] for i in reversed(turn["items"]) if i["type"] == "text")


def test_send_a_file_and_the_model_sees_it_in_full():
    rr, client = _api()
    f = _upload(rr, client, "周报.docx", make_docx([("h1", "进度"), ("p", "本周完成了上传。")]))
    assert f["file"] == {"id": f["id"], "name": "周报.docx", "type": "docx", "size": f["size"], "pages": None, "chars": f["file"]["chars"]}
    v = _new(client, "文件里有什么", [f["id"]])
    (turn,) = v["turns"]
    assert turn["files"] == [f["file"]] and _reply(turn) == "看到了 1 个文件：周报.docx。"
    msgs = rr.graph.get_state(rr._cfg(v["thread_id"])).values["messages"]
    assert history.text_of(msgs[0]) == "文件里有什么"  # 存着的消息里只有那句话和编号，文字在文件表里
    # 再说一句，之前发的文件照样给模型
    v = last(events(client.post(f"/api/research/threads/{v['thread_id']}/messages", json={"text": "文件里还有什么"})), "state")
    assert _reply(v["turns"][1]) == "看到了 1 个文件：周报.docx。"


def test_long_file_is_read_with_read_file_and_shows_a_step(monkeypatch):
    monkeypatch.setattr(file_tools, "INLINE_FILE_CHARS", 500)
    rr, client = _api()
    pages = [f"Page {i} filler text about nothing in particular" for i in range(1, 30)]
    pages[17] = "Page 18 the deposit is 5000 yuan"
    f = _upload(rr, client, "合同.pdf", make_pdf(pages))
    assert f["file"]["pages"] == 29
    v = _new(client, "文件里有什么", [f["id"]])
    assert _reply(v["turns"][0]) == "看到了 1 个文件：合同.pdf（开头）。"
    v = last(events(client.post(f"/api/research/threads/{v['thread_id']}/messages", json={"text": "文件里找deposit"})), "state")
    turn = v["turns"][1]
    (step,) = [i for i in turn["items"] if i["type"] == "step" and i["kind"] == "file"]
    assert step["ok"] and step["text"] == "合同.pdf" and (step["limits"], step["parts"], step["hits"]) == (["deposit"], "", 1)
    assert "[part 18: 第 18 页]" in _reply(turn)
    v = last(events(client.post(f"/api/research/threads/{v['thread_id']}/messages", json={"text": "文件里第18段"})), "state")
    assert _reply(v["turns"][2]).startswith("读了：[part 18: 第 18 页]\nPage 18 the deposit")
    (step,) = [i for i in v["turns"][2]["items"] if i["type"] == "step" and i["kind"] == "file"]
    assert (step["text"], step["limits"], step["parts"]) == ("合同.pdf", [], "18")


def test_read_file_only_reads_files_of_this_conversation():
    rr, client = _api()
    mine = _upload(rr, client, "a.txt", b"secret plan")
    _new(client, "直接答：好", [mine["id"]])
    other = _new(client, f"文件里第1段 编号{mine['id']}", [])
    (step,) = [i for i in other["turns"][0]["items"] if i["type"] == "step" and i["kind"] == "file"]
    assert not step["ok"] and step["message"] == "没读成" and _reply(other["turns"][0]) == "没读成。"
    assert step["text"] == mine["id"]  # 没读成的没有名字，记编号，页面按这段对话发的文件对名字（这段对话里没有，就只写“读文件”）


def test_sending_rejects_unready_and_foreign_files_without_starting(monkeypatch):
    rr, client = _api()
    bad = _upload(rr, client, "scan.pdf", make_pdf([""]), ready=False)
    aio.run(rr.files.settle())
    assert client.get(f"/api/files/{bad['id']}").json() | {"id": 0} == {
        "id": 0, "name": "scan.pdf", "type": "pdf", "size": bad["size"], "status": "failed", "error": "这个 PDF 是扫描的图片，读不出文字",
        "reason": "scanned"}
    r = client.post("/api/research/threads", json={"text": "看看", "files": [bad["id"]]})
    assert (r.status_code, r.json()["detail"]) == (400, {"code": "files_not_sendable", "reason": "unreadable", "message": "有文件读不了，去掉再发"})
    theirs = TestClient(api_main.app, headers=bearer(user_token("u2")))
    f = _upload(rr, theirs, "a.txt", b"hello")
    r = client.post("/api/research/threads", json={"text": "看看", "files": [f["id"]]})
    assert (r.status_code, r.json()["detail"]["reason"]) == (400, "expired")
    assert client.get(f"/api/files/{f['id']}").status_code == 404 and client.delete(f"/api/files/{f['id']}").status_code == 404
    r = client.post("/api/research/threads", json={"text": "看看", "files": ["f"] * (history.MAX_FILES + 1)})
    assert r.status_code == 422


def test_upload_rejections_are_plain():
    rr, client = _api()
    r = client.post("/api/files", files={"file": ("a.png", b"\x89PNG\r\n\x1a\n" + b"\0" * 64, "image/png")})
    assert (r.status_code, r.json()["detail"]) == (415, {"code": "file_rejected", "reason": "image", "message": "图片请直接作为图片发送"})
    r = client.post("/api/files", files={"file": ("a.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 64, "application/msword")})
    assert (r.status_code, r.json()["detail"]["reason"]) == (415, "legacy_office")
    r = client.post("/api/files", files=[("file", ("a.txt", b"a", "text/plain")), ("file", ("b.txt", b"b", "text/plain"))])
    assert (r.status_code, r.json()["detail"]) == (400, "一次传一个文件")
    assert client.post("/api/files", json={"x": 1}).status_code == 415
    guest = TestClient(api_main.app, headers=bearer(guest_token()))
    assert guest.post("/api/files", files={"file": ("a.txt", b"hello", "text/plain")}).status_code == 200


def test_remove_an_unsent_file():
    rr, client = _api()
    f = _upload(rr, client, "a.txt", b"hello")
    path = rr.files.root / aio.run(rr.files.store.get(f["id"])).storage
    assert path.exists() and client.delete(f"/api/files/{f['id']}").json() == {"ok": True}
    assert not path.exists() and client.get(f"/api/files/{f['id']}").status_code == 404
    # 解析的时候被去掉的：解析完写结果时那一行已经没了，文字不留
    aio.run(rr.files.store.finish(f["id"], READY, {}, "", [Part("第 1 页", "晚到的结果")]))
    assert aio.run(rr.files.store.parts(f["id"])) == [] and aio.run(rr.files.store.get(f["id"])) is None


def test_download_the_original_only_from_its_conversation():
    rr, client = _api()
    data = make_docx([("p", "原件")])
    f = _upload(rr, client, "周报 v2.docx", data)
    v = _new(client, "直接答：收到", [f["id"]])
    r = client.get(f"/api/research/threads/{v['thread_id']}/files/{f['id']}")
    assert r.status_code == 200 and r.content == data
    assert r.headers["content-disposition"].startswith("attachment;") and "%E5%91%A8%E6%8A%A5" in r.headers["content-disposition"]
    other = _new(client, "直接答：别的", [])
    assert client.get(f"/api/research/threads/{other['thread_id']}/files/{f['id']}").status_code == 404
    theirs = TestClient(api_main.app, headers=bearer(user_token("u2")))
    assert theirs.get(f"/api/research/threads/{v['thread_id']}/files/{f['id']}").status_code == 404


def test_edit_and_regenerate_keep_the_files_and_delete_removes_them():
    rr, client = _api()
    f = _upload(rr, client, "a.txt", b"hello world")
    v = _new(client, "直接答：看看", [f["id"]])
    tid = v["thread_id"]
    v = last(events(client.post(f"/api/research/threads/{tid}/edit", json={"text": "文件里有什么"})), "state")
    assert v["turns"][0]["files"][0]["id"] == f["id"] and _reply(v["turns"][0]) == "看到了 1 个文件：a.txt。"
    v = last(events(client.post(f"/api/research/threads/{tid}/regenerate", json={})), "state")
    assert v["turns"][0]["files"][0]["id"] == f["id"]
    row = aio.run(rr.files.store.get(f["id"]))
    assert client.delete(f"/api/research/threads/{tid}").status_code == 200
    assert aio.run(rr.files.store.get(f["id"])) is None and not (rr.files.root / row.storage).exists()


def test_queued_messages_cannot_carry_files():
    rr, client = _api()
    v = _new(client, "直接答：好", [])
    r = client.post(f"/api/research/threads/{v['thread_id']}/queue", json={"text": "再看看", "files": ["file-x"]})
    assert (r.status_code, r.json()["detail"]) == (400, "回答完再发带文件的消息")
