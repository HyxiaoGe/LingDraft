"""模型写文件（Artifact，2026-11-08 Sean 定，见 docs/工作区和Artifact方案.md）：create_file 新建，edit_file 出新一版。

- 文档（Markdown）、网页（HTML）、代码文件三种，后端一样存，只是页面上显示得不一样。每一版是文件表里的一行、存全文（见 FileService.write）；
  各版用第一版的编号串起来，模型认的是它，读（read_file）、改的总是这段对话里最新的一版。版本号按对话里的消息数（history.written_files）。
- 一轮里一份文件只出一版（2026-11-08 Sean 定）：模型在同一轮里又改了这一轮写的那版（比如写完嫌长自己压一遍），新的顶替它、版本号不变，
  那一版作废删掉。用户看到的版本是一轮一轮的结果，中间稿不算。
- 什么时候写由模型判断（系统提示词的 agent.files）。改用片段替换（每段 old_text 要恰好出现一次，不做模糊匹配）或者整篇重写。
- 输出被截断（finish_reason=length）的那次调用不存：截断时参数多半还能补成合法的 JSON（LangChain 按半截解析），存下来就是半篇文档。
- 用户发的文件只读（Sean 定）：edit_file 给的是它们的编号时说明只能另写一份。
- 不收钱（models.toml 的 [tools] 没定价）。只给主 agent：子 agent 的结果交回主 agent，由主 agent 写。
没写成的结果 status=error，artifact 的 failure 是原因（页面按 history.WRITE_FAILURES 说人话），给模型的是英文说明。
"""

from __future__ import annotations

import logging
from typing import Literal

from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.files import artifacts
from app.graph.research import prompts
from app.graph.research.history import CREATE_TOOL, EDIT_TOOL, last_human, owned_file_ids, written_files, written_of
from app.graph.research.state import WrittenFile

log = logging.getLogger(__name__)

CUT_REASONS = ("length", "max_tokens")  # 输出到了上限：OpenAI 兼容接口的 finish_reason，Anthropic 的 stop_reason


class CreateFileArgs(BaseModel):
    # 参数的先后就是模型多半写出来的先后：标题、类型在前，页面边写边显示时先知道是哪份
    title: str = Field(description=prompts.render("tools.create_file.title"))
    type: Literal["markdown", "html", "code"] = Field(description=prompts.render("tools.create_file.type"))
    content: str = Field(description=prompts.render("tools.create_file.content"))


class EditItem(BaseModel):
    old_text: str = Field(description=prompts.render("tools.edit_file.old_text"))
    new_text: str = Field(description=prompts.render("tools.edit_file.new_text"))


class EditFileArgs(BaseModel):
    file_id: str = Field(description=prompts.render("tools.edit_file.file_id"))
    change_note: str = Field(description=prompts.render("tools.edit_file.change_note"))
    title: str | None = Field(default=None, description=prompts.render("tools.edit_file.title"))
    edits: list[EditItem] | None = Field(default=None, description=prompts.render("tools.edit_file.edits"))
    content: str | None = Field(default=None, description=prompts.render("tools.edit_file.content"))


def cut_off(m: BaseMessage) -> bool:
    """这条模型消息是不是到了最长输出被截断的。"""
    meta = m.response_metadata or {}
    return meta.get("finish_reason") in CUT_REASONS or meta.get("stop_reason") in CUT_REASONS


def _issuer(messages: list[BaseMessage], call_id: str) -> AIMessage | None:
    return next((m for m in reversed(messages) if isinstance(m, AIMessage) and any(tc.get("id") == call_id for tc in m.tool_calls)), None)


def _was_cut(messages: list[BaseMessage], call_id: str) -> bool:
    """这次调用在被截断的那条消息里，而且是最后一个调用（截断只会截在最后一个；前面几个是完整的）。"""
    m = _issuer(messages, call_id)
    return m is not None and cut_off(m) and bool(m.tool_calls) and m.tool_calls[-1].get("id") == call_id


def _fail(runtime: ToolRuntime, name: str, code: str, key: str, **kw) -> ToolMessage:
    return ToolMessage(content=prompts.render(f"tools.write_file.{key}", **kw), name=name, tool_call_id=runtime.tool_call_id or "",
                       status="error", artifact={"failure": code})


def _where(runtime: ToolRuntime) -> tuple[object | None, str, str | None]:
    """文件服务、这段对话归谁、对话编号（工作区）。"""
    ctx = runtime.context
    tid = getattr(ctx, "thread_id", None) or ((runtime.config or {}).get("configurable") or {}).get("thread_id")
    return getattr(ctx, "files", None), getattr(ctx, "owner", None) or "", tid


async def _save(runtime: ToolRuntime, name: str, *, title: str, kind: str, content: str, note: str = "",
                prev: WrittenFile | None = None) -> ToolMessage:
    messages = runtime.state.get("messages", [])
    service, owner, workspace = _where(runtime)
    if service is None or not workspace:
        return _fail(runtime, name, "unavailable", "unavailable")
    if not content.strip():
        return _fail(runtime, name, "empty", "empty")
    if len(content) > artifacts.MAX_CHARS:
        return _fail(runtime, name, "too_long", "too_long", chars=len(content), limit=artifacts.MAX_CHARS)
    i = last_human(messages)
    turn = (messages[i].id if i >= 0 else None) or ""
    # 这一轮已经写过这份（上一版是这一轮写的）：顶替那一版，版本号、上一版不变；改了什么接在那一版后面（这一轮新建的还是第 1 版，不记）
    redo = prev is not None and any(getattr(written_of(m), "id", None) == prev.id for m in messages[i + 1:])
    version, prev_id = (prev.version + 1 if prev else 1), (prev.id if prev else None)
    if redo:
        assert prev is not None
        before = await service.get(prev.id, owner)  # type: ignore[attr-defined]
        version, prev_id = prev.version, before.prev_id if before else None
        note = "" if prev.version == 1 else "；".join(dict.fromkeys(n for n in (prev.note, note) if n))[:200]
    try:
        row = await service.write(owner=owner, workspace_id=workspace, turn_id=turn, title=title, kind=kind, content=content, note=note,
                                  prev_id=prev_id, series_id=prev.series if prev else None, version=version)
    except Exception:
        log.exception("存模型写的文件出错：%s", workspace)
        return _fail(runtime, name, "unavailable", "unavailable")
    if redo:
        assert prev is not None
        try:
            await service.discard(prev.id)  # type: ignore[attr-defined]
        except Exception:  # 没删掉只是多一行没人指着的，重新回答、删对话时按轮、按对话一起删
            log.exception("删作废的那一版出错：%s", prev.id)
    w = WrittenFile(id=row.id, series=row.series_id or row.id, version=row.version, title=title, type=kind, name=row.name,  # type: ignore[arg-type]
                    lang=row.meta.get("lang", ""), chars=len(content), note=note)
    return ToolMessage(content=prompts.render("tools.write_file.saved", title=title, version=w.version, id=w.series, chars=w.chars,
                                              edited=prev is not None, redo=redo),
                       name=name, tool_call_id=runtime.tool_call_id or "", artifact={"file": w.model_dump()})


def make_write_tools() -> list:
    @tool(CREATE_TOOL, description=prompts.render("tools.create_file.description"), args_schema=CreateFileArgs)
    async def create_file(runtime: ToolRuntime, title: str, type: str, content: str) -> ToolMessage:  # noqa: A002  参数名就是 type
        if _was_cut(runtime.state.get("messages", []), runtime.tool_call_id or ""):
            return _fail(runtime, CREATE_TOOL, "cut_off", "cut_off")
        title = artifacts.clean_title(title)
        if not title:
            return _fail(runtime, CREATE_TOOL, "empty", "no_title")
        return await _save(runtime, CREATE_TOOL, title=title, kind=type, content=content)

    @tool(EDIT_TOOL, description=prompts.render("tools.edit_file.description"), args_schema=EditFileArgs)
    async def edit_file(runtime: ToolRuntime, file_id: str, change_note: str, title: str | None = None, edits: list[EditItem] | None = None,
                        content: str | None = None) -> ToolMessage:
        messages = runtime.state.get("messages", [])
        if _was_cut(messages, runtime.tool_call_id or ""):
            return _fail(runtime, EDIT_TOOL, "cut_off", "cut_off")
        fid = file_id.strip()
        prev = written_files(messages).get(fid)
        if prev is None:
            if fid in owned_file_ids(messages):
                return _fail(runtime, EDIT_TOOL, "read_only", "read_only")
            return _fail(runtime, EDIT_TOOL, "not_found", "not_found", id=fid)
        if (content is None) == (not edits):
            return _fail(runtime, EDIT_TOOL, "no_match", "edits_or_content")
        service, _, _ = _where(runtime)
        base = await service.text(prev.id) if service is not None else None  # type: ignore[attr-defined]
        if base is None:
            return _fail(runtime, EDIT_TOOL, "unavailable", "unavailable")
        if content is None:
            try:
                content = artifacts.apply_edits(base, [artifacts.Edit(e.old_text, e.new_text) for e in edits or []])
            except artifacts.EditError as e:
                key = "edit_empty" if e.count < 0 else "edit_missing" if e.count == 0 else "edit_ambiguous"
                return _fail(runtime, EDIT_TOOL, "no_match", key, n=e.n, count=e.count, title=prev.title)
        if content == base and not (title and artifacts.clean_title(title) != prev.title):
            return _fail(runtime, EDIT_TOOL, "no_change", "no_change")
        new_title = artifacts.clean_title(title or "") or prev.title
        note = " ".join(change_note.split())[:200]
        return await _save(runtime, EDIT_TOOL, title=new_title, kind=prev.type, content=content, note=note, prev=prev)

    for t in (create_file, edit_file):
        t.handle_tool_error = True
    return [create_file, edit_file]
