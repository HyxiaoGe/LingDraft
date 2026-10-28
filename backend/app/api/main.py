"""FastAPI 接口（M2）。全部挂在 /api 下，前端 dev server 代理过来。"""
from __future__ import annotations

import io
import uuid
import zipfile
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict

from app.api.research_runner import NothingToRetry, ResearchRunner, StaleInterrupt
from app.api.runner import BaseRunner, GraphRunner, RunConflict, valid_thread_id
from app.graph.research.answers import InvalidAnswer

app = FastAPI(title="LingDraft PoC")
_runner: GraphRunner | None = None
_research: ResearchRunner | None = None


def runner() -> GraphRunner:
    global _runner
    if _runner is None:
        _runner = GraphRunner()
    return _runner


def set_runner(r: GraphRunner) -> None:
    global _runner
    _runner = r


def research() -> ResearchRunner:
    global _research
    if _research is None:
        _research = ResearchRunner()
    return _research


def set_research_runner(r: ResearchRunner) -> None:
    global _research
    _research = r


def _sse(gen):
    return StreamingResponse(gen, media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _view(r: BaseRunner, thread_id: str) -> dict:
    """URL 里的 thread_id 格式不对、或线程不存在，一律 404。"""
    view = r.state_view(thread_id) if valid_thread_id(thread_id) else {"exists": False}
    if not view["exists"]:
        raise HTTPException(404, "thread 不存在")
    return view


def _dir(r: BaseRunner, thread_id: str) -> Path:
    try:
        return r.thread_dir(thread_id)
    except ValueError:
        raise HTTPException(404, "thread 不存在") from None


def _inside(root: Path, path: Path) -> bool:
    """解析掉符号链接以后仍在线程目录下：目录里有指向外面的链接（子目录或文件）也读不出去。"""
    return path.resolve().is_relative_to(root.resolve())


def _safe_file(root: Path, name: str, sub: str | None = None) -> Path:
    """root 是线程目录，sub 是其中的子目录（如 package）；边界按线程目录算，子目录本身是链接也不行。"""
    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(400, "非法文件名")
    path = (root / sub if sub else root) / name
    if not _inside(root, path) or not path.is_file():
        raise HTTPException(404, "文件不存在")
    return path


def _zip(root: Path, src: Path, filename: str) -> Response:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in src.iterdir():
            if p.is_file() and _inside(root, p):
                z.write(p, p.name)
    return Response(buf.getvalue(), media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.post("/api/threads")
async def create_thread(
    request: Annotated[str, Form()],
    image: Annotated[UploadFile | None, File()] = None,
):
    r = runner()
    thread_id = uuid.uuid4().hex[:8]
    image_path = None
    if image is not None and image.filename:
        suffix = Path(image.filename).suffix.lower() or ".png"
        if suffix not in (".png", ".jpg", ".jpeg", ".webp"):
            raise HTTPException(400, "只支持 png / jpg / webp")
        tdir = r.thread_dir(thread_id)
        tdir.mkdir(parents=True, exist_ok=True)
        image_path = tdir / f"product{suffix}"
        image_path.write_bytes(await image.read())
    return _sse(r.start(thread_id, request, str(image_path) if image_path else None))


class ResumeBody(BaseModel):
    payload: dict


@app.post("/api/threads/{thread_id}/resume")
async def resume_thread(thread_id: str, body: ResumeBody):
    r = runner()
    _view(r, thread_id)
    try:
        return _sse(r.resume(thread_id, body.payload))
    except RunConflict as e:
        raise HTTPException(409, str(e)) from None


@app.get("/api/threads/{thread_id}/state")
async def get_state(thread_id: str):
    return _view(runner(), thread_id)


@app.get("/api/threads/{thread_id}/files/{name}")
async def get_file(thread_id: str, name: str):
    return FileResponse(_safe_file(_dir(runner(), thread_id), name))


@app.get("/api/threads/{thread_id}/export")
async def export_zip(thread_id: str):
    if not _view(runner(), thread_id)["exported"]:
        raise HTTPException(404, "尚未导出，请先在 review 中确认")
    tdir = _dir(runner(), thread_id)
    return _zip(tdir, tdir, f"poster-{thread_id}.zip")


# ---- 调研场景 ----


class ResearchCreateBody(BaseModel):
    request: str


@app.post("/api/research/threads")
async def research_create(body: ResearchCreateBody):
    if not body.request.strip():
        raise HTTPException(400, "需求不能为空")
    return _sse(research().start(uuid.uuid4().hex[:8], body.request.strip()))


class ResearchResumeBody(BaseModel):
    """interrupt_id 是页面上这张卡的中断 id（state / interrupt 事件里给的）；对不上说明卡已过期。"""

    model_config = ConfigDict(extra="forbid")
    interrupt_id: str
    payload: dict


@app.post("/api/research/threads/{thread_id}/resume")
async def research_resume(thread_id: str, body: ResearchResumeBody):
    r = research()
    _view(r, thread_id)
    try:
        return _sse(r.resume(thread_id, body.payload, body.interrupt_id))
    except (RunConflict, StaleInterrupt) as e:
        raise HTTPException(409, str(e)) from None
    except InvalidAnswer as e:
        raise HTTPException(400, str(e)) from None


@app.post("/api/research/threads/{thread_id}/retry")
async def research_retry(thread_id: str):
    """失败的线程从断开的那一步接着跑；没失败的返回 409。"""
    r = research()
    _view(r, thread_id)
    try:
        return _sse(r.retry(thread_id))
    except (RunConflict, NothingToRetry) as e:
        raise HTTPException(409, str(e)) from None


@app.get("/api/research/threads/{thread_id}/events")
async def research_events(thread_id: str, after: str | None = None):
    """接上线程当前这次执行的事件流（after 是收到的最后一个事件 id），以 end 事件收尾；断线、刷新后用。"""
    r = research()
    _view(r, thread_id)
    return _sse(r.follow(thread_id, after))


@app.get("/api/research/threads/{thread_id}/state")
async def research_state(thread_id: str):
    return _view(research(), thread_id)


@app.get("/api/research/threads/{thread_id}/files/{name}")
async def research_file(thread_id: str, name: str):
    return FileResponse(_safe_file(_dir(research(), thread_id), name), media_type="text/markdown; charset=utf-8")


@app.get("/api/research/threads/{thread_id}/package/{name}")
async def research_package_file(thread_id: str, name: str):
    path = _safe_file(_dir(research(), thread_id), name, "package")
    media = "application/json" if name.endswith(".json") else "text/markdown; charset=utf-8"
    return FileResponse(path, media_type=media)


@app.get("/api/research/threads/{thread_id}/export")
async def research_export(thread_id: str):
    if not _view(research(), thread_id)["exported"]:
        raise HTTPException(404, "尚未导出，请先在终审中确认")
    tdir = _dir(research(), thread_id)
    return _zip(tdir, tdir / "package", f"research-{thread_id}.zip")
