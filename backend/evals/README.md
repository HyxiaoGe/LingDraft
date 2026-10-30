# 回放评测

`research/cases.yaml` 是调研场景的用例，每个用例是一段多轮对话，用真实模型跑（DeepSeek 加 agent-tools 网关），检查方式见 `app/evals/research.py`：

```bash
cd backend
uv run python -m app.evals.research                      # 全部用例
uv run python -m app.evals.research --case cost --case simple
uv run python -m app.evals.research --case rewrite --repeat 3 --no-claims
```

每个用例用独立的内存检查点和临时目录，不碰本机的服务和 sqlite；走和页面同一个运行器，提问按用例里写的回答，没写的跳过。
结果写到 `data/evals/research-<时间>.json`（每轮的对话、步骤、版本、用量、检查结果，最后一版的论断核对），
同名的 `.md` 是给人看的对话记录。终端打印每个用例的检查结果和合计用量。

改了 `app/graph/research/prompts.toml`、`app/graph/research/skills/` 或调研链路的代码，先挑相关的用例跑一遍再合并；
Pro 贵，每次跑之前想清楚这次要回答什么问题，答到就停，不追求全过。只跑一遍时结果有波动，拿不准的用例加 `--repeat`。

每轮的检查：

- **自动检查**：每轮都要跑完、有正文、实时片段的消息 id 和存下来的对得上；`expect` 里写了的才检查，包括出没出新版本、
  问没问、搜没搜、用了哪个调研方法、读了几篇、搜了几次、有没有限定时间、引用里有没有某个站点、只改了指定的那一节、那一节有没有变短。
- **judge 标准**：裁判模型看这一轮的正文和当时的报告，按用例写的标准判断。
- **论断支撑**：最后一版报告的论断，裁判模型拿 agent 当时看到的材料（搜索摘要、读到的正文）逐条核对，不能有 unsupported（`--no-claims` 不做）。

写用例的约定：

- **来源必填**：`source` 写清是哪次探针、事故、改动，或交互约定里的哪个场景，复盘时能追溯。
- **只写期望行为，不写标准答案**：该不该问、该不该出新版本、该用哪个调研方法、该不该读原文；能用轨迹判的不交给裁判模型。
- **按位置写操作**：选中第几句（`quote_sentence`）、排除编号为几的来源（`exclude`）、重写第几节（`rewrite_section`），都对着上一轮结束时的报告。
- **judge 标准要可判定**：写成“正文 / 报告做到了 X，且没有 Y”，不要让裁判用自己的知识判断事实对不对。
- **调研方法的示例不用用例里的对象**：示例教的是写法，用了用例里的对象等于把答案写进了提示词。
- **期望行为和现有规则不一致时先和 Sean 确认再入库。**
