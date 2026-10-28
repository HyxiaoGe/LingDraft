"""调研场景离线测试：假模型按固定脚本调工具，网关用 MockTransport。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.cli import interrupt_view, pending, research_resume
from app.graph.research.agent import (
    ASK_BLOCKED,
    DANGLING_NOTE,
    LIMIT_MARK,
    CallLimitMiddleware,
    answer_dangling_calls,
    ask_resume,
    ask_result,
    build_agent,
    collect_asks,
    collect_sources,
    final_answer_text,
    is_ask_interrupt,
)
from app.graph.research.graph import build_graph, memory_checkpointer, open_sqlite_checkpointer
from app.graph.research.state import MAX_ASKS, MAX_SEARCHES
from app.research_llm import FakeResearchPlanner

REQUEST = "AI 海报工具在局部修改上的现状（2026）"


def cfg(tid: str) -> dict:
    return {"configurable": {"thread_id": tid}}


def intr(result: dict) -> dict | None:
    ints = result.get("__interrupt__") or []
    return ints[0].value if ints else None


@pytest.fixture
def planner() -> FakeResearchPlanner:
    return FakeResearchPlanner()


@pytest.fixture
def graph(planner, tmp_path: Path):
    return build_graph(planner, memory_checkpointer(), tmp_path / "out")


def resume(graph, tid: str, payload: dict) -> dict:
    """像 CLI / 接口层那样：先过同一份校验、转成图要的格式，再按中断 id 恢复。"""
    return graph.invoke(research_resume(graph, cfg(tid), payload), cfg(tid))


def pkg_of(p: dict) -> Path:
    """终审卡对应的下载包目录（每版写报告时一起写好）。"""
    return Path(p["report_path"]).parent / "package"


def run_to_review(graph, tid: str = "t1", answer: dict | None = None) -> dict:
    r = graph.invoke({"request": REQUEST}, cfg(tid))
    p = intr(r)
    assert p["type"] == "research.scope"
    assert p["scope"]["question"] and [q["id"] for q in p["questions"]] == ["q1"] and p["questions"][0]["why"]
    r = resume(graph, tid, {"answers": {"q1": "改文字"}})
    p = intr(r)
    assert is_ask_interrupt(p)
    view = interrupt_view(p)
    assert view["type"] == "research.ask" and view["options"] == ["包含", "不包含"] and view["why"]
    r = resume(graph, tid, answer or {"answer": "包含"})
    p = intr(r)
    assert p["type"] == "research.review"
    return p


def test_full_flow_traceable_and_delivered(graph, tmp_path):
    p = run_to_review(graph)
    assert p["version"] == 1 and p["answer_how"] == "agent"
    assert all(c["passed"] for c in p["checks"]), p["checks"]
    sources = {s["id"]: s for s in p["sources"]}
    claims = p["claims"]
    # 整理步骤按候选答案分了两节；每条论断都能回指到系统选用的来源；事实 / 推断可辨
    assert [s["id"] for s in p["sections"]] == ["c1", "c2"]
    assert len(claims) == 3 and {c["outline_id"] for c in claims} == {"c1", "c2"}
    for c in claims:
        assert c["source_ids"] and all(sources[s]["status"] == "adopted" for s in c["source_ids"])
    assert {c["kind"] for c in claims} == {"fact", "inference"}
    # 读过正文的来源用正文片段，其他是搜索摘要
    assert {s["excerpt_kind"] for s in sources.values()} == {"body", "summary"}
    # 无来源的论断被丢弃并记录；挂在它上面的句子也从正文移除，不能变成“不需要引用的过渡句”
    assert any("丢弃无来源论断" in n for n in p["notes"])
    assert "没有来源的论断应被丢弃" not in p["report"] and any("一起从正文移除" in n for n in p["notes"])
    # 报告里推断句带标记并带引用标号；来源表完整
    report = p["report"]
    assert "推断：" in report and "[S1]" in report and "## 来源" in report
    # 动态提问的回答原样进报告单独一节、不进假设；澄清的回答进了范围
    assert "## 用户的回答" in report and "范围是否包含 PPT 工具？ → 包含" in report
    assert not any("包含" in a for a in p["assumptions"])
    assert "改文字" in report

    # 这一版写出来就有下载包，不等批准；论断状态照实写（系统交付不等于人工确认）
    pkg = pkg_of(p)
    assert {f.name for f in pkg.iterdir()} == {"manifest.json", "report.md", "items.json", "sources.json"}
    assert (pkg / "report.md").read_text() == p["report"]
    items = json.loads((pkg / "items.json").read_text())
    kinds = {i["kind"] for i in items}
    assert {"scope", "outline", "claim.fact", "claim.inference"} <= kinds
    assert all(i["status"] == "draft" for i in items if i["kind"].startswith("claim."))
    r = resume(graph, "t1", {"action": "approve"})
    assert r["approved"] and graph.get_state(cfg("t1")).next == ()
    adopted = json.loads((pkg / "sources.json").read_text())
    assert adopted and all(s["status"] == "adopted" for s in adopted)
    manifest = json.loads((pkg / "manifest.json").read_text())
    assert manifest["scenario"] == "research" and manifest["thread_id"] == "t1" and manifest["version"] == 1 and manifest["answered"]


def test_exclude_source_orphans_dependent_claim(graph):
    p = run_to_review(graph)
    fact = next(c for c in p["claims"] if c["kind"] == "fact" and c["outline_id"] == "c1")
    sid = fact["source_ids"][0]
    r = resume(graph, "t1", {"action": "exclude_source", "target": sid})
    p2 = intr(r)
    assert p2["type"] == "research.review" and p2["version"] == 2
    src = next(s for s in p2["sources"] if s["id"] == sid)
    assert src["status"] == "excluded"
    orphan = next(c for c in p2["claims"] if c["id"] == fact["id"])
    assert orphan["orphaned"] and orphan["status"] == "draft" and orphan["source_ids"] == []
    assert any(fact["id"] in n and "失去来源支撑" in n for n in p2["notes"])
    # 报告里不再出现这条论断，校验仍全部通过；只重写了受影响的节
    assert fact["statement"] not in p2["report"]
    assert all(c["passed"] for c in p2["checks"]), p2["checks"]
    # 改完出第 2 版，下载包跟着换成第 2 版；被排除的来源不在包里
    pkg = pkg_of(p2)
    assert json.loads((pkg / "manifest.json").read_text())["version"] == 2
    assert fact["id"] not in {i["id"] for i in json.loads((pkg / "items.json").read_text())}
    assert sid not in {s["id"] for s in json.loads((pkg / "sources.json").read_text())}


def test_edit_claim_marks_user_edited_and_rewrites(graph, planner):
    p = run_to_review(graph)
    target = p["claims"][0]
    r = resume(graph, "t1", {"action": "edit_claim", "target": target["id"], "content": "用户改过的论断"})
    p2 = intr(r)
    c = next(c for c in p2["claims"] if c["id"] == target["id"])
    assert c["status"] == "user_edited" and c["statement"] == "用户改过的论断"
    assert "用户改过的论断" in p2["report"]
    assert [x for x in planner.calls if x[0] == "write"][-1][1] == target["outline_id"]


def test_rewrite_section_with_instruction(graph, planner):
    run_to_review(graph)
    r = resume(graph, "t1", {"action": "rewrite_section", "target": "c2", "instruction": "更简短"})
    p = intr(r)
    assert p["version"] == 2 and "更简短" in p["report"]
    writes = [x for x in planner.calls if x[0] == "write"]
    assert writes[-1] == ("write", "c2", "更简短")
    # 初稿没有调用重写：直接拼装整理结果
    assert len(writes) == 1


def test_free_text_answer_kept_verbatim(graph):
    # 自由文本不做关键词判断：原样记录，“不确定预算，但禁止出网”这种混合回答不会被当成跳过
    p = run_to_review(graph, answer={"answer": "不确定预算，但公司明确禁止合同内容出网"})
    assert "范围是否包含 PPT 工具？ → 不确定预算，但公司明确禁止合同内容出网" in p["report"]
    assert not any("跳过" in a for a in p["assumptions"])


def test_skip_ask_records_assumption(graph):
    p = run_to_review(graph, answer={"skip": True})
    assert any("用户跳过了" in a and "PPT" in a for a in p["assumptions"])
    assert "## 用户的回答" not in p["report"]


def test_invalid_scope_rejected(graph):
    graph.invoke({"request": REQUEST}, cfg("t1"))
    with pytest.raises(ValueError, match="不是当前问题"):
        resume(graph, "t1", {"answers": {"nope": "x"}})
    assert is_ask_interrupt(intr(resume(graph, "t1", {"answers": {"q1": "改文字"}})))  # 被拒的回答不留痕，正确的照常继续


def test_simple_question_one_section_no_cards(graph):
    """需求清楚、问题简单：不弹范围卡，agent 不提问，整理成一节，直接到终审。"""
    r = graph.invoke({"request": "最近新加坡的天气情况如何？"}, cfg("t1"))
    p = intr(r)
    assert p["type"] == "research.review"
    v = graph.get_state(cfg("t1")).values
    assert v["scope_confirmed"] and v["clarify"] == [] and v["asks_used"] == 0
    assert [s["id"] for s in p["sections"]] == ["c1"] and len(p["claims"]) == 2


def test_clarify_skip_records_assumption(graph):
    r = graph.invoke({"request": REQUEST}, cfg("t2"))
    r = resume(graph, "t2", {"answers": {"q1": None}})
    assert is_ask_interrupt(intr(r))
    assert any("未回答" in a for a in graph.get_state(cfg("t2")).values["assumptions"])


def test_gap_fill_runs_research_once_more(graph, planner):
    r = graph.invoke({"request": "某题（回补）"}, cfg("t1"))
    p = intr(r)
    assert p["type"] == "research.review"
    v = graph.get_state(cfg("t1")).values
    assert v["gap_rounds"] == 1 and v["gap"] is None
    assert sum(1 for c in planner.calls if c[0] == "consolidate") == 2
    assert any("回研究环节补一轮" in n for n in p["notes"])


def test_budget_exhausted_tool_removed_and_still_answers(graph):
    """模型一直搜：搜够上限后工具被摘掉，模型只能作答；执行层没有多执行一次。"""
    r = graph.invoke({"request": "一直搜（不收尾）"}, cfg("t1"))
    p = intr(r)
    assert p["type"] == "research.review" and p["answer_how"] == "agent"
    v = graph.get_state(cfg("t1")).values
    assert len(v["sources"]) == 3 * MAX_SEARCHES


def test_no_text_triggers_controlled_close_out(graph):
    r = graph.invoke({"request": "一直搜（无答案）"}, cfg("t1"))
    p = intr(r)
    assert p["type"] == "research.review" and p["answer_how"] == "close"
    assert any("没有给出文字答案" in n for n in p["notes"])
    check = next(c for c in p["checks"] if c["name"] == "answer_present")
    assert check["passed"] and "收尾" in check["detail"]
    assert "收尾" in graph.get_state(cfg("t1")).values["answer"]


def test_invalid_tool_call_is_retried(graph):
    r = graph.invoke({"request": "题（坏JSON）"}, cfg("t1"))
    p = intr(r)
    assert p["type"] == "research.review" and p["answer_how"] == "agent" and p["claims"]


def test_resume_across_graph_instances_with_sqlite(planner, tmp_path):
    """中断在子图里发生；换一个进程（新的图实例、同一个 sqlite）也要能接着跑。"""
    db = tmp_path / "cp.sqlite"
    g1 = build_graph(planner, open_sqlite_checkpointer(db), tmp_path / "out")
    r = g1.invoke({"request": REQUEST}, cfg("t1"))
    r = resume(g1, "t1", {"answers": {"q1": "改文字"}})
    assert is_ask_interrupt(intr(r))
    g2 = build_graph(FakeResearchPlanner(), open_sqlite_checkpointer(db), tmp_path / "out")
    assert is_ask_interrupt(pending(g2, cfg("t1")).value)
    r = resume(g2, "t1", {"answer": "包含"})
    assert intr(r)["type"] == "research.review"


def test_build_agent_without_ask_drops_tool(planner):
    agent = build_agent(planner.agent_model(), planner.tools(), "sys", ask_limit=0)
    names = {t.name for t in agent.get_graph().nodes["tools"].data.tools_by_name.values()} if "tools" in agent.get_graph().nodes else set()
    assert "ask_user" not in names


def test_final_answer_text_ignores_limit_message_and_tool_calls():
    """超限消息靠中间件打的标记识别，不看文字：模型自己写的同样开头的话照样是答案。"""
    limit = CallLimitMiddleware(run_limit=30, exit_behavior="end").before_model({"messages": [], "run_model_call_count": 30}, None)
    marked = limit["messages"][0]
    assert limit["jump_to"] == "end" and marked.response_metadata[LIMIT_MARK]
    msgs = [
        AIMessage(content="答案", ),
        AIMessage(content="", tool_calls=[{"name": "search_web", "args": {}, "id": "1", "type": "tool_call"}]),
        marked,
    ]
    assert final_answer_text(msgs) == "答案"
    assert final_answer_text(msgs[1:]) is None
    assert final_answer_text([AIMessage(content="Model call limits exceeded 是这个库的提示语")]) == "Model call limits exceeded 是这个库的提示语"


def test_agent_hitting_call_limit_has_no_answer(planner, monkeypatch):
    import app.graph.research.agent as agent_mod

    monkeypatch.setattr(agent_mod, "MAX_MODEL_CALLS", 2)
    agent = build_agent(planner.agent_model(), planner.tools(), "sys", ask_limit=0)
    res = agent.invoke({"messages": [("user", "一直搜（不收尾）")]})
    assert res["messages"][-1].response_metadata.get(LIMIT_MARK)
    assert final_answer_text(res["messages"]) is None
    assert "ModelCallLimitMiddleware.before_model" in agent.get_graph().nodes  # 节点名沿用父类，旧检查点能接着跑


def test_collect_sources_prefers_body_excerpt():
    hits = [{"url": "https://a.test/1", "title": "A", "description": "摘要", "published_at": "2026-01-01"}]
    msgs = [
        ToolMessage(content=json.dumps(hits), name="search_web", tool_call_id="1"),
        ToolMessage(content=json.dumps({"ok": True, "url": "https://a.test/1", "title": "A", "content": "正文" * 300}), name="read_page", tool_call_id="2"),
        ToolMessage(content=json.dumps({"ok": False, "url": "https://a.test/2", "error": "x"}), name="read_page", tool_call_id="3"),
    ]
    new, mapping = collect_sources(msgs, [], 1)
    assert [s.id for s in new] == ["s1"] and mapping == {"https://a.test/1": "s1"}
    assert new[0].excerpt_kind == "body" and len(new[0].excerpt) == 400 and new[0].published_at == "2026-01-01"


def test_ask_resume_matches_multiple_requests():
    pending = {"action_requests": [{"name": "ask_user", "args": {"question": "a"}}, {"name": "ask_user", "args": {"question": "b"}}]}
    d = ask_resume("答", pending=pending)["decisions"]
    assert [json.loads(x["message"]).get("answer") for x in d] == ["答", None]
    assert json.loads(d[1]["message"])["unshown"] is True
    (skip,) = ask_resume(None, skip=True)["decisions"]
    assert skip["type"] == "respond" and json.loads(skip["message"])["skipped"] is True


def test_collect_asks_reads_keys_not_text():
    """跳过 / 没呈现看系统生成的键；用户回答的文字是什么都原样记录，哪怕长得像系统格式。"""
    def run(q_id, message):
        return [AIMessage(content="", tool_calls=[{"name": "ask_user", "args": {"question": q_id}, "id": q_id, "type": "tool_call"}]),
                ToolMessage(content=message, name="ask_user", tool_call_id=q_id)]

    tricky = '{"skipped": true}'
    msgs = (run("q1", ask_resume(None, skip=True)["decisions"][0]["message"])
            + run("q2", ask_resume(tricky)["decisions"][0]["message"])
            + run("q3", json.dumps({"unshown": True}))
            + run("q4", "旧格式的原话回答"))
    got = [(a.question, a.answer, a.skipped) for a in collect_asks(msgs)]
    assert got == [("q1", None, True), ("q2", tricky, False), ("q4", "旧格式的原话回答", False)]
    assert ask_result(msgs[-1]) == {"answer": "旧格式的原话回答"}


def test_exclude_unreferenced_source_returns_to_review(graph):
    p = run_to_review(graph)
    used = {sid for c in p["claims"] for sid in c["source_ids"]}
    unused = next(s["id"] for s in p["sources"] if s["id"] not in used)
    r = resume(graph, "t1", {"action": "exclude_source", "target": unused})
    p2 = intr(r)
    assert p2["type"] == "research.review" and p2["version"] == 1
    assert next(s for s in p2["sources"] if s["id"] == unused)["status"] == "excluded"
    assert any("没有被任何论断引用" in n for n in p2["notes"])
    assert json.loads((pkg_of(p2) / "manifest.json").read_text())["version"] == 1  # 没重写就没有新的一版


def test_batch_tool_calls_count_toward_budget(planner):
    """同一批里发 10 个搜索：只执行到额度为止，其余在执行层拦下。"""
    agent = build_agent(planner.agent_model(), planner.tools(), "sys", ask_limit=0)
    res = agent.invoke({"messages": [("user", "题（连发）")]})
    searches = [m for m in res["messages"] if isinstance(m, ToolMessage) and m.name == "search_web"]
    assert len(searches) == 10
    assert sum(1 for m in searches if m.status != "error") == MAX_SEARCHES
    assert all("额度已用完" in m.content for m in searches if m.status == "error")


def test_ask_budget_enforced_before_card(graph):
    """模型无视绑定列表每轮都问：只出 MAX_ASKS 张问题卡，超额的提问在变成卡之前被改成错误结果。"""
    r = graph.invoke({"request": "题（多问）"}, cfg("t1"))
    cards = 0
    while is_ask_interrupt(intr(r)):
        cards += 1
        assert cards <= MAX_ASKS + 1
        r = resume(graph, "t1", {"answer": f"答 {cards}"})
    assert cards == MAX_ASKS
    p = intr(r)
    assert p["type"] == "research.review"
    v = graph.get_state(cfg("t1")).values
    assert v["asks_used"] == MAX_ASKS
    assert all(c["passed"] for c in p["checks"]), p["checks"]


def test_unanswered_without_gap_text_still_refills(graph, planner):
    r = graph.invoke({"request": "题（没回答）"}, cfg("t1"))
    p = intr(r)
    assert p["type"] == "research.review"
    v = graph.get_state(cfg("t1")).values
    assert v["gap_rounds"] == 1 and v["answered"] is True
    assert sum(1 for c in planner.calls if c[0] == "consolidate") == 2
    assert next(c for c in p["checks"] if c["name"] == "question_answered")["passed"]


def test_close_out_history_answers_dangling_tool_calls():
    msgs = [
        HumanMessage("题"),
        AIMessage(content="", tool_calls=[{"name": "search_web", "args": {}, "id": "ok1", "type": "tool_call"}]),
        ToolMessage(content="[]", name="search_web", tool_call_id="ok1"),
        AIMessage(content="", invalid_tool_calls=[{"name": "search_web", "args": "{bad", "id": "bad1", "error": "x", "type": "invalid_tool_call"}]),
    ]
    out = answer_dangling_calls(msgs)
    assert len(out) == 5 and isinstance(out[-1], ToolMessage) and out[-1].tool_call_id == "bad1" and out[-1].content == DANGLING_NOTE
    assert answer_dangling_calls(msgs[:3]) == msgs[:3]
    assert ASK_BLOCKED != "ask_user"


# ---- 第 3 批：预算、回补、重写、提问计数、来源状态、交付 ----


def test_batch_with_ask_still_counts_toward_budget(graph):
    """同一批 10 个搜索里夹一个提问：回答提问后执行剩下的调用，额度照样按批算，不会因为提问而全放过。"""
    r = graph.invoke({"request": "题（连发带问）"}, cfg("t1"))
    assert is_ask_interrupt(intr(r))
    p = intr(resume(graph, "t1", {"answer": "包含"}))
    assert p["type"] == "research.review"
    v = graph.get_state(cfg("t1")).values
    assert len(v["sources"]) == 3 * MAX_SEARCHES  # 每次成功的搜索带回 3 条候选


def test_unshown_ask_does_not_use_budget(graph):
    """模型同一批发两个提问，只有第一个呈现给用户；没呈现的不占额度，之后还能再问一次。"""
    r = graph.invoke({"request": "题（双问）"}, cfg("t1"))
    titles = []
    while is_ask_interrupt(intr(r)):
        titles.append(interrupt_view(intr(r))["title"])
        assert len(titles) <= MAX_ASKS
        r = resume(graph, "t1", {"answer": f"答 {len(titles)}"})
    assert titles == ["同批第 1 问？", "后来又问一个？"]
    v = graph.get_state(cfg("t1")).values
    assert v["asks_used"] == 2 and [a.question for a in v["asks"]] == titles


def test_gap_round_without_answer_keeps_first_result(graph):
    """回补那一轮没拿到答案：第一轮的答案和论断都留着，报告照常给出，并写明还缺什么。"""
    r = graph.invoke({"request": "某题（回补，补查失败）"}, cfg("t1"))
    p = intr(r)
    assert p["type"] == "research.review" and p["claims"]
    v = graph.get_state(cfg("t1")).values
    assert v["answer"] and "补查失败" in v["answer"] and v["answered"] is False
    assert any("沿用上一轮" in n for n in p["notes"])
    check = next(c for c in p["checks"] if c["name"] == "question_answered")
    assert not check["passed"] and "缺价格数据" in check["detail"]
    assert "没有完全回答" in p["report"] and "缺价格数据" in p["report"]
    assert (pkg_of(p) / "report.md").read_text() == p["report"]


def test_gap_round_consolidate_failure_keeps_first_result(graph, planner):
    r = graph.invoke({"request": "某题（回补，二次整理失败）"}, cfg("t1"))
    p = intr(r)
    assert sum(1 for c in planner.calls if c[0] == "consolidate") == 2
    assert p["type"] == "research.review" and p["claims"] and p["sections"]
    assert any("保留上一轮" in n for n in p["notes"])
    assert graph.get_state(cfg("t1")).values["answered"] is False
    assert "缺价格数据" in p["report"]


def test_source_status_follows_current_report(graph):
    """回补后重新整理：只有现在报告里引用的来源算系统选用，第一轮引用、这一轮没用上的退回候选。"""
    p = intr(graph.invoke({"request": "某题（回补）"}, cfg("t1")))
    cited = {sid for c in p["claims"] if not c["orphaned"] for sid in c["source_ids"]}
    adopted = {s["id"] for s in p["sources"] if s["status"] == "adopted"}
    assert cited and adopted == cited
    assert {s["id"] for s in json.loads((pkg_of(p) / "sources.json").read_text())} == cited


def test_source_status_recomputed_from_sections():
    from app.graph.research.report import source_status
    from app.graph.research.state import Claim, SectionText, Sentence, Source

    sources = [Source(id="s1", url="u1", status="adopted"), Source(id="s2", url="u2", status="adopted"),
               Source(id="s3", url="u3", status="excluded"), Source(id="s4", url="u4")]
    claims = [Claim(id="k1", statement="a", kind="fact", source_ids=["s1"], outline_id="c1"),
              Claim(id="k2", statement="b", kind="fact", source_ids=["s2"], outline_id="c1")]
    sections = {"c1": SectionText(outline_id="c1", sentences=[Sentence(text="a", claim_id="k1")])}  # 重写后 k2 不在正文里了
    assert [s.status for s in source_status(sources, sections, claims)] == ["adopted", "candidate", "excluded", "candidate"]


def test_rewrite_gets_user_constraints(graph, planner):
    """重写某一节时带上范围和用户的回答，不会写出和用户已确认的前提冲突的内容。"""
    run_to_review(graph)
    resume(graph, "t1", {"action": "rewrite_section", "target": "c1", "instruction": "更简短"})
    got = planner.write_inputs[-1]
    assert "改文字" in (got["scope"].extra or "")
    assert got["user_facts"] == ["范围是否包含 PPT 工具？ → 包含"]


def test_live_rewrite_prompt_carries_scope_and_user_facts():
    from app.graph.research.state import Claim, OutlineNode, Scope
    from app.research_llm import LiveResearchPlanner

    scope = Scope(question="选哪个向量库？", time_range="2026", extra="部署方式：只能私有化")
    msgs = LiveResearchPlanner._write_messages(
        scope, OutlineNode(id="c1", title="结论", question="选哪个？"),
        [Claim(id="k1", statement="A 支持私有化", kind="fact", source_ids=["s1"], outline_id="c1")],
        "更简短", ["数据能不能出网？ → 不能出网"],
    )
    user = msgs[-1][1]
    assert "只能私有化" in user and "2026" in user and "不能出网" in user and "更简短" in user and "k1" in user


def test_invalid_report_is_still_delivered(graph):
    """整理失败、报告里没有带来源的论断：照样给出这一版和下载包，校验结果只是说明。"""
    p = intr(graph.invoke({"request": "题（整理失败）"}, cfg("t1")))
    assert p["type"] == "research.review" and p["version"] == 1 and p["claims"] == []
    failed = {c["name"] for c in p["checks"] if not c["passed"]}
    assert {"report_valid", "question_answered"} <= failed
    assert any("整理失败" in n for n in p["notes"]) and "没有完全回答" in p["report"]
    pkg = pkg_of(p)
    assert {f.name for f in pkg.iterdir()} == {"manifest.json", "report.md", "items.json", "sources.json"}
    manifest = json.loads((pkg / "manifest.json").read_text())
    assert manifest["version"] == 1 and manifest["answered"] is False
    r = resume(graph, "t1", {"action": "approve"})  # “不用改了”只是结束
    assert r["approved"] and graph.get_state(cfg("t1")).next == ()


# ---- 用户的话交给模型理解；标记、时间范围不靠代码匹配文字 ----


def _plan(*ops, reply=None):
    from app.research_llm import ReviewOp, ReviewPlan

    return ReviewPlan(ops=[ReviewOp(**{"target": None, "content": None, "instruction": None, **o}) for o in ops], reply=reply)


def test_free_text_goes_to_model_and_is_applied(graph, planner):
    """终审时用户直接说的话原样交给模型；模型理解出的操作和按钮走同一套校验和应用。"""
    run_to_review(graph)
    planner.review_plans = [_plan({"action": "rewrite_section", "target": "c1", "instruction": "更简短"})]
    p = intr(resume(graph, "t1", {"action": "instruct", "text": "第一节更简短"}))
    assert ("interpret", "第一节更简短") in planner.calls
    assert p["version"] == 2 and ("write", "c1", "更简短") in planner.calls
    assert graph.get_state(cfg("t1")).values["versions"][-1].note == "重写「局部修改机制」：更简短"
    assert not any(c[0] == "write" and c[1] == "c2" for c in planner.calls)
    assert any(n.startswith("按你的话（第一节更简短）理解为：重写「局部修改机制」：更简短") for n in p["notes"])
    assert p["reply"] is None


def test_free_text_with_two_requests(graph, planner):
    p = run_to_review(graph)
    sid = next(c["source_ids"][0] for c in p["claims"] if c["outline_id"] == "c2")
    planner.review_plans = [_plan({"action": "exclude_source", "target": sid},
                                  {"action": "rewrite_section", "target": "c1", "instruction": "先给结论"})]
    p2 = intr(resume(graph, "t1", {"action": "instruct", "text": "排掉那条来源，第一节先给结论"}))
    assert p2["version"] == 2
    assert next(s for s in p2["sources"] if s["id"] == sid)["status"] == "excluded"
    writes = [c for c in planner.calls if c[0] == "write"]
    assert writes == [("write", "c1", "先给结论")]  # 要求只给 c1；c2 唯一的论断失去来源，正文留空不调模型
    assert any("「事实准确性」没有可用论断" in n for n in p2["notes"])


def test_free_text_not_understood_returns_reply(graph, planner):
    """模型对不上（比如用户的话有歧义）：不改报告，带着模型的回复回到终审，回复只出现在下一张卡上。"""
    run_to_review(graph)
    planner.review_plans = [_plan(reply="S2 和 s2 指的不是同一条来源，你要排除的是哪一条？")]
    p = intr(resume(graph, "t1", {"action": "instruct", "text": "排除 S2"}))
    assert p["type"] == "research.review" and p["version"] == 1
    assert p["reply"] == "S2 和 s2 指的不是同一条来源，你要排除的是哪一条？"
    assert all(s["status"] != "excluded" for s in p["sources"])
    p2 = intr(resume(graph, "t1", {"action": "rewrite_section", "target": "c1"}))
    assert p2["version"] == 2 and p2["reply"] is None


def test_free_text_with_bad_target_keeps_valid_ops(graph, planner):
    """模型给的对象不存在：那一条不做并告诉用户，其余照做。"""
    run_to_review(graph)
    planner.review_plans = [_plan({"action": "exclude_source", "target": "s99"},
                                  {"action": "rewrite_section", "target": "c2", "instruction": "多说明局限"})]
    p = intr(resume(graph, "t1", {"action": "instruct", "text": "去掉第九十九条来源，第二节多说局限"}))
    assert p["version"] == 2 and ("write", "c2", "多说明局限") in planner.calls
    assert "s99" in (p["reply"] or "")


def test_free_text_approve_alone_ends_but_not_with_changes(graph, planner):
    run_to_review(graph, "t1")
    planner.review_plans = [_plan({"action": "approve"})]
    r = resume(graph, "t1", {"action": "instruct", "text": "可以了"})
    assert r["approved"] and graph.get_state(cfg("t1")).next == ()

    run_to_review(graph, "t2")
    planner.review_plans = [_plan({"action": "approve"}, {"action": "rewrite_section", "target": "c1", "instruction": "更简短"})]
    p = intr(resume(graph, "t2", {"action": "instruct", "text": "挺好，不过第一节再短点"}))
    assert p["type"] == "research.review" and p["version"] == 2  # 有修改就只做修改，用户看过新一版再说结束


def test_free_text_model_failure_returns_to_review(graph, planner, monkeypatch):
    run_to_review(graph)

    def boom(*a, **k):
        raise RuntimeError("超时")

    monkeypatch.setattr(planner, "interpret_review", boom)
    p = intr(resume(graph, "t1", {"action": "instruct", "text": "第一节更简短"}))
    assert p["type"] == "research.review" and p["version"] == 1 and "没能处理" in p["reply"]


def test_instruct_requires_text(graph):
    from app.graph.research.answers import InvalidAnswer

    run_to_review(graph)
    with pytest.raises(InvalidAnswer):
        resume(graph, "t1", {"action": "instruct", "text": "  "})
    with pytest.raises(InvalidAnswer):
        resume(graph, "t1", {"action": "instruct", "text": "x", "target": "c1"})


def test_live_interpret_prompt_carries_ids_and_report_numbers():
    from app.graph.research.state import Claim, OutlineNode, Scope, Source
    from app.research_llm import LiveResearchPlanner

    msgs = LiveResearchPlanner._interpret_messages(
        Scope(question="问题"), "# 问题\n\n## 结论\n\n一句话[S3]\n",
        [OutlineNode(id="c1", title="结论", question="?")],
        [Claim(id="k1", statement="一句话", kind="fact", source_ids=["s3"], outline_id="c1")],
        [Source(id="s3", url="https://a.test", title="A", status="adopted"), Source(id="s1", url="https://b.test", title="B")],
        "排除 S1",
    )
    user = msgs[-1][1]
    assert "- c1 | 结论" in user and "- k1 | c1 | fact | 一句话 | s3" in user
    assert "- s3 | A | https://a.test | 报告里引用了" in user and "- s1 | B | https://b.test | 搜到但报告没引用" in user
    assert "用户说：排除 S1" in user


def test_inference_marked_from_claim_kind_not_text(graph):
    """推断句的标记由代码按论断类型加（假模型不写前缀）；页面用的块和下载的 report.md 出自同一份数据。"""
    from app.graph.research.report import to_markdown

    p = run_to_review(graph)
    kind = {c["id"]: c["kind"] for c in p["claims"]}
    sents = [s for sec in p["sections"] for s in sec["sentences"] if s["claim_id"]]
    assert sents and all(s["text"].startswith("推断：") == (kind[s["claim_id"]] == "inference") for s in sents)
    blocks = [b for b in p["blocks"] if b["type"] == "sentence" and b["claim_id"]]
    assert all(b["inference"] == (kind[b["claim_id"]] == "inference") and b["marks"] for b in blocks)
    assert to_markdown(p["blocks"]) == p["report"] == (pkg_of(p) / "report.md").read_text()
    # 报告里的来源标注就是来源 id：排除一条来源后，其余来源的标注不变
    labels = {b["source_id"]: b["label"] for b in p["blocks"] if b["type"] == "source"}
    assert labels and all(lab == sid.upper() for sid, lab in labels.items())
    first = next(iter(labels))
    p2 = intr(resume(graph, "t1", {"action": "exclude_source", "target": first}))
    labels2 = {b["source_id"]: b["label"] for b in p2["blocks"] if b["type"] == "source"}
    assert first not in labels2 and all(labels[s] == lab for s, lab in labels2.items())
    assert "inference_marked" not in {c["name"] for c in p["checks"]}


def test_time_range_judged_by_model(graph, planner):
    """来源在不在时间范围里由整理步骤的模型判断；校验只看正文用到的、被标出来的来源。"""
    from app.research_llm import TimeIssue

    planner.time_issues = [TimeIssue(source_id="s1", reason="发布于 2023 年，不在 2026 年内"),
                           TimeIssue(source_id="s99", reason="不存在的来源")]
    p = run_to_review(graph)
    cited = {sid for c in p["claims"] for sid in c["source_ids"]}
    check = next(c for c in p["checks"] if c["name"] == "scope_time")
    assert ("s1" in cited) and not check["passed"] and "2023" in check["detail"] and "s99" not in check["detail"]
    assert graph.get_state(cfg("t1")).values["time_issues"] == {"s1": "发布于 2023 年，不在 2026 年内"}


def test_time_check_ignores_uncited_sources():
    from app.graph.research.report import run_checks
    from app.graph.research.state import Claim, SectionText, Sentence, Source

    sources = [Source(id="s1", url="u1", title="t", retrieved_at="x", status="adopted"), Source(id="s2", url="u2")]
    claims = [Claim(id="k1", statement="a", kind="fact", source_ids=["s1"], outline_id="c1")]
    sections = {"c1": SectionText(outline_id="c1", sentences=[Sentence(text="a", claim_id="k1")])}
    res = {c.name: c for c in run_checks(sections, claims, sources, {"s2": "太旧"})}
    assert res["scope_time"].passed
    res = {c.name: c for c in run_checks(sections, claims, sources, {"s1": "太旧"})}
    assert not res["scope_time"].passed and res["scope_time"].detail == "s1：太旧；"
