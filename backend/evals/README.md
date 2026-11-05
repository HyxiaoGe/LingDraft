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

## 决策点回放

```bash
uv run python -m app.evals.research --record --case lookup-index      # 先录：录像时顺带存下每次调模型之前的状态
uv run python -m app.evals.decisions --samples 10                      # 每个决策点只重跑那一下，跑 10 次
uv run python -m app.evals.decisions --compare data/evals/<改之前>.json data/evals/<改之后>.json  # 两份结果差得够不够大
```

- **是什么**（`app/evals/decisions.py`）：从录的那遍里挑一次调模型（`evals/research/decisions.yaml` 里写用例、第几轮、第几次，`call: last` 是写正文的那一下），
  把那之前的状态原样拿出来，只重跑这一次，到执行工具之前停，工具一个都不执行。调了工具就看调了什么（`expect`）；
  没调工具就是正文，裁判按 `judge` 看、拿录的那遍的材料核对引用。前面的步骤、搜到读到的材料每次都一样，差别只来自这一下；一次只花一次调模型，便宜到可以每个点跑 10 次。
- **状态从哪来**：`--record` 时检查点每一步都存（页面照旧一轮结束才写），每次调模型之前的那个存在 `data/evals/tapes/<用例>.snapshots.json`，不进仓库。
  重录以后，决策点前面的对话跟着变。重跑时只放那个检查点、不带它之后的写入，所以那一步一定是重新调的模型。
- **用的是现在的代码**：系统提示词、压缩、工具说明（从服务现取）是新的；对话、工具结果、这一轮的日期是录的那时的。
- **测不出来的**：改动要是会让模型前面搜别的词，这里看不出来，最后还要整段回放（`--replay --repeat 3`）确认；
  工具结果的整理（来源编号、天气整理）在检查点里已经做好了，改这部分代码这里测不出来；换模型时，中间的决策点前面带着原来那个模型的消息，第一次调模型的最干净。
- **怎么算变了**：`--compare` 对同一个决策点的同一项检查算双侧 Fisher 精确检验，p < 0.05 才算变了。每边 10 次时大约要差 6 次（2/10 对 8/10 算，4/10 对 6/10 不算）。

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
