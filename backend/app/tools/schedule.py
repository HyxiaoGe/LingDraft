"""定时任务的工具（见 app.scheduled）：用户在对话里说一句话，模型建、改、删、列。工具说明在 prompts/tools_schedule.toml。

- 建的任务挂在这段对话上：到点了在这里多跑一轮。交代由模型写成完整的一份（到点时没人在场，问不了）。
- 只给登录用户的主 agent：游客、没归谁的老对话、命令行和评测（没有任务服务）调模型时去掉（见 agent.can_schedule），硬调了说不能用。
- 任务只认这个人自己的，哪段对话里建的都能改、能删。
- 校验都在 ScheduledTasks 里，不过的按 TaskError 的 code 换成给模型看的话（tools.scheduled_task.<code>）。不收钱（models.toml 的 [tools] 没定价）。
- 定时任务触发的那一轮，发给模型的那份交代前面加一段说明（with_run_notes）。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.messages import BaseMessage
from langchain_core.tools import ToolException, tool
from pydantic import BaseModel, Field

from app import config
from app.graph.research import history, prompts
from app.graph.research.history import CREATE_TASK_TOOL, DELETE_TASK_TOOL, LIST_TASKS_TOOL, UPDATE_TASK_TOOL
from app.identity import USER, is_guest_sub
from app.scheduled.rule import CHINA_TZ
from app.scheduled.service import PROMPT_MAX, TITLE_MAX, ScheduledTasks, TaskError
from app.scheduled.store import ACTIVE, PAUSED, Task

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
SENT_AT = "sent_at"  # 用户消息 additional_kwargs 里的发送时间（见 research_runner）：定时触发的就是触发的时间


def _limits() -> dict[str, int]:
    return {"max_tasks": config.SCHEDULED_TASKS_MAX, "title_max": TITLE_MAX, "prompt_max": PROMPT_MAX}


def _text(key: str) -> str:
    return prompts.render(key, **_limits())


class CreateTaskArgs(BaseModel):
    title: str = Field(description=_text(f"tools.{CREATE_TASK_TOOL}.title"))
    prompt: str = Field(description=_text(f"tools.{CREATE_TASK_TOOL}.prompt"))
    cron: str | None = Field(default=None, description=_text(f"tools.{CREATE_TASK_TOOL}.cron"))
    run_at: str | None = Field(default=None, description=_text(f"tools.{CREATE_TASK_TOOL}.run_at"))


class UpdateTaskArgs(BaseModel):
    task_id: str = Field(description=_text(f"tools.{UPDATE_TASK_TOOL}.task_id"))
    title: str | None = Field(default=None, description=_text(f"tools.{UPDATE_TASK_TOOL}.title"))
    prompt: str | None = Field(default=None, description=_text(f"tools.{UPDATE_TASK_TOOL}.prompt"))
    cron: str | None = Field(default=None, description=_text(f"tools.{UPDATE_TASK_TOOL}.cron"))
    run_at: str | None = Field(default=None, description=_text(f"tools.{UPDATE_TASK_TOOL}.run_at"))
    paused: bool | None = Field(default=None, description=_text(f"tools.{UPDATE_TASK_TOOL}.paused"))


class DeleteTaskArgs(BaseModel):
    task_id: str = Field(description=_text(f"tools.{DELETE_TASK_TOOL}.task_id"))


def when(at: datetime | None) -> str | None:
    """给模型看的时间：北京时间，带星期。"""
    if at is None:
        return None
    at = at.astimezone(CHINA_TZ)
    return f"{at:%Y-%m-%d %H:%M} ({WEEKDAYS[at.weekday()]})"


def how_far(at: datetime | None, now: datetime) -> str | None:
    """下一次是今天、明天还是几天后（按北京时间的日子算）。模型只知道今天几号、不知道现在几点：
    光给时间，它会把今天还没到点的说成明天（2026-11-13 真实模型验证时遇到）。"""
    if at is None:
        return None
    days = (at.astimezone(CHINA_TZ).date() - now.astimezone(CHINA_TZ).date()).days
    return "today" if days <= 0 else "tomorrow" if days == 1 else f"in {days} days"


def for_model(t: Task, now: datetime, here: str | None = None) -> dict[str, Any]:
    """一个任务给模型看的样子。now 是现在；here 是现在这段对话：任务是不是跑在这里。"""
    out: dict[str, Any] = {"task_id": t.id, "title": t.title, "status": t.status}
    out.update({"cron": t.cron} if t.cron else {"run_at": when(t.once_at)})
    out["next_run"] = when(t.next_at)
    if t.next_at:
        out["next_run_is"] = how_far(t.next_at, now)
    if t.last_at:
        out.update(last_run=when(t.last_at), last_result=t.last_status)
    if here is not None:
        out["runs_in_this_conversation"] = t.thread_id == here
    return out


def _service(runtime: ToolRuntime) -> tuple[ScheduledTasks, str, Any]:
    ctx = runtime.context
    tasks, owner = getattr(ctx, "tasks", None), getattr(ctx, "owner", None)
    if tasks is None or not owner or is_guest_sub(owner):
        raise ToolException(_text("tools.scheduled_task.unavailable"))
    return tasks, owner, ctx


def _failed(e: TaskError) -> ToolException:
    return ToolException(_text(f"tools.scheduled_task.{e.code}"))


def _done(t: Task, here: str | None, tasks: ScheduledTasks) -> tuple[str, dict]:
    now = tasks.now()
    return (json.dumps({**for_model(t, now, here), "now": when(now), "timezone": "Asia/Shanghai"}, ensure_ascii=False), {"task": tasks.view(t)})


def make_schedule_tools() -> list:
    @tool(CREATE_TASK_TOOL, description=_text(f"tools.{CREATE_TASK_TOOL}.description"), args_schema=CreateTaskArgs,
          response_format="content_and_artifact")
    async def create_scheduled_task(runtime: ToolRuntime, title: str, prompt: str, cron: str | None = None,
                                    run_at: str | None = None) -> tuple[str, dict]:
        tasks, owner, ctx = _service(runtime)
        try:  # 这段对话以后删了，任务下次跑时新开一段：是不是工作任务、用哪个模型照这段对话的
            t = await tasks.create(owner, getattr(ctx, "tier", None) or USER, title=title, prompt=prompt, cron=cron, once_at=run_at,
                                   thread_id=ctx.thread_id, work=history.work_mode(runtime.state), option=runtime.state.get("option"))
        except TaskError as e:
            raise _failed(e) from None
        return _done(t, ctx.thread_id, tasks)

    @tool(UPDATE_TASK_TOOL, description=_text(f"tools.{UPDATE_TASK_TOOL}.description"), args_schema=UpdateTaskArgs,
          response_format="content_and_artifact")
    async def update_scheduled_task(runtime: ToolRuntime, task_id: str, title: str | None = None, prompt: str | None = None,
                                    cron: str | None = None, run_at: str | None = None, paused: bool | None = None) -> tuple[str, dict]:
        tasks, owner, ctx = _service(runtime)
        status = None if paused is None else PAUSED if paused else ACTIVE
        try:
            t = await tasks.update(owner, task_id.strip(), title=title, prompt=prompt, cron=cron, once_at=run_at, status=status)
        except TaskError as e:
            raise _failed(e) from None
        return _done(t, ctx.thread_id, tasks)

    @tool(DELETE_TASK_TOOL, description=_text(f"tools.{DELETE_TASK_TOOL}.description"), args_schema=DeleteTaskArgs,
          response_format="content_and_artifact")
    async def delete_scheduled_task(runtime: ToolRuntime, task_id: str) -> tuple[str, dict]:
        tasks, owner, _ = _service(runtime)
        try:
            t = await tasks.delete(owner, task_id.strip())
        except TaskError as e:
            raise _failed(e) from None
        return json.dumps({"deleted": True, "task_id": t.id, "title": t.title}, ensure_ascii=False), {"task": tasks.view(t)}

    # 没有参数的工具不能给空的 args_schema（LangChain 遇到空的就什么都不传，runtime 也不传），参数表只能从签名推；
    # runtime 标成 [Any, Any]：不标的话推出来的 context 是 None 类型，校验参数时每次都告警
    @tool(LIST_TASKS_TOOL, description=_text(f"tools.{LIST_TASKS_TOOL}.description"))
    async def list_scheduled_tasks(runtime: ToolRuntime[Any, Any]) -> str:
        tasks, owner, ctx = _service(runtime)
        now = tasks.now()
        return json.dumps({"tasks": [for_model(t, now, ctx.thread_id) for t in await tasks.of_owner(owner)], "limit": tasks.max_tasks,
                           "now": when(now), "timezone": "Asia/Shanghai"}, ensure_ascii=False)

    out = [create_scheduled_task, update_scheduled_task, delete_scheduled_task, list_scheduled_tasks]
    for t in out:
        t.handle_tool_error = True
    return out


def with_run_notes(messages: list[BaseMessage]) -> list[BaseMessage]:
    """定时任务触发的那几轮，发给模型的那份：交代前面加一段说明（哪个任务、什么时候触发的、没人在场，见 prompts 的 scheduled.run）。
    存下来的消息不动（就是任务里那份交代）；说明只按消息上记的标题和触发时间写，每次调模型拼出来的都一样，前缀缓存接得上。"""
    out: list[BaseMessage] = []
    for m in messages:
        mark = history.scheduled_of(m)
        if mark is None or not isinstance(m.content, str):
            out.append(m)
            continue
        try:
            at = when(datetime.fromisoformat(m.additional_kwargs.get(SENT_AT, "")))
        except ValueError:
            at = None
        note = prompts.render("scheduled.run", title=mark.get("title") or "", at=at or "the scheduled time")
        out.append(m.model_copy(update={"content": note + m.content}))
    return out
