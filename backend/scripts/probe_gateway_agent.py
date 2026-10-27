"""用网关的真实搜索 / 读取工具跑一轮调研 agent：搜 → 读一页 → 问用户 → 恢复 → 结论带引用。

用法：PYTHONPATH=. uv run python scripts/probe_gateway_agent.py
"""

from __future__ import annotations

import json
import sys
import time

from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app import config
from app.tools.gateway import GatewayClient
from app.tools.research_tools import make_research_tools

TOPIC = "AI 海报工具在局部修改上的现状（2026）"
SYSTEM = (
    "你是调研助手，正在为一份调研收集候选来源。步骤：\n"
    "1. 用 search_web 搜一次（count=5）。\n"
    "2. 挑一条最相关的用 read_page 读正文。\n"
    "3. 然后用 ask_user 问用户一次：调研范围是否包含 PPT 工具，给两个选项。\n"
    "4. 拿到答案后，用三句话给出初步结论，并列出引用的网址和每条来源的发布日期。\n"
    "不要在同一步调用两个工具。只引用真实搜到的网址。"
)


def main() -> int:
    client = GatewayClient(config.AGENT_TOOLS_URL, config.AGENT_TOOLS_API_KEY, scene="research_probe")
    llm = ChatOpenAI(
        model=config.ARK_MODEL,
        api_key=config.ARK_API_KEY,
        base_url=config.ARK_BASE_URL,
        temperature=0,
        extra_body={"thinking": {"type": "disabled"}},
        timeout=90,
    )
    agent = create_agent(
        model=llm,
        tools=make_research_tools(client),
        system_prompt=SYSTEM,
        middleware=[HumanInTheLoopMiddleware(interrupt_on={"ask_user": {"allowed_decisions": ["respond"]}})],
        checkpointer=InMemorySaver(),
    )
    cfg = {"configurable": {"thread_id": "probe-gw"}}

    t0 = time.time()
    r1 = agent.invoke({"messages": [("user", f"调研题目：{TOPIC}")]}, config=cfg)
    print(f"第一段 {time.time() - t0:.1f}s")
    _dump(r1["messages"])
    interrupts = r1.get("__interrupt__") or []
    if not interrupts:
        print("没有中断，结束。")
        return 1
    req = interrupts[0].value["action_requests"][0]["args"]
    print("提问：", json.dumps(req, ensure_ascii=False))

    t0 = time.time()
    r2 = agent.invoke(
        Command(resume={"decisions": [{"type": "respond", "message": "只看海报工具，不含 PPT。"}]}), config=cfg
    )
    print(f"恢复后 {time.time() - t0:.1f}s")
    _dump(r2["messages"][len(r1["messages"]) :])
    client.close()
    return 0


def _dump(msgs) -> None:
    for m in msgs:
        if m.type == "ai" and getattr(m, "tool_calls", None):
            for tc in m.tool_calls:
                print(f"  → 调用 {tc['name']} {json.dumps(tc['args'], ensure_ascii=False)[:160]}")
        elif m.type == "tool":
            body = m.content
            try:
                data = json.loads(body)
            except (TypeError, ValueError):
                data = None
            if m.name == "search_web" and isinstance(data, list):
                print(f"  ← {m.name} {len(data)} 条")
                for h in data:
                    print(f"      - {h.get('published_at') or '----------'} {h.get('title', '')[:50]} | {h.get('url')}")
            elif m.name == "read_page" and isinstance(data, dict):
                print(
                    f"  ← {m.name} ok={data.get('ok')} provider={data.get('provider')} "
                    f"len={len(data.get('content', ''))} truncated={data.get('truncated')} kind={data.get('kind', '')}"
                )
            else:
                print(f"  ← {m.name} {str(body)[:120]}")
        elif m.type == "ai":
            print("  AI:", (m.content or "")[:800])


if __name__ == "__main__":
    sys.exit(main())
