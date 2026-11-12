"""定时任务的建、改、删、列（接口和模型的工具都走这里，见 api.main、app.tools.schedule），和每次到点以后怎么记（运行器调，见 api.research_runner）。

校验不过抛 TaskError，code 是哪一种：接口按它换成给人看的话，工具按它换成给模型看的话（这里的 message 只进日志）。
任务只认自己的：不是这个人的和没有一样，都是 NOT_FOUND。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app import config
from app.scheduled import rule
from app.scheduled.rule import CHINA_TZ, BadRule
from app.scheduled.store import (ACTIVE, BLOCKED, BY_FAILURES, BY_USER, DONE, FAILED, LOST, NO_CREDITS, OK, PAUSED, RUNNING, STOPPED, Task,
                                 TaskStore, new_id)

log = logging.getLogger(__name__)

TITLE_MAX = 40
PROMPT_MAX = 4000
# TaskError 的 code（rule 的四种照原样用）
BAD_TITLE, BAD_PROMPT, NO_SCHEDULE, IN_THE_PAST, TOO_MANY, NOT_FOUND, BAD_STATUS = (
    "bad_title", "bad_prompt", "no_schedule", "in_the_past", "too_many", "not_found", "bad_status")


class TaskError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def _now() -> datetime:
    return datetime.now(UTC)


class ScheduledTasks:
    def __init__(self, store: TaskStore, clock: Callable[[], datetime] = _now, max_tasks: int | None = None,
                 max_failures: int | None = None) -> None:
        self.store, self.clock = store, clock
        self.max_tasks = config.SCHEDULED_TASKS_MAX if max_tasks is None else max_tasks
        self.max_failures = config.SCHEDULED_MAX_FAILURES if max_failures is None else max_failures

    def now(self) -> datetime:
        return self.clock()

    # ---- 建、改、删、列 ----

    def _when(self, cron: str | None, once_at: str | datetime | None) -> tuple[str | None, datetime | None, datetime]:
        """校验什么时候跑，返回（cron，只跑一次的时间点，下一次什么时候）。两样只能给一样。"""
        if bool(cron and cron.strip()) == bool(once_at):
            raise TaskError(NO_SCHEDULE)
        now = self.now()
        try:
            if cron and cron.strip():
                expr = rule.normalize(cron)
                return expr, None, rule.next_run(expr, now)
            at = rule.parse_once(once_at) if isinstance(once_at, str) else once_at.astimezone(CHINA_TZ)
        except BadRule as e:
            raise TaskError(e.code, str(e)) from None
        if at <= now:
            raise TaskError(IN_THE_PAST, at.isoformat())
        return None, at, at

    @staticmethod
    def _title(title: str) -> str:
        title = " ".join(title.split())
        if not title or len(title) > TITLE_MAX:
            raise TaskError(BAD_TITLE)
        return title

    @staticmethod
    def _prompt(prompt: str) -> str:
        prompt = prompt.strip()
        if not prompt or len(prompt) > PROMPT_MAX:
            raise TaskError(BAD_PROMPT)
        return prompt

    async def _live(self, owner: str) -> int:
        return sum(1 for t in await self.store.of_owner(owner) if t.status != DONE)

    async def create(self, owner: str, tier: str, *, title: str, prompt: str, cron: str | None = None, once_at: str | datetime | None = None,
                     thread_id: str | None = None, work: bool = False, option: str | None = None) -> Task:
        title, prompt = self._title(title), self._prompt(prompt)
        cron, at, next_at = self._when(cron, once_at)
        if await self._live(owner) >= self.max_tasks:
            raise TaskError(TOO_MANY, str(self.max_tasks))
        now = self.now()
        task = Task(id=new_id(), owner=owner, tier=tier, title=title, prompt=prompt, created_at=now, updated_at=now, cron=cron, once_at=at,
                    thread_id=thread_id, work=work, option=option, next_at=next_at)
        await self.store.add(task)
        log.info("建了定时任务：%s %s %s", task.id, owner, cron or at)
        return task

    async def get(self, owner: str, task_id: str) -> Task:
        t = await self.store.get(task_id)
        if t is None or t.owner != owner:
            raise TaskError(NOT_FOUND, task_id)
        return t

    async def of_owner(self, owner: str) -> list[Task]:
        return await self.store.of_owner(owner)

    async def update(self, owner: str, task_id: str, *, tier: str | None = None, title: str | None = None, prompt: str | None = None,
                     cron: str | None = None, once_at: str | datetime | None = None, status: str | None = None) -> Task:
        """改给到的那几样。status 只能是 active（恢复）、paused（暂停）。改了什么时候跑、恢复，下一次都从现在重新算；
        跑完了的一次性任务给了新的时间就又开着。恢复一个时间已经过了的一次性任务不行（IN_THE_PAST），要一起给新的时间。"""
        t = await self.get(owner, task_id)
        if status not in (None, ACTIVE, PAUSED):
            raise TaskError(BAD_STATUS, status)
        fields: dict[str, Any] = {}
        if tier is not None:
            fields["tier"] = tier
        if title is not None:
            fields["title"] = self._title(title)
        if prompt is not None:
            fields["prompt"] = self._prompt(prompt)
        rescheduled = bool(cron and cron.strip()) or bool(once_at)
        if rescheduled:
            fields["cron"], fields["once_at"], _ = self._when(cron, once_at)
        new_cron, new_once = fields.get("cron", t.cron), fields.get("once_at", t.once_at)
        want = status or (ACTIVE if rescheduled and t.status == DONE else t.status)
        if want == PAUSED:
            fields.update(status=PAUSED, next_at=None)
            if t.status != PAUSED:
                fields["paused_by"] = BY_USER
        elif want == ACTIVE and (rescheduled or t.status != ACTIVE):
            if t.status != ACTIVE and await self._live(owner) - (t.status != DONE) >= self.max_tasks:
                raise TaskError(TOO_MANY, str(self.max_tasks))
            _, _, next_at = self._when(new_cron, new_once)
            fields.update(status=ACTIVE, paused_by="", failures=0, next_at=next_at)
        if not fields:
            return t
        return await self.store.patch(task_id, updated_at=self.now(), **fields) or t

    async def delete(self, owner: str, task_id: str) -> Task:
        t = await self.get(owner, task_id)
        await self.store.delete(task_id)
        log.info("删了定时任务：%s %s", task_id, owner)
        return t

    # ---- 到点以后（运行器调） ----

    def _after(self, t: Task) -> dict[str, Any]:
        """这一次交出去（或者跳过）以后，下一次什么时候：重复的从现在往后数（错过的几次并成这一次）；只跑一次的到此为止。"""
        if t.cron is None:
            return {"status": DONE, "next_at": None}
        return {"next_at": rule.next_run(t.cron, self.now())}

    async def launched(self, t: Task, thread_id: str, *, manual: bool = False) -> Task:
        """这一次交出去了。manual 是用户点的“立即执行”：不动原来定的下一次。"""
        fields = {} if manual else self._after(t)
        return await self.store.patch(t.id, thread_id=thread_id, last_at=self.now(), last_status=RUNNING, **fields) or t

    async def skipped(self, t: Task, why: str) -> Task:
        """这一次没跑（why：NO_CREDITS、BLOCKED）：记一笔，等下一次。"""
        log.info("定时任务这一次没跑：%s %s", t.id, why)
        return await self.store.patch(t.id, last_at=self.now(), last_status=why, **self._after(t)) or t

    async def finished(self, task_id: str, status: str) -> Task | None:
        """交出去的那一次结束了。status：OK、STOPPED、FAILED、LOST（服务重启打断的）。
        没跑成的数着，连着 max_failures 次就停下（paused_by 是 failures），等用户来看；
        还没到这个数、被重启打断的，马上再来一次（被打断的那一轮重来，见 research_runner._ascheduled_input）。"""
        t = await self.store.get(task_id)
        if t is None:
            return None
        fields: dict[str, Any] = {"last_status": status}
        if status in (OK, STOPPED):
            fields["failures"] = 0
        elif status in (FAILED, LOST):
            fields["failures"] = t.failures + 1
            if t.status == PAUSED:
                pass  # 用户已经停了它
            elif fields["failures"] >= self.max_failures:
                fields.update(status=PAUSED, paused_by=BY_FAILURES, next_at=None)
                log.warning("定时任务连着 %d 次没跑成，停下了：%s", fields["failures"], task_id)
            elif status == LOST:
                fields.update(status=ACTIVE, next_at=self.now())
        return await self.store.patch(task_id, **fields)

    # ---- 给页面、给模型看的 ----

    @staticmethod
    def view(t: Task) -> dict[str, Any]:
        def iso(d: datetime | None) -> str | None:
            return d.astimezone(UTC).isoformat(timespec="seconds") if d else None
        return {"id": t.id, "title": t.title, "prompt": t.prompt, "cron": t.cron, "once_at": iso(t.once_at),
                "schedule": rule.describe(t.cron, t.once_at), "status": t.status, "paused_by": t.paused_by, "thread_id": t.thread_id,
                "work": t.work, "model": t.option, "next_at": iso(t.next_at), "last_at": iso(t.last_at), "last_status": t.last_status,
                "created_at": iso(t.created_at), "updated_at": iso(t.updated_at)}


__all__ = ["ACTIVE", "BLOCKED", "DONE", "FAILED", "LOST", "NO_CREDITS", "OK", "PAUSED", "RUNNING", "STOPPED", "ScheduledTasks", "Task", "TaskError"]
