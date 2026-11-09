"""模型写的文件（Artifact，2026-11-08 Sean 定，见 docs/工作区和Artifact方案.md）：create_file 新建、edit_file 出新一版，
每版一行存全文；边写边推给页面；截断的不存；重新回答删那一轮写的版本；之前几轮的整篇内容压缩时换成说明。"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app import aio
from app.api import main as api_main
from app.api.drafts import DraftTracker, PartialArgs
from app.api.research_runner import ResearchRunner, conversation
from app.files import artifacts
from app.files.parse import PART_CHARS
from app.files.service import FileService
from app.files.store import MemoryFileStore
from app.graph.research import compaction, history, prompts
from app.graph.research.agent import ChatContext
from app.tools.files import make_read_file_tool
from app.tools.write_file import make_write_tools
from tests.auth_helpers import USER
from tests.research_helpers import events, last, run_async

# ---- 切段、按片段改、边收边解 ----


def test_parts_join_back_to_the_exact_text():
    rnd = random.Random(7)
    pieces = ["# 标题\n", "\n", "## 一、背景  \n", "正文" * 900 + "\n", "```py\n# 不是标题\n```\n", "  行尾空格   \n", "\n\n\n", "x" * (PART_CHARS + 300)]
    for _ in range(30):
        text = "".join(rnd.choice(pieces) for _ in range(rnd.randint(0, 40)))
        for kind in artifacts.TYPES:
            parts = artifacts.split(text, kind)
            assert "".join(p.text for p in parts) == text and all(len(p.text) <= PART_CHARS for p in parts)
    md = artifacts.split("# 报告\n\n" + "引言" * 600 + "\n\n## 一、背景\n\n" + "背景" * 1500 + "\n\n## 二、结论\n\n```\n# 注释\n```\n", "markdown")
    assert [p.title for p in md] == ["报告", "一、背景", "二、结论"]
    assert [p.title for p in artifacts.split("a\nb\nc", "code")] == ["第 1–3 行"]
    assert artifacts.outline("# A\n## B\n```\n# C\n```\n#### D\n") == ["A", "  B"]


def test_edits_must_match_exactly_once():
    assert artifacts.apply_edits("甲乙丙", [artifacts.Edit("乙", "二"), artifacts.Edit("二丙", "二三")]) == "甲二三"
    for edits, n, count in (([artifacts.Edit("丁", "x")], 1, 0), ([artifacts.Edit("甲", "x"), artifacts.Edit("乙", "y")], 2, 2),
                            ([artifacts.Edit("", "x")], 1, -1)):
        try:
            artifacts.apply_edits("甲乙乙", edits)
            raise AssertionError("应该对不上")
        except artifacts.EditError as e:
            assert (e.n, e.count) == (n, count)


def test_file_names_and_languages():
    assert artifacts.file_name("上海/三日游: 攻略", "markdown") == "上海_三日游_ 攻略.md"
    assert artifacts.file_name("落地页", "html") == "落地页.html"
    assert [artifacts.file_name(n, "code") for n in ("fetch.py", "Makefile", "README")] == ["fetch.py", "Makefile", "README.txt"]
    assert [artifacts.lang_of(n) for n in ("a.py", "b.ts", "Makefile", "c")] == ["python", "typescript", "makefile", ""]


def test_partial_args_decode_any_chunking():
    args = {"title": '上海"三日"游 😀', "type": "markdown", "edits": [{"old_text": "a}", "new_text": "[b"}], "n": 3,
            "content": "# 标题\n\n引号\"、反斜杠\\、制表\t、😀🎉\n" + "正文" * 500}
    for ascii_only in (True, False):
        raw = json.dumps(args, ensure_ascii=ascii_only)
        p, i, rnd = PartialArgs(), 0, random.Random(1)
        while i < len(raw):
            k = rnd.randint(1, 6)
            p.feed(raw[i:i + k])
            i += k
        assert (p.value("title"), p.value("content"), p.value("type")) == (args["title"], args["content"], "markdown")
        assert p.done == {"title", "type", "content"} and not p.broken
    half = PartialArgs()
    half.feed('{"title": "报告", "content": "第一段\\n第二')
    assert half.value("content") == "第一段\n第二" and half.done == {"title"}
    bad = PartialArgs()
    bad.feed("not json")
    assert bad.broken


def test_drafts_send_name_first_fields_once_and_only_new_text():
    t = DraftTracker()
    raw = json.dumps({"title": "T", "type": "markdown", "content": "abcdef"})
    t.add("m1", [{"name": "create_file", "id": "c1", "index": 0, "args": raw[:20]}, {"name": "search_web", "id": "c2", "index": 1, "args": "{}"}])
    assert t.take() == [{"id": "c1", "title": "T", "name": "create_file", "mid": "m1"}]
    t.add("m1", [{"name": None, "id": None, "index": 0, "args": raw[20:-4]}])
    t.add("m1", [{"name": None, "id": None, "index": 0, "args": raw[-4:]}])
    assert t.take() == [{"id": "c1", "type": "markdown", "text": "abcdef"}] and t.take() == []


# ---- 工具本身 ----


def _runtime(messages, files, call_id="call-1", thread="t1") -> ToolRuntime:
    return ToolRuntime(state={"messages": messages}, context=ChatContext(files=files, owner="u1", thread_id=thread), config={},
                       stream_writer=lambda _: None, tool_call_id=call_id, store=None)


def _tools():
    create, edit = make_write_tools()
    return create.coroutine, edit.coroutine


def _service(tmp_path: Path) -> FileService:
    return FileService(MemoryFileStore(), tmp_path, isolated=False)


def test_create_then_edit_reads_and_versions(tmp_path):
    async def go():
        files = _service(tmp_path)
        create, edit = _tools()
        user = HumanMessage("写一份报告", id="turn-1")
        call = AIMessage("", tool_calls=[{"name": "create_file", "args": {}, "id": "call-1"}])
        made = await create(_runtime([user, call], files), title="  报告 ", type="markdown", content="# 报告\n\n甲乙丙\n")
        w = history.written_of(made)
        assert made.status == "success" and (w.version, w.title, w.name, w.type, w.series) == (1, "报告", "报告.md", "markdown", w.id)
        assert 'Saved version 1 of "报告"' in made.content and w.series in made.content
        row = await files.store.get(w.id)
        assert (row.origin, row.turn_id, row.workspace_id, row.owner, row.status) == ("generated", "turn-1", "t1", "u1", "ready")
        assert (tmp_path / row.storage).read_text() == "# 报告\n\n甲乙丙\n"
        msgs = [user, call, made, HumanMessage("改一下", id="turn-2"), AIMessage("", tool_calls=[{"name": "edit_file", "args": {}, "id": "call-2"}])]
        rt = _runtime(msgs, files, "call-2")
        v2 = await edit(rt, file_id=w.series, change_note="  乙 改成 二 ", edits=[_edit("乙", "二")])
        w2 = history.written_of(v2)
        assert (w2.version, w2.series, w2.note, w2.title) == (2, w.series, "乙 改成 二", "报告") and await files.text(w2.id) == "# 报告\n\n甲二丙\n"
        row2 = await files.store.get(w2.id)
        assert (row2.prev_id, row2.series_id, row2.version, row2.turn_id) == (w.id, w.series, 2, "turn-2")
        assert await files.text(w.id) == "# 报告\n\n甲乙丙\n"  # 旧版还在
        msgs.append(v2)
        assert history.written_files(msgs) == {w.series: w2}
        # 模型写的也能读：按编号读最新一版，整篇读、在文件里找都行
        read = make_read_file_tool().coroutine
        whole, art = await read(_runtime(msgs, files, "call-3"), file_id=w.series)
        assert "甲二丙" in whole and (art["name"], art["version"]) == ("报告", 2)
        found, art = await read(_runtime(msgs, files, "call-3"), file_id=w.series, query="甲二")
        assert 'match "甲二"' in found and "报告" in found and art["found"] == 1
        # 对不上、出现两次、没变化、没给改什么、编号不对、用户发的文件
        fails = [await edit(_runtime(msgs, files, "call-2"), file_id=w.series, change_note="x", edits=[_edit("丁", "x")]),
                 await edit(_runtime(msgs, files, "call-2"), file_id=w.series, change_note="x", edits=[_edit("\n", "x")]),
                 await edit(_runtime(msgs, files, "call-2"), file_id=w.series, change_note="x", content="# 报告\n\n甲二丙\n"),
                 await edit(_runtime(msgs, files, "call-2"), file_id=w.series, change_note="x"),
                 await edit(_runtime(msgs, files, "call-2"), file_id="file-nope", change_note="x", content="y")]
        assert [(f.status, f.artifact["failure"]) for f in fails] == [("error", "no_match"), ("error", "no_match"), ("error", "no_change"),
                                                                      ("error", "no_match"), ("error", "not_found")]
        assert "does not appear" in fails[0].content and "appears 3 times" in fails[1].content
        attached = HumanMessage("看看", additional_kwargs={history.FILES: [{"id": "file-up", "name": "a.txt", "type": "text", "size": 1}]})
        ro = await edit(_runtime([attached, *msgs], files, "call-2"), file_id="file-up", change_note="x", content="y")
        assert ro.artifact == {"failure": "read_only"} and "cannot be edited" in ro.content
        # 改标题也算新一版
        renamed = await edit(_runtime(msgs, files, "call-2"), file_id=w.series, change_note="改名", title="新报告", content="# 报告\n\n甲二丙\n")
        assert history.written_of(renamed).title == "新报告" and history.written_of(renamed).name == "新报告.md"
        # 写的不算上传的个数
        assert await files.store.uploads_since("u1", datetime(2000, 1, 1, tzinfo=UTC)) == 0

    run_async(go())


def test_rewriting_in_the_same_turn_keeps_one_version(tmp_path):
    """一轮里一份文件只出一版（2026-11-08 Sean 定）：同一轮里又改了这一轮写的，顶替那一版、版本号不变，那一版删掉；
    下一轮再改才是新一版，那一轮里改几次都还是这一版，改了什么接起来。"""
    async def go():
        files = _service(tmp_path)
        create, edit = _tools()
        user = HumanMessage("写一份报告", id="turn-1")
        c1 = AIMessage("", tool_calls=[{"name": "create_file", "args": {}, "id": "call-1"}])
        made = await create(_runtime([user, c1], files, "call-1"), title="报告", type="markdown", content="# 报告\n\n" + "长" * 50 + "\n")
        w1 = history.written_of(made)
        draft = await files.store.get(w1.id)
        msgs = [user, c1, made, AIMessage("", tool_calls=[{"name": "edit_file", "args": {}, "id": "call-2"}])]
        again = await edit(_runtime(msgs, files, "call-2"), file_id=w1.series, change_note="压短", content="# 报告\n\n短\n")
        w = history.written_of(again)
        assert (w.version, w.series, w.note) == (1, w1.series, "") and "still version 1" in again.content
        assert await files.store.get(w1.id) is None and not (tmp_path / draft.storage).exists()
        row = await files.store.get(w.id)
        assert (row.version, row.prev_id, row.series_id, row.turn_id) == (1, None, w1.series, "turn-1")
        msgs.append(again)
        assert history.written_files(msgs) == {w1.series: w}
        # 下一轮改：第 2 版；这一轮里又改一次，还是第 2 版，上一版还是第 1 版
        msgs += [AIMessage("写好了"), HumanMessage("改一下", id="turn-2"), AIMessage("", tool_calls=[{"name": "edit_file", "args": {}, "id": "call-3"}])]
        v2 = await edit(_runtime(msgs, files, "call-3"), file_id=w1.series, change_note="短改成简", edits=[_edit("短", "简")])
        assert "still" not in v2.content and history.written_of(v2).version == 2
        msgs += [v2, AIMessage("", tool_calls=[{"name": "edit_file", "args": {}, "id": "call-4"}])]
        v2b = await edit(_runtime(msgs, files, "call-4"), file_id=w1.series, change_note="加一句", edits=[_edit("简", "简。再补一句")])
        b = history.written_of(v2b)
        assert (b.version, b.note) == (2, "短改成简；加一句") and await files.store.get(history.written_of(v2).id) is None
        assert (await files.store.get(b.id)).prev_id == w.id and await files.text(b.id) == "# 报告\n\n简。再补一句\n"
        assert await files.text(w.id) == "# 报告\n\n短\n"  # 上一轮的那版还在

    run_async(go())


def _edit(old: str, new: str):
    from app.tools.write_file import EditItem

    return EditItem(old_text=old, new_text=new)


def test_cut_off_output_is_not_saved_and_no_service_says_so(tmp_path):
    async def go():
        files = _service(tmp_path)
        create, _ = _tools()
        user = HumanMessage("写", id="turn-1")
        cut = AIMessage("", tool_calls=[{"name": "search_web", "args": {}, "id": "call-0"}, {"name": "create_file", "args": {}, "id": "call-1"}],
                        response_metadata={"finish_reason": "length"})
        got = await create(_runtime([user, cut], files), title="长文", type="markdown", content="写到一半")
        assert (got.status, got.artifact) == ("error", {"failure": "cut_off"}) and "cut off" in got.content
        assert await files.store.of_workspace("t1") == []
        # 截断只截在最后一个调用：前面的是完整的
        early = AIMessage("", tool_calls=[{"name": "create_file", "args": {}, "id": "call-1"}, {"name": "search_web", "args": {}, "id": "call-2"}],
                          response_metadata={"finish_reason": "length"})
        assert (await create(_runtime([user, early], files), title="短文", type="markdown", content="完整的")).status == "success"
        none = await create(_runtime([user, AIMessage("", tool_calls=[{"name": "create_file", "args": {}, "id": "call-1"}])], None),
                            title="x", type="markdown", content="y")
        assert none.artifact == {"failure": "unavailable"}

    run_async(go())


# ---- 过程里的一步、压缩 ----


def test_steps_for_written_failed_and_broken_writes():
    w = {"id": "file-2", "series": "file-1", "version": 2, "title": "报告", "type": "markdown", "name": "报告.md", "chars": 10, "note": "改短"}
    ok = ToolMessage("Saved", name="edit_file", tool_call_id="c1", artifact={"file": w})
    bad = ToolMessage("Nothing was changed", name="edit_file", tool_call_id="c2", status="error", artifact={"failure": "no_match"})
    calls = [{"name": "edit_file", "args": {"file_id": "file-1", "change_note": "改短"}, "id": "c1"},
             {"name": "edit_file", "args": {"file_id": "file-1", "change_note": "x"}, "id": "c2"}]
    m = AIMessage("", id="a1", tool_calls=calls,
                  invalid_tool_calls=[{"name": "create_file", "args": '{"title": "半', "id": "c3", "error": "bad", "type": "invalid_tool_call"}])
    pending = history.step_of(calls[0], None)
    assert (pending.kind, pending.text, pending.target, pending.edit, pending.pending) == ("write", "", "file-1", True, True)
    items = conversation([HumanMessage("改", id="h1"), m, ok, bad], [])[0]["items"]
    steps = [i for i in items if i["type"] == "step"]
    assert [(s["id"], s["ok"], s["text"], s["target"], s["message"]) for s in steps] == [
        ("c1", True, "报告", "file-1", ""), ("c2", False, "", "file-1", "要改的地方没对上，没改成"), ("c3", False, "", "", "内容没写完整，没写成")]
    assert steps[0]["file"]["version"] == 2 and steps[0]["file"]["note"] == "改短"


def test_old_write_calls_lose_their_content_in_compaction():
    big = "正文" * 5000
    m = AIMessage("", tool_calls=[{"name": "create_file", "args": {"title": "报告", "type": "markdown", "content": big}, "id": "c1"},
                                  {"name": "search_web", "args": {"query": "q"}, "id": "c2"}],
                  invalid_tool_calls=[{"name": "edit_file", "args": '{"content": "' + big, "id": "c3", "error": "x", "type": "invalid_tool_call"}])
    out = compaction._cleared(m)
    note = prompts.render("compact.write_cleared")
    assert out.tool_calls[0]["args"] == {"title": "报告", "type": "markdown", "content": note}
    assert out.tool_calls[1]["args"] == {"query": "q"} and out.invalid_tool_calls[0]["args"] == note
    assert compaction.message_tokens(out) < compaction.message_tokens(m) / 20
    assert big not in compaction.render_turn([HumanMessage("写"), m])


# ---- 经接口：写、改、读、截断、重新回答、删 ----


def _api() -> tuple[ResearchRunner, TestClient]:
    rr = ResearchRunner(fake=True)
    api_main.set_research_runner(rr)
    return rr, TestClient(api_main.app, headers=USER)


def _write_steps(turn: dict) -> list[dict]:
    return [i for i in turn["items"] if i["type"] == "step" and i["kind"] == "write"]


def _say(client, tid, text):
    return events(client.post(f"/api/research/threads/{tid}/messages", json={"text": text}))


def test_write_edit_read_regenerate_and_delete_through_the_api():
    rr, client = _api()
    evs = events(client.post("/api/research/threads", json={"text": "写文档《上海三日游攻略》"}))
    v = last(evs, "state")
    tid = v["thread_id"]
    (step,) = _write_steps(v["turns"][0])
    f1 = step["file"]
    assert step["ok"] and (f1["title"], f1["version"], f1["name"], step["edit"]) == ("上海三日游攻略", 1, "上海三日游攻略.md", False)
    drafts = [d for e, d in evs if e == "draft"]
    assert drafts[0]["id"] == step["id"] and drafts[0]["name"] == "create_file" and drafts[0]["title"] == "上海三日游攻略"
    assert "".join(d.get("text", "") for d in drafts).startswith("# 上海三日游攻略\n\n## 一、背景")
    body = client.get(f"/api/research/threads/{tid}/files/{f1['id']}")
    assert body.status_code == 200 and body.text.startswith("# 上海三日游攻略") and body.headers["content-disposition"].startswith("attachment;")
    assert any(i["type"] == "text" and i["text"] == "写好了，在右边。" for i in v["turns"][0]["items"])
    # 改一处：出第 2 版，同一个编号串着，旧版照样取得到
    v = last(_say(client, tid, "改文档把第一段内容改成第一段新内容"), "state")
    (step,) = _write_steps(v["turns"][1])
    f2 = step["file"]
    assert step["edit"] and (f2["version"], f2["series"], f2["note"]) == (2, f1["series"], "按要求改了一处")
    assert "第一段新内容" in client.get(f"/api/research/threads/{tid}/files/{f2['id']}").text
    assert "第一段新内容" not in client.get(f"/api/research/threads/{tid}/files/{f1['id']}").text
    # 读的是最新一版
    v = last(_say(client, tid, f"文件里第1段 编号{f1['series']}"), "state")
    (read,) = [i for i in v["turns"][2]["items"] if i["type"] == "step" and i["kind"] == "file"]
    assert read["ok"] and read["text"] == "上海三日游攻略" and "第一段新内容" in next(i["text"] for i in v["turns"][2]["items"] if i["type"] == "text")
    # 对不上的：如实写没改成，不出新版
    v = last(_say(client, tid, "改文档把不存在的话改成别的"), "state")
    (step,) = _write_steps(v["turns"][3])
    assert not step["ok"] and step["message"] == "要改的地方没对上，没改成" and step["file"] is None
    # 截断的不存
    v = last(_say(client, tid, "重写文档 截断"), "state")
    (step,) = _write_steps(v["turns"][4])
    assert not step["ok"] and step["message"] == "内容太长，输出被截断了，没写完"
    assert len(aio.run(rr.files.store.of_workspace(tid))) == 2
    # 重新回答：这一轮（整篇重写）写的那版删掉，之前几轮的留着
    v = last(_say(client, tid, "重写文档"), "state")
    f3 = _write_steps(v["turns"][5])[0]["file"]
    assert f3["version"] == 3
    v = last(events(client.post(f"/api/research/threads/{tid}/regenerate", json={})), "state")
    f3b = _write_steps(v["turns"][5])[0]["file"]
    assert f3b["version"] == 3 and f3b["id"] != f3["id"]
    ids = {r.id for r in aio.run(rr.files.store.of_workspace(tid))}
    assert ids == {f1["id"], f2["id"], f3b["id"]}
    assert client.get(f"/api/research/threads/{tid}/files/{f3['id']}").status_code == 404
    # 别的对话取不到，删对话一起删
    other = last(events(client.post("/api/research/threads", json={"text": "直接答：好"})), "state")
    assert client.get(f"/api/research/threads/{other['thread_id']}/files/{f1['id']}").status_code == 404
    assert client.delete(f"/api/research/threads/{tid}").status_code == 200
    assert aio.run(rr.files.store.of_workspace(tid)) == []


def test_web_pages_and_code_files_keep_their_type():
    rr, client = _api()
    v = last(events(client.post("/api/research/threads", json={"text": "写网页《落地页》"})), "state")
    f = _write_steps(v["turns"][0])[0]["file"]
    assert (f["type"], f["name"]) == ("html", "落地页.html")
    v = last(_say(client, v["thread_id"], "写代码文件"), "state")
    f = _write_steps(v["turns"][1])[0]["file"]
    assert (f["type"], f["name"], f["lang"], f["title"]) == ("code", "main.py", "python", "main.py")
