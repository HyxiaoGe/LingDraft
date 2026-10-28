# 回放评测

`research/cases.yaml` 是调研场景的用例，用真实模型跑（DeepSeek 加 agent-tools 网关），检查方式见 `app/evals/research.py`：

```bash
cd backend
uv run python -m app.evals.research                      # 全部用例
uv run python -m app.evals.research --case selection --repeat 3
```

每个用例用独立的内存检查点，不碰本机的服务和 sqlite。结果写到 `data/evals/research-<时间>.json`（含轨迹、每条论断的裁判结论和报告），终端打印每个用例的检查结果。
改了 `app/graph/research/prompts.toml`、`app/graph/research/skills/` 或调研链路的代码，先跑一遍再合并；只跑一遍时结果有波动，拿不准的用例加 `--repeat`。

一个用例通过要同时满足：

- **轨迹检查**：`expect` 里写了的才检查，包括弹没弹范围卡、agent 问没问、用了哪个调研方法、搜了几次、读了几篇、有没有限定时间、引用里有没有某个站点；另外都要求出了报告、整理判断问题已回答。
- **论断支撑**：裁判模型拿 agent 当时看到的材料（搜索摘要、读到的正文）逐条核对论断，不能有 unsupported。
- **judge 标准**：裁判模型按用例写的标准判断报告。

写用例的约定：

- **来源必填**：`source` 写清是哪次探针、事故或改动发现的问题，复盘时能追溯。
- **只写期望行为，不写标准答案**：该不该问、该用哪个调研方法、该不该读原文；能用轨迹判的不交给裁判模型。
- **judge 标准要可判定**：写成“报告做到了 X，且没有 Y”，不要让裁判用自己的知识判断事实对不对。
- **调研方法的示例不用用例里的对象**：示例教的是写法，用了用例里的对象等于把答案写进了提示词。
- **期望行为和现有规则不一致时先和 Sean 确认再入库。**
