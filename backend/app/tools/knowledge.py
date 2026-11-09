"""查知识库的工具 search_knowledge（2026-11-09 Sean 定，见 app.knowledge）。

- 只给主 agent，只在这段对话第一轮时知识库里有文件才有：工具列表里一直放着（前缀缓存），没有文件的对话调模型时去掉（见 agent.ModelSelect）。
  说明里列出文件名（for_files）：第一轮取的那份（state 的 kb_files，见 agent.TurnMiddleware），后面沿用；之后新传的这段对话里照样查得到。
- 结果：开头一句“最接近的不一定相关，没有就直说”，每块写清文件名、章节、第几段、文件编号、来源 id。想看前后文用 read_file 按段读。
  来源 id 按“文件的第几段”算（sources.kb_url），和网页来源一起编号；查到的块记在 artifact 里，页面拿它画来源。
- 收钱照 models.toml 的 [tools]（和 search_web 一个价），没查成的不收（ToolChargeMiddleware）。
- 日志里只记编号，不记查到的正文（Fusion 踩过：文档原文进了工具调用日志）。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, ToolException, tool
from pydantic import BaseModel, Field

from app.graph.research import prompts
from app.graph.research.sources import KB_TOOL, kb_url, known_in, source_id, url_key

log = logging.getLogger(__name__)

LISTED_FILES = 50  # 说明里最多列这么多个文件名


class SearchKnowledgeArgs(BaseModel):
    query: str = Field(description=prompts.render("tools.search_knowledge.query"))


def description(files: list[str]) -> str:
    return prompts.render("tools.search_knowledge.description", files=files[:LISTED_FILES], more=max(0, len(files) - LISTED_FILES)).strip()


def for_files(t: BaseTool, files: list[str]) -> BaseTool:
    """这段对话用的那份：说明里列着第一轮时知识库里的文件。"""
    return t.model_copy(update={"description": description(files)})


def make_search_knowledge_tool():
    @tool(KB_TOOL, description=description([]), args_schema=SearchKnowledgeArgs, response_format="content_and_artifact")
    async def search_knowledge(runtime: ToolRuntime, query: str) -> tuple[str, dict]:
        q = query.strip()
        if not q:
            raise ToolException(prompts.render("tools.search_knowledge.empty_query"))
        kb, owner = getattr(runtime.context, "kb", None), getattr(runtime.context, "owner", None)
        if kb is None or not owner:
            raise ToolException(prompts.render("tools.search_knowledge.unavailable"))
        try:
            hits = await kb.search(owner, q)
        except Exception as e:  # noqa: BLE001  库、向量接口出问题：告诉模型查不了，这一轮照常
            log.warning("查知识库没查成：%s: %s", type(e).__name__, str(e)[:200])
            raise ToolException(prompts.render("tools.search_knowledge.unavailable")) from None
        when = datetime.now(UTC).isoformat(timespec="seconds")
        if not hits:
            return prompts.render("tools.search_knowledge.no_hits", query=q).strip(), {"retrieved_at": when, "query": q, "hits": []}
        known = known_in(runtime.state)
        items = []
        for h in hits:
            url = kb_url(h.file_id, h.part)
            sid = known[url_key(url)] = source_id(url, known)
            items.append({"id": sid, "url": url, "file_id": h.file_id, "name": h.name, "part": h.part, "title": h.title, "content": h.content})
        log.info("查知识库：%d 块 %s", len(items), [(h["file_id"], h["part"]) for h in items])
        return prompts.render("tools.search_knowledge.results", query=q, hits=items).strip(), {"retrieved_at": when, "query": q, "hits": items}

    search_knowledge.handle_tool_error = True
    return search_knowledge
