"""FastAPI 接口（M2）。全部挂在 /api 下，前端 dev server 代理过来。"""
from __future__ import annotations

import io
import uuid
import zipfile
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel

from app.api.research_runner import ResearchRunner
from app.api.runner import GraphRunner

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
    if not r.state_view(thread_id)["exists"]:
        raise HTTPException(404, "thread 不存在")
    return _sse(r.resume(thread_id, body.payload))


@app.get("/api/threads/{thread_id}/state")
async def get_state(thread_id: str):
    view = runner().state_view(thread_id)
    if not view["exists"]:
        raise HTTPException(404, "thread 不存在")
    return view


@app.get("/api/threads/{thread_id}/files/{name}")
async def get_file(thread_id: str, name: str):
    if "/" in name or name.startswith("."):
        raise HTTPException(400, "非法文件名")
    path = runner().thread_dir(thread_id) / name
    if not path.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(path)


@app.get("/api/threads/{thread_id}/export")
async def export_zip(thread_id: str):
    view = runner().state_view(thread_id)
    if not view["exists"] or not view["exported"]:
        raise HTTPException(404, "尚未导出，请先在 review 中确认")
    tdir = runner().thread_dir(thread_id)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in tdir.iterdir():
            if p.is_file():
                z.write(p, p.name)
    return Response(buf.getvalue(), media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="poster-{thread_id}.zip"'})


# ---- 调研场景 ----


class ResearchCreateBody(BaseModel):
    request: str


@app.post("/api/research/threads")
async def research_create(body: ResearchCreateBody):
    if not body.request.strip():
        raise HTTPException(400, "需求不能为空")
    return _sse(research().start(uuid.uuid4().hex[:8], body.request.strip()))


@app.post("/api/research/threads/{thread_id}/resume")
async def research_resume(thread_id: str, body: ResumeBody):
    r = research()
    if not r.state_view(thread_id)["exists"]:
        raise HTTPException(404, "thread 不存在")
    return _sse(r.resume(thread_id, body.payload))


@app.get("/api/research/threads/{thread_id}/state")
async def research_state(thread_id: str):
    view = research().state_view(thread_id)
    if not view["exists"]:
        raise HTTPException(404, "thread 不存在")
    return view


def _safe_file(base: Path, name: str) -> Path:
    if "/" in name or name.startswith("."):
        raise HTTPException(400, "非法文件名")
    path = base / name
    if not path.is_file():
        raise HTTPException(404, "文件不存在")
    return path


@app.get("/api/research/threads/{thread_id}/files/{name}")
async def research_file(thread_id: str, name: str):
    return FileResponse(_safe_file(research().thread_dir(thread_id), name), media_type="text/markdown; charset=utf-8")


@app.get("/api/research/threads/{thread_id}/package/{name}")
async def research_package_file(thread_id: str, name: str):
    path = _safe_file(research().thread_dir(thread_id) / "package", name)
    media = "application/json" if name.endswith(".json") else "text/markdown; charset=utf-8"
    return FileResponse(path, media_type=media)


@app.get("/api/research/threads/{thread_id}/export")
async def research_export(thread_id: str):
    view = research().state_view(thread_id)
    if not view["exists"] or not view["exported"]:
        raise HTTPException(404, "尚未导出，请先在终审中确认")
    pkg = research().thread_dir(thread_id) / "package"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in pkg.iterdir():
            if p.is_file():
                z.write(p, p.name)
    return Response(buf.getvalue(), media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="research-{thread_id}.zip"'})
