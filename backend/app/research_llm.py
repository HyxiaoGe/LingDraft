"""调研场景的模型接入层：ResearchPlanner 协议，LiveResearchPlanner（DeepSeek / 豆包，按配置）与 FakeResearchPlanner（离线）。

agent 本身由 app.graph.research.agent 构建，这里只提供它要的聊天模型和工具，以及无工具的结构化步骤（intake、整理、重写）。
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal, Protocol

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import BaseModel, ConfigDict, Field

from app import config
from app.graph.research import prompts
from app.graph.research.state import (
    CONSOLIDATE_RETRIES,
    Claim,
    ClarifyQuestion,
    IntakeResult,
    OutlineNode,
    Scope,
    SectionText,
    Sentence,
    Source,
)
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


class ConsolidateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sections: list[SectionDraft]
    claims: list[ClaimDraft]
    assumptions: list[str] = Field(description="候选答案里没有来源支撑的说法、自行采用的取舍")
    gap: str | None = Field(description="缺少的关键证据，一句话；没有则 null")


class RewriteDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sentences: list[SentenceDraftById]


class SentenceDraftById(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    claim_id: str | None


RewriteDraft.model_rebuild()


# ---- 协议 ----


class ResearchPlanner(Protocol):
    def intake(self, request: str) -> IntakeResult: ...

    def consolidate(self, scope: Scope, answer: str, sources: list[Source]) -> ConsolidateResult: ...

    def write_section(self, scope: Scope, node: OutlineNode, claims: list[Claim], instruction: str | None) -> SectionText: ...

    def agent_model(self) -> BaseChatModel: ...

    def tools(self) -> list: ...


def _claims_block(claims: list[Claim]) -> str:
    return "\n".join(f"- {c.id} [{c.kind}] {c.statement}" for c in claims) or "（无）"


def _sources_block(sources: list[Source]) -> str:
    lines = []
    for s in sources:
        if s.status == "excluded":
            continue
        lines.append(f"- {s.id} | {s.url} | {s.title or '（无标题）'} | {s.excerpt_kind} | {s.excerpt.replace(chr(10), ' ')[:300]}")
    return "\n".join(lines) or "（无）"


# ---- 聊天模型工厂 ----


def make_chat_model(provider: str, model: str = "", *, thinking: bool, effort: str = "high", timeout: int = 180) -> BaseChatModel:
    """按提供方构造聊天模型。

    DeepSeek 思考模式下：多轮工具调用必须把 reasoning_content 原样传回（langchain-openai / langchain-deepseek 都不做，这里补）；
    不接受强制 tool_choice（with_structured_output / ToolStrategy 会设），这里去掉；temperature 无效。
    """
    if provider == "ark":
        from langchain_openai import ChatOpenAI

        if not config.ARK_API_KEY:
            raise RuntimeError("未设置 ARK_API_KEY")
        return ChatOpenAI(
            model=model or config.ARK_MODEL, base_url=config.ARK_BASE_URL, api_key=config.ARK_API_KEY,
            extra_body={"thinking": {"type": "enabled" if thinking else "disabled"}}, timeout=timeout, max_retries=2, temperature=0,
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
                for m, d in zip(msgs, payload["messages"]):
                    if isinstance(m, AIMessage) and m.additional_kwargs.get("reasoning_content") is not None:
                        d["reasoning_content"] = m.additional_kwargs["reasoning_content"]
            return payload

    kw: dict[str, Any] = {"reasoning_effort": effort} if thinking else {"temperature": 0}
    return ChatDeepSeekRT(
        model=model or config.DEEPSEEK_MODEL, api_key=config.DEEPSEEK_API_KEY, api_base=config.DEEPSEEK_BASE_URL,
        extra_body={"thinking": {"type": "enabled" if thinking else "disabled"}}, timeout=timeout, max_retries=2, **kw,
    )


# ---- 真实模型 ----


class LiveResearchPlanner:
    def __init__(self, gateway: GatewayClient | None = None) -> None:
        provider, model = config.RESEARCH_PROVIDER, config.RESEARCH_MODEL
        self._agent = make_chat_model(provider, model, thinking=config.RESEARCH_THINKING, effort=config.RESEARCH_EFFORT)
        # 结构化步骤：整理用思考（要判断证据对应关系），intake 与重写不用（快）
        self._think = make_chat_model(provider, model, thinking=True, effort=config.RESEARCH_EFFORT, timeout=120)
        self._fast = make_chat_model(provider, model, thinking=False, timeout=90)
        self._gateway = gateway or GatewayClient(config.AGENT_TOOLS_URL, config.AGENT_TOOLS_API_KEY, scene="research")
        self._tools = make_research_tools(self._gateway)

    def _structured(self, llm, schema):
        return llm.with_structured_output(schema, method="json_schema", strict=True)

    def intake(self, request: str) -> IntakeResult:
        return self._structured(self._fast, IntakeResult).invoke(
            [("system", prompts.INTAKE_SYSTEM), ("user", f"用户需求：{request}")]
        )

    def consolidate(self, scope: Scope, answer: str, sources: list[Source]) -> ConsolidateResult:
        user = (
            f"研究问题：{scope.question}\n范围：{scope.model_dump_json(exclude_none=True)}\n\n"
            f"候选答案：\n{answer}\n\n来源列表（id | 网址 | 标题 | 片段类型 | 片段）：\n{_sources_block(sources)}\n"
        )
        chain = self._structured(self._think, ConsolidateResult)
        last: Exception | None = None
        for _ in range(CONSOLIDATE_RETRIES):
            try:
                return chain.invoke([("system", prompts.CONSOLIDATE_SYSTEM), ("user", user)])
            except Exception as e:  # noqa: BLE001
                last = e
        raise RuntimeError(f"整理失败：{last}")

    def write_section(self, scope: Scope, node: OutlineNode, claims: list[Claim], instruction: str | None) -> SectionText:
        user = (
            f"研究问题：{scope.question}\n章节：{node.title}\n这一节要回答：{node.question}\n"
            f"可用论断：\n{_claims_block(claims)}\n"
        )
        if instruction:
            user += f"用户对这一节的要求：{instruction}\n"
        draft = self._structured(self._fast, RewriteDraft).invoke([("system", prompts.WRITE_SYSTEM), ("user", user)])
        return SectionText(outline_id=node.id, sentences=[Sentence(text=s.text, claim_id=s.claim_id) for s in draft.sentences])

    def agent_model(self) -> BaseChatModel:
        return self._agent

    def tools(self) -> list:
        return self._tools


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


class FakeToolModel(BaseChatModel):
    """按对话进度发固定的工具调用：搜 → 读 → （海报题问一次）→ 文字答案。

    需求里的关键字控制特殊路径：
    - “坏JSON”：第一步先发一个参数无法解析的工具调用，看重试中间件；
    - “不收尾”：有搜索额度就一直搜，看额度用尽后工具被摘掉；
    - “无答案”：工具被摘掉后也不给文字，看受控收尾。
    """

    bound: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "fake-tool-model"

    def bind_tools(self, tools: Any, **kwargs: Any) -> FakeToolModel:
        return self.model_copy(update={"bound": [getattr(t, "name", getattr(t, "__name__", "")) for t in tools]})

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        last = messages[-1]
        req = _request_of(messages)
        n = len(messages)
        if isinstance(last, HumanMessage) and str(last.content).startswith("（系统）工具已不可用"):
            msg = AIMessage(content=_answer_text(req, _hits(messages), _answer(messages), how="收尾"))
        elif isinstance(last, HumanMessage):
            if "坏JSON" in req and not any(isinstance(m, AIMessage) for m in messages):
                msg = AIMessage(content="", invalid_tool_calls=[{"name": "search_web", "args": '{"query": "坏', "id": f"bad_{n}",
                                                                   "error": "Expecting ',' delimiter", "type": "invalid_tool_call"}])
            else:
                msg = _call("search_web", {"query": f"{req} 现状", "count": 3}, n)
        elif isinstance(last, ToolMessage) and last.status == "error":
            msg = _call("search_web", {"query": f"{req} 再查", "count": 3}, n) if "search_web" in self.bound else AIMessage(content="")
        elif isinstance(last, ToolMessage) and last.name == "search_web":
            if "不收尾" in req or "无答案" in req:
                msg = _call("search_web", {"query": f"{req} 继续 {n}", "count": 3}, n) if "search_web" in self.bound else (
                    AIMessage(content="") if "无答案" in req else AIMessage(content=_answer_text(req, _hits(messages), None)))
            else:
                msg = _call("read_page", {"url": json.loads(last.content)[0]["url"]}, n)
        elif isinstance(last, ToolMessage) and last.name == "read_page" and "海报" in req and "ask_user" in self.bound and not _asked(messages):
            msg = _call("ask_user", {"question": "范围是否包含 PPT 工具？", "options": ["包含", "不包含"], "why": "不问会混入 PPT 工具的结论"}, n)
        else:
            msg = AIMessage(content=_answer_text(req, _hits(messages), _answer(messages)))
        return ChatResult(generations=[ChatGeneration(message=msg)])


def _call(name: str, args: dict, n: int) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{name}_{n}", "type": "tool_call"}])


def _request_of(messages: list[BaseMessage]) -> str:
    for m in messages:
        if isinstance(m, HumanMessage):
            return str(m.content)
    return ""


def _asked(messages: list[BaseMessage]) -> bool:
    return any(isinstance(m, AIMessage) and any(tc["name"] == "ask_user" for tc in m.tool_calls) for m in messages)


def _answer(messages: list[BaseMessage]) -> str | None:
    for m in messages:
        if isinstance(m, ToolMessage) and m.name == "ask_user":
            return str(m.content)
    return None


def _hits(messages: list[BaseMessage]) -> list[dict]:
    for m in messages:
        if isinstance(m, ToolMessage) and m.name == "search_web" and m.status != "error":
            return json.loads(m.content)
    return []


def _answer_text(req: str, hits: list[dict], answer: str | None, how: str = "") -> str:
    """假的候选答案：每条带 [节|类别|网址] 标记，FakeResearchPlanner.consolidate 按它整理。"""
    u = [h["url"] for h in hits] + ["https://nowhere.test/x"] * 3
    lines = [f"结论：{req} 已经可用但不稳定。{how}"]
    lines.append(f"- [c1|fact|{u[0]}] 来源 1 明确写了局部修改要多轮。")
    lines.append(f"- [c1|inference|{u[1]}] 按来源 2 推测稳定性仍是短板。")
    if "事实准确性" in req or "海报" in req:
        lines.append(f"- [c2|fact|{u[1]}] 事实准确性靠模板兜底。")
    lines.append("- [c1|fact|https://nowhere.test/x] 没有来源的论断应被丢弃。")
    if answer:
        lines.append(f"用户回答：{answer}")
    return "\n".join(lines)


class FakeResearchPlanner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self._gateway = fake_gateway_client()
        self._tools = make_research_tools(self._gateway)
        self._model = FakeToolModel()

    def intake(self, request: str) -> IntakeResult:
        self.calls.append(("intake", request))
        scope = Scope(question=request, time_range="2026" if "2026" in request else None)
        # 模拟“需求模糊才问”：提到局部修改但没说是改文字还是换图时问一个；其余需求直接通过
        questions = []
        if "局部修改" in request and not any(k in request for k in ("改文字", "换图")):
            questions.append(ClarifyQuestion(id="q1", title="局部修改指的是哪种？", options=["改文字", "换图"], why="两种方向的检索词和评测标准不同"))
        return IntakeResult(scope=scope, questions=questions)

    def consolidate(self, scope: Scope, answer: str, sources: list[Source]) -> ConsolidateResult:
        self.calls.append(("consolidate", answer))
        by_url = {s.url: s.id for s in sources if s.status != "excluded"}
        titles = {"c1": ("局部修改机制", "主流工具怎么做局部修改？"), "c2": ("事实准确性", "价格日期这类事实怎么保证？")}
        claims: list[ClaimDraft] = []
        per_sec: dict[str, list[str]] = {}
        for i, m in enumerate(re.finditer(r"- \[(c\d)\|(fact|inference)\|(\S+)\] (.+)", answer), start=1):
            sec, kind, url, text = m.groups()
            claims.append(ClaimDraft(ref=f"k{i}", statement=text.strip(), kind=kind, source_ids=[by_url[url]] if url in by_url else [],
                                     confidence="high" if kind == "fact" else "medium"))
            per_sec.setdefault(sec, []).append(f"k{i}")
        sections = []
        for sec, refs in per_sec.items():
            title, q = titles[sec]
            sents = [SentenceDraft(text=f"{title}概述。", claim_ref=None)]
            for ref in refs:
                c = next(x for x in claims if x.ref == ref)
                sents.append(SentenceDraft(text=("推断：" if c.kind == "inference" else "") + c.statement, claim_ref=ref))
            sections.append(SectionDraft(title=title, question=q, sentences=sents))
        assumptions = [ln.strip() for ln in answer.splitlines() if ln.startswith("用户回答：")]
        first = sum(1 for c in self.calls if c[0] == "consolidate") == 1
        gap = "缺价格数据" if ("回补" in scope.question and first) else None
        return ConsolidateResult(sections=sections, claims=claims, assumptions=assumptions, gap=gap)

    def write_section(self, scope: Scope, node: OutlineNode, claims: list[Claim], instruction: str | None) -> SectionText:
        self.calls.append(("write", node.id, instruction))
        sentences = [Sentence(text=f"{node.title}概述。", claim_id=None)]
        for c in claims:
            prefix = "推断：" if c.kind == "inference" else ""
            text = f"{prefix}{c.statement}"
            if instruction:
                text += f"（{instruction}）"
            sentences.append(Sentence(text=text, claim_id=c.id))
        return SectionText(outline_id=node.id, sentences=sentences)

    def agent_model(self) -> BaseChatModel:
        return self._model

    def tools(self) -> list:
        return self._tools
