"""以前的对话补登记图片（2026-11-09，工作区那一批）：用户发的、画好的图登记进文件表（见 FileService.add_image）。

页面取图按文件表认是不是这段对话的，以前的图没登记就取不到，所以上线后马上跑一次（本机库、dev 库各一次）。
可以重跑：登记过的跳过。只看每段对话最后存下的状态：没跑完那一轮里画的图（还没写进状态）不在里面。

    uv run python -m app.files.backfill_images            # 只数，不写
    uv run python -m app.files.backfill_images --write    # 写进去
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from langchain_core.messages import BaseMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app import config
from app.api.run_store import PostgresRunStore
from app.files.service import FileService
from app.files.store import PostgresFileStore
from app.graph.research import history
from app.graph.research.graph import make_serde, postgres_pool, thread_ids
from app.graph.research.state import AttachedImage, GeneratedImage

log = logging.getLogger(__name__)


async def register_images(files: FileService, thread_id: str, owner: str, messages: list[BaseMessage], *, write: bool) -> tuple[int, int]:
    """一段对话里的图登记进文件表：返回 (还没登记的几张, 一共几张)；write 才真的写。画的图记在画它的那一轮（那一轮用户那句话的编号）下。"""
    turn: str | None = None
    missing = total = 0
    for m in messages:
        if history.is_request(m):
            turn = m.id
        found: list[tuple[AttachedImage | GeneratedImage, bool]] = [(a, False) for a in history.attached_images(m)]
        if (img := history.image_of(m)) and img.stored:
            found.append((img, True))
        for img, generated in found:
            total += 1
            if await files.store.get(img.id) is not None:
                continue
            missing += 1
            if write:
                await files.add_image(img.id, owner=owner, workspace_id=thread_id, generated=generated, turn_id=turn if generated else None,
                                      mime_type=img.mime_type, width=img.width, height=img.height)
    return missing, total


async def main(write: bool) -> None:
    pool = await postgres_pool(config.DATABASE_URL)
    try:
        saver, runs, store = AsyncPostgresSaver(pool, serde=make_serde()), PostgresRunStore(pool), PostgresFileStore(pool)
        await store.setup()
        files = FileService(store, config.FILES_DIR)
        threads = await thread_ids(saver)
        missing = total = touched = 0
        for tid in threads:
            got = await saver.aget_tuple({"configurable": {"thread_id": tid, "checkpoint_ns": ""}})
            messages = ((got.checkpoint.get("channel_values") or {}).get("messages") or []) if got else []
            m, t = await register_images(files, tid, await runs.owner_of(tid) or "", messages, write=write)
            missing, total, touched = missing + m, total + t, touched + (m > 0)
        verb = "登记了" if write else "要登记"
        print(f"{len(threads)} 段对话，{total} 张图；{verb} {missing} 张（{touched} 段对话）")
    finally:
        await pool.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser(description="以前的对话补登记图片")
    ap.add_argument("--write", action="store_true", help="写进去（不加只数）")
    asyncio.run(main(ap.parse_args().write))
