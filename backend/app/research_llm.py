"""对话场景的模型接入层：ResearchPlanner 协议，LiveResearchPlanner（DeepSeek / 豆包，按配置）与 FakeResearchPlanner（离线）。

agent 本身由 app.graph.research.agent 构建，这里提供它要的聊天模型和查资料的工具，以及几个不带工具的快模型步骤：
起标题、推荐问题，还有内容审核拦下请求时把资料一条条单独送审。
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol
from uuid import UUID

import httpx
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import BaseModel, ConfigDict, Field

from app import config
from app.graph.research import prompts
from app.model_guard import ContentBlocked, GuardedChatModel
from app.tools.gateway import GatewayClient
from app.tools.research_tools import make_research_tools

PROBE_WORKERS = 4  # 单条送审同时发几个

# ---- 结构化输出 schema（扁平、全部 required，符合 strict 模式） ----


class Suggestions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    questions: list[str] = Field(description="3 个用户接下来可能会问的问题")


# ---- 协议 ----


class ResearchPlanner(Protocol):
    def agent_model(self) -> BaseChatModel: ...

    def tools(self) -> list: ...

    def title(self, text: str) -> str: ...

    def suggest(self, question: str, reply: str) -> list[str]: ...

    def blocked(self, texts: Sequence[str]) -> list[bool]: ...


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
    """按提供方构造聊天模型。每次调用都过熔断，服务商的错误换成 ModelUnavailable / ContentBlocked（见 app.model_guard）。

    DeepSeek 思考模式下：同一轮里的多次工具调用必须把 reasoning_content 原样传回（langchain-openai / langchain-deepseek 都不做，这里补），
    之前几轮的不传；不接受强制 tool_choice（with_structured_output 会设），这里去掉；temperature 无效。
    """
    if provider == "ark":
        from langchain_openai import ChatOpenAI

        class ChatArk(GuardedChatModel, ChatOpenAI):
            pass

        if not config.ARK_API_KEY:
            raise RuntimeError("未设置 ARK_API_KEY")
        return ChatArk(
            model=model or config.ARK_MODEL, base_url=config.ARK_BASE_URL, api_key=config.ARK_API_KEY,
            extra_body={"thinking": {"type": "enabled" if thinking else "disabled"}}, timeout=timeout, max_retries=2, temperature=0,
            stream_usage=True, callbacks=callbacks,
        )
    if provider != "deepseek":
        raise ValueError(f"未知的模型提供方：{provider}")
    from langchain_deepseek import ChatDeepSeek

    if not config.DEEPSEEK_API_KEY:
        raise RuntimeError("未设置 DEEPSEEK_API_KEY")

    class ChatDeepSeekRT(GuardedChatModel, ChatDeepSeek):
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


class LiveResearchPlanner:
    def __init__(self, gateway: GatewayClient | None = None) -> None:
        provider, model = config.RESEARCH_PROVIDER, config.RESEARCH_MODEL
        self._agent = make_chat_model(provider, model, thinking=config.RESEARCH_THINKING, effort=config.RESEARCH_EFFORT,
                                      callbacks=[ThinkingTimer()])
        # 快模型步骤（起标题、推荐问题、单条送审）不开思考：都是小活，开思考慢得多
        self._fast = make_chat_model(provider, model, thinking=False, timeout=120)
        self._gateway = gateway or GatewayClient(config.AGENT_TOOLS_URL, config.AGENT_TOOLS_API_KEY, scene="research")
        self._tools = make_research_tools(self._gateway)

    def agent_model(self) -> BaseChatModel:
        return self._agent

    def tools(self) -> list:
        return self._tools

    def title(self, text: str) -> str:
        out = self._fast.invoke([("system", prompts.render("title.system")), ("user", prompts.render("title.user", text=text))])
        return str(out.content).strip()

    def suggest(self, question: str, reply: str) -> list[str]:
        res = _structured(self._fast, Suggestions).invoke([("system", prompts.render("suggest.system")),
                                                           ("user", prompts.render("suggest.user", question=question, reply=reply))])
        return [q.strip() for q in res.questions if q.strip()]

    def blocked(self, texts: Sequence[str]) -> list[bool]:
        """每段文字单独发一次（只有这段、不思考、只要 1 个 token），看内容审核拦不拦。服务不可用照常抛出。"""
        probe = self._fast.bind(max_tokens=1)

        def one(text: str) -> bool:
            try:
                probe.invoke([("user", text)])
            except ContentBlocked:
                return True
            return False

        with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
            return list(pool.map(one, texts))


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
    """按这一轮的进度发固定的工具调用：搜 → 读 → 文字回复。每次回复都带一段假思考和用量。

    用户的话里的关键字控制特殊路径（只给测试用）：
    - “直接答”：不调工具直接回复；“不回复”：最后不给文字；
    - “坏JSON”：第一步先发一个参数无法解析的工具调用；“一直坏”：每次都发。
    内容审核（模拟服务商）：请求里任何地方有 FAKE_BLOCKED 就整个拒绝；用户的话里有“查完就拦”时，有工具结果就拒绝；
    有“输出拦截”时，吐完思考后输出被截断。
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
        n = len(messages)

        def call(tool: str, **args) -> tuple[str, list[dict]]:
            return "", [{"name": tool, "args": args, "id": f"call_{tool}_{n}"}]

        if "坏JSON" in text and (not bad or "一直坏" in text) and not names:
            return "", [{"name": "search_web", "args": _BAD_ARGS, "id": f"bad_{n}", "invalid": True}]
        if "直接答" in text:
            return "直接回答：这个问题不用查。", []
        if "search_web" not in names:
            return call("search_web", query=text.splitlines()[0][:20], count=3)
        if "read_page" not in names and hits:
            return call("read_page", url=hits[0]["url"])
        if "不回复" in text:
            return "", []
        return f"结论：可以用但不稳定 [{hits[0]['id']}]，详情见第二条 [{hits[1]['id']}]。" if hits else "没查到。", []

    def _message(self, messages: list[BaseMessage]) -> AIMessage:
        _fake_review(messages)
        content, calls = self._decide(messages)
        valid = [{**c, "type": "tool_call"} for c in calls if not c.get("invalid")]
        invalid = [{"name": c["name"], "args": c["args"], "id": c["id"], "error": "bad json", "type": "invalid_tool_call"}
                   for c in calls if c.get("invalid")]
        return AIMessage(content=content, tool_calls=valid, invalid_tool_calls=invalid, id=f"fake-{len(messages)}",
                         additional_kwargs={"reasoning_content": "（假思考）先想一想。"}, usage_metadata=FAKE_USAGE)

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._message(messages))])

    def _chunks(self, msg: AIMessage) -> list[AIMessageChunk]:
        """流式的片段：先吐思考，再分两段吐正文，最后吐工具调用（参数整段）和用量。"""
        half = len(msg.content) // 2
        chunks = [AIMessageChunk(content="", id=msg.id, additional_kwargs={"reasoning_content": msg.additional_kwargs["reasoning_content"]})]
        chunks += [AIMessageChunk(content=part, id=msg.id) for part in (msg.content[:half], msg.content[half:]) if part]
        tcc = [{"name": tc["name"], "args": json.dumps(tc["args"], ensure_ascii=False), "id": tc["id"], "index": k, "type": "tool_call_chunk"}
               for k, tc in enumerate(msg.tool_calls)]
        tcc += [{"name": tc["name"], "args": tc["args"], "id": tc["id"], "index": len(tcc) + k, "type": "tool_call_chunk"}
                for k, tc in enumerate(msg.invalid_tool_calls)]
        chunks.append(AIMessageChunk(content="", id=msg.id, tool_call_chunks=tcc, usage_metadata=FAKE_USAGE, chunk_position="last"))
        return chunks

    def _stream(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any) -> Iterator[ChatGenerationChunk]:
        for k, c in enumerate(self._chunks(self._message(messages))):  # 回调（on_llm_new_token）由 BaseChatModel 发
            if k == 1 and "输出拦截" in _turn_text(messages):
                raise ContentBlocked(output=True)
            yield ChatGenerationChunk(message=c)


FAKE_BLOCKED = "【审核不通过】"


def _turn_text(messages: list[BaseMessage]) -> str:
    return next((str(m.content) for m in reversed(messages) if isinstance(m, HumanMessage)), "")


def _fake_review(messages: list[BaseMessage]) -> None:
    if any(FAKE_BLOCKED in str(m.content) for m in messages):
        raise ContentBlocked(output=False)
    if "查完就拦" in _turn_text(messages) and any(isinstance(m, ToolMessage) and m.name in ("search_web", "read_page") for m in messages):
        raise ContentBlocked(output=False)


def _hits(messages: list[BaseMessage]) -> list[dict]:
    """最近一次成功搜索的结果。"""
    for m in reversed(messages):
        if isinstance(m, ToolMessage) and m.name == "search_web" and m.status != "error":
            return json.loads(m.content)
    return []


class FakeResearchPlanner:
    def __init__(self, gateway: GatewayClient | None = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.fail_title = False
        self._gateway = gateway or fake_gateway_client()
        self._tools = make_research_tools(self._gateway)
        self._model = FakeToolModel()

    def agent_model(self) -> BaseChatModel:
        return self._model

    def tools(self) -> list:
        return self._tools

    def title(self, text: str) -> str:
        self.calls.append(("title", text))
        if self.fail_title:
            raise RuntimeError("假模型：起标题失败")
        return f"标题：{text[:8]}"

    def suggest(self, question: str, reply: str) -> list[str]:
        self.calls.append(("suggest", question))
        return [f"追问{k}：{question[:6]}" for k in (1, 2, 3)]

    def blocked(self, texts: Sequence[str]) -> list[bool]:
        self.calls.append(("blocked", list(texts)))
        return [FAKE_BLOCKED in t for t in texts]
