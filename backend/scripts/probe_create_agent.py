"""验证豆包 function calling 配 create_agent + HumanInTheLoopMiddleware。

跑一轮：模型调假搜索 → 调 ask_user 触发中断 → 用 respond 恢复 → 出最终回答。
用法：uv run python scripts/probe_create_agent.py
"""

from __future__ import annotations

import json
import os
import sys
import time

from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app import config

CALLS: list[tuple[str, dict]] = []


@tool
def fake_search(query: str, count: int = 5) -> str:
    """搜索公开网页，返回候选来源列表（标题、网址、摘要、日期）。"""
    CALLS.append(("fake_search", {"query": query, "count": count}))
    results = [
        {"title": "WorkBuddy 9 月升级：点选元素改 PPT，改一页不影响其他页", "url": "https://example.com/a",
         "description": "官方称人机双写，只在信息缺失时追问。", "published_at": "2026-09-14"},
        {"title": "某评测：AI 海报工具局部修改普遍要 3～4 轮", "url": "https://example.com/b",
         "description": "HTML 海报溢出反复修。", "published_at": "2026-07-21"},
        {"title": "Canva AI 海报：描述后给 3 个模板", "url": "https://example.com/c",
         "description": "无澄清步骤。", "published_at": "2026-05-19"},
    ]
    return json.dumps(results[:count], ensure_ascii=False)


@tool
def ask_user(question: str, options: list[str], why: str) -> str:
    """向用户提一个问题。只在术语歧义、来源矛盾、需要收窄范围时使用。why 填不问会怎样。"""
    CALLS.append(("ask_user", {"question": question, "options": options, "why": why}))
    return "（此处不会执行，由中间件拦截）"


SYSTEM = (
    "你是调研助手。流程：先用 fake_search 搜一次；"
    "搜完后，“AI 海报工具”这个范围有歧义（是否包含 PPT 工具），你必须用 ask_user 问用户一次，给两个选项；"
    "拿到答案后，用两句话给出结论，并列出引用的网址。"
    + ("" if os.environ.get("PROBE_ALLOW_PARALLEL") else "不要在同一步里同时调用两个工具。")
)


def main() -> int:
    llm = ChatOpenAI(
        model=config.ARK_MODEL,
        api_key=config.ARK_API_KEY,
        base_url=config.ARK_BASE_URL,
        temperature=0,
        extra_body={"thinking": {"type": "disabled"}},
    )
    agent = create_agent(
        model=llm,
        tools=[fake_search, ask_user],
        system_prompt=SYSTEM,
        middleware=[HumanInTheLoopMiddleware(interrupt_on={"ask_user": {"allowed_decisions": ["respond"]}})],
        checkpointer=InMemorySaver(),
    )
    cfg = {"configurable": {"thread_id": "probe"}}

    t0 = time.time()
    r1 = agent.invoke({"messages": [("user", "AI 海报工具在局部修改上的现状如何？")]}, config=cfg)
    t1 = time.time() - t0
    interrupts = r1.get("__interrupt__") or []
    print(f"第一段 {t1:.1f}s，工具调用顺序：{[c[0] for c in CALLS]}")
    if not interrupts:
        print("没有中断；最后一条消息：", r1["messages"][-1].content[:300])
        return 1
    payload = interrupts[0].value
    print("中断 payload：", json.dumps(payload, ensure_ascii=False)[:600])

    t0 = time.time()
    r2 = agent.invoke(
        Command(resume={"decisions": [{"type": "respond", "message": "只看海报工具，不含 PPT。"}]}),
        config=cfg,
    )
    t2 = time.time() - t0
    msgs = r2["messages"]
    tool_msgs = [m for m in msgs if m.type == "tool"]
    print(f"恢复后 {t2:.1f}s，工具消息 {len(tool_msgs)} 条；ask_user 的工具结果：",
          [m.content for m in tool_msgs if m.name == "ask_user"])
    print("最终回答：", msgs[-1].content[:500])
    print("全部工具调用：", CALLS)
    # ask_user 被中间件在执行前拦截，所以它的函数体不该被调用过
    ok = [c[0] for c in CALLS] == ["fake_search"] and "example.com" in msgs[-1].content
    print("RESULT", "PASS" if ok else "CHECK")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
