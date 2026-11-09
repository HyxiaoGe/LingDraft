"""算向量、重排：都是百炼（同一个 DASHSCOPE_API_KEY）。返回结果和用了多少 token（记账用，见 service）。

- 向量走百炼的 OpenAI 兼容接口，直接用 openai 的客户端：LangChain 的 OpenAIEmbeddings 只给向量、不给用量，建索引要按实际 token 扣。
  换智谱、混元这些兼容接口的只换参数。一次最多 10 条（百炼的上限）。
- 重排（qwen3.7-text-rerank）只有百炼自己的接口，用 httpx 调。
- 没配百炼的 key、假模型跑的（本机、测试）用 HashEmbedder：按词算的假向量，同样的词靠得近，不调接口；不重排。
"""

from __future__ import annotations

import hashlib
import math
from typing import Protocol

import httpx

from app import config
from app.knowledge import text
from app.knowledge.store import DIMS

BATCH = 10  # 百炼一次最多算 10 条
TIMEOUT_S = 30.0


class Embedder(Protocol):
    ref: str  # "厂商:模型"，记账用（models.toml 的 [prices]）
    model: str  # 记在块上：以后换模型知道哪些要重算

    async def embed(self, texts: list[str]) -> tuple[list[list[float]], int]: ...  # 最多 BATCH 条；返回向量和 token 数


class Reranker(Protocol):
    ref: str

    async def rerank(self, query: str, documents: list[str], top_n: int) -> tuple[list[int], int]: ...  # 按相关排好的下标、token 数


class DashScopeEmbedder:
    def __init__(self, api_key: str, base_url: str = config.DASHSCOPE_BASE_URL, model: str = config.KB_EMBED_MODEL) -> None:
        from openai import AsyncOpenAI

        self.model, self.ref = model, f"dashscope:{model}"
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=2, timeout=TIMEOUT_S)

    async def embed(self, texts: list[str]) -> tuple[list[list[float]], int]:
        r = await self._client.embeddings.create(model=self.model, input=texts, dimensions=DIMS, encoding_format="float")
        data = sorted(r.data, key=lambda d: d.index)
        if len(data) != len(texts):
            raise RuntimeError(f"向量接口回了 {len(data)} 条，发的是 {len(texts)} 条")
        return [d.embedding for d in data], (r.usage.total_tokens if r.usage else 0)


class DashScopeReranker:
    def __init__(self, api_key: str, url: str = config.KB_RERANK_URL, model: str = config.KB_RERANK_MODEL) -> None:
        self.model, self.ref, self.url, self._key = model, f"dashscope:{model}", url, api_key
        self._http: httpx.AsyncClient | None = None  # 在对话的事件循环里第一次用时建

    async def rerank(self, query: str, documents: list[str], top_n: int) -> tuple[list[int], int]:
        if self._http is None:
            self._http = httpx.AsyncClient(headers={"Authorization": f"Bearer {self._key}"}, timeout=TIMEOUT_S)
        r = await self._http.post(self.url, json={"model": self.model, "input": {"query": query, "documents": documents},
                                                  "parameters": {"top_n": top_n, "return_documents": False}})
        r.raise_for_status()
        body = r.json()
        order = [int(x["index"]) for x in body["output"]["results"]]
        if any(not 0 <= i < len(documents) for i in order):
            raise RuntimeError(f"重排回的下标不对：{order}")
        return order, int((body.get("usage") or {}).get("total_tokens") or 0)


class HashEmbedder:
    """假的：词（见 text.query_terms）散到 DIMS 维上再归一，同样的词靠得近。token 数按字数算。"""

    ref = model = "fake:hash"

    async def embed(self, texts: list[str]) -> tuple[list[list[float]], int]:
        out = []
        for t in texts:
            v = [0.0] * DIMS
            for w in text.query_terms(t) or [t]:
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % DIMS] += 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out, sum(len(t) for t in texts)


def live() -> tuple[Embedder, Reranker | None]:
    """配了百炼的 key 用百炼，没配的用假的、不重排。"""
    if key := config.DASHSCOPE_API_KEY:
        return DashScopeEmbedder(key), DashScopeReranker(key)
    return HashEmbedder(), None
