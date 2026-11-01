"""端到端的评委：用户最后看到的回答，每种做法和参照（base0：2026-11-01 用 0d24e84 跑的那次，提示词和 base 一样）成对比，Flash 当评委，
每对比两次、交换前后。base 自己对 base0 就是评委加上两次运行的波动有多大。

先把参照的回答放到 output/subagent-budget/e2e/base0-<问题>.answer.md，再（在 backend 下）：
  PYTHONPATH=. LINGDRAFT_DATABASE_URL= uv run python "../docs/LangGraph研究/实验/06-subagent-budget/e2e_judge.py" base b5 b8
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app import config
from app.research_llm import make_chat_model

OUT = Path("output/subagent-budget/e2e")  # 相对 backend
PRICE = (0.003, 0.15, 0.6)
QUESTIONS = {
    "deep": "帮我分别深入调研一下 Rust、Go、Zig 这三门语言 2026 年下半年各自的版本更新和主要变化",
    "cities": "帮我对比一下杭州、成都、武汉这三个城市 2026 年出台的购房支持政策",
}

PROMPT = """You are evaluating two answers that an AI research assistant wrote to the same user question. Judge only the content of the answers against the question below; you cannot see the sources, so do not judge which facts are true.

Criteria, in order of importance:
1. Coverage: does the answer address every part of the question?
2. Specificity: concrete facts such as versions, dates, numbers, and names, rather than vague statements.
3. Care: it separates confirmed facts from plans, expectations, and uncertain items, and flags conflicts.
4. Sourcing: claims carry source ids such as [s3fa9c1].
Do not reward length for its own sake: content that adds nothing the question asks for does not make an answer better, and a shorter answer that covers the same question-relevant facts is as good.

<question>
{question}
</question>

<answer_A>
{a}
</answer_A>

<answer_B>
{b}
</answer_B>

Return only a JSON object, no other text:
{{"missing_in_A": ["question-relevant content that B has and A lacks, a few words each, at most 6 items"],
 "missing_in_B": ["the same for B"],
 "winner": "A" or "B" or "tie",
 "margin": "slight" or "clear" or "large" (use "slight" for a tie),
 "reason": "one sentence"}}"""

model = make_chat_model(config.RESEARCH_PROVIDER, config.DEEPSEEK_FLASH_MODEL, thinking=True, effort="high", timeout=300)


def judge(question: str, a: str, b: str) -> tuple[dict, float]:
    msg = model.invoke([("user", PROMPT.format(question=question, a=a, b=b))])
    text = str(msg.content).strip()
    text = text[text.index("{"): text.rindex("}") + 1]
    u = msg.usage_metadata or {}
    cached = (u.get("input_token_details") or {}).get("cache_read", 0)
    cost = (cached * PRICE[0] + (u.get("input_tokens", 0) - cached) * PRICE[1] + u.get("output_tokens", 0) * PRICE[2]) / 1e6
    return json.loads(text), cost


def compare(variant: str, q: str) -> dict:
    ref = (OUT / f"base0-{q}.answer.md").read_text()
    other = (OUT / f"{variant}-{q}.answer.md").read_text()
    first, c1 = judge(QUESTIONS[q], ref, other)   # A=参照, B=做法
    second, c2 = judge(QUESTIONS[q], other, ref)  # A=做法, B=参照
    score = {"A": -1, "B": 1, "tie": 0}  # 从做法这边看：赢 +1，输 -1
    s = score[first["winner"]] - score[second["winner"]]
    return {"variant": variant, "q": q, "score": s, "first": first, "second": second, "cost": c1 + c2}


def main() -> None:
    variants = sys.argv[1:]
    jobs = [(v, q) for v in variants for q in QUESTIONS if (OUT / f"{v}-{q}.answer.md").exists()]
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda j: compare(*j), jobs))
    (OUT / "judge.json").write_text(json.dumps(results, ensure_ascii=False, indent=1))
    print("分数：做法对参照，两次比较相加，+2 两次都赢，-2 两次都输，0 打平或一赢一输")
    for r in results:
        lost = r["first"]["missing_in_B"] + r["second"]["missing_in_A"]  # 做法这边缺的
        gained = r["first"]["missing_in_A"] + r["second"]["missing_in_B"]  # 参照那边缺的
        print(f"\n== {r['variant']} {r['q']}: {r['score']:+d}（{r['first']['winner']}/{r['first']['margin']}，{r['second']['winner']}/{r['second']['margin']}）")
        print(f"  做法缺 {lost[:6]}")
        print(f"  参照缺 {gained[:6]}")
    print(f"\n评委花费 ${sum(r['cost'] for r in results):.3f}")


if __name__ == "__main__":
    main()
