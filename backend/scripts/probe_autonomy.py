"""真实豆包：自主决策对照验证（走方向 B 之前的探针，不改图）。

  PYTHONPATH=. uv run python scripts/probe_autonomy.py [--plain] [--only 1,3]

同一个薄 agent（搜索 / 读原文 / 问用户 + 提交答案）跑 4 个正反例，看三件事：
简单题是否直接做完；复杂题是否自己多查不问人；真缺用户信息时是否问一次；来源矛盾时是否自己核对不问。
--plain 去掉 ToolStrategy(Answer) 结构化输出，用纯文本收尾，用来对比结构化输出对行为的影响。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware, ModelCallLimitMiddleware, ToolCallLimitMiddleware
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field

from app import config
from app.graph.research.agent import ask_resume, ask_view, is_ask_interrupt
from app.research_llm import make_chat_model
from app.tools.gateway import GatewayClient
from app.tools.research_tools import make_research_tools

SYSTEM = """你是调研助理。回答用户的问题，并给出证据。

做法：
- 用 search_web 搜索、用 read_page 读原文。一次搜索的摘要已经够回答，就直接回答；不够再搜、再读。不要为了凑篇幅多查。
- 只有在缺少只有用户才能提供的信息或取舍、而且不同答案会明显改变要做的事时，才用 ask_user 问一次。
- 资料不足、来源互相矛盾、问题比预想的复杂，都是你自己的事：继续查、核对口径，实在定不了就在 open_questions 里说明，不要推给用户。
- 回答的组织方式由你定：简单问题一句结论加几条关键事实；多方面的问题分点或分节。
- 每条关键事实都要能指向搜到或读过的网址。查完就提交 Answer。"""


class Fact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    statement: str
    source_urls: list[str] = Field(description="支撑它的网址，必须是搜到或读过的")


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(description="先一句结论")
    format: Literal["short", "list", "report"] = Field(description="你认为这个答案适合的组织方式")
    key_facts: list[Fact]
    open_questions: list[str] = Field(description="仍不确定、来源矛盾或没查到的点")
    confidence: Literal["high", "medium", "low"]


CASES = [
    {"id": 1, "name": "简单事实题", "request": "最近新加坡的天气情况如何？",
     "expect": "不问；搜 1～2 次就提交；format=short", "reply": None},
    {"id": 2, "name": "清楚的调研题", "request": "AI 海报工具在局部修改和事实准确性上的现状（2026），只看中文信源",
     "expect": "不问；搜 3 次以上；format=report", "reply": None},
    {"id": 3, "name": "缺用途的模糊题", "request": "帮我调研一下大模型的落地情况",
     "expect": "问 1 次（行业 / 场景或地域），回答后继续", "reply": "To B 企业服务，中国大陆"},
    {"id": 4, "name": "来源矛盾题", "request": "DeepSeek V3 的训练成本到底是多少？不同说法差很多",
     "expect": "不问；自己核对口径；在 open_questions 或 summary 里说明分歧", "reply": None},
]


def make_model(provider: str, model: str, thinking: bool, effort: str):
    return make_chat_model(provider, model, thinking=thinking, effort=effort)


def build(model, tools, plain: bool):
    middleware = [
        HumanInTheLoopMiddleware(interrupt_on={"ask_user": {"allowed_decisions": ["respond"]}}),
        ToolCallLimitMiddleware(tool_name="search_web", run_limit=8, exit_behavior="continue"),
        ToolCallLimitMiddleware(tool_name="read_page", run_limit=6, exit_behavior="continue"),
        ToolCallLimitMiddleware(tool_name="ask_user", run_limit=2, exit_behavior="continue"),
        ModelCallLimitMiddleware(run_limit=30, exit_behavior="end"),
    ]
    agent = create_agent(
        model=model, tools=tools, system_prompt=SYSTEM, middleware=middleware,
        response_format=None if plain else ToolStrategy(Answer), name="probe_agent",
    )
    agent.checkpointer = InMemorySaver()
    return agent


def run_case(agent, case: dict) -> dict:
    cfg = {"configurable": {"thread_id": f"probe-{case['id']}"}}
    t0 = time.time()
    asked: list[dict] = []
    r = agent.invoke({"messages": [("user", case["request"])]}, cfg)
    while r.get("__interrupt__"):
        pending = r["__interrupt__"][0].value
        if not is_ask_interrupt(pending):
            break
        view = ask_view(pending)
        asked.append({"title": view["title"], "options": view["options"], "why": view["why"], "t": round(time.time() - t0, 1)})
        print(f"    ask_user: {view['title']} {view['options']} | 不问会怎样：{view['why']}", flush=True)
        reply = case["reply"] or "你自己判断，不用问我。"
        r = agent.invoke(Command(resume=ask_resume(reply, False, pending)), cfg)
    elapsed = round(time.time() - t0, 1)
    msgs = agent.get_state(cfg).values["messages"]
    calls = {"search_web": 0, "read_page": 0, "ask_user": 0, "Answer": 0}
    queries: list[str] = []
    for m in msgs:
        if isinstance(m, AIMessage):
            for tc in m.tool_calls or []:
                calls[tc["name"]] = calls.get(tc["name"], 0) + 1
                if tc["name"] == "search_web":
                    queries.append(tc["args"].get("query", ""))
    model_calls = sum(1 for m in msgs if isinstance(m, AIMessage))
    usage = {"input": 0, "output": 0}
    transcript = []
    for m in msgs:
        if isinstance(m, AIMessage):
            u = m.usage_metadata or {}
            usage["input"] += u.get("input_tokens", 0)
            usage["output"] += u.get("output_tokens", 0)
            transcript.append({"role": "ai", "content": (m.content if isinstance(m.content, str) else str(m.content))[:300],
                               "tool_calls": [tc["name"] for tc in m.tool_calls or []], "finish": (m.response_metadata or {}).get("finish_reason"),
                               "reasoning_len": len(str(m.additional_kwargs.get("reasoning_content") or "")),
                               "extra_keys": sorted(m.additional_kwargs.keys()),
                               "usage": u, "invalid_tool_calls": [(x.get("name"), str(x.get("error"))[:4000]) for x in (m.invalid_tool_calls or [])],
                               "meta": {k: str(v)[:200] for k, v in (m.response_metadata or {}).items() if k != "model_provider"}})
        elif isinstance(m, ToolMessage):
            transcript.append({"role": "tool", "name": m.name, "status": getattr(m, "status", None), "content": str(m.content)[:200]})
    tool_msgs = sum(1 for m in msgs if isinstance(m, ToolMessage))
    sr = r.get("structured_response")
    answer = sr.model_dump() if isinstance(sr, BaseModel) else None
    final_text = next((m.content for m in reversed(msgs) if isinstance(m, AIMessage) and m.content and not m.tool_calls), None)
    return {
        "case": case["id"], "name": case["name"], "request": case["request"], "expect": case["expect"],
        "elapsed_s": elapsed, "model_calls": model_calls, "tool_messages": tool_msgs, "calls": calls,
        "queries": queries, "asked": asked, "answer": answer, "final_text": (final_text or "")[:1500],
        "usage": usage, "transcript": transcript,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plain", action="store_true")
    ap.add_argument("--provider", choices=["ark", "deepseek"], default="ark")
    ap.add_argument("--model", default="")
    ap.add_argument("--thinking", choices=["on", "off"], default="off")
    ap.add_argument("--effort", choices=["low", "high", "max"], default="high")
    ap.add_argument("--only", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    only = {int(x) for x in args.only.split(",") if x}
    model = make_model(args.provider, args.model, args.thinking == "on", args.effort)
    print(f"模型：{args.provider} {getattr(model, 'model_name', '')} 思考={args.thinking} effort={args.effort if args.thinking == 'on' else '-'}", flush=True)
    agent = build(model, make_research_tools(GatewayClient(config.AGENT_TOOLS_URL, config.AGENT_TOOLS_API_KEY, scene="research")), args.plain)
    results = []
    for case in CASES:
        if only and case["id"] not in only:
            continue
        print(f"\n=== 例 {case['id']} {case['name']}：{case['request']}\n    期望：{case['expect']}", flush=True)
        try:
            res = run_case(agent, case)
        except Exception as e:  # noqa: BLE001
            res = {"case": case["id"], "name": case["name"], "error": f"{type(e).__name__}: {e}"}
            print(f"    出错：{res['error']}", flush=True)
            results.append(res)
            continue
        a = res["answer"] or {}
        print(f"    {res['elapsed_s']}s · 模型调用 {res['model_calls']} · 搜索 {res['calls']['search_web']} · 读 {res['calls']['read_page']} · 问 {res['calls']['ask_user']} · token 入 {res['usage']['input']} 出 {res['usage']['output']}", flush=True)
        last = res["transcript"][-1] if res["transcript"] else {}
        print(f"    最后一条：{last.get('role')} {last.get('tool_calls') or ''} finish={last.get('finish')} content={last.get('content','')[:120]!r}", flush=True)
        print(f"    检索词：{res['queries']}", flush=True)
        if a:
            print(f"    format={a['format']} confidence={a['confidence']} 事实 {len(a['key_facts'])} 条 · 未定 {len(a['open_questions'])} 条", flush=True)
            print(f"    结论：{a['summary'][:200]}", flush=True)
            for q in a["open_questions"][:3]:
                print(f"    未定：{q[:160]}", flush=True)
        else:
            print(f"    文本收尾：{res['final_text'][:300]}", flush=True)
        results.append(res)
    out = Path(args.out) if args.out else Path("output") / f"probe_autonomy_{'plain' if args.plain else 'structured'}_{datetime.now(UTC).strftime('%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1))
    print(f"\n结果写到 {out}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
