"""调研场景的模型接入层：ResearchPlanner 协议，ArkResearchPlanner（豆包）与 FakeResearchPlanner（离线）。

agent 本身由 app.graph.research.agent 构建，这里只提供它要的聊天模型和工具。
"""

from __future__ import annotations

import json
import re
from typing import Any, Protocol

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict, Field

from app import config
from app.graph.research import prompts
from app.graph.research.state import Claim, ClarifyQuestion, IntakeResult, OutlineNode, Scope, SectionText, Sentence
from app.tools.gateway import GatewayClient
from app.tools.research_tools import make_research_tools

# ---- 结构化输出 schema ----


class OutlineNodeDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    title: str
    question: str


class OutlinePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nodes: list[OutlineNodeDraft]


class SentenceDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    claim_id: str | None


class SectionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sentences: list[SentenceDraft]


# ---- 协议 ----


class ResearchPlanner(Protocol):
    def intake(self, request: str) -> IntakeResult: ...

    def plan(self, scope: Scope) -> list[OutlineNode]: ...

    def write_section(
        self, scope: Scope, node: OutlineNode, claims: list[Claim], instruction: str | None
    ) -> SectionText: ...

    def agent_model(self) -> BaseChatModel: ...

    def tools(self) -> list: ...


def _claims_block(claims: list[Claim]) -> str:
    return "\n".join(f"- {c.id} [{c.kind}] {c.statement}" for c in claims) or "（无）"


# ---- 豆包 ----


class ArkResearchPlanner:
    def __init__(self, gateway: GatewayClient | None = None) -> None:
        if not config.ARK_API_KEY:
            raise RuntimeError("未设置 ARK_API_KEY")
        common = {
            "base_url": config.ARK_BASE_URL,
            "api_key": config.ARK_API_KEY,
            "extra_body": {"thinking": {"type": "disabled"}},
            "timeout": 90,
            "max_retries": 2,
            "temperature": 0,
        }
        self._pro = ChatOpenAI(model=config.ARK_MODEL, **common)
        self._lite = ChatOpenAI(model=config.ARK_MODEL_LITE, **common)
        self._gateway = gateway or GatewayClient(config.AGENT_TOOLS_URL, config.AGENT_TOOLS_API_KEY, scene="research")
        self._tools = make_research_tools(self._gateway)

    def _structured(self, llm, schema):
        return llm.with_structured_output(schema, method="json_schema", strict=True)

    def intake(self, request: str) -> IntakeResult:
        return self._structured(self._lite, IntakeResult).invoke(
            [("system", prompts.INTAKE_SYSTEM), ("user", f"用户需求：{request}")]
        )

    def plan(self, scope: Scope) -> list[OutlineNode]:
        plan = self._structured(self._pro, OutlinePlan).invoke(
            [("system", prompts.PLAN_SYSTEM), ("user", f"研究范围：\n{scope.model_dump_json(indent=1)}")]
        )
        return [OutlineNode(id=n.id, title=n.title, question=n.question) for n in plan.nodes]

    def write_section(self, scope: Scope, node: OutlineNode, claims: list[Claim], instruction: str | None) -> SectionText:
        user = (
            f"研究问题：{scope.question}\n章节：{node.title}\n这一章要回答：{node.question}\n"
            f"可用论断：\n{_claims_block(claims)}\n"
        )
        if instruction:
            user += f"用户对这一章的要求：{instruction}\n"
        draft = self._structured(self._pro, SectionDraft).invoke([("system", prompts.WRITE_SYSTEM), ("user", user)])
        return SectionText(outline_id=node.id, sentences=[Sentence(text=s.text, claim_id=s.claim_id) for s in draft.sentences])

    def agent_model(self) -> BaseChatModel:
        return self._pro

    def tools(self) -> list:
        return self._tools


# ---- 离线假实现 ----


def fake_gateway_client() -> GatewayClient:
    """用 MockTransport 模拟网关：每次搜索给 3 条稳定的候选，读页返回正文。"""

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
    """按对话进度发固定的工具调用：搜 → 读 → （第一章问一次）→ Findings。"""

    asked_sections: set[str] = Field(default_factory=set)

    @property
    def _llm_type(self) -> str:
        return "fake-tool-model"

    def bind_tools(self, tools: Any, **kwargs: Any) -> FakeToolModel:
        return self

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        last = messages[-1]
        section = _section_of(messages)
        n = len(messages)
        if isinstance(last, HumanMessage):
            msg = _call("search_web", {"query": f"{section} 局部修改", "count": 3}, n)
        elif isinstance(last, ToolMessage) and last.name == "search_web":
            hits = json.loads(last.content)
            msg = _call("read_page", {"url": hits[0]["url"]}, n)
        elif isinstance(last, ToolMessage) and last.name == "read_page" and section == "c1" and not _asked(messages):
            msg = _call(
                "ask_user",
                {"question": "范围是否包含 PPT 工具？", "options": ["包含", "不包含"], "why": "不问会混入 PPT 工具的结论"},
                n,
            )
        else:
            hits = _hits(messages)
            answer = _answer(messages)
            findings = {
                "claims": [
                    {"statement": f"{section} 事实：来源 1 明确写了局部修改要多轮。", "kind": "fact",
                     "source_urls": [hits[0]["url"]], "confidence": "high"},
                    {"statement": f"{section} 推断：按来源 2 推测稳定性仍是短板。", "kind": "inference",
                     "source_urls": [hits[1]["url"]], "confidence": "medium"},
                    {"statement": "没有来源的论断应被丢弃。", "kind": "fact", "source_urls": ["https://nowhere.test/x"],
                     "confidence": "low"},
                ],
                "assumptions": [f"用户回答：{answer}"] if answer else [],
                "summary": f"{section} 找到两条可用论断。",
            }
            msg = _call("Findings", findings, n)
        return ChatResult(generations=[ChatGeneration(message=msg)])


def _call(name: str, args: dict, n: int) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{name}_{n}", "type": "tool_call"}])


def _section_of(messages: list[BaseMessage]) -> str:
    for m in messages:
        if isinstance(m, HumanMessage):
            found = re.search(r"\[section:(\w+)\]", str(m.content))
            if found:
                return found.group(1)
    return "c?"


def _asked(messages: list[BaseMessage]) -> bool:
    return any(isinstance(m, AIMessage) and any(tc["name"] == "ask_user" for tc in m.tool_calls) for m in messages)


def _answer(messages: list[BaseMessage]) -> str | None:
    for m in messages:
        if isinstance(m, ToolMessage) and m.name == "ask_user":
            return str(m.content)
    return None


def _hits(messages: list[BaseMessage]) -> list[dict]:
    for m in messages:
        if isinstance(m, ToolMessage) and m.name == "search_web":
            return json.loads(m.content)
    return []


class FakeResearchPlanner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self._gateway = fake_gateway_client()
        self._tools = make_research_tools(self._gateway)
        self._model = FakeToolModel()

    def intake(self, request: str) -> IntakeResult:
        self.calls.append(("intake", request))
        scope = Scope(question=f"{request}的现状如何？", time_range="2026" if "2026" in request else None)
        # 模拟"需求模糊才问"：提到局部修改但没说是改文字还是换图时问一个；其余需求直接通过
        questions = []
        if "局部修改" in request and not any(k in request for k in ("改文字", "换图")):
            questions.append(ClarifyQuestion(id="q1", title="局部修改指的是哪种？", options=["改文字", "换图"], why="两种方向的检索词和评测标准不同"))
        return IntakeResult(scope=scope, questions=questions)

    def plan(self, scope: Scope) -> list[OutlineNode]:
        self.calls.append(("plan", scope.question))
        return [
            OutlineNode(id="c1", title="局部修改机制", question="主流工具怎么做局部修改？"),
            OutlineNode(id="c2", title="事实准确性", question="价格日期这类事实怎么保证？"),
        ]

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
