"""成对比较：每种做法的报告和基线（base）的报告，同一份交代，Flash 当评委；每对比两次、交换前后，抵消位置偏好。

用法见 bench.py 开头。
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app import config
from app.research_llm import make_chat_model

HERE = Path(__file__).parent
OUT = Path("output/subagent-budget")  # 相对 backend
PRICE = (0.003, 0.15, 0.6)

PROMPT = """You are evaluating two research reports that research sub-agents wrote for the same task. A lead agent will use the report to answer a user. Judge only the content of the reports against the task below; you cannot see the sources, so do not judge which facts are true.

Criteria, in order of importance:
1. Coverage: does the report answer every key question and follow the report structure the task asks for?
2. Specificity: concrete facts such as versions, dates, numbers, and names, rather than vague statements.
3. Care: it separates confirmed facts from plans, expectations, and uncertain items, and flags conflicts.
4. Sourcing: claims carry source ids such as [s3fa9c1].
Do not reward length for its own sake: content that adds nothing the task asks for does not make a report better, and a shorter report that covers the same task-relevant facts is as good.

<task>
{task}
</task>

<report_A>
{a}
</report_A>

<report_B>
{b}
</report_B>

Return only a JSON object, no other text:
{{"missing_in_A": ["task-relevant content that B has and A lacks, a few words each, at most 6 items"],
 "missing_in_B": ["the same for B"],
 "winner": "A" or "B" or "tie",
 "margin": "slight" or "clear" or "large" (use "slight" for a tie),
 "reason": "one sentence"}}"""

model = make_chat_model(config.RESEARCH_PROVIDER, config.DEEPSEEK_FLASH_MODEL, thinking=True, effort="high", timeout=300)


def judge(task: str, a: str, b: str) -> tuple[dict, float]:
    msg = model.invoke([("user", PROMPT.format(task=task, a=a, b=b))])
    text = str(msg.content).strip()
    text = text[text.index("{"): text.rindex("}") + 1]
    u = msg.usage_metadata or {}
    cached = (u.get("input_token_details") or {}).get("cache_read", 0)
    cost = (cached * PRICE[0] + (u.get("input_tokens", 0) - cached) * PRICE[1] + u.get("output_tokens", 0) * PRICE[2]) / 1e6
    return json.loads(text), cost


def compare(variant: str, key: str) -> dict:
    base = json.loads((OUT / "base" / f"{key}.json").read_text())
    other = json.loads((OUT / variant / f"{key}.json").read_text())
    task = base["messages"][0]["content"]  # 两边的交代一样（tracker 等做法不改交代）
    first, c1 = judge(task, base["report"], other["report"])   # A=base, B=variant
    second, c2 = judge(task, other["report"], base["report"])  # A=variant, B=base
    score = {"A": -1, "B": 1, "tie": 0}  # 从做法这边看：赢 +1，输 -1
    s = score[first["winner"]] - score[second["winner"]]
    return {"variant": variant, "key": key, "score": s, "first": first, "second": second, "cost": c1 + c2}


def main() -> None:
    variants = sys.argv[1:]
    keys = [p.stem.removesuffix(".report") for p in sorted((OUT / "base").glob("*.report.md"))]
    jobs = [(v, k) for v in variants for k in keys]
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda j: compare(*j), jobs))
    (OUT / "judge.json").write_text(json.dumps(results, ensure_ascii=False, indent=1))
    print("分数：做法对基线，两次比较相加，+2 两次都赢，-2 两次都输，0 打平或一赢一输")
    for v in variants:
        rs = [r for r in results if r["variant"] == v]
        print(f"\n== {v}: 合计 {sum(r['score'] for r in rs):+d}  " + "  ".join(f"{r['key']} {r['score']:+d}" for r in rs))
        for r in rs:
            lost = r["first"]["missing_in_B"] + r["second"]["missing_in_A"]  # 做法这边缺的
            gained = r["first"]["missing_in_A"] + r["second"]["missing_in_B"]  # 基线那边缺的
            print(f"  {r['key']}: 做法缺 {lost[:6]}")
            print(f"  {' ' * len(r['key'])}  基线缺 {gained[:4]}")
    print(f"\n评委花费 ${sum(r['cost'] for r in results):.3f}")


if __name__ == "__main__":
    main()
