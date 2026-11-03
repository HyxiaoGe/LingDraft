"""生图：经网关调 image-service 出图，取回来存进灵稿自己的数据目录，页面从灵稿后端取（见 api.main）。照 Fusion 的做法：
- 模型只拿到画成没画成，以及这张图的编号；拿不到也传不进任何图片地址。
- 改图时按编号在这段对话里找回原图在 image-service 里的地址，当参考图交回去；找不到不算画了一次。
- 生图要花钱，一轮最多画 MAX_IMAGES_PER_TURN 次，不自动重试。
图片文件按对话分目录放，删对话时整个目录删掉。
"""

from __future__ import annotations

import io
import math
import re
import shutil
import uuid
from pathlib import Path
from typing import Literal, get_args

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from PIL import Image, UnidentifiedImageError

from app.graph.research.history import IMAGE_TOOL, image_of, last_human
from app.graph.research.state import GeneratedImage

# image-service 的 Gemini 备用模型都接受的比例；Seedream 接受 1:16 到 16:1 的任意比例，限在这组里才能切到备用模型（照 Fusion）
AspectRatio = Literal["1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"]
ASPECT_RATIOS: tuple[str, ...] = get_args(AspectRatio)
# 画质档：standard 是 Seedream 5.0 Flash，high 是 Seedream 5.0 Pro；不填用 image-service 的默认模型
Quality = Literal["standard", "high"]
MAX_PROMPT_CHARS = 1000
MAX_STYLE_CHARS = 60
MAX_REFERENCE_SUBJECT_CHARS = 80
MAX_IMAGES_PER_TURN = 3
_ID = re.compile(r"[0-9a-f]{12}")
_FILE = re.compile(r"[0-9a-f]{12}\.(?:png|jpg|webp)")
_THREAD = re.compile(r"[0-9A-Za-z_-]{1,64}")
_FORMATS = {"PNG": ("png", "image/png"), "JPEG": ("jpg", "image/jpeg"), "WEBP": ("webp", "image/webp")}



class ImageStore:
    """生成的图：<root>/<对话 id>/<图片编号>.<扩展名>。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _dir(self, thread_id: str) -> Path:
        if not _THREAD.fullmatch(thread_id):
            raise ValueError(f"对话 id 不对：{thread_id!r}")
        return self.root / thread_id

    def save(self, thread_id: str, name: str, data: bytes) -> None:
        d = self._dir(thread_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(data)

    def path(self, thread_id: str, name: str) -> Path | None:
        """页面来取的图；名字不对、不存在都是 None。"""
        if not _THREAD.fullmatch(thread_id) or not _FILE.fullmatch(name):
            return None
        p = self._dir(thread_id) / name
        return p if p.is_file() else None

    def remove(self, thread_id: str, names: list[str]) -> None:
        for name in names:
            if p := self.path(thread_id, name):
                p.unlink(missing_ok=True)

    def drop(self, thread_id: str) -> None:
        if _THREAD.fullmatch(thread_id):
            shutil.rmtree(self._dir(thread_id), ignore_errors=True)


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def valid_id(image_id: str) -> bool:
    return bool(_ID.fullmatch(image_id))


def find_source(messages: list[BaseMessage], image_id: str) -> tuple[GeneratedImage, str] | None:
    """这段对话里编号是 image_id 的图，和它在 image-service 里的地址（改图时交回去）。"""
    for m in messages:
        img = image_of(m)
        if img and img.id == image_id and isinstance(url := m.artifact.get("source_url"), str):
            return img, url
    return None


def attempts_before(messages: list[BaseMessage], call_id: str | None) -> int:
    """这一轮里排在这次调用前面、真去画了的生图调用有几次（没画成也算，照 Fusion：发出去就可能花了钱）。
    参数不对、找不到要改的图、超了次数的没去画，不算。同一条模型消息里并行的几次还没有结果，都算去画了：宁可少画一张，不多花钱。"""
    turn = messages[last_human(messages) + 1:]
    results = {m.tool_call_id: m for m in turn if isinstance(m, ToolMessage)}
    n = 0
    for m in turn:
        if not isinstance(m, AIMessage):
            continue
        for tc in m.tool_calls:
            if tc["name"] != IMAGE_TOOL:
                continue
            if tc.get("id") == call_id:
                return n
            r = results.get(tc.get("id"))
            if r is None or (isinstance(r.artifact, dict) and r.artifact.get("attempted")):
                n += 1
    return n


def closest_aspect_ratio(width: int, height: int) -> str:
    """改图时沿用原图的形状：取支持的比例里和原图最接近的（照 Fusion）。"""
    if width <= 0 or height <= 0:
        return "1:1"
    target = math.log(width / height)

    def distance(ratio: str) -> float:
        w, h = ratio.split(":")
        return abs(math.log(int(w) / int(h)) - target)

    return min(ASPECT_RATIOS, key=distance)


def inspect_image(data: bytes) -> tuple[str, str, int, int]:
    """按内容认图片：(扩展名, MIME 类型, 宽, 高)。不是 PNG / JPEG / WebP 抛 ValueError。"""
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt, (w, h) = img.format, img.size
    except (UnidentifiedImageError, OSError) as e:
        raise ValueError("不是图片") from e
    if fmt not in _FORMATS or w <= 0 or h <= 0:
        raise ValueError(f"不支持的图片格式：{fmt}")
    ext, mime = _FORMATS[fmt]
    return ext, mime, w, h
