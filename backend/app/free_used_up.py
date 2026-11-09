"""免费额度用完的模型记在库里（free_used_up 表），重启以后照样不用。

只记在进程内存里时，每次重启（每次部署都会）以后，Auto 判为简单的对话又会先挑到用完的那个：回答开头先显示它，撞了额度用完再换成备用
（2026-11-09 Sean 看到模型名闪一下），这段对话也就一直用备用，用不上后面还能用的免费额度了。
按（模型, 到期日）记：续了额度、在 models.toml 里改了到期日的重新能用。只有对话存在 Postgres 里的才记（测试的内存存储不记）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from app import costs
from app.model_catalog import Catalog, Free

log = logging.getLogger(__name__)

Exec = Callable[[str, tuple], Awaitable[list[dict]]]

_DDL = "CREATE TABLE IF NOT EXISTS free_used_up (model text NOT NULL, until date NOT NULL, at timestamptz NOT NULL, PRIMARY KEY (model, until))"


async def attach(catalog: Catalog, exec_: Exec) -> None:
    """起服务时：读出以前记下的，交给 catalog；之后头一次报用完的，在当前的事件循环（对话的）上写一行，不等它写完。
    报用完的地方有在事件循环里的（主 agent、子 agent），也有在别的线程里的（起标题这些内部用途），所以都交回这个循环去写。"""
    await exec_(_DDL, ())
    known = {(r["model"], r["until"]) for r in await exec_("SELECT model, until FROM free_used_up", ())}
    loop = asyncio.get_running_loop()

    def save(f: Free) -> None:
        sql = "INSERT INTO free_used_up (model, until, at) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING"
        done = asyncio.run_coroutine_threadsafe(exec_(sql, (f.model, f.until, costs.now())), loop)

        def check(d) -> None:
            if not d.cancelled() and d.exception() is not None:
                log.warning("免费额度用完没记进库（这次进程里照样不用）：%s: %r", f.model, d.exception())
        done.add_done_callback(check)

    if marked := catalog.track_used_up(known, save):
        log.info("免费额度已经用完的（库里记的）：%s", "、".join(sorted(marked)))
