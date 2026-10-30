# 回放评测

`research/cases.yaml` 是对话场景的用例（模块名沿用 research），每个用例是一段对话，用真实模型跑（DeepSeek 加 agent-tools 网关），检查方式见 `app/evals/research.py`：

```bash
cd backend
uv run python -m app.evals.research                      # 全部用例
uv run python -m app.evals.research --case weather --case fact
uv run python -m app.evals.research --case cost --repeat 3 --no-sources
```

每个用例用独立的内存检查点，不碰本机的服务和 sqlite；走和页面同一个运行器。
结果写到 `data/evals/research-<时间>.json`（每轮的对话、步骤、用量、检查结果、引用核对），
同名的 `.md` 是给人看的对话记录。终端打印每个用例的检查结果和合计用量。

改了 `app/graph/research/prompts.toml` 或对话链路的代码，先挑相关的用例跑一遍再合并；
Pro 贵，每次跑之前想清楚这次要回答什么问题，答到就停，不追求全过。只跑一遍时结果有波动，拿不准的用例加 `--repeat`。

每轮的检查：

- **自动检查**：每轮都要跑完、有回复（这一轮的结果是 ok，最后一条模型消息有正文）、实时片段的消息 id 和存下来的对得上、
  正文里引用的来源 id 都存在；`expect` 里写了的才检查：搜没搜、读到几篇。
- **judge 标准**（回答好不好）：裁判模型看这一轮的正文，按用例写的标准判断。
- **引用核对**（来源对不对）：正文里带角标的说法，裁判模型拿 agent 当时看到的材料（搜索摘要、读到的正文）逐条核对，不能有 unsupported（`--no-sources` 不做）。

写用例的约定：

- **来源必填**：`source` 写清是哪次讨论、事故或改动，复盘时能追溯。
- **只写期望行为，不写标准答案**：该不该搜、该不该读原文；能用轨迹判的不交给裁判模型。
- **judge 标准要可判定**：写成“正文做到了 X，且没有 Y”，不要让裁判用自己的知识判断事实对不对。
- **期望行为和现有规则不一致时先和 Sean 确认再入库。**
