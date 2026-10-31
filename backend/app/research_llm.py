"""对话场景的模型接入层：ResearchPlanner 协议，LiveResearchPlanner（DeepSeek / 豆包，按配置）与 FakeResearchPlanner（离线）。

agent 本身由 app.graph.research.agent 构建，这里提供它要的聊天模型和查资料的工具，以及几个不带工具的快模型步骤：
起标题、推荐问题，还有内容审核拦下请求时把资料一条条单独送审。
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import threading
import time
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from uuid import UUID

import httpx
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from PIL import Image, ImageDraw
from pydantic import BaseModel, ConfigDict, Field

from app import config
from app.graph.research import prompts
from app.model_guard import ContentBlocked, GuardedChatModel
from app.tools.gateway import GatewayClient
from app.tools.research_tools import TIME_TOOL, make_research_tools

PROBE_WORKERS = 4  # 单条送审同时发几个

# ---- 结构化输出 schema（扁平、全部 required，符合 strict 模式） ----


class Suggestions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    questions: list[str] = Field(description="Three follow-up questions the user may want to ask")


# ---- 协议 ----


class ResearchPlanner(Protocol):
    def agent_model(self) -> BaseChatModel: ...

    def tools(self) -> list: ...

    def title(self, text: str) -> str: ...

    def suggest(self, question: str, reply: str) -> list[str]: ...

    def blocked(self, texts: Sequence[str]) -> list[bool]: ...


# ---- 思考计时 ----

THINKING_MS = "thinking_ms"  # 思考用时记在消息的 response_metadata 里的键


class ThinkingClock:
    """一次模型调用思考了多久：从第一个思考片段到第一个正文或工具调用片段，还没等到就算到现在。"""

    def __init__(self) -> None:
        self._start: float | None = None
        self._end: float | None = None

    def see(self, msg: AIMessageChunk) -> None:
        t = time.monotonic()
        if msg.additional_kwargs.get("reasoning_content"):
            self._start = self._start or t
        elif self._start is not None and self._end is None and (msg.content or msg.tool_call_chunks):
            self._end = t

    def ms(self) -> int | None:
        """没有思考片段就是 None。"""
        return None if self._start is None else int(((self._end or time.monotonic()) - self._start) * 1000)


class ThinkingTimer(BaseCallbackHandler):
    """记下每次模型调用思考了多久（见 ThinkingClock），写进这条消息的 response_metadata[THINKING_MS]，随消息存进检查点。
    只有流式调用才有片段，才计时。用户点了停止、调用没跑完的，由运行器存那半截消息时一起记（见 api.research_runner.StreamBuffer）。"""

    def __init__(self) -> None:
        self._mu = threading.Lock()
        self._runs: dict[UUID, ThinkingClock] = {}

    def on_chat_model_start(self, serialized, messages, *, run_id: UUID, **kwargs: Any) -> None:
        pass  # 不实现的话回调管理器会退回 on_llm_start，把消息全转成字符串

    def on_llm_new_token(self, token, *, chunk=None, run_id: UUID, **kwargs: Any) -> None:
        msg = getattr(chunk, "message", None)
        if not isinstance(msg, AIMessageChunk):
            return
        with self._mu:
            self._runs.setdefault(run_id, ThinkingClock()).see(msg)

    def on_llm_end(self, response, *, run_id: UUID, **kwargs: Any) -> None:
        with self._mu:
            clock = self._runs.pop(run_id, None)
        ms = clock.ms() if clock else None
        if ms is None or not response.generations or not response.generations[0]:
            return
        msg = getattr(response.generations[0][0], "message", None)
        if msg is not None:  # 就是最后存进 state 的那个消息对象
            msg.response_metadata[THINKING_MS] = ms

    def on_llm_error(self, error, *, run_id: UUID, **kwargs: Any) -> None:
        with self._mu:
            self._runs.pop(run_id, None)


# ---- 聊天模型工厂 ----


def make_chat_model(provider: str, model: str = "", *, thinking: bool, effort: str = "high", timeout: int = 180,
                    callbacks: list | None = None) -> BaseChatModel:
    """按提供方构造聊天模型。每次调用都过熔断，服务商的错误换成 ModelUnavailable / ContentBlocked（见 app.model_guard）。

    DeepSeek 思考模式下：带工具的请求要把 reasoning_content 原样传回（langchain-openai / langchain-deepseek 都不做，这里补）。
    同一轮里的不传接口报 400；之前几轮的照它现在的文档也传，会拼进上下文，只传这一轮的话，下一轮上一轮的消息变了，前缀缓存从那里断
    （2026-11-01 Sean 定）。不接受强制 tool_choice（with_structured_output 会设），这里去掉；temperature 无效。
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
                for m, d in zip(msgs, payload["messages"]):
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
        out = self._fast.invoke([("user", prompts.render("title.prompt", content=text))])
        return str(out.content).strip()

    def suggest(self, question: str, reply: str) -> list[str]:
        res = _structured(self._fast, Suggestions).invoke([("user", prompts.render("suggest.prompt", question=question, reply=reply))])
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


FAKE_CITIES = [
    {"location_id": "101280101", "name": "广州", "adm1": "广东省", "adm2": "广州", "country": "中国", "rank": 11},
    {"location_id": "101010300", "name": "朝阳", "adm1": "北京市", "adm2": "北京", "country": "中国", "rank": 15},
    {"location_id": "101071201", "name": "朝阳", "adm1": "辽宁省", "adm2": "朝阳", "country": "中国", "rank": 23},
]
FAKE_SKY = ["晴", "多云", "小雨", "阴", "雷阵雨", "晴", "多云"]


def _fake_weather(path: str, params) -> httpx.Response:
    """假的和风：城市只有广州和两个朝阳；预报从北京时间今天、这个小时起，下午有一阵雨。"""
    now = datetime.now(timezone(timedelta(hours=8))).replace(minute=0, second=0, microsecond=0)
    if path == "/weather/v1/cities":
        q = params.get("location", "")
        return httpx.Response(200, json={"cities": [c for c in FAKE_CITIES if c["name"] in q or q in c["name"]]})
    if path == "/weather/v1/forecast/daily":
        days = [{"date": (now + timedelta(days=k)).date().isoformat(), "text_day": FAKE_SKY[k], "text_night": "晴",
                 "temp_max": str(30 - k), "temp_min": str(21 - k), "wind_dir_day": "北风", "wind_scale_day": "1-3"} for k in range(7)]
        return httpx.Response(200, json={"daily": days})
    if path == "/weather/v1/forecast/hourly":
        hours = []
        for k in range(24):
            t = now + timedelta(hours=k)
            wet = 14 <= t.hour <= 16
            hours.append({"time": t.isoformat(timespec="minutes"), "temp": str(22 + round(8 * max(0.0, 1 - abs(t.hour - 14) / 9))),
                          "text": "小雨" if wet else "晴", "pop": "70" if wet else "0", "precip": "1.2" if wet else "0.0"})
        return httpx.Response(200, json={"hourly": hours})
    return httpx.Response(404, json={"error": "not_found"})


FAKE_IMAGE_FILTERED = "【违规图】"  # 画图的描述里有它，假 image-service 按内容审核没过拒绝
FAKE_IMAGE_SLOW = "慢慢画"  # 描述里有它，假 image-service 过几秒才出图（看占位、试停止）
FAKE_IMAGE_SLOW_S = 6.0


def _fake_png(prompt: str, ratio: str, edit: bool) -> bytes:
    """按比例画一张纯色底、带个方块的 PNG，颜色随描述变；改出来的图方块是白的。"""
    w, h = (int(x) for x in ratio.split(":"))
    size = (512, round(512 * h / w)) if w >= h else (round(512 * w / h), 512)
    d = hashlib.md5(prompt.encode()).digest()
    img = Image.new("RGB", size, (60 + d[0] % 150, 60 + d[1] % 150, 60 + d[2] % 150))
    ImageDraw.Draw(img).rectangle((size[0] // 3, size[1] // 3, size[0] * 2 // 3, size[1] * 2 // 3),
                                  fill=(255, 255, 255) if edit else (20, 24, 31))
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def _fake_image_service() -> Any:
    """假 image-service：POST /image/v1/generate 出图，图在 GET /image/static/images/<id>.png。"""
    made: dict[str, bytes] = {}

    def handle(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            data = made.get(req.url.path.removeprefix("/image"))
            return httpx.Response(200, content=data, headers={"content-type": "image/png"}) if data else httpx.Response(404)
        body = json.loads(req.content)
        prompt = body["prompt"]
        if FAKE_IMAGE_FILTERED in prompt:
            return httpx.Response(422, json={"error": "content_filtered", "message": "blocked", "retryable": False,
                                             "model": "doubao-seedream-5-0-flash"})
        if FAKE_IMAGE_SLOW in prompt:
            time.sleep(FAKE_IMAGE_SLOW_S)
        iid = f"fake{len(made) + 1}"
        path = f"/static/images/{iid}.png"
        made[path] = _fake_png(prompt, body.get("aspect_ratio") or "1:1", bool(body.get("reference_images")))
        model = "doubao-seedream-5-0-pro" if body.get("quality") == "high" else "doubao-seedream-5-0-flash"
        return httpx.Response(200, json={"id": iid, "image_url": path, "model": model, "requested_model": model, "fallback_used": False,
                                         "grounding_used": False, "cached": False, "created_at": "2026-10-31T00:00:00Z",
                                         "reference_images_used": len(body.get("reference_images") or []),
                                         "reference_search": {"status": "skipped"}})

    return handle


def fake_gateway_client() -> GatewayClient:
    """用 MockTransport 模拟网关：每次搜索给 3 条稳定的候选，读页返回正文（第 3 条读不到）；天气见 _fake_weather，画图见 _fake_image_service。"""
    images = _fake_image_service()

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.startswith("/weather/"):
            return _fake_weather(req.url.path, req.url.params)
        if req.url.path == "/image/v1/generate" or req.url.path.startswith("/image/static/"):
            return images(req)
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


_FAKE_IMAGE_ID = re.compile(r"Image id: ([0-9a-f]{12})")  # 生图结果里给模型的编号（prompts.toml 的 tools.generate_image.success）
FAKE_USAGE = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
              "input_token_details": {"cache_read": 60}, "output_token_details": {"reasoning": 5}}
_BAD_ARGS = '{"query": "a" x}'  # 解析不了的参数


class FakeToolModel(BaseChatModel):
    """按这一轮的进度发固定的工具调用：搜 → 读 → 文字回复。每次回复都带一段假思考和用量。

    用户的话里的关键字控制特殊路径（只给测试用）：
    - “直接答”：不调工具直接回复；“不回复”：最后不给文字；
    - “坏JSON”：第一步先发一个参数无法解析的工具调用；“一直坏”：每次都发。
    - “几点”：先取当前时间，再照常往下走。
    - “天气”：先查天气，地名是“天气”前面那段（没有就是广州），再照常搜、读。
    - “画”：画一张图（描述就是用户的话，“横”的是 16:9），画完直接回复，不搜；“改图”：改这段对话里最近画的那张；
      “再画”“再生成”：用上一次的参数再画一张（regenerate）。
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
        if "几点" in text and TIME_TOOL in self.bound and TIME_TOOL not in names:
            return call(TIME_TOOL)
        if "直接答" in text:
            return "直接回答：这个问题不用查。", []
        if "天气" in text and "get_weather" in self.bound and "get_weather" not in names:
            return call("get_weather", location=text.split("天气")[0].strip()[:8] or "广州")
        if any(k in text for k in ("画", "改图", "再生成")) and "generate_image" in self.bound:
            return self._draw(text, messages, turn, call)
        if "search_web" not in names:
            return call("search_web", query=text.splitlines()[0][:20], count=3)
        if "read_page" not in names and hits:
            return call("read_page", url=hits[0]["url"])
        if "不回复" in text:
            return "", []
        return f"结论：可以用但不稳定 [{hits[0]['id']}]，详情见第二条 [{hits[1]['id']}]。" if hits else "没查到。", []

    @staticmethod
    def _draw(text: str, messages: list[BaseMessage], turn: list[BaseMessage], call) -> tuple[str, list[dict]]:
        if done := next((m for m in reversed(turn) if isinstance(m, ToolMessage) and m.name == "generate_image"), None):
            return ("没画成。" if done.status == "error" else "画好了，看看要不要调整。"), []
        prompt = text.splitlines()[0][:200]
        ids = _FAKE_IMAGE_ID.findall("\n".join(str(m.content) for m in messages if isinstance(m, ToolMessage) and m.name == "generate_image"))
        if "改图" in text and ids:
            return call("generate_image", prompt=prompt, source_image_id=ids[-1])
        before = [tc["args"] for m in messages if isinstance(m, AIMessage) for tc in m.tool_calls if tc["name"] == "generate_image"]
        if ("再画" in text or "再生成" in text) and before:
            return call("generate_image", **{**before[-1], "regenerate": True})
        return call("generate_image", prompt=prompt, **({"aspect_ratio": "16:9"} if "横" in text else {}))

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
