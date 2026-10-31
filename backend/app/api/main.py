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

from app import config
from app.api.favicon import Favicons
from app.api.inspiration import InspirationError, Inspirations
from app.api.research_runner import NothingToRegenerate, NothingToRetry, NotIdle, ResearchRunner
from app.api.runner import BaseRunner, GraphRunner, RunConflict, valid_thread_id

app = FastAPI(title="LingDraft PoC")
_runner: GraphRunner | None = None
_research: ResearchRunner | None = None
_favicons: Favicons | None = None
_inspirations: Inspirations | None = None


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


def favicons() -> Favicons:
    global _favicons
    if _favicons is None:
        _favicons = Favicons(config.DATA_DIR / "favicons")
    return _favicons


def set_favicons(f: Favicons | None) -> None:
    global _favicons
    _favicons = f


def inspirations() -> Inspirations:
    global _inspirations
    if _inspirations is None:
        _inspirations = Inspirations(config.FUSION_URL)
    return _inspirations


def set_inspirations(i: Inspirations | None) -> None:
    global _inspirations
    _inspirations = i


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


def _safe_file(root: Path, name: str) -> Path:
    """root 是线程目录；边界按线程目录算，目录里指向外面的链接也读不出去。"""
    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(400, "非法文件名")
    path = root / name
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


# ---- 对话场景（模块和接口路径沿用 research） ----


class ResearchMessageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str


class ResearchTitleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str


TITLE_MAX = 40


def _text(body: ResearchMessageBody) -> str:
    if not body.text.strip():
        raise HTTPException(400, "说点什么再发送")
    return body.text


@app.get("/api/research/threads")
async def research_list():
    """线程列表：所有对话的标题和状态。"""
    return {"threads": research().list_threads()}


@app.post("/api/research/threads")
async def research_create(body: ResearchMessageBody):
    return _sse(research().start(str(uuid.uuid4()), _text(body)))


@app.post("/api/research/threads/{thread_id}/messages")
async def research_say(thread_id: str, body: ResearchMessageBody):
    """接着说。运行中、上一轮没跑完时返回 409。"""
    r = research()
    _view(r, thread_id)
    text = _text(body)
    try:
        return _sse(r.say(thread_id, text))
    except (RunConflict, NotIdle) as e:
        raise HTTPException(409, str(e)) from None


@app.post("/api/research/threads/{thread_id}/stop")
async def research_stop(thread_id: str):
    """让正在跑（或排队）的这一轮停下；停下的结果从事件流里来。没在跑的返回 409。"""
    r = research()
    _view(r, thread_id)
    if not r.stop(thread_id):
        raise HTTPException(409, "这个对话没有在运行")
    return {"ok": True}


@app.post("/api/research/threads/{thread_id}/regenerate")
async def research_regenerate(thread_id: str):
    """最后一轮重新回答。运行中、上一轮没跑完、还没有回答时返回 409。"""
    r = research()
    _view(r, thread_id)
    try:
        return _sse(r.regenerate(thread_id))
    except (RunConflict, NotIdle, NothingToRegenerate) as e:
        raise HTTPException(409, str(e)) from None


@app.post("/api/research/threads/{thread_id}/edit")
async def research_edit(thread_id: str, body: ResearchMessageBody):
    """改了最后那句话，按新的话重新回答。运行中、上一轮没跑完、还没有说过话时返回 409。"""
    r = research()
    _view(r, thread_id)
    text = _text(body)
    try:
        return _sse(r.regenerate(thread_id, text))
    except (RunConflict, NotIdle, NothingToRegenerate) as e:
        raise HTTPException(409, str(e)) from None


@app.patch("/api/research/threads/{thread_id}")
async def research_rename(thread_id: str, body: ResearchTitleBody):
    """改标题，返回列表里的这一行。运行中、上一轮没跑完时返回 409。"""
    r = research()
    _view(r, thread_id)
    title = " ".join(body.title.split())
    if not title or len(title) > TITLE_MAX:
        raise HTTPException(400, "标题不能为空" if not title else f"标题最多 {TITLE_MAX} 个字")
    try:
        r.rename(thread_id, title)
    except (RunConflict, NotIdle) as e:
        raise HTTPException(409, str(e)) from None
    return r.summary_view(thread_id)


@app.delete("/api/research/threads/{thread_id}")
async def research_delete(thread_id: str):
    """删掉这段对话，找不回来。运行中返回 409。"""
    r = research()
    _view(r, thread_id)
    try:
        r.delete(thread_id)
    except RunConflict as e:
        raise HTTPException(409, str(e)) from None
    return {"ok": True}


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


@app.get("/api/research/threads/{thread_id}/images/{name}")
async def research_image(thread_id: str, name: str):
    """对话里画的图。文件名是图片编号，内容不会变，浏览器可以一直缓存；对话删了图也跟着删。"""
    path = research().images.path(thread_id, name)
    if path is None:
        raise HTTPException(404, "没有这张图")
    return FileResponse(path, headers={"Cache-Control": "private, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"})


@app.get("/api/inspirations")
async def inspiration_list():
    """新对话页的“今日灵感”（从 Fusion 取，见 inspiration.py）。一次都没取到回 502，页面显示固定的几个例子。"""
    try:
        return {"questions": await inspirations().get()}
    except InspirationError as e:
        raise HTTPException(502, str(e)) from None


@app.get("/api/favicon/{host}")
async def favicon(host: str):
    """来源网站的图标（后端代取并缓存）。取不到返回 404，页面显示域名首字母；404 也让浏览器缓存一天，不反复来要。"""
    try:
        got = await favicons().get(host, wait=2.5)
    except ValueError:
        raise HTTPException(400, "域名格式不对") from None
    except TimeoutError:  # 还在取（慢网站）：先让页面显示首字母，后台取完存起来，下次打开就有
        return Response(status_code=404, headers={"Cache-Control": "no-store"})
    if got is None:
        return Response(status_code=404, headers={"Cache-Control": "public, max-age=86400"})
    data, kind = got
    # 图标是别人网站上的文件：SVG 被直接打开时不让它跑脚本
    return Response(data, media_type=kind, headers={
        "Cache-Control": "public, max-age=604800", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox"})
