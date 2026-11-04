# 回放评测

`research/cases.yaml` 是对话场景的用例（模块名沿用 research），每个用例是一段对话，用真实模型跑（DeepSeek 加 agent-tools 网关），检查方式见 `app/evals/research.py`：

```bash
cd backend
uv run python -m app.evals.research                      # 全部用例
uv run python -m app.evals.research --case weather --case fact
uv run python -m app.evals.research --case cost --repeat 3 --no-sources
```

每个用例用独立的内存检查点，不碰本机的服务和配置的库；走和页面同一个运行器。
结果写到 `data/evals/research-<时间>.json`（每轮的对话、步骤、用量、检查结果、引用核对），
同名的 `.md` 是给人看的对话记录。终端打印每个用例的检查结果和合计用量。

改了 `app/graph/research/prompts.toml` 或对话链路的代码，先挑相关的用例跑一遍再合并；
Pro 贵，每次跑之前想清楚这次要回答什么问题，答到就停，不追求全过。只跑一遍时结果有波动，拿不准的用例加 `--repeat`。

## 录像回放、重新打分、汇总

```bash
uv run python -m app.evals.research --record                       # 真跑，把工具结果录下来（每个用例一份，整份换掉）
uv run python -m app.evals.research --replay --repeat 2            # 用录像回放：工具不发请求，材料和录的时候一样
uv run python -m app.evals.research --rejudge data/evals/<结果>.json --repeat 2   # 不跑 agent，拿旧回答重新打分
uv run python -m app.evals.research --stats data/evals/<结果1>.json data/evals/<结果2>.json  # 每项检查过了几次
```

- **录像**（`app/evals/tape.py`）：录在 `data/evals/tapes/<用例>.json`，不进仓库（里面是搜到、读到的网页）。
  按工具名和参数对上，对上了直接给录好的结果；没对上的（比如换了搜索词）照常真调、补录进去，每轮记下对上几次、真调几次。
  工具说明照常从服务现取，中间件、来源编号、卡片照常走真代码。回放时这个用例的时钟挪到录的时候（`app/clock.py`），
  系统提示词里的日期、`get_current_time`、裁判看的日期都是录的那天，“明天”的天气才对得上。录像旧了（网页、价格变了）就重新 `--record`。
- **重新打分**：结果文件里存了每轮的来源、裁判看的材料和那一轮的日期。改了 `expect`、`judge` 标准或裁判的提示词，
  拿旧回答按现在的用例重新检查，agent 不重跑；用例里的对话改过了的记为出错。`--repeat` 是每个回答判几遍，用来看裁判自己的波动。
- **汇总**（`--stats`，跑的时候 `--repeat` 大于 1 也会打印）：几遍下来每个用例每轮每项检查过了几次，分成会翻的、一直不过的、一直过的。
  会翻的检查单跑一遍不算数；一个改动要算改好了，得把一直不过的改成过，而且没让一直过的变成不过。

每轮的检查：

- **自动检查**：每轮都要跑完、有回复（这一轮的结果是 ok，最后一条模型消息有正文）、实时片段的消息 id 和存下来的对得上、
  正文里引用的来源 id 都存在；`expect` 里写了的才检查：搜没搜、读到几篇、取没取当前时间、查没查（`looked_up`：搜索、读页、天气、子任务任一）。每次调模型的输入和其中命中缓存的记在结果的 `calls` 里。
- **judge 标准**（回答好不好）：裁判模型看这一轮的正文，按用例写的标准判断。
- **引用核对**（来源对不对）：正文里带角标的说法，裁判模型拿 agent 当时看到的材料（搜索摘要、读到的正文）逐条核对，不能有 unsupported（`--no-sources` 不做）。

写用例的约定：

- **来源必填**：`source` 写清是哪次讨论、事故或改动，复盘时能追溯。
- **只写期望行为，不写标准答案**：该不该搜、该不该读原文；能用轨迹判的不交给裁判模型。
- **judge 标准要可判定**：写成“正文做到了 X，且没有 Y”，不要让裁判用自己的知识判断事实对不对。
- **期望行为和现有规则不一致时先和 Sean 确认再入库。**
