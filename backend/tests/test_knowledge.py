"""知识库（2026-11-09 Sean 定，见 app.knowledge）：传进知识库、后台解析和建索引、向量加关键词合并再重排地查，
对话里第一轮有文件才给查知识库的工具，查到的段落当来源；read_file 能读知识库的文件；建索引按 token 扣额度。只用假向量。"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import aio, config, costs
from app.api import main as api_main
from app.api.research_runner import ResearchRunner
from app.files.parse import Part
from app.files.service import INDEXING, UNBOUND_TTL, FileService
from app.files.store import FAILED, PARSING, READY, FileRow, MemoryFileStore
from app.identity import Identity
from app.knowledge import text
from app.knowledge.remote import HashEmbedder
from app.knowledge.service import KnowledgeBase, SearchUnavailable, rrf, workspace
from app.knowledge.store import Found, MemoryChunkStore
from tests.auth_helpers import USER, bearer, guest_token, user_token
from tests.research_helpers import events, last, run_async

FILLER = "\n\n".join(f"附注{i}：本段是排版用的填充文字，没有实际内容。" for i in range(40))  # 一节过了一千字，解析才分成单独的一段
PLAN = "".join(f"# {title}\n\n{body}\n\n{FILLER}\n\n" for title, body in (
    ("付款方式", "合同签订后七个工作日内，甲方支付首付款百分之三十。尾款在验收合格后一次付清。"),
    ("交付时间", "乙方应在二〇二七年三月一日前完成全部交付，逾期每天按合同总价的千分之一支付违约金。"),
    ("保密条款", "双方对合作中知悉的商业秘密负有保密义务，期限为合同终止后五年。"),
))


class Rows(list):
    """换掉 costs.LEDGER：交过来的行直接收着。"""

    def put(self, row: dict) -> None:
        self.append(row)


@pytest.fixture
def ledger(monkeypatch) -> Rows:
    r = Rows()
    monkeypatch.setattr(costs, "LEDGER", r)
    return r


def _api(**headers) -> tuple[ResearchRunner, TestClient]:
    rr = ResearchRunner(fake=True)
    api_main.set_research_runner(rr)
    return rr, TestClient(api_main.app, headers=headers or USER)


def _kb_upload(rr: ResearchRunner, client: TestClient, name: str, data: bytes) -> dict:
    r = client.post("/api/knowledge/files", files={"file": (name, data, "application/octet-stream")})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == PARSING
    aio.run(rr.kb.settle())
    return client.get(f"/api/knowledge/files/{r.json()['id']}").json()


def _reply(turn: dict) -> str:
    return next(i["text"] for i in reversed(turn["items"]) if i["type"] == "text")


# ---- 切块、分词、合并 ----


def test_chunks_split_parts_and_keep_their_numbers():
    parts = [Part("第一章", "短的一段。"), Part("第二章", "长句子。" * 400)]
    cs = text.chunks(parts)
    assert cs[0] == text.Chunk(1, 0, "第一章", "短的一段。")
    rest = [c for c in cs if c.part == 2]
    assert len(rest) > 1 and [c.idx for c in rest] == list(range(len(rest)))
    assert all(len(c.content) <= text.CHUNK_CHARS for c in rest) and all(c.content.endswith("。") for c in rest)
    assert text.head("合同.pdf", "第二章") == "合同.pdf · 第二章" and text.head("a.txt", "") == "a.txt"


def test_terms_drop_single_characters_and_punctuation():
    assert text.query_terms("违约金怎么算？Deadline 是哪天") == ["违约", "违约金", "怎么", "deadline", "哪天"]
    assert "的" not in text.doc_terms("乙方的违约金")


def test_rrf_rewards_chunks_found_both_ways():
    a, b, c = (Found("f", 1, i, "n", "", str(i)) for i in range(3))
    assert rrf([[a, b], [c, b]]) == [b, a, c]


# ---- 传、建索引、删 ----


def test_upload_index_preview_and_delete(ledger):
    rr, client = _api()
    f = _kb_upload(rr, client, "合同.md", PLAN.encode())
    assert f["status"] == READY and f["name"] == "合同.md"
    view = client.get("/api/knowledge").json()
    assert view["available"] and [x["id"] for x in view["files"]] == [f["id"]] and view["files"][0]["created_at"]
    assert view["limits"] == {"files": config.KB_MAX_FILES, "chars": config.KB_MAX_CHARS, "mb": 20}
    assert view["used"] == {"files": 1, "chars": f["file"]["chars"]}
    row = aio.run(rr.files.store.get(f["id"]))
    assert aio.run(rr.kb.chunks.count("u1")) == row.meta["chunks"] > 3
    # 知识库的文件不归哪段对话，不会被当成没发出去的清掉；不算对话里每天传的个数
    assert row.workspace_id == workspace("u1") and "stage" not in row.meta
    assert aio.run(rr.files.sweep(now=costs.now() + 2 * UNBOUND_TTL)) == 0
    assert aio.run(rr.files.store.uploads_since("u1", costs.now().replace(year=2000))) == 0
    # 建索引按 token 记在这个人名下，算给用户（额度）
    (row_cost,) = [r for r in ledger if r["purpose"] == costs.KB_INDEX]
    assert (row_cost["owner"], row_cost["model"], row_cost["turn_id"]) == ("u1", "hash", None) and row_cost["input_tokens"] > 0
    # 预览、原件
    parts = client.get(f"/api/knowledge/files/{f['id']}/parts").json()
    assert [p["title"] for p in parts["parts"]] == ["付款方式", "交付时间", "保密条款"]
    assert client.get(f"/api/knowledge/files/{f['id']}/original").content == PLAN.encode()
    # 别人看不到、删不了；对话里的接口也拿不到
    other = TestClient(api_main.app, headers=bearer(user_token("u2")))
    assert other.get(f"/api/knowledge/files/{f['id']}").status_code == 404
    assert other.get(f"/api/knowledge/files/{f['id']}/parts").status_code == 404
    assert other.delete(f"/api/knowledge/files/{f['id']}").status_code == 404
    assert client.delete(f"/api/files/{f['id']}").status_code == 404
    # 删了：块、原件都没了
    path = rr.files.root / row.storage
    assert client.delete(f"/api/knowledge/files/{f['id']}").json() == {"ok": True}
    assert not path.exists() and aio.run(rr.kb.chunks.count("u1")) == 0
    assert client.get(f"/api/knowledge/files/{f['id']}").status_code == 404 and client.get("/api/knowledge").json()["files"] == []


def test_guests_have_no_knowledge_base():
    rr, client = _api()
    guest = TestClient(api_main.app, headers=bearer(guest_token()))
    assert guest.get("/api/knowledge").json() | {"limits": 0} == {"available": False, "files": [], "limits": 0, "used": {"files": 0, "chars": 0}}
    r = guest.post("/api/knowledge/files", files={"file": ("a.md", b"hello world", "text/markdown")})
    assert (r.status_code, r.json()["detail"]) == (403, {"code": "file_rejected", "reason": "login_required", "message": "登录后才能用知识库"})


def test_limits_on_files_and_characters(monkeypatch):
    rr, client = _api()
    monkeypatch.setattr(config, "KB_MAX_FILES", 2)
    r = client.post("/api/knowledge/files", files={"file": ("a.png", b"\x89PNG\r\n\x1a\n" + b"\0" * 64, "image/png")})
    assert (r.status_code, r.json()["detail"]["reason"]) == (415, "image")  # 和对话里一样不收的
    a = _kb_upload(rr, client, "a.md", PLAN.encode())
    assert a["status"] == READY
    n = aio.run(rr.kb.chunks.count("u1"))
    monkeypatch.setattr(config, "KB_MAX_CHARS", a["file"]["chars"] + 100)
    b = _kb_upload(rr, client, "b.md", PLAN.encode())  # 加起来超了字数：没建索引
    assert (b["status"], b["reason"]) == (FAILED, "kb_chars") and aio.run(rr.kb.chunks.count("u1")) == n
    r = client.post("/api/knowledge/files", files={"file": ("c.md", b"hello there", "text/markdown")})
    assert (r.status_code, r.json()["detail"]) == (409, {"code": "file_rejected", "reason": "kb_full", "message": "知识库最多放 2 个文件", "limit": 2})


class Broken(HashEmbedder):
    ref = model = "dashscope:qwen3.7-text-embedding-flash"

    def __init__(self, fail_after: int = 0) -> None:
        self.calls, self.fail_after = 0, fail_after

    async def embed(self, texts):
        self.calls += 1
        if self.calls > self.fail_after:
            raise RuntimeError("向量接口 500")
        return await super().embed(texts)


def _kb(embedder=None, reranker=None) -> KnowledgeBase:
    files = FileService(MemoryFileStore(), config.FILES_DIR, isolated=False)
    return KnowledgeBase(files, MemoryChunkStore(), embedder or HashEmbedder(), reranker)


async def _upload(kb: KnowledgeBase, name: str, data: bytes):
    row = await kb.upload(Identity("u1", "user"), name, data)
    await kb.settle()
    return await kb.files.store.get(row.id)


def test_failed_indexing_is_not_billed(ledger):
    async def go():
        kb = _kb(Broken(fail_after=0))
        await kb.files.start()
        await kb.start()
        return kb, await _upload(kb, "a.md", PLAN.encode())
    kb, row = run_async(go())
    assert (row.status, row.meta["code"]) == (FAILED, "index_failed") and "stage" not in row.meta
    assert [r["purpose"] for r in ledger] == [costs.KB_INDEX_FAILED]


def test_indexing_resumes_after_a_restart():
    """解析好了、索引没建完服务就重启了：不重新解析，直接接着建。"""
    async def go():
        kb = _kb()
        store = kb.files.store
        await store.add(FileRow(id="file-0123456789abcdef", owner="u1", workspace_id=workspace("u1"), name="a.md", type="markdown", size=1,
                                storage="none"))  # 原件不在：重新解析就会失败
        await store.finish("file-0123456789abcdef", PARSING, {"chars": 20, "stage": INDEXING}, "", [Part("一", "重启以后接着建索引的文字。")])
        await kb.files.start()
        await kb.start()
        await kb.settle()
        return await store.get("file-0123456789abcdef"), await kb.chunks.count("u1")
    row, n = run_async(go())
    assert row.status == READY and n == 1


def test_deleted_while_indexing_leaves_no_chunks():
    class Slow(HashEmbedder):
        go: asyncio.Event

        async def embed(self, texts):
            await self.go.wait()
            return await super().embed(texts)

    async def go():
        slow = Slow()
        slow.go = asyncio.Event()
        kb = _kb(slow)
        await kb.files.start()
        await kb.start()
        who = Identity("u1", "user")
        row = await kb.upload(who, "a.md", PLAN.encode())
        await kb.files.settle()
        while (await kb.files.store.get(row.id)).meta.get("stage") != INDEXING:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)  # 建索引的已经在等向量了
        assert await kb.delete(who, row.id)
        slow.go.set()
        await kb.settle()
        return await kb.chunks.count("u1"), await kb.files.store.get(row.id)
    assert run_async(go()) == (0, None)


# ---- 查 ----


class Reverse:
    """假重排：倒过来排（看得出重排的结果用上了）。"""

    ref = "dashscope:qwen3.7-text-rerank"

    async def rerank(self, query, documents, top_n):
        return list(reversed(range(len(documents))))[:top_n], 100


class BrokenRerank(Reverse):
    async def rerank(self, query, documents, top_n):
        raise RuntimeError("重排 503")


def _search(kb: KnowledgeBase, query: str, docs: dict[str, bytes]):
    async def go():
        await kb.files.start()
        await kb.start()
        for name, data in docs.items():
            await _upload(kb, name, data)
        return await kb.search("u1", query)
    return run_async(go())


def test_search_finds_the_passage_and_names_its_file_and_part():
    hits = _search(_kb(), "逾期交付的违约金", {"合同.md": PLAN.encode(), "其他.md": "# 天气\n\n今天晴，适合出门散步。".encode()})
    assert (hits[0].name, hits[0].part, hits[0].title) == ("合同.md", 2, "交付时间") and "千分之一" in hits[0].content


def test_rerank_decides_the_order_and_its_failure_falls_back(ledger):
    docs = {"合同.md": PLAN.encode()}
    plain = _search(_kb(), "违约金 保密 首付款", docs)
    assert [h.part for h in _search(_kb(reranker=Reverse()), "违约金 保密 首付款", docs)] == [h.part for h in reversed(plain)]
    assert [h.part for h in _search(_kb(reranker=BrokenRerank()), "违约金 保密 首付款", docs)] == [h.part for h in plain]
    assert {r["model"] for r in ledger if r["purpose"] == costs.KB_SEARCH} == {"hash", "qwen3.7-text-rerank"}


def test_keywords_still_work_when_embedding_fails_and_nothing_found_is_an_error():
    async def go():
        kb = _kb(Broken(fail_after=1))  # 建索引那次成，查询时没成
        await kb.files.start()
        await kb.start()
        await _upload(kb, "合同.md", PLAN.encode())
        hits = await kb.search("u1", "违约金")
        with pytest.raises(SearchUnavailable):
            await kb.search("u1", "完全无关的词汇组合")
        return hits
    assert run_async(go())[0].part == 2


# ---- 对话里 ----


def test_conversation_searches_the_knowledge_base_and_cites_it(ledger):
    rr, client = _api()
    # 知识库里还没有文件时开始的对话：没有这个工具，之后传了也一样（文件列表第一轮取一次，前缀不变）
    before = last(events(client.post("/api/research/threads", json={"text": "知识库里 违约金"})), "state")
    assert _reply(before["turns"][0]) == "没有知识库可查。"
    f = _kb_upload(rr, client, "合同.md", PLAN.encode())
    v = last(events(client.post(f"/api/research/threads/{before['thread_id']}/messages", json={"text": "知识库里 违约金"})), "state")
    assert _reply(v["turns"][1]) == "没有知识库可查。"
    # 新对话：说明里列着文件，查到的段落是来源，过程里一步“查知识库”，按次收钱
    v = last(events(client.post("/api/research/threads", json={"text": "知识库里 逾期交付的违约金"})), "state")
    turn = v["turns"][0]
    (step,) = [i for i in turn["items"] if i["type"] == "step"]
    assert (step["kind"], step["text"], step["ok"]) == ("knowledge", "逾期交付的违约金", True) and step["hits"] >= 1
    sid = step["source_ids"][0]
    assert _reply(turn) == f"知识库里说 [{sid}]。"
    src = v["sources"][sid]
    assert src | {"retrieved_at": "", "excerpt": ""} == {
        "id": sid, "url": f"kb://{f['id']}/2", "title": "合同.md · 交付时间", "retrieved_at": "", "published_at": None, "excerpt": "",
        "excerpt_kind": "knowledge", "file_id": f["id"], "part": 2}
    assert "千分之一" in src["excerpt"]
    state = rr.graph.get_state(rr._cfg(v["thread_id"])).values
    assert state["kb_files"] == ["合同.md"]
    tool = next(m for m in state["messages"] if getattr(m, "name", None) == "search_knowledge")
    assert tool.content.startswith("Passages from the user's knowledge base") and f"Source id: {sid}" in tool.content
    assert f"(file id {f['id']}, part 2)" in tool.content
    assert any(r["purpose"] == costs.TOOL and r["model"] == "search_knowledge" and r["yuan"] == 0.005 for r in ledger)
    # 同一段对话里再查：同一段还是同一个来源 id
    v = last(events(client.post(f"/api/research/threads/{v['thread_id']}/messages", json={"text": "知识库里 违约金按多少算"})), "state")
    (step,) = [i for i in v["turns"][1]["items"] if i["type"] == "step"]
    assert sid in step["source_ids"]


def test_model_sees_the_file_list_in_the_tool_description():
    rr, client = _api()
    _kb_upload(rr, client, "合同.md", PLAN.encode())
    seen = []
    model = rr.planner._model
    real = type(model).bind_tools

    def spy(self, tools, **kw):
        seen.append({getattr(t, "name", ""): getattr(t, "description", "") for t in tools})
        return real(self, tools, **kw)
    type(model).bind_tools = spy
    try:
        last(events(client.post("/api/research/threads", json={"text": "直接答：好"})), "state")
    finally:
        type(model).bind_tools = real
    assert "- 合同.md\n" in seen[0]["search_knowledge"]


def test_read_file_reads_knowledge_base_files_of_this_user_only():
    rr, client = _api()
    f = _kb_upload(rr, client, "合同.md", PLAN.encode())
    v = last(events(client.post("/api/research/threads", json={"text": f"文件里第3段 编号{f['id']}"})), "state")
    (step,) = [i for i in v["turns"][0]["items"] if i["type"] == "step"]
    assert step["ok"] and step["text"] == "合同.md" and "保密" in _reply(v["turns"][0])
    other = TestClient(api_main.app, headers=bearer(user_token("u2")))
    v = last(events(other.post("/api/research/threads", json={"text": f"文件里第3段 编号{f['id']}"})), "state")
    assert _reply(v["turns"][0]) == "没读成。"
