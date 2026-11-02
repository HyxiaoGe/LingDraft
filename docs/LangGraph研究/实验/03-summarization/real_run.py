"""上下文压缩做进产品以后的真实模型验证（2026-11-03）：临时实例（内存检查点、临时数据目录），触发线调低，几轮就过线。
调真实模型（DeepSeek V4 Pro 主 agent、Flash 写短版）和真实搜索，要花钱：一次三轮约 0.2 元（闲时价）。加 --fake 用假模型。

用法（在 backend/ 下）：
  LINGDRAFT_DATABASE_URL= COMPACT_TRIGGER_TOKENS=18000 COMPACT_TARGET_TOKENS=12000 PYTHONPATH=. uv run python <本文件> layer1 [--fake]
  LINGDRAFT_DATABASE_URL= COMPACT_TRIGGER_TOKENS=14000 COMPACT_TARGET_TOKENS=8000 PYTHONPATH=. uv run python <本文件> layer2 [--fake]
（11-03 跑 layer1 时估算还没算工具定义，用的是 12000 / 6000；算上以后约多 6000，换成上面的数大致一样。）

要回答的问题：
- layer1：之前一轮的读页正文被换成说明后，问那一页原文里的细节，模型会不会用 recall 取回、取回的对不对；过线之后缓存能不能接上。
- layer2：之前几轮换成 Flash 写的短版后，问第一轮回答里的细节，答得对不对（靠短版或者 recall 取回那一轮）；短版留没留来源 id。
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
from pathlib import Path

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, ToolMessage

from app import config
from app.graph.research import history
from app.research_llm import FakeResearchPlanner, LiveResearchPlanner, fake_gateway_client
from tests.test_research import Chat

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("app.graph.research.compaction").setLevel(logging.INFO)

SCRIPTS = {
    "layer1": [
        "Python 3.14 正式版有哪些主要的新特性？请读一两篇官方的介绍（比如 docs.python.org 的 What's New）再回答。",
        "Rust 2024 edition 主要改了什么？请读一两篇官方的介绍再回答。",
        "第一个问题里你读过的 Python 官方文档，原文里关于 free-threaded（自由线程）是怎么说的？请引用原文一句，不要重新搜索。",
    ],
    "layer2": [
        "Python 3.14 正式版有哪些主要的新特性？请读一两篇官方的介绍（比如 docs.python.org 的 What's New）再回答，按重要程度列出来。",
        "Rust 2024 edition 主要改了什么？请读一两篇官方的介绍再回答。",
        "你第一轮回答里列的第二个新特性是什么？它的来源是哪篇？不要重新搜索。",
    ],
}


class Sent(BaseCallbackHandler):
    """记下主 agent 每次调模型发出去的消息里有几处被换成说明、几份短版。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def on_chat_model_start(self, serialized, messages, **kw):
        text = "\n".join(str(m.content) for m in messages[0])
        self.calls.append({"removed": text.count("[Removed to save space]"), "short": text.count("[Short version of my side of turn"),
                           "messages": len(messages[0])})


def main() -> None:
    mode, fake = sys.argv[1], "--fake" in sys.argv
    print(f"触发线 {config.COMPACT_TRIGGER_TOKENS}，目标 {config.COMPACT_TARGET_TOKENS}，{'假模型' if fake else '真实模型'}")
    planner = FakeResearchPlanner(fake_gateway_client(page_repeat=300)) if fake else LiveResearchPlanner()
    sent = Sent()
    planner.agent_model().callbacks = [*(planner.agent_model().callbacks or []), sent]
    if not fake:
        orig = planner.summarize

        def summarize(prompt):
            out = orig(prompt)
            print(f"  [短版] {out[:700]!r}")
            return out

        planner.summarize = summarize
    chat = Chat(Path(tempfile.mkdtemp(prefix="compact-")), planner=planner)
    for k, text in enumerate(SCRIPTS[mode], 1):
        start = len(sent.calls)
        view = chat.say(text)
        turn = view["turns"][-1]
        v = chat.values()
        msgs = v["messages"]
        turn_msgs = msgs[history.last_human(msgs) + 1:]
        calls = [(m.usage_metadata or {}) for m in turn_msgs if isinstance(m, AIMessage) and m.usage_metadata]
        print(f"\n=== 第 {k} 轮：{text}")
        print("  结果", turn["record"]["outcome"], "用量", json.dumps(turn["record"]["usage"]))
        for c, s in zip(calls, sent.calls[start:], strict=False):
            print(f"  调用：输入 {c.get('input_tokens')} 命中 {(c.get('input_token_details') or {}).get('cache_read')} "
                  f"输出 {c.get('output_tokens')}｜发出去 {s['messages']} 条，说明 {s['removed']} 处，短版 {s['short']} 份")
        for m in turn_msgs:
            if isinstance(m, ToolMessage) and m.name == "recall":
                print(f"  [recall] {m.status} {str(m.content)[:200]!r}")
            if isinstance(m, AIMessage):
                for tc in m.tool_calls:
                    print(f"  [调用工具] {tc['name']} {json.dumps(tc['args'], ensure_ascii=False)[:150]}")
        print("  回答：", history.final_text(turn_msgs)[:1200])
        c = v.get("compaction")
        if c:
            print(f"  压缩：第一层到 {c.cleared_through[:8]}，第二层到 {c.summarized_through[:8]}，短版 {len(c.summaries)} 份")
    total_in = sum((m.usage_metadata or {}).get("input_tokens", 0) for m in chat.values()["messages"] if isinstance(m, AIMessage))
    total_hit = sum(((m.usage_metadata or {}).get("input_token_details") or {}).get("cache_read", 0)
                    for m in chat.values()["messages"] if isinstance(m, AIMessage))
    total_out = sum((m.usage_metadata or {}).get("output_tokens", 0) for m in chat.values()["messages"] if isinstance(m, AIMessage))
    yuan = (total_in - total_hit) * 4.5e-6 + total_hit * 0.15e-6 + total_out * 13.5e-6
    print(f"\n主 agent 合计：输入 {total_in}（命中 {total_hit}），输出 {total_out}，闲时约 {yuan:.2f} 元（高峰翻倍）；子 agent、短版另算")


if __name__ == "__main__":
    main()
