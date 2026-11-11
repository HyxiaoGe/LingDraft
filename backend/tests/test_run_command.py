"""在沙箱里跑命令（run_command，2026-11-11 Sean 定，见 docs/沙箱方案.md）：只在工作任务里给、游客没有；跑之前把用户发的文件放进 uploads/，
跑完把 outputs/ 里的登记进文件表（图进 image-service）；一轮里同一个路径只留最后一版；没跑成的说原因；停止时把命令停掉；
重新回答、删对话时产出跟着删。只用假模型（“跑命令：…”）和假沙箱（见 FakeSandbox）。"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app import aio, config
from app.api import main as api_main
from app.api.research_runner import ResearchRunner
from app.files.service import OTHER, FileService
from app.files.store import MemoryFileStore
from app.graph.research import history
from app.graph.research.agent import ChatContext
from app.graph.research.state import Turn
from app.research_llm import FakeImageService, FakeSandbox
from app.tools import run_command as rc
from app.tools.gateway import GatewayAuthError, GatewayClient, GatewayLimitError, SandboxError
from app.tools.run_command import Sandbox, clip, make_run_command_tool, upload_paths
from tests.auth_helpers import USER, bearer, guest_token
from tests.research_helpers import events, last, new, run_async, state, wait_until

TID = "0b8c6f0e-3a57-4f0e-9d2f-6c1d2a7b9e10"  # 对话编号是 uuid，沙箱的工作区编号就用它


# ---- 网关客户端 ----


def test_gateway_maps_sandbox_errors():
    replies: list[httpx.Response | Exception] = []

    def handle(req: httpx.Request) -> httpx.Response:
        r = replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def err(status: int, code: str) -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": code, "message": code}})

    async def go():
        gw = GatewayClient("http://gw", "k", scene="research", transport=httpx.MockTransport(handle))
        replies[:] = [err(404, "workspace_not_found")]
        assert await gw.sandbox_files("w") == {}
        replies[:] = [httpx.Response(200, json={"files": [{"path": "uploads/a.csv", "size": 3}]})]
        assert await gw.sandbox_files("w") == {"uploads/a.csv": 3}
        for reply, kind, status, retryable in ((err(503, "sandbox_busy"), "sandbox_busy", 503, True),
                                               (err(409, "workspace_busy"), "workspace_busy", 409, False),
                                               (err(413, "workspace_over_quota"), "workspace_over_quota", 413, False),
                                               (httpx.Response(502, text="bad gateway"), "unavailable", 502, False),
                                               (httpx.ConnectError("down"), "unavailable", 0, True),
                                               (httpx.Response(200, text="not json"), "unavailable", 200, False)):
            replies[:] = [reply]
            with pytest.raises(SandboxError) as e:
                await gw.sandbox_run("w", "ls", 60)
            assert (e.value.kind, e.value.status, e.value.retryable) == (kind, status, retryable)
        for reply, exc in ((httpx.Response(401), GatewayAuthError), (httpx.Response(403), GatewayAuthError), (httpx.Response(429), GatewayLimitError)):
            replies[:] = [reply]
            with pytest.raises(exc):
                await gw.sandbox_run("w", "ls", 60)
        # 删文件、删工作区：本来就没有的也算删了；停命令、删工作区没成只记日志
        replies[:] = [err(404, "not_found"), err(404, "workspace_not_found"), err(503, "sandbox_unavailable"), httpx.ConnectError("down")]
        await gw.sandbox_remove("w", "uploads/a.csv")
        assert await gw.sandbox_delete("w") is True and await gw.sandbox_delete("w") is False
        await gw.sandbox_cancel("w")
        assert not replies
        await gw.aclose()

    run_async(go())


# ---- 小件 ----


def test_clip_keeps_head_and_tail():
    assert clip("abc", 10) == "abc"
    out = clip("a" * 60 + "b" * 60, 20)
    assert out.startswith("a" * 10 + "\n[... 100 characters omitted ...]\n") and out.endswith("b" * 10)


def test_upload_paths_rename_duplicates_and_shorten():
    def row(i: int, name: str):
        from app.files.store import FileRow
        return FileRow(id=f"file-{i}", owner="u1", workspace_id=TID, name=name, type="csv", size=1, storage=f"x/{i}")

    paths = upload_paths([row(1, "销售.csv"), row(2, "销售.csv"), row(3, "README"), row(4, "README"), row(5, "销售.csv"), row(6, "长" * 120 + ".xlsx")])
    assert list(paths)[:5] == ["uploads/销售.csv", "uploads/销售 (2).csv", "uploads/README", "uploads/README (2)", "uploads/销售 (3).csv"]
    long = list(paths)[5]
    assert long.endswith(".xlsx") and len(long.removeprefix("uploads/").encode()) <= rc.NAME_BYTES


# ---- 工具本身 ----


class Env:
    """一段工作任务的对话：假沙箱、假 image-service、内存的文件表。"""

    def __init__(self, tmp_path: Path) -> None:
        self.sb, self.images = FakeSandbox(), FakeImageService()
        self.gateway = self.images.gateway_client(self.sb)
        self.files = FileService(MemoryFileStore(), tmp_path, isolated=False)
        self.sandbox = Sandbox(self.gateway)
        self.run = make_run_command_tool().coroutine
        self.user = HumanMessage("分析一下", id="turn-1")
        self.msgs: list = [self.user]
        self.n = 0

    async def attach(self, name: str, data: bytes) -> str:
        """用户发一个文件（传上来、解析好、绑到这段对话），挂在这一轮那句话上。"""
        if self.files._queue is None:
            await self.files.start()
        row = await self.files.upload("u1", "user", name, data)
        await self.files.settle()
        att = await self.files.bind([row.id], "u1", TID)
        have = self.user.additional_kwargs.get(history.FILES, [])
        self.user.additional_kwargs[history.FILES] = [*have, *(a.model_dump() for a in att)]
        return row.id

    def runtime(self, call_id: str, *, work: bool = True, owner: str = "u1", sandbox: bool = True, thread: str = TID) -> ToolRuntime:
        ctx = ChatContext(files=self.files, owner=owner, thread_id=thread, sandbox=self.sandbox if sandbox else None)
        turns = [Turn(id="turn-1", started_at="2026-11-12T00:00:00+00:00", work=work)]
        return ToolRuntime(state={"messages": list(self.msgs), "turns": turns}, context=ctx, config={}, stream_writer=lambda _: None,
                           tool_call_id=call_id, store=None)

    async def call(self, command: str, **kw) -> ToolMessage:
        """模型调一次，结果接进对话。"""
        self.n += 1
        cid = f"call-{self.n}"
        self.msgs.append(AIMessage("", tool_calls=[{"name": history.RUN_TOOL, "args": {"command": command}, "id": cid}]))
        timeout = kw.pop("timeout_seconds", None)
        out = await self.run(self.runtime(cid, **kw), command=command, timeout_seconds=timeout)
        self.msgs.append(out)
        return out


def test_run_syncs_uploads_and_registers_outputs(tmp_path):
    async def go():
        env = Env(tmp_path)
        fid = await env.attach("销售.csv", "月份,销售额\n一月,120\n".encode())
        out = await env.call("ls\ncat uploads/销售.csv\nchart outputs/图表.png\nwrite outputs/汇总.csv 月份,合计\nwrite draft.py print(1)", timeout_seconds=500)
        run = history.run_of(out)
        assert out.status == "success" and run.ok and (run.exit_code, run.killed, run.duration_ms) == (0, "", 1200)
        assert env.sb.runs == [(TID, env.msgs[-2].tool_calls[0]["args"]["command"], rc.MAX_TIMEOUT_S)]  # 超过上限的按上限
        assert run.stdout.splitlines()[:2] == ["uploads/销售.csv", "月份,销售额"]  # 跑之前原件已经在 uploads/ 里
        chart, table = run.files
        assert (chart.path, chart.name, chart.kind, chart.type, chart.mime_type, chart.width > 0) == ("outputs/图表.png", "图表.png", "image", "png",
                                                                                                    "image/png", True)
        assert (table.path, table.name, table.kind, table.type, table.preview) == ("outputs/汇总.csv", "汇总.csv", "file", "csv", True)
        assert env.images.uploaded == [chart.id] and chart.size == len(env.images.made[chart.id][0])
        # 都登记在文件表里：这一轮产出的；图按表认得出是这段对话的，文件能下载、能看
        rows = {r.id: r for r in await env.files.store.of_workspace(TID)}
        assert set(rows) == {fid, chart.id, table.id}
        assert [(rows[i].origin, rows[i].turn_id, rows[i].owner, rows[i].kind, rows[i].name) for i in (chart.id, table.id)] == [
            ("generated", "turn-1", "u1", "image", "图表.png"), ("generated", "turn-1", "u1", "document", "汇总.csv")]
        assert await env.files.image_in(chart.id, TID)
        path, _ = await env.files.original(table.id, TID)
        assert path.read_text() == "月份,合计" and (await env.files.preview(table.id, TID)) is not None
        # 草稿不登记；给模型的话里说了哪些给了用户，不带编号
        assert "outputs/图表.png (image," in out.content and "- outputs/汇总.csv" in out.content and "draft.py" not in out.content
        assert chart.id not in out.content and table.id not in out.content and "Exit code: 0 (1.2 s)" in out.content
        assert history.owned_image_ids(env.msgs) == [chart.id]
        # 再跑一条：原件已经在了不重放；过程记录带着结果
        await env.call("echo 好了")
        assert env.sb.puts == [(TID, "uploads/销售.csv")]
        step = history.step_of(env.msgs[-2].tool_calls[0], env.msgs[-1])
        assert (step.kind, step.text, step.ok, step.run.stdout, step.run.files) == ("command", "echo 好了", True, "好了\n", [])
        pending = history.step_of(env.msgs[-2].tool_calls[0], None)
        assert pending.pending and pending.kind == "command" and pending.run is None

    run_async(go())


def test_same_path_in_one_turn_keeps_only_the_latest(tmp_path):
    async def go():
        env = Env(tmp_path)
        first = history.run_of(await env.call("chart outputs/图表.png\nwrite outputs/汇总.csv a,b\nwrite outputs/说明.md # 一")).files
        again = history.run_of(await env.call("chart outputs/图表.png\nwrite outputs/汇总.csv a,b,c")).files
        ids = {r.id for r in await env.files.store.of_workspace(TID)}
        assert ids == {first[2].id, again[0].id, again[1].id}  # 重写的两个只留后来的，没重写的还在
        assert env.images.deleted == [first[0].id] and first[0].id not in env.images.made
        assert await env.files.original(first[1].id, TID) is None and (await env.files.original(again[1].id, TID))[0].read_text() == "a,b,c"
        # 下一轮再写同一个路径：上一轮的那个留着
        env.msgs += [AIMessage("好了"), HumanMessage("再来", id="turn-2")]
        later = history.run_of(await env.call("chart outputs/图表.png")).files
        assert {again[0].id, later[0].id} <= {r.id for r in await env.files.store.of_workspace(TID)} and len(env.images.deleted) == 1
        assert (await env.files.store.get(later[0].id)).turn_id == "turn-2"

    run_async(go())


def test_outputs_that_are_not_shown(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FILE_MAX_BYTES", 1000)

    async def go():
        env = Env(tmp_path)
        many = "\n".join(f"write outputs/{i:02d}.txt x" for i in range(rc.MAX_OUTPUTS + 2))
        out = await env.call(f"big outputs/-大.bin 2000\nwrite outputs/-空.txt \n{many}\nbig outputs/z.bin 10")
        run = history.run_of(out)
        assert [f.name for f in run.files] == [f"{i:02d}.txt" for i in range(rc.MAX_OUTPUTS)]
        assert "- outputs/-大.bin: larger than 0 MB" in out.content and "- outputs/-空.txt: the file is empty" in out.content
        assert f"- outputs/10.txt: only the first {rc.MAX_OUTPUTS} output files" in out.content and "- outputs/z.bin: only the first" in out.content
        # 认不出类型的只能下载；图超过 image-service 的上限的当普通文件
        env.msgs += [AIMessage("好了"), HumanMessage("再来", id="turn-2")]
        monkeypatch.setattr(config, "FILE_MAX_BYTES", 20 * 1024 * 1024)
        monkeypatch.setattr(rc, "MAX_UPLOAD_BYTES", 10)
        odd = history.run_of(await env.call("big outputs/data.bin 10\nchart outputs/大图.png")).files
        assert [(f.name, f.kind, f.type, f.preview) for f in odd] == [("data.bin", "file", "text", True), ("大图.png", "file", OTHER, False)]
        assert env.images.uploaded == [] and (await env.files.original(odd[1].id, TID)) is not None

    run_async(go())


def test_image_service_down_keeps_the_chart_as_a_file(tmp_path):
    async def go():
        env = Env(tmp_path)

        async def down(data: bytes) -> dict:
            from app.tools.gateway import GatewayError
            raise GatewayError("传图失败", kind="http_503")
        env.gateway.upload_image = down
        (f,) = history.run_of(await env.call("chart outputs/图表.png")).files
        assert (f.kind, f.type, f.preview) == ("file", OTHER, False) and (await env.files.original(f.id, TID)) is not None

    run_async(go())


def test_uploads_follow_the_conversation(tmp_path, monkeypatch):
    async def go():
        env = Env(tmp_path)
        a = await env.attach("数据.csv", b"a,b\n1,2\n")
        await env.attach("数据.csv", b"a,b\n3,4\n5,6\n")
        big = await env.attach("大.csv", b"x," * 400)
        monkeypatch.setattr(rc, "SANDBOX_MAX_UPLOAD_BYTES", 500)
        env.sb.workspaces[TID] = {"uploads/旧的.csv": b"old", "keep.py": b"print(1)"}
        out = await env.call("ls")
        assert history.run_of(out).stdout.split() == ["keep.py", "uploads/数据", "(2).csv", "uploads/数据.csv"]  # 旧的删了，草稿不动，太大的没放
        assert env.sb.workspaces[TID]["uploads/数据 (2).csv"] == b"a,b\n3,4\n5,6\n"
        assert "- 数据.csv -> uploads/数据 (2).csv" in out.content and "larger than 0 MB: 大.csv" in out.content
        # 文件不在了（用户删了、清掉了）：下一条命令之前从 uploads/ 拿掉
        await env.files.drop([await env.files.store.get(a), await env.files.store.get(big)])
        await env.call("ls")
        assert sorted(env.sb.workspaces[TID]) == ["keep.py", "uploads/数据.csv"] and env.sb.workspaces[TID]["uploads/数据.csv"] == b"a,b\n3,4\n5,6\n"

    run_async(go())


def test_failed_commands_still_count_as_run(tmp_path):
    async def go():
        env = Env(tmp_path)
        failed = await env.call("echo 开始\nchart outputs/半张.png\nfail 2")
        run = history.run_of(failed)
        assert failed.status == "success" and (run.exit_code, run.ok, run.killed) == (2, False, "") and "ValueError: boom" in run.stderr
        assert [f.name for f in run.files] == ["半张.png"] and "Exit code: 2" in failed.content
        step = history.step_of(env.msgs[-2].tool_calls[0], failed)
        assert (step.ok, step.message, step.run.exit_code) == (False, "", 2)
        slow = await env.call("timeout")
        assert (history.run_of(slow).killed, history.run_of(slow).exit_code) == ("timeout", 137)
        assert "ran longer than 60 seconds" in slow.content and "set timeout_seconds (at most 120)" in slow.content
        longest = await env.call("timeout", timeout_seconds=120)
        assert "That is the longest a command may run" in longest.content
        assert "used more memory" in (await env.call("memory")).content
        assert "(the command printed nothing)" in (await env.call("rm nothing")).content

    run_async(go())


def test_commands_that_did_not_run(tmp_path):
    async def go():
        env = Env(tmp_path)

        async def failure(command: str = "ls", **kw) -> tuple[str, str, str]:
            out = await env.call(command, **kw)
            step = history.step_of(env.msgs[-2].tool_calls[0], out)
            assert history.run_of(out) is None and step.run is None and not step.ok
            return out.status, out.artifact["failure"], step.message

        assert await failure("  \n ") == ("error", "empty", "命令是空的，没跑")
        # 不是工作任务、游客、没归谁的对话、没有沙箱、对话编号不像样：模型硬调了也不跑
        for kw in ({"work": False}, {"owner": "guest:abc"}, {"owner": ""}, {"sandbox": False}, {"thread": "../etc"}):
            assert await failure(**kw) == ("error", "unavailable", "运行环境暂时用不了")
        assert env.sb.runs == []
        for fail, code, text in (((503, "sandbox_busy"), "busy", "运行环境正忙，没跑成"), ((409, "workspace_busy"), "busy", "运行环境正忙，没跑成"),
                                 ((503, "sandbox_unavailable"), "unavailable", "运行环境暂时用不了"), ((429, "daily_limit"), "limit", "今天运行命令的次数用完了"),
                                 ((500, "internal"), "unavailable", "运行环境暂时用不了")):
            env.sb.fail = fail
            assert await failure() == ("error", code, text)
        # 密钥不对、网关没放行：不是模型能处理的，照常抛出去
        env.sb.fail = (403, "forbidden")
        with pytest.raises(GatewayAuthError):
            await env.call("ls")
        env.msgs.pop()
        # 工作区写满了、坏了：删掉重来，告诉模型草稿没了
        env.sb.fail = None
        env.sb.workspaces[TID] = {"draft.py": b"x"}
        real = env.gateway.sandbox_run

        async def full(*a):
            raise SandboxError("跑命令失败：HTTP 413 workspace_over_quota", kind="workspace_over_quota", status=413)
        env.gateway.sandbox_run = full
        assert await failure() == ("error", "reset", "工作目录满了，已经清空，没跑成")
        assert env.sb.deleted == [TID] and TID not in env.sb.workspaces and "has been reset" in env.msgs[-1].content
        env.gateway.sandbox_run = real
        assert history.run_of(await env.call("echo 又能跑了")).ok

    run_async(go())


def test_commands_of_one_conversation_run_one_at_a_time(tmp_path):
    async def go():
        env = Env(tmp_path)
        env.sb.block = threading.Event()
        calls = [asyncio.create_task(env.run(env.runtime(f"c{i}"), command="slow\nchart outputs/图表.png")) for i in range(3)]
        other = asyncio.create_task(env.run(env.runtime("c9", thread="11111111-2222-3333-4444-555555555555"), command="slow"))
        await asyncio.sleep(0.3)
        assert env.sb.running == 2  # 这段对话一条，另一段对话一条
        env.sb.block.set()
        done = await asyncio.gather(*calls, other)
        assert all(history.run_of(d).ok for d in done) and len(env.sb.runs) == 4

    run_async(go())


def test_stopping_cancels_the_running_command(tmp_path):
    async def go():
        env = Env(tmp_path)
        env.sb.block = threading.Event()
        task = asyncio.create_task(env.run(env.runtime("c1"), command="slow\nchart outputs/图表.png"))
        for _ in range(100):
            if env.sb.running:
                break
            await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(100):
            if env.sb.cancelled:
                break
            await asyncio.sleep(0.02)
        env.sb.block.set()
        assert env.sb.cancelled == [TID] and env.images.uploaded == []
        assert not env.sandbox.lock(TID).locked()  # 锁放开了，下一条能跑
        assert history.run_of(await env.call("echo 接着跑")).ok

    run_async(go())


# ---- 经接口：工作任务里才有，产出在这一轮的条目里，重新回答、删对话跟着删 ----


@pytest.fixture
def rr(tmp_path):
    return ResearchRunner(fake=True)


@pytest.fixture
def client(rr):
    api_main.set_research_runner(rr)
    return TestClient(api_main.app, headers=USER)


def _steps(turn: dict) -> list[dict]:
    return [i for i in turn["items"] if i["type"] == "step" and i["kind"] == "command"]


def _reply(turn: dict) -> str:
    return next(i["text"] for i in reversed(turn["items"]) if i["type"] == "text")


def test_work_task_runs_a_command_and_shows_the_outputs(rr, client):
    sb = rr.planner.sandbox
    up = client.post("/api/files", files={"file": ("销售.csv", "月份,销售额\n一月,120\n".encode(), "text/csv")}).json()
    aio.run(rr.files.settle())
    evs = events(client.post("/api/research/threads", json={
        "text": "跑两次：cat uploads/销售.csv；chart outputs/图表.png；write outputs/汇总.csv 月份,合计", "files": [up["id"]], "work": True}))
    v = last(evs, "state")
    tid, turn = v["thread_id"], v["turns"][-1]
    assert v["status"] == "idle" and _reply(turn).startswith("跑完了，退出码 0，给了 2 个文件：图表.png、汇总.csv")
    first, second = _steps(turn)
    assert first["text"].startswith("cat uploads/销售.csv\nchart") and first["ok"] and first["run"]["stdout"].startswith("月份,销售额")
    chart, table = second["run"]["files"]
    assert [f["path"] for f in first["run"]["files"]] == [chart["path"], table["path"]] and first["run"]["files"][0]["id"] != chart["id"]
    # 执行中发的事件：先一条 pending，做完的带着结果，不带工具结果本身
    live = [d["step"] for e, d in evs if e == "step" and d["step"]["kind"] == "command"]
    assert [s["pending"] for s in live] == [True, False, True, False] and live[1]["run"]["files"] and all("result" not in d for e, d in evs if e == "step")
    # 图从取图的接口拿，文件能下载、能看；第一次跑的那两个（同一轮同一个路径）已经作废
    img = client.get(f"/api/research/threads/{tid}/images/{chart['id']}")
    assert img.status_code == 200 and img.headers["content-type"] == "image/png"
    assert client.get(f"/api/research/threads/{tid}/files/{table['id']}").content == "月份,合计".encode()
    assert client.get(f"/api/research/threads/{tid}/files/{table['id']}/parts").status_code == 200
    old_chart, old_table = first["run"]["files"]
    assert client.get(f"/api/research/threads/{tid}/images/{old_chart['id']}").status_code == 404
    assert client.get(f"/api/research/threads/{tid}/files/{old_table['id']}").status_code == 404
    assert sorted(sb.workspaces[tid]) == ["outputs/图表.png", "outputs/汇总.csv", "uploads/销售.csv"]
    # 不收钱：这一轮的工具花费里没有它
    assert all(r.get("tool") != history.RUN_TOOL for r in aio.run(rr.run_store.costs_of(tid)) if isinstance(r, dict)) if hasattr(rr.run_store, "costs_of") else True
    # 重新回答：这一轮的产出删掉（图从 image-service 删），重新跑出来的是新的
    v2 = last(events(client.post(f"/api/research/threads/{tid}/regenerate", json={})), "state")
    again = _steps(v2["turns"][-1])[-1]["run"]["files"]
    assert {f["id"] for f in again}.isdisjoint({chart["id"], table["id"]})
    assert client.get(f"/api/research/threads/{tid}/images/{chart['id']}").status_code == 404 and chart["id"] in rr.planner.images.deleted
    assert client.get(f"/api/research/threads/{tid}/files/{table['id']}").status_code == 404
    assert client.get(f"/api/research/threads/{tid}/images/{again[0]['id']}").status_code == 200
    # 删对话：产出、沙箱里的工作区一起删
    assert client.delete(f"/api/research/threads/{tid}").status_code in (200, 204)
    assert sb.deleted == [tid] and tid not in sb.workspaces and again[0]["id"] in rr.planner.images.deleted
    assert aio.run(rr.files.store.of_workspace(tid)) == []


def test_chat_and_guests_do_not_get_the_tool(rr, client):
    sb = rr.planner.sandbox
    chat = new(client, "跑命令：echo 你好")
    assert _reply(chat["turns"][-1]) == "没有跑命令的工具。" and not _steps(chat["turns"][-1])
    guest = TestClient(api_main.app, headers=bearer(guest_token()))
    g = new(guest, "跑命令：echo 你好", work=True)
    assert _reply(g["turns"][-1]) == "没有跑命令的工具。"
    work = new(client, "跑命令：echo 你好", work=True)
    assert _reply(work["turns"][-1]).startswith("跑完了，退出码 0") and len(sb.runs) == 1
    # 没调过命令的对话删的时候不去删工作区
    client.delete(f"/api/research/threads/{chat['thread_id']}")
    assert sb.deleted == []


def test_sandbox_switched_off(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SANDBOX_ENABLED", False)
    rr = ResearchRunner(fake=True)
    api_main.set_research_runner(rr)
    client = TestClient(api_main.app, headers=USER)
    assert rr.sandbox is None and _reply(new(client, "跑命令：echo 你好", work=True)["turns"][-1]) == "没有跑命令的工具。"


def test_parallel_calls_and_failures_through_the_api(rr, client):
    sb = rr.planner.sandbox
    v = new(client, "一起跑：slow；chart outputs/图表.png", work=True)
    a, b = _steps(v["turns"][-1])
    assert sb.most == 1 and a["ok"] and b["ok"]  # 一条消息里并行调的，排着队跑
    kept = [f["id"] for s in (a, b) for f in s["run"]["files"]]
    alive = [i for i in kept if client.get(f"/api/research/threads/{v['thread_id']}/images/{i}").status_code == 200]
    assert len(kept) == 2 and len(alive) == 1  # 同一个路径只留一张
    sb.fail = (503, "sandbox_busy")
    busy = new(client, "跑命令：echo 你好", work=True)
    (step,) = _steps(busy["turns"][-1])
    assert (step["ok"], step["message"], step["run"]) == (False, "运行环境正忙，没跑成", None) and _reply(busy["turns"][-1]) == "没跑成。"
    assert state(client, busy["thread_id"])["status"] == "idle"


def test_stop_while_a_command_is_running(rr, client):
    sb = rr.planner.sandbox
    sb.block = threading.Event()
    got: list = []
    t = threading.Thread(target=lambda: got.append(events(client.post("/api/research/threads", json={"text": "跑命令：slow；echo 完了", "work": True}))))
    t.start()
    assert wait_until(lambda: sb.running == 1)
    tid = sb.runs[0][0]
    assert client.post(f"/api/research/threads/{tid}/stop").status_code == 200
    t.join(20)
    assert wait_until(lambda: sb.cancelled == [tid])
    sb.block.set()
    v = state(client, tid)
    (step,) = _steps(v["turns"][-1])
    assert v["status"] == "idle" and (step["ok"], step["message"], step["pending"]) == (False, "停止了，没有做完", False)
