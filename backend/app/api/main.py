"""FastAPI 接口（M2）。全部挂在 /api 下，前端 dev server 代理过来。"""
from __future__ import annotations

import io
import logging
import mimetypes
import threading
import uuid
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import Annotated, NamedTuple

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.datastructures import UploadFile as FormFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import config, costs, tracing
from app.api.favicon import Favicons
from app.api.inspiration import InspirationError, Inspirations
from app.api.research_runner import AlreadyPending, NothingToRegenerate, NothingToRetry, NotIdle, ResearchRunner, UnknownModel
from app.api.run_store import STATUSES
from app.api.runner import BaseRunner, GraphRunner, RunConflict, valid_thread_id
from app.files.service import NotSendable, UploadRejected, view as file_view
from app.graph.research.memory import TEXT_MAX as MEMORY_TEXT_MAX
from app.graph.research.memory import DreamSchedule
from app.identity import ADMIN, Authenticator, AuthUnavailable, BadToken, Identity, make_authenticator
from app.quota import Exhausted, Limits
from app.graph.research import history
from app.graph.research.state import AttachedImage
from app.tools.gateway import MAX_UPLOAD_BYTES, GatewayError, ImageRejected

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """服务起来时先打开对话的存储：没配库、连不上就报错，服务起不来（见 research_runner.open_storage）。
    再开整理记忆的定时任务（见 graph.research.memory.DreamSchedule），关掉时停。配了 LangSmith 就传对话的过程（见 app.tracing）。
    每次调模型记一行成本（见 app.costs），关掉时把还没写的写完。"""
    traced = tracing.start()
    costs.open_ledger()
    research()
    schedule = None
    if config.DREAM_ENABLED:
        schedule = DreamSchedule(lambda: research().dream(), lambda: research().memory.last_dream(), config.DREAM_HOUR)
        schedule.start()
    yield
    if schedule:
        schedule.stop()
    costs.LEDGER.stop()
    if traced:
        tracing.stop()


app = FastAPI(title="LingDraft PoC", lifespan=lifespan)
_research_mu = threading.Lock()  # 服务启动时建好；测试不走启动流程，请求可能同时第一次用到运行器，只建一个
_runner: GraphRunner | None = None
_research: ResearchRunner | None = None
_favicons: Favicons | None = None
_inspirations: Inspirations | None = None
_authenticator: Authenticator | None = None


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
    with _research_mu:
        if _research is None:
            _research = ResearchRunner()
            _research.recover()  # 进程里第一次建：之前留下的“还在跑”的运行记录都是上一个进程的
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


def authenticator() -> Authenticator:
    global _authenticator
    if _authenticator is None:
        _authenticator = make_authenticator(config.AUTH_URL, config.AUTH_CLIENT_ID, config.AUTH_INTERNAL_URL, config.AUTH_REDIS_URL,
                                            config.GUEST_SECRET, config.GUEST_PER_IP_DAILY)
    return _authenticator


def set_authenticator(a: Authenticator | None) -> None:
    global _authenticator
    _authenticator = a


def identity(request: Request) -> Identity:
    """这个请求是谁（见 app.identity）：Authorization: Bearer 里是游客令牌或 auth 服务的 access token。
    没带、不对、过期、退出了都是 401，页面收到 401 先重新领游客身份再试一次，还不行就当没登录；auth 服务的公钥取不到是 503。"""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(401, "没有登录信息", headers={"WWW-Authenticate": "Bearer"})
    try:
        return authenticator().identify(header[7:].strip())
    except BadToken as e:
        raise HTTPException(401, str(e), headers={"WWW-Authenticate": "Bearer"}) from None
    except AuthUnavailable as e:
        logger.warning("验登录失败：%s", e)
        raise HTTPException(503, "登录服务暂时不可用，稍后再试") from None


Who = Annotated[Identity, Depends(identity)]


def client_ip(request: Request) -> str:
    """来源地址：经 nginx 转过来的看 X-Forwarded-For 的第一个（只用来数游客身份领了几个，见 identity.GuestTokens）。"""
    forwarded = request.headers.get("X-Forwarded-For", "")
    return forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "")


def _sse(gen):
    return StreamingResponse(gen, media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _view(r: BaseRunner, thread_id: str) -> dict:
    """URL 里的 thread_id 格式不对、或线程不存在，一律 404。"""
    view = r.state_view(thread_id) if valid_thread_id(thread_id) else {"exists": False}
    if not view["exists"]:
        raise HTTPException(404, "thread 不存在")
    return view


def _mine(r: ResearchRunner, thread_id: str, who: Identity) -> dict:
    """对话接口都先过这里：线程不存在、或不归这个人（见 ResearchRunner.can_see），一律 404，不透露有没有。"""
    view = _view(r, thread_id)
    if not r.can_see(thread_id, who):
        raise HTTPException(404, "thread 不存在")
    return view


def _admit(r: ResearchRunner, who: Identity) -> None:
    """开始一轮之前看额度（见 app.quota）：用完了回 429，detail 是个对象（code 是 credits_exhausted，带什么时候重置、是不是游客），页面据此提示。"""
    try:
        r.quota.admit(who)
    except Exhausted as e:
        raise HTTPException(429, e.detail()) from None


def _dir(r: GraphRunner, thread_id: str) -> Path:
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
# 接口都是普通函数，FastAPI 放到线程池里跑：运行器的同步方法把活交给对话的事件循环、等结果（见 app.aio），不卡住这里的事件循环。
# 事件流（SSE）照旧在这里的事件循环里读事件日志。


class ResearchMessageBody(BaseModel):
    """thinking 是输入框旁边的思考开关，没给按模型的默认（能开关的默认开）。
    files 是这句话带的文件的编号（先传到 /api/files、解析好了才能发），最多 history.MAX_FILES 个。"""

    model_config = ConfigDict(extra="forbid")
    text: str
    thinking: bool | None = None
    files: list[str] = Field(default_factory=list, max_length=history.MAX_FILES)


class ResearchCreateBody(ResearchMessageBody):
    """新对话：model 是选的模型（/api/models 里的 id），没给是 Auto；定下就不换。"""

    model: str | None = None


class ResearchRegenerateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    thinking: bool | None = None


class ResearchTitleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str


TITLE_MAX = 40


class MemoryEditBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str


def _text(body: ResearchMessageBody) -> str:
    if not body.text.strip():
        raise HTTPException(400, "说点什么再发送")
    return body.text


class _Incoming(NamedTuple):
    body: BaseModel
    images: list[bytes]  # 这句话带的图，按发的先后（没带是空的）


def _parse(model: type[BaseModel], raw: str | bytes) -> BaseModel:
    try:
        return model.model_validate_json(raw)
    except ValidationError as e:
        raise RequestValidationError(e.errors(include_url=False)) from None


def _incoming(model: type[BaseModel]):
    """发消息（新对话、接着说）收两种请求：只有字的照旧是 JSON；带图的是 multipart/form-data，body 字段是同样的 JSON，
    每张图一个 image 字段，最多 history.MAX_ATTACHED 张（多了回 400）。这里只看大小（每张超过 4 MB 回 413），类型交给 image-service 认。"""

    async def read(request: Request) -> _Incoming:
        if not request.headers.get("content-type", "").startswith("multipart/form-data"):
            return _Incoming(_parse(model, await request.body()), [])
        try:
            form = await request.form(max_files=history.MAX_ATTACHED, max_fields=1)
        except StarletteHTTPException as e:  # 文件、字段多了 starlette 回 400（它的 HTTPException，不是 FastAPI 的）
            raise HTTPException(400, f"一次最多带 {history.MAX_ATTACHED} 张图") from e
        try:
            raw, files = form.get("body"), form.getlist("image")
            if not isinstance(raw, str) or not all(isinstance(f, FormFile) for f in files):  # 表单里的是 starlette 的，不是 FastAPI 的子类
                raise HTTPException(422, "body 要是 JSON 字段，image 要是文件")
            body = _parse(model, raw)
            datas = [await f.read(MAX_UPLOAD_BYTES + 1) for f in files]  # type: ignore[union-attr]
        finally:
            await form.close()
        if any(len(d) > MAX_UPLOAD_BYTES for d in datas):
            raise HTTPException(413, "图片不能超过 4 MB")
        return _Incoming(body, datas)

    return read


def _upload(r: ResearchRunner, datas: list[bytes]) -> list[AttachedImage]:
    """这句话带的图一张张传进 image-service，返回记在消息上的信息。发出去之前就传：消息里要记它们的编号。
    有一张传不上去，已经传上去的删掉，整句不发。"""
    done: list[AttachedImage] = []
    try:
        for data in datas:
            done.append(r.upload_image(data))
    except ImageRejected as e:
        _discard(r, done)
        if e.kind == "image_too_large":
            raise HTTPException(413, "图片不能超过 4 MB") from None
        raise HTTPException(415, "只支持 PNG、JPEG、WebP 图片") from None
    except GatewayError as e:
        _discard(r, done)
        logger.warning("传图失败：%s", e)
        raise HTTPException(502, "图片暂时传不上去") from None
    return done


def _discard(r: ResearchRunner, images: list[AttachedImage]) -> None:
    """图传上去了，这一轮却没开始（运行中、上一轮没跑完、模型不对）：删掉。别的错不删：说不准那句话是不是已经记下了。"""
    r.discard_images([i.id for i in images])


# ---- 用户发的文件（见 app.files）：先传上来、后台解析，页面问状态，解析好了随消息发出去 ----

FILE_TOO_LARGE = f"文件不能超过 {config.FILE_MAX_BYTES // (1024 * 1024)} MB"


class _Upload(NamedTuple):
    name: str | None
    data: bytes


async def _uploaded(request: Request) -> _Upload:
    """传文件：multipart/form-data，一个 file 字段。超过上限回 413：请求头说的长度就超了的不读，读的时候也只读到上限多一个字节。"""
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise HTTPException(415, "请用表单上传文件")
    try:
        length = int(request.headers.get("content-length") or 0)
    except ValueError:
        length = 0
    if length > config.FILE_MAX_BYTES + 64 * 1024:  # 留出表单本身的那点
        raise HTTPException(413, FILE_TOO_LARGE)
    try:
        form = await request.form(max_files=1, max_fields=1)
    except StarletteHTTPException as e:
        raise HTTPException(400, "一次传一个文件") from e
    try:
        f = form.get("file")
        if not isinstance(f, FormFile):
            raise HTTPException(422, "file 要是文件")
        name, data = f.filename, await f.read(config.FILE_MAX_BYTES + 1)
    finally:
        await form.close()
    if len(data) > config.FILE_MAX_BYTES:
        raise HTTPException(413, FILE_TOO_LARGE)
    return _Upload(name, data)


@app.post("/api/files")
def file_upload(who: Who, up: Annotated[_Upload, Depends(_uploaded)]):  # 先认人再读表单
    """传一个文件：存好原件、排进后台解析，马上返回（status 是 parsing），页面接着问 /api/files/{id}。
    认不出、不收的（图片、老格式、压缩包……）415，太大 413，今天传满了 429：detail 是 {code: "file_rejected", reason, message, …}，
    页面按 reason 说人话（今天传满了还带 limit、guest、login_limit）。"""
    try:
        row = research().upload_file(who.sub, who.tier, up.name, up.data)
    except UploadRejected as e:
        raise HTTPException(e.status, {"code": "file_rejected", "reason": e.code, "message": e.message, **e.extra}) from None
    return file_view(row)


@app.get("/api/files/{file_id}")
def file_status(file_id: str, who: Who):
    """解析好了没有：status 是 parsing / ready / failed，failed 的 reason 是没读成的原因（见 files.service.view）。别人的、没有的（过期清掉了）404。"""
    row = research().file(file_id, who.sub)
    if row is None:
        raise HTTPException(404, "文件已经过期了，请重新上传")
    return file_view(row)


@app.delete("/api/files/{file_id}")
def file_remove(file_id: str, who: Who):
    """去掉还没发出去的文件（输入框里点叉）。发出去的跟着对话走，删对话才删；别人的、没有的、发出去的都 404。"""
    if not research().remove_file(file_id, who.sub):
        raise HTTPException(404, "没有这个文件")
    return {"ok": True}


# ---- 身份（见 app.identity） ----


@app.get("/api/auth/config")
def auth_config():
    """页面配登录 SDK 用：auth 服务地址和灵稿的 client_id；login 为 false 是没开登录（没配 AUTH_CLIENT_ID），页面不显示登录入口。"""
    on = authenticator().login_enabled
    return {"login": on, "auth_url": config.AUTH_URL if on else None, "client_id": config.AUTH_CLIENT_ID if on else None}


GUESTS_PER_IP = "guests_per_ip"  # 领游客身份 429 的 detail.code


@app.post("/api/auth/guest")
def auth_guest(request: Request):
    """领一个新的游客身份：{"token", "expires_at"}。页面没有身份（第一次来、或存的令牌被拒了）时调，存在浏览器里，每个请求带上。
    一个地址一天领太多返回 429，detail 带上每个地址一天几个、登录后的额度和换算（页面这时没有身份、取不到 /api/credits，
    靠这些说清“为什么 + 登录后会怎样”，见 frontend/src/login.jsx 的 LoginDialog）。"""
    guests = authenticator().guests
    got = guests.issue(client_ip(request))
    if got is None:
        limits = Limits.from_config()
        raise HTTPException(429, {"code": GUESTS_PER_IP, "message": "这个网络今天新领的游客身份太多了，请登录或明天再来",
                                  "per_ip": guests.per_ip_daily, "per_ask": config.CREDITS_PER_ASK,
                                  "login": {"day": limits.user_daily, "week": limits.user_weekly}})
    token, exp = got
    return {"token": token, "expires_at": exp}


@app.get("/api/auth/me")
def auth_me(who: Who):
    """带的令牌是谁：{"sub", "tier", "email", "guest"}。页面启动时用它验一下存的令牌还认不认。"""
    return {"sub": who.sub, "tier": who.tier, "email": who.email, "guest": who.guest}


class ClaimBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    guest_token: str


@app.post("/api/auth/claim")
def auth_claim(body: ClaimBody, who: Who):
    """登录以后把游客期间的对话并进账号（2026-11-06 Sean 定）：带着登录的令牌来，body 里是之前的游客令牌。返回 {"moved": 并了几段}。
    游客令牌不对是 400（不是 401：登录的令牌是好的）；自己还是游客是 403。"""
    if who.guest:
        raise HTTPException(403, "登录后才能并入对话")
    try:
        guest = authenticator().guests.verify(body.guest_token)
    except BadToken as e:
        raise HTTPException(400, str(e)) from None
    return {"moved": research().claim(who, guest)}


# ---- 对话 ----


@app.get("/api/credits")
def credits_view(who: Who):
    """这个人的额度（见 app.quota）：{"unlimited": 管理员不限, "warn_at": 用了几成开始提醒, "per_ask": “约可问 N 次”按一次多少换算,
    "windows": [{kind, limit, used, resets_at}]}，kind 是 day（今天）、week（这周，登录用户才有）、guests（所有游客今天合计，页面不显示）；
    游客多一个 "login": {"day", "week"}（登录后每天、每周多少）。"""
    return research().quota.view(who)


@app.get("/api/memory")
def memory_view(who: Who):
    """长期记忆：这个人的全部记忆和整理的时间（游客没有）。每条带 thread_id（从哪段对话记下的），页面能点回去。"""
    return research().memory_view(who)


# 记忆页能改、能删（2026-11-07 Sean 定，推翻 11-03 的“只能看”）；不做手动新增。删的撤销在页面上做：5 秒后才发删除
@app.patch("/api/memory/{item_id}")
def memory_edit(item_id: str, body: MemoryEditBody, who: Who):
    """改一条记忆的文字，返回改过的这一条。空白并成一个空格（提示词里一条一行）；空的、太长 400；游客、不是这个人的、没有这一条一律 404。"""
    text = " ".join(body.text.split())
    if not text or len(text) > MEMORY_TEXT_MAX:
        raise HTTPException(400, "记忆不能为空" if not text else f"一条记忆最多 {MEMORY_TEXT_MAX} 个字")
    item = research().edit_memory(who, item_id, text)
    if item is None:
        raise HTTPException(404, "没有这条记忆")
    return item


@app.delete("/api/memory/{item_id}")
def memory_delete(item_id: str, who: Who):
    """删一条记忆，以后回答不再参考。游客、不是这个人的、没有这一条一律 404。"""
    if not research().forget_memory(who, item_id):
        raise HTTPException(404, "没有这条记忆")
    return {"ok": True}


@app.get("/api/research/threads")
def research_list(who: Who):
    """线程列表：这个人的对话的标题和状态。登录以前没归谁的老对话谁都看不到。"""
    return {"threads": research().list_threads(who)}


@app.get("/api/models")
def research_models(who: Who):
    """新对话能选的模型，Auto 在第一个（列表见 app/models.toml，没配 key 的不在里面）。这一档选不了的也列出来，标着 locked。
    names 是选项以外、回答开头可能出现的模型（Auto 用的免费额度）的名字和图标。"""
    return {"models": research().models(who.tier), "names": research().model_names()}


@app.post("/api/research/threads")
def research_create(who: Who, msg: Annotated[_Incoming, Depends(_incoming(ResearchCreateBody))]):  # 先认人再读表单
    """新对话，归这个人。选的模型没有、停用了时返回 400；这一档选不了的（游客选了要登录的）返回 403，不能绕过页面上的锁。
    模型只在开新对话时选，之后每轮照对话定下的：游客打不开登录用户的对话，这里查一次就够。
    可以带图（见 _incoming）：每张超过 4 MB 回 413，不是 PNG / JPEG / WebP 回 415，传不上去回 502。
    可以带文件（body 的 files，先传到 /api/files）：还在解析、没读成、过期了回 400，什么都没开始。"""
    body: ResearchCreateBody = msg.body
    r = research()
    if body.model and r.locked(body.model, who.tier):
        raise HTTPException(403, "登录后才能用这个模型")
    _admit(r, who)
    text = _text(body)
    images = _upload(r, msg.images)
    try:
        return _sse(r.start(str(uuid.uuid4()), text, body.model, body.thinking, owner=who.sub, images=images, files=body.files))
    except UnknownModel as e:
        _discard(r, images)
        raise HTTPException(400, str(e)) from None
    except NotSendable as e:
        _discard(r, images)
        raise _not_sendable(e) from None


@app.post("/api/research/threads/{thread_id}/messages")
def research_say(thread_id: str, who: Who, msg: Annotated[_Incoming, Depends(_incoming(ResearchMessageBody))]):
    """接着说。运行中、上一轮没跑完时返回 409；额度用完了 429。可以带图、带文件，同新对话（带的文件不能发时 400）。
    排着的话、插进这一轮的话不能带图、带文件（/queue 只收 JSON）：图和文件要在这一轮开始时就在。"""
    body: ResearchMessageBody = msg.body
    r = research()
    _mine(r, thread_id, who)
    _admit(r, who)
    text = _text(body)
    images = _upload(r, msg.images)
    try:
        return _sse(r.say(thread_id, text, body.thinking, images, body.files))
    except (RunConflict, NotIdle) as e:
        _discard(r, images)
        raise HTTPException(409, str(e)) from None
    except NotSendable as e:
        _discard(r, images)
        raise _not_sendable(e) from None


def _not_sendable(e: NotSendable) -> HTTPException:
    """带的文件不能发：400，detail 是 {code: "files_not_sendable", reason, message}，页面按 reason 说人话、话和文件放回输入框。"""
    return HTTPException(400, {"code": "files_not_sendable", "reason": e.code, "message": str(e)})


@app.post("/api/research/threads/{thread_id}/queue")
def research_queue(thread_id: str, body: ResearchMessageBody, who: Who):
    """正在回答时又说的话：排着，这一轮答完或停下就接着发。返回 {"queued": 排着的那句}。
    这一轮刚好答完了就直接发，返回 {"queued": null}，页面去接事件流（/events）。
    已经有一句没发出去（排着或等着插进这一轮）、或上一轮没跑完时返回 409；额度用完了 429。"""
    r = research()
    _mine(r, thread_id, who)
    if body.files:
        raise HTTPException(400, "回答完再发带文件的消息")
    _admit(r, who)
    text = _text(body)
    try:
        return {"queued": r.queue(thread_id, text, body.thinking)}
    except (NotIdle, AlreadyPending) as e:
        raise HTTPException(409, str(e)) from None


@app.delete("/api/research/threads/{thread_id}/queue")
def research_unqueue(thread_id: str, who: Who):
    """撤回排着的话，返回 {"text": 原文}，页面放回输入框；已经发出去了或没有，text 是 null。"""
    r = research()
    _mine(r, thread_id, who)
    return {"text": r.unqueue(thread_id)}


@app.post("/api/research/threads/{thread_id}/guide")
def research_guide(thread_id: str, who: Who):
    """排着的话不等这一轮答完，插进这一轮：主 agent 下一次调模型之前放进对话；这一轮没再调模型就结束了的，照常排着发。
    返回 {"guided": 等着插进去的那句}；已经没在跑了（排着的话已经发出去了）是 null。"""
    r = research()
    _mine(r, thread_id, who)
    return {"guided": r.guide(thread_id)}


@app.delete("/api/research/threads/{thread_id}/guide")
def research_unguide(thread_id: str, who: Who):
    """撤回等着插进这一轮的话，返回 {"text": 原文}，页面放回输入框；已经插进去了或没有，text 是 null。"""
    r = research()
    _mine(r, thread_id, who)
    return {"text": r.unguide(thread_id)}


@app.post("/api/research/threads/{thread_id}/stop")
def research_stop(thread_id: str, who: Who):
    """让正在跑（或排队）的这一轮停下；停下的结果从事件流里来。没在跑的返回 409。"""
    r = research()
    _mine(r, thread_id, who)
    if not r.stop(thread_id):
        raise HTTPException(409, "这个对话没有在运行")
    return {"ok": True}


@app.post("/api/research/threads/{thread_id}/regenerate")
def research_regenerate(thread_id: str, who: Who, body: ResearchRegenerateBody | None = None):
    """最后一轮重新回答，可以带上这次的思考开关（不带按原来的）。运行中、上一轮没跑完、还没有回答时返回 409；额度用完了 429。"""
    r = research()
    _mine(r, thread_id, who)
    _admit(r, who)
    try:
        return _sse(r.regenerate(thread_id, thinking=body.thinking if body else None))
    except (RunConflict, NotIdle, NothingToRegenerate) as e:
        raise HTTPException(409, str(e)) from None


@app.post("/api/research/threads/{thread_id}/edit")
def research_edit(thread_id: str, body: ResearchMessageBody, who: Who):
    """改了最后那句话，按新的话重新回答。运行中、上一轮没跑完、还没有说过话时返回 409；额度用完了 429。"""
    r = research()
    _mine(r, thread_id, who)
    _admit(r, who)
    text = _text(body)
    try:
        return _sse(r.regenerate(thread_id, text, body.thinking))
    except (RunConflict, NotIdle, NothingToRegenerate) as e:
        raise HTTPException(409, str(e)) from None


@app.patch("/api/research/threads/{thread_id}")
def research_rename(thread_id: str, body: ResearchTitleBody, who: Who):
    """改标题，返回列表里的这一行。运行中、上一轮没跑完时返回 409。"""
    r = research()
    _mine(r, thread_id, who)
    title = " ".join(body.title.split())
    if not title or len(title) > TITLE_MAX:
        raise HTTPException(400, "标题不能为空" if not title else f"标题最多 {TITLE_MAX} 个字")
    try:
        r.rename(thread_id, title)
    except (RunConflict, NotIdle) as e:
        raise HTTPException(409, str(e)) from None
    return r.summary_view(thread_id)


# 置顶（2026-11-07 批次 D）：存后端，换设备也在；运行中也能置顶。返回列表里的这一行（pinned_at 是置顶的时间，取消了是 null）
@app.post("/api/research/threads/{thread_id}/pin")
def research_pin(thread_id: str, who: Who):
    r = research()
    _mine(r, thread_id, who)
    return r.pin(thread_id, True)


@app.delete("/api/research/threads/{thread_id}/pin")
def research_unpin(thread_id: str, who: Who):
    r = research()
    _mine(r, thread_id, who)
    return r.pin(thread_id, False)


@app.delete("/api/research/threads/{thread_id}")
def research_delete(thread_id: str, who: Who):
    """删掉这段对话，找不回来。运行中返回 409。"""
    r = research()
    _mine(r, thread_id, who)
    try:
        r.delete(thread_id)
    except RunConflict as e:
        raise HTTPException(409, str(e)) from None
    return {"ok": True}


@app.post("/api/research/threads/{thread_id}/retry")
def research_retry(thread_id: str, who: Who):
    """失败的线程从断开的那一步接着跑；没失败的返回 409；额度用完了 429。"""
    r = research()
    _mine(r, thread_id, who)
    _admit(r, who)
    try:
        return _sse(r.retry(thread_id))
    except (RunConflict, NothingToRetry) as e:
        raise HTTPException(409, str(e)) from None


@app.get("/api/research/threads/{thread_id}/events")
def research_events(thread_id: str, who: Who, after: str | None = None):
    """接上线程当前这次执行的事件流（after 是收到的最后一个事件 id），以 end 事件收尾；断线、刷新后用。"""
    r = research()
    _mine(r, thread_id, who)
    return _sse(r.follow(thread_id, after))


@app.get("/api/research/threads/{thread_id}/runs")
def research_thread_runs(thread_id: str, who: Who):
    """这段对话的每次运行（新的在前）：怎么来的、状态、时间、出错的原因。排查问题用，页面不用。"""
    r = research()
    _mine(r, thread_id, who)
    return {"runs": r.thread_runs(thread_id)}


RUNS_LIMIT = 200


@app.get("/api/research/runs")
def research_runs(who: Who, status: str | None = None, limit: int = 50):
    """所有对话里最近的运行，status 只看某个状态（比如 error）。排查问题用，页面不用；只有管理员能看（跨所有人的对话）。"""
    if who.tier != ADMIN:
        raise HTTPException(403, "只有管理员能看")
    if status is not None and status not in STATUSES:
        raise HTTPException(400, f"status 只能是 {'、'.join(STATUSES)}")
    if not 1 <= limit <= RUNS_LIMIT:
        raise HTTPException(400, f"limit 在 1 到 {RUNS_LIMIT} 之间")
    return {"runs": research().recent_runs(status, limit)}


@app.get("/api/research/threads/{thread_id}/state")
def research_state(thread_id: str, who: Who):
    return _mine(research(), thread_id, who)


@app.get("/api/research/threads/{thread_id}/files/{file_id}")
def research_file(thread_id: str, file_id: str, who: Who):
    """下载这段对话里用户发的文件（原件）。页面带着登录信息取（fetch 成 blob 再存），所以这里认人：不是这个人的对话一律 404。"""
    r = research()
    _mine(r, thread_id, who)
    got = r.file_original(thread_id, file_id)
    if got is None:
        raise HTTPException(404, "没有这个文件")
    path, row = got
    mime = mimetypes.guess_type(row.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=mime, filename=row.name, content_disposition_type="attachment",
                        headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"})


@app.get("/api/research/threads/{thread_id}/files/{file_id}/parts")
def research_file_parts(thread_id: str, file_id: str, who: Who):
    """右栏预览用户发的文件：抽出来的文字，按段（PDF 一页、PPT 一张、表格一块，段名是“第 3 页”这样的），太长的只给前面一部分（more）。
    PDF 页面上不用这个：桌面上看原件（浏览器自己的 PDF 查看器），手机上直接下载。"""
    r = research()
    _mine(r, thread_id, who)
    got = r.file_preview(thread_id, file_id)
    if got is None:
        raise HTTPException(404, "没有这个文件")
    row, parts, more = got
    return {"id": row.id, "name": row.name, "type": row.type, "parts": [{"title": p.title, "text": p.text} for p in parts], "more": more}


@app.get("/api/research/threads/{thread_id}/images/{image_id}")
def research_image(thread_id: str, image_id: str):
    """对话里的图（画的、用户发的）：图存在 image-service 里，后端按编号经网关取来转给页面（不暴露服务地址）。
    只给这段对话的图（按文件表认，见 ResearchRunner.image）。
    页面用 <img src> 取，带不了登录信息，所以不认人：知道对话 id 和图的编号（都是随机的）就能取。
    内容不会变，浏览器可以一直缓存；对话删了图也跟着删。网关、服务出问题回 502，页面显示加载失败、可以重试。"""
    try:
        got = research().image(thread_id, image_id) if valid_thread_id(thread_id) else None
    except GatewayError as e:
        logger.warning("取图失败：%s", e)
        raise HTTPException(502, "图片暂时取不到") from None
    if got is None:
        raise HTTPException(404, "没有这张图")
    data, mime = got
    return Response(data, media_type=mime,
                    headers={"Cache-Control": "private, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"})


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


@app.get("/api/health")
async def health():
    """探活（容器健康检查、uptime-kuma）：进程在、能回话就行，不碰数据库和外部服务。version 是部署的提交号，看部署上去的是哪一版。"""
    return {"ok": True, "version": config.VERSION or None}


class Pages(StaticFiles):
    """部署时的前端页面。有这个文件就给文件；地址里没有扩展名的（/chat/new、/chat/<线程 id>）给 index.html，由前端按地址选页面，
    刷新也打得开。/api 下没有的接口、缺了的资源文件照常 404：发版后旧页面要旧资源，拿到的不能是 HTML。
    index.html 不让浏览器直接用缓存：发了新版，刷新就拿到新的资源文件名；资源文件名带内容哈希，可以一直缓存。"""

    async def get_response(self, path: str, scope) -> Response:
        if path.split("/")[0] == "api":
            raise StarletteHTTPException(404)
        try:
            resp = await super().get_response(path, scope)
        except StarletteHTTPException as e:
            if e.status_code != 404 or PurePosixPath(path).suffix:
                raise
            resp = await super().get_response("index.html", scope)
        if Path(getattr(resp, "path", "")).name == "index.html":
            resp.headers["Cache-Control"] = "no-cache"
        return resp


# 放在最后：前面的接口都没对上，才轮到页面
if config.STATIC_DIR:
    app.mount("/", Pages(directory=config.STATIC_DIR, html=True), name="pages")
