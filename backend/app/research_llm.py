"""调研场景的模型接入层：ResearchPlanner 协议，LiveResearchPlanner（DeepSeek / 豆包，按配置）与 FakeResearchPlanner（离线）。

agent 本身由 app.graph.research.agent 构建，这里提供它要的聊天模型和查资料的工具，以及几个不带工具的快模型步骤：
整理报告草稿、起标题、推荐问题。
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Iterator, Sequence
from datetime import date
from typing import Any, Literal, Protocol
from uuid import UUID

import httpx
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import BaseModel, ConfigDict, Field

from app import config
from app.graph.research import prompts
from app.graph.research.sources import SOURCE_ID, cited
from app.graph.research.state import CONSOLIDATE_RETRIES, Source
from app.tools.gateway import GatewayClient
from app.tools.research_tools import make_research_tools

# ---- 结构化输出 schema（扁平、全部 required，符合 strict 模式） ----


class ClaimDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ref: str = Field(description="k1、k2…")
    statement: str = Field(description="一句话论断")
    kind: Literal["fact", "inference"]
    source_ids: list[str] = Field(description="来源列表里的 id，至少一个")
    confidence: Literal["high", "medium", "low"]


class SentenceDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    claim_ref: str | None = Field(description="这句依据的论断 ref；纯过渡句为 null")


class SectionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    question: str = Field(description="这一节回答的问题")
    sentences: list[SentenceDraft]


class TimeIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(description="来源列表里的 id")
    reason: str = Field(description="一句话：发布时间是什么、为什么不在用户要的时间范围里")


class ConsolidateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sections: list[SectionDraft]
    claims: list[ClaimDraft]
    assumptions: list[str] = Field(description="草稿里没有来源支撑的说法、自行采用的取舍")
    time_issues: list[TimeIssue] = Field(description="论断引用的来源里，发布时间不在用户要的时间范围里的；用户没限定时间或都在范围内则为空")


class Suggestions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    questions: list[str] = Field(description="3 个用户接下来可能会问的问题")


# ---- 协议 ----


class ResearchPlanner(Protocol):
    def agent_model(self) -> BaseChatModel: ...

    def tools(self) -> list: ...

    def consolidate(self, requests: Sequence[str], draft: str, sources: Sequence[Source], user_facts: Sequence[str] = (),
                    report: str | None = None, part: str | None = None) -> ConsolidateResult: ...

    def title(self, text: str) -> str: ...

    def suggest(self, question: str, reply: str, has_report: bool) -> list[str]: ...


def _facts_block(user_facts: Sequence[str]) -> str:
    return prompts.render("shared.user_facts", facts=list(user_facts)) if user_facts else ""


def _report_block(report: str | None) -> str:
    """读过的调研方法的报告结构；没读方法时整段不出现。"""
    return prompts.render("shared.report_guide", report=report) if report else ""


def _sources_block(sources: Sequence[Source]) -> str:
    lines = [f"- {s.id} | {s.url} | {s.title or '（无标题）'} | {s.published_at or '未知'} | {s.excerpt_kind} | {s.excerpt.replace(chr(10), ' ')[:300]}"
             for s in sources]
    return "\n".join(lines) or "（无）"


# ---- 思考计时 ----


class ThinkingTimer(BaseCallbackHandler):
    """记下每次模型调用思考了多久：从第一个思考片段到第一个正文或工具调用片段（没有就到结束），
    写进这条消息的 response_metadata["thinking_ms"]，随消息存进检查点。只有流式调用才有片段，才计时。"""

    def __init__(self) -> None:
        self._mu = threading.Lock()
        self._runs: dict[UUID, list[float | None]] = {}

    def on_chat_model_start(self, serialized, messages, *, run_id: UUID, **kwargs: Any) -> None:
        pass  # 不实现的话回调管理器会退回 on_llm_start，把消息全转成字符串

    def on_llm_new_token(self, token, *, chunk=None, run_id: UUID, **kwargs: Any) -> None:
        msg = getattr(chunk, "message", None)
        if msg is None:
            return
        t = time.monotonic()
        with self._mu:
            r = self._runs.setdefault(run_id, [None, None])
            if msg.additional_kwargs.get("reasoning_content"):
                r[0] = r[0] or t
            elif r[0] is not None and r[1] is None and (msg.content or getattr(msg, "tool_call_chunks", None)):
                r[1] = t

    def on_llm_end(self, response, *, run_id: UUID, **kwargs: Any) -> None:
        with self._mu:
            r = self._runs.pop(run_id, None)
        if not r or r[0] is None or not response.generations or not response.generations[0]:
            return
        msg = getattr(response.generations[0][0], "message", None)
        if msg is not None:  # 就是最后存进 state 的那个消息对象
            msg.response_metadata["thinking_ms"] = int(((r[1] or time.monotonic()) - r[0]) * 1000)

    def on_llm_error(self, error, *, run_id: UUID, **kwargs: Any) -> None:
        with self._mu:
            self._runs.pop(run_id, None)


# ---- 聊天模型工厂 ----


def make_chat_model(provider: str, model: str = "", *, thinking: bool, effort: str = "high", timeout: int = 180,
                    callbacks: list | None = None) -> BaseChatModel:
    """按提供方构造聊天模型。

    DeepSeek 思考模式下：同一轮里的多次工具调用必须把 reasoning_content 原样传回（langchain-openai / langchain-deepseek 都不做，这里补），
    之前几轮的不传；不接受强制 tool_choice（with_structured_output 会设），这里去掉；temperature 无效。
    """
    if provider == "ark":
        from langchain_openai import ChatOpenAI

        if not config.ARK_API_KEY:
            raise RuntimeError("未设置 ARK_API_KEY")
        return ChatOpenAI(
            model=model or config.ARK_MODEL, base_url=config.ARK_BASE_URL, api_key=config.ARK_API_KEY,
            extra_body={"thinking": {"type": "enabled" if thinking else "disabled"}}, timeout=timeout, max_retries=2, temperature=0,
            stream_usage=True, callbacks=callbacks,
        )
    if provider != "deepseek":
        raise ValueError(f"未知的模型提供方：{provider}")
    from langchain_deepseek import ChatDeepSeek

    if not config.DEEPSEEK_API_KEY:
        raise RuntimeError("未设置 DEEPSEEK_API_KEY")

    class ChatDeepSeekRT(ChatDeepSeek):
        def _get_request_payload(self, input_, *, stop=None, **kwargs):
            payload = super()._get_request_payload(input_, stop=stop, **kwargs)
            if thinking and payload.get("tool_choice") not in (None, "auto", "none"):
                payload.pop("tool_choice")
            msgs = self._convert_input(input_).to_messages()
            if len(msgs) == len(payload.get("messages", [])):
                turn = next((i for i in range(len(msgs) - 1, -1, -1) if isinstance(msgs[i], HumanMessage)), -1)
                for m, d in list(zip(msgs, payload["messages"]))[turn + 1:]:
                    if isinstance(m, AIMessage) and m.additional_kwargs.get("reasoning_content") is not None:
                        d["reasoning_content"] = m.additional_kwargs["reasoning_content"]
            return payload

    kw: dict[str, Any] = {"reasoning_effort": effort} if thinking else {"temperature": 0}
    return ChatDeepSeekRT(
        model=model or config.DEEPSEEK_MODEL, api_key=config.DEEPSEEK_API_KEY, api_base=config.DEEPSEEK_BASE_URL,
        extra_body={"thinking": {"type": "enabled" if thinking else "disabled"}}, timeout=timeout, max_retries=2,
        stream_usage=True, callbacks=callbacks, **kw,
    )


# ---- 真实模型 ----


def _structured(llm, schema):
    """DeepSeek 的 json_schema 实际走工具调用；模型偶尔直接回一段文字、不调工具，langchain 这时给 None 而不报错。
    转成异常，交给调用方的重试或失败处理，不让 None 流进后面的步骤。"""

    def parsed(out: dict):
        if out["parsed"] is None:
            raise RuntimeError(f"模型没有按结构返回：{out['parsing_error'] or '没有调用结构化输出'}")
        return out["parsed"]

    return llm.with_structured_output(schema, method="json_schema", strict=True, include_raw=True) | parsed


def consolidate_messages(requests: Sequence[str], draft: str, sources: Sequence[Source], user_facts: Sequence[str] = (),
                         report: str | None = None, part: str | None = None) -> list:
    user = prompts.render("consolidate.user", today=date.today().isoformat(), requests="\n".join(f"- {r}" for r in requests),
                          user_facts=_facts_block(user_facts), guide=_report_block(report), part=part, draft=draft,
                          sources=_sources_block(sources))
    return [("system", prompts.render("consolidate.system")), ("user", user)]


class LiveResearchPlanner:
    def __init__(self, gateway: GatewayClient | None = None) -> None:
        provider, model = config.RESEARCH_PROVIDER, config.RESEARCH_MODEL
        self._agent = make_chat_model(provider, model, thinking=config.RESEARCH_THINKING, effort=config.RESEARCH_EFFORT,
                                      callbacks=[ThinkingTimer()])
        # 快模型步骤不开思考：思考模式下整理一次要 100 秒以上；结构化输出走 json_schema strict，字段与引用再由代码校验
        self._fast = make_chat_model(provider, model, thinking=False, timeout=120)
        self._gateway = gateway or GatewayClient(config.AGENT_TOOLS_URL, config.AGENT_TOOLS_API_KEY, scene="research")
        self._tools = make_research_tools(self._gateway)

    def agent_model(self) -> BaseChatModel:
        return self._agent

    def tools(self) -> list:
        return self._tools

    def consolidate(self, requests: Sequence[str], draft: str, sources: Sequence[Source], user_facts: Sequence[str] = (),
                    report: str | None = None, part: str | None = None) -> ConsolidateResult:
        chain = _structured(self._fast, ConsolidateResult)
        last: Exception | None = None
        for _ in range(CONSOLIDATE_RETRIES):
            try:
                return chain.invoke(consolidate_messages(requests, draft, sources, user_facts, report, part))
            except Exception as e:  # noqa: BLE001
                last = e
        raise RuntimeError(f"整理失败：{last}")

    def title(self, text: str) -> str:
        out = self._fast.invoke([("system", prompts.render("title.system")), ("user", prompts.render("title.user", text=text))])
        return str(out.content).strip().strip("「」“”\"'《》。.").strip()

    def suggest(self, question: str, reply: str, has_report: bool) -> list[str]:
        res = _structured(self._fast, Suggestions).invoke([("system", prompts.render("suggest.system", report=has_report)),
                                                           ("user", prompts.render("suggest.user", question=question, reply=reply))])
        return [q.strip() for q in res.questions if q.strip()]


# ---- 离线假实现 ----


def fake_gateway_client() -> GatewayClient:
    """用 MockTransport 模拟网关：每次搜索给 3 条稳定的候选，读页返回正文（第 3 条读不到）。"""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/search/search":
            q = json.loads(req.content).get("query", "")
            slug = re.sub(r"\W+", "-", q)[:24] or "q"
            hits = [
                {
                    "title": f"关于“{q}”的评测 {i}",
                    "url": f"https://example.test/{slug}/{i}",
                    "description": f"摘要 {i}：{q}",
                    "content": f"长摘要 {i}：围绕“{q}”的说明。",
                    "published_at": f"2026-0{i}-01T00:00:00+00:00",
                }
                for i in (1, 2, 3)
            ]
            return httpx.Response(200, json={"results": hits, "provider": "fake"})
        if req.url.path == "/reader/read":
            url = req.url.params.get("url", "")
            if url.endswith("/3"):
                return httpx.Response(404, json={"detail": {"kind": "page_not_found", "message": "gone", "retryable": False}})
            return httpx.Response(200, json={"title": "正文标题", "content": f"正文：{url} 的内容。" * 5, "provider": "direct"})
        return httpx.Response(404, json={"error": "not_found"})

    return GatewayClient("http://fake-gw", "fake-key", scene="research", transport=httpx.MockTransport(handler))


FAKE_USAGE = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
              "input_token_details": {"cache_read": 60}, "output_token_details": {"reasoning": 5}}
_BAD_ARGS = '{"query": "a" x}'  # 解析不了的参数


class FakeToolModel(BaseChatModel):
    """按这一轮的进度发固定的工具调用：搜 → 读 → （要报告时）写报告 → 文字回复。每次回复都带一段假思考和用量。

    用户的话里的关键字控制特殊路径：
    - “直接答”：不调工具直接回复；“不回复”：最后不给文字；
    - “问一下”：先问一次；“双问”：同一批发两个提问；
    - “报告”“调研”：查完写报告；“连写”：同一批发两个 write_report；
    - “坏JSON”：第一步先发一个参数无法解析的工具调用；“一直坏”：每次都发；
    - “选型”：先读“对比与选型”的调研方法；“错方法”：先读一个不存在的方法；
    - 系统附注里的排除来源、改论断、重写一节：调对应的工具，改完回复。
    """

    bound: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "fake-tool-model"

    def bind_tools(self, tools: Any, **kwargs: Any) -> FakeToolModel:
        return self.model_copy(update={"bound": [getattr(t, "name", getattr(t, "__name__", "")) for t in tools]})

    def _decide(self, messages: list[BaseMessage]) -> tuple[str, list[dict]]:
        i = next(i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], HumanMessage))
        text, turn = str(messages[i].content), messages[i + 1:]
        names = [tc["name"] for m in turn if isinstance(m, AIMessage) for tc in m.tool_calls]
        bad = [tc for m in turn if isinstance(m, AIMessage) for tc in m.invalid_tool_calls]
        hits = _hits(messages)
        ids = [h["id"] for h in hits]
        n = len(messages)

        def call(tool: str, **args) -> tuple[str, list[dict]]:
            return "", [{"name": tool, "args": args, "id": f"call_{tool}_{n}"}]

        if m := re.search(r"要排除的是来源 (s[0-9a-f]+)", text):
            return call("exclude_source", source_id=m.group(1), change="排除了一条来源") if "exclude_source" not in names else ("排除好了。", [])
        if m := re.search(r"把论断 (k\d+)「.*?」改成了：「(.+?)」", text):
            return call("edit_claim", claim_id=m.group(1), statement=m.group(2), change="按你的说法改了一句") if "edit_claim" not in names else ("改好了。", [])
        if m := re.search(r"点了节 (c\d+)「", text):
            if "revise_section" in names:
                return "这一节重写好了。", []
            sid = _report_ids(messages)[:1] or ids[:1]
            return call("revise_section", section_id=m.group(1), title="重写的节", draft=f"重写后的说法。[{sid[0]}]", change="重写了一节")
        if "坏JSON" in text and (not bad or "一直坏" in text) and not names:
            return "", [{"name": "search_web", "args": _BAD_ARGS, "id": f"bad_{n}", "invalid": True}]
        if "双问" in text and "ask_user" not in names:
            return "", [{"name": "ask_user", "args": {"question": f"同批第 {k} 问？", "options": ["A", "B"], "why": "测试"},
                         "id": f"call_pair_{k}"} for k in (1, 2)]
        if "问一下" in text and "ask_user" not in names:
            return call("ask_user", question="范围包含 PPT 工具吗？", options=["包含", "不包含"], why="不问会混进 PPT 工具的结论")
        if "直接答" in text:
            return "直接回答：这个问题不用查。", []
        if ("选型" in text or "错方法" in text) and "load_skill" not in names:
            return call("load_skill", name="comparison" if "选型" in text else "nope")
        if "search_web" not in names:
            return call("search_web", query=text.splitlines()[0][:20], count=3)
        if "read_page" not in names and hits:
            return call("read_page", url=hits[0]["url"])
        wants_report = "报告" in text or "调研" in text
        if wants_report and "write_report" not in names:
            draft = (f"## 结论\n局部修改已经可用但不稳定。[{ids[0]}]\n稳定性仍是短板。（推断）[{ids[1]}]\n没有来源的说法。\n"
                     f"## 事实准确性\n事实准确性靠模板兜底。[{ids[1]}][{ids[0]}]\n引用不存在的来源。[s000000]")
            args = {"title": "局部修改调研", "draft": draft, "change": "第一版"}
            if "连写" in text:
                return "", [{"name": "write_report", "args": args, "id": f"call_w1_{n}"}, {"name": "write_report", "args": args, "id": f"call_w2_{n}"}]
            return "", [{"name": "write_report", "args": args, "id": f"call_write_report_{n}"}]
        if "不回复" in text:
            return "", []
        return f"结论：可以用但不稳定 [{ids[0]}]。详细的看报告。" if ids else "没查到。", []

    def _message(self, messages: list[BaseMessage]) -> AIMessage:
        content, calls = self._decide(messages)
        valid = [{**c, "type": "tool_call"} for c in calls if not c.get("invalid")]
        invalid = [{"name": c["name"], "args": c["args"], "id": c["id"], "error": "bad json", "type": "invalid_tool_call"}
                   for c in calls if c.get("invalid")]
        return AIMessage(content=content, tool_calls=valid, invalid_tool_calls=invalid, id=f"fake-{len(messages)}",
                         additional_kwargs={"reasoning_content": "（假思考）先想一想。"}, usage_metadata=FAKE_USAGE)

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._message(messages))])

    def _stream(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any) -> Iterator[ChatGenerationChunk]:
        """流式：先吐思考，再分两段吐正文，最后吐工具调用（参数整段）和用量。"""
        msg = self._message(messages)
        half = len(msg.content) // 2
        chunks = [AIMessageChunk(content="", id=msg.id, additional_kwargs={"reasoning_content": msg.additional_kwargs["reasoning_content"]})]
        chunks += [AIMessageChunk(content=part, id=msg.id) for part in (msg.content[:half], msg.content[half:]) if part]
        tcc = [{"name": tc["name"], "args": json.dumps(tc["args"], ensure_ascii=False), "id": tc["id"], "index": k, "type": "tool_call_chunk"}
               for k, tc in enumerate(msg.tool_calls)]
        tcc += [{"name": tc["name"], "args": tc["args"], "id": tc["id"], "index": len(tcc) + k, "type": "tool_call_chunk"}
                for k, tc in enumerate(msg.invalid_tool_calls)]
        chunks.append(AIMessageChunk(content="", id=msg.id, tool_call_chunks=tcc, usage_metadata=FAKE_USAGE, chunk_position="last"))
        for c in chunks:  # 回调（on_llm_new_token）由 BaseChatModel 发
            yield ChatGenerationChunk(message=c)


def _hits(messages: list[BaseMessage]) -> list[dict]:
    """最近一次成功搜索的结果。"""
    for m in reversed(messages):
        if isinstance(m, ToolMessage) and m.name == "search_web" and m.status != "error":
            return json.loads(m.content)
    return []


def _report_ids(messages: list[BaseMessage]) -> list[str]:
    """最近一次改报告的工具返回的报告里引用的来源。"""
    for m in reversed(messages):
        if isinstance(m, ToolMessage) and m.name in ("write_report", "revise_section", "edit_claim", "exclude_source") and m.status != "error":
            return cited(str(m.content))
    return []


class FakeResearchPlanner:
    def __init__(self, gateway: GatewayClient | None = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.time_issues: list[TimeIssue] = []  # 整理时要报告的时间范围问题（测试按需设）
        self.fail_consolidate = False
        self.fail_title = False
        self._gateway = gateway or fake_gateway_client()
        self._tools = make_research_tools(self._gateway)
        self._model = FakeToolModel()

    def agent_model(self) -> BaseChatModel:
        return self._model

    def tools(self) -> list:
        return self._tools

    def consolidate(self, requests: Sequence[str], draft: str, sources: Sequence[Source], user_facts: Sequence[str] = (),
                    report: str | None = None, part: str | None = None) -> ConsolidateResult:
        """按草稿的写法整理：“## 标题”分节，一行一句；带来源 id 的行是论断，标了“（推断）”的是推断。"""
        self.calls.append(("consolidate", {"requests": list(requests), "draft": draft, "sources": [s.id for s in sources],
                                           "user_facts": list(user_facts), "report": report, "part": part}))
        if self.fail_consolidate:
            raise RuntimeError("假模型：整理失败")
        sections: list[SectionDraft] = []
        claims: list[ClaimDraft] = []
        for line in (x.strip() for x in draft.splitlines()):
            if line.startswith("## "):
                sections.append(SectionDraft(title=line[3:].strip(), question=f"{line[3:].strip()}是什么情况？", sentences=[]))
                continue
            if not line or not sections:
                continue
            ids = cited(line)
            text = SOURCE_ID.sub("", line).replace("（推断）", "").strip()
            ref = None
            if ids:
                ref = f"k{len(claims) + 1}"
                claims.append(ClaimDraft(ref=ref, statement=text, kind="inference" if "（推断）" in line else "fact", source_ids=ids,
                                         confidence="medium"))
            sections[-1].sentences.append(SentenceDraft(text=text, claim_ref=ref))
        assumptions = [s.text for sd in sections for s in sd.sentences if s.claim_ref is None and "没有来源" in s.text]
        return ConsolidateResult(sections=sections, claims=claims, assumptions=assumptions, time_issues=list(self.time_issues))

    def title(self, text: str) -> str:
        self.calls.append(("title", text))
        if self.fail_title:
            raise RuntimeError("假模型：起标题失败")
        return f"标题：{text[:8]}"

    def suggest(self, question: str, reply: str, has_report: bool) -> list[str]:
        self.calls.append(("suggest", question))
        return [f"追问{k}：{question[:6]}" for k in (1, 2, 3)]
