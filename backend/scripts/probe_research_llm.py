"""真实豆包：验证调研场景依赖的两种结构化输出。

  PYTHONPATH=. uv run python scripts/probe_research_llm.py

1. with_structured_output(json_schema, strict) 用于带可空字段的 Scope / 大纲 / 章节正文。
2. create_agent + ToolStrategy(Findings) 用于 agent 的最终产出（配合真实网关的搜索 / 读取）。
"""

from __future__ import annotations

import time

from langgraph.checkpoint.memory import InMemorySaver

from app.graph.research import prompts
from app.graph.research.agent import Findings, build_agent, collect_sources
from app.graph.research.state import Claim, OutlineNode
from app.research_llm import ArkResearchPlanner


def main() -> None:
    p = ArkResearchPlanner()
    t = time.time()
    scope = p.intake("AI 海报工具在局部修改和事实准确性上的现状（2026），只看中文信源")
    print(f"[intake {time.time() - t:.1f}s]", scope.model_dump_json(indent=1))
    t = time.time()
    outline = p.plan(scope)
    print(f"[plan {time.time() - t:.1f}s]", [(n.id, n.title) for n in outline])

    node = outline[0]
    system = prompts.AGENT_SYSTEM.format(
        scope=scope.model_dump_json(indent=1), title=node.title, question=node.question,
        others="；".join(f"{n.id} {n.title}" for n in outline[1:]), max_searches=3, max_reads=3, ask_note="",
    )
    # 子图需要父图检查点；单独跑时自己带一个
    agent = build_agent(p.agent_model(), p.tools(), system, allow_ask=False)
    agent.checkpointer = InMemorySaver()
    t = time.time()
    r = agent.invoke({"messages": [("user", f"[section:{node.id}] 请为章节“{node.title}”收集证据并输出 Findings。")]},
                     {"configurable": {"thread_id": "probe"}})
    print(f"[agent {time.time() - t:.1f}s] 消息数 {len(r['messages'])}")
    for m in r["messages"]:
        if getattr(m, "tool_calls", None):
            print("  调用:", [(tc["name"], tc["args"]) for tc in m.tool_calls])
    findings = r.get("structured_response")
    print("  structured_response 类型:", type(findings).__name__)
    assert isinstance(findings, Findings)
    sources, url_to_id = collect_sources(r["messages"], [], 1)
    print(f"  来源 {len(sources)} 条；论断 {len(findings.claims)} 条；假设 {findings.assumptions}")
    claims = []
    for i, pc in enumerate(findings.claims, 1):
        sids = [url_to_id[u] for u in pc.source_urls if u in url_to_id]
        print(f"  - [{pc.kind}/{pc.confidence}] {pc.statement} ← {sids or '（网址不在来源里：' + str(pc.source_urls) + '）'}")
        if sids:
            claims.append(Claim(id=f"k{i}", statement=pc.statement, kind=pc.kind, source_ids=sids, outline_id=node.id))

    t = time.time()
    sec = p.write_section(scope, OutlineNode(id=node.id, title=node.title, question=node.question), claims, None)
    print(f"[write {time.time() - t:.1f}s]")
    for s in sec.sentences:
        print(f"  ({s.claim_id}) {s.text}")


if __name__ == "__main__":
    main()
