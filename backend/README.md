# LingDraft backend（海报：M1 图 + M2 接口；调研：R3 图 + CLI + R4 接口）

黑五海报单场景的 LangGraph 实现：`intake → confirm_facts(中断) → 风格 → plan_layout → render → check → review(中断) → revise / manual_edit / change_fact / export`。
设计见 `../docs/黑五海报PoC技术方案.md`。

调研场景（图 + CLI + 接口 + 前端）：一条线程就是一段对话。每一轮用户说一句话（可以带上在报告里选中的文字，或报告上的按钮：排除来源、改论断、重写一节），同一个 `create_agent` 接着上下文往下做：搜索、读页、需要时 `ask_user` 停下来问（会中断），要写报告或改报告就调四个报告工具改工作稿（`write_report` / `revise_section` / `edit_claim` / `exclude_source`，草稿由快模型整理成节和论断，每次都把改完的整份报告返回给 agent），最后给一段带来源角标的回复。这一轮改过报告就在结束时落成一版、跑校验（校验只是说明，不拦交付），没改就不出新版。模型默认 DeepSeek V4 Pro（调研 agent 开思考，整理、起标题、推荐追问这些结构化步骤不开）。交互约定见 `../docs/调研交互约定.md`。

## 运行

先把 `.env.example` 复制为 `.env` 并填入 `ARK_API_KEY`（不填只能用 `--fake` / `LINGDRAFT_FAKE=1`）。

```bash
uv sync
uv run playwright install chromium
uv run pytest -q            # 180 个用例，不联网（FakePlanner / FakeResearchPlanner + MockTransport 网关）
```

Web 方式（M2）：

```bash
LINGDRAFT_FAKE=1 uv run uvicorn app.api.main:app --port 8000   # 去掉 LINGDRAFT_FAKE 走豆包，需要 ARK_API_KEY
cd ../frontend && npm install && npm run dev                    # http://localhost:5173，/api 代理到 8000
```

环境变量：`ARK_API_KEY`、`ARK_MODEL`、`ARK_MODEL_LITE`、`ARK_IMAGE_MODEL`、`LINGDRAFT_BACKGROUND=0` 关闭背景图生成、`LINGDRAFT_FAKE=1` 用假模型、`LINGDRAFT_RESEARCH_WORKERS` 同时跑几个调研线程（默认 4，多出来的排队）；调研场景另需 `AGENT_TOOLS_URL` / `AGENT_TOOLS_API_KEY`（agent-tools 网关）与 `DEEPSEEK_API_KEY`；`RESEARCH_PROVIDER`（deepseek / ark）、`RESEARCH_MODEL`、`RESEARCH_THINKING`、`RESEARCH_EFFORT` 可改调研模型。开关型变量（`LINGDRAFT_FAKE`、`LINGDRAFT_BACKGROUND`、`RESEARCH_THINKING`）认 1/0、true/false、yes/no、on/off，不分大小写；写成别的值启动时直接报错。

接口：`POST /api/threads`（multipart：request，image 可选）和 `POST /api/threads/{id}/resume`（`{"payload": ...}`）都返回 SSE：`thread` / `started` / `progress` / `node` / `interrupt` / `error` 事件，`state` 事件是完整状态，最后以 `end` 收尾；每个事件带 `id`（“执行-序号”），安静 15 秒发一行注释保活；`GET /api/threads/{id}/state`；`GET /api/threads/{id}/files/{name}`；`GET /api/threads/{id}/export` 打包 zip。

调研接口，前缀 `/api/research/threads`：`GET` 列出线程；`POST`（json `{"text"}`，开一段新对话）；`/{id}/messages`（json `{"text", "quote"?, "action"?}`，接着说）；`/{id}/resume`、`/{id}/retry`、`/{id}/events?after=<事件 id>`、`/{id}/state`。几个 POST 都返回 SSE：`thread` / `started` / `title` / `step`（工具进度）/ `delta`（`{id, kind: thinking|text, text}`，逐字的思考和正文，id 和存下来的消息一致）/ `writing`（报告草稿写了多少字）/ `node` / `interrupt` / `state` / `error` / `end`。调研没有文件和打包接口。agent 的提问在接口上是 `{"type":"research.ask", title, options, why, id}`，回传 `{"answer"}` 或 `{"skip":true}`（二选一），由 `ResearchRunner` 转成中间件的 decisions。`state` 是整段对话：`turns`（每一轮用户说的话、按顺序的思考 / 正文 / 工具步骤 / 提问，以及这一轮的用时、出的版本、推荐追问、用量）、`sources`、当前的 `report`（`draft` 为真表示这一轮改了报告、还没落成新版）、`versions`。

调研的 resume 要带上当前这张卡的中断 id：`{"interrupt_id": "...", "payload": {...}}`，id 在 `state.interrupt.id` 和 `interrupt` 事件里。回答先过 `app/graph/research/answers.py` 的校验（CLI 也用同一份）再交给图：不合法返回 400，线程停在原卡上；卡已过期（线程往前走了）或这一步还在排队 / 运行返回 409。同一个线程同时只接受一次执行：请求被接受时占位，执行完才释放，浏览器断开不影响；新线程排队时也能马上查到状态。

调研线程的 `state.status`：`queued` / `running`（有执行占着位）、`waiting`（停在提问上等用户）、`idle`（这一轮跑完了，可以接着说）、`failed`（某一步出错，或执行被掐断、比如服务重启；原因在 `state.error`）。失败不会抹掉已有内容，`POST /{id}/retry` 从最后一个检查点接着跑：失败的那一步重做，agent 里已经做过的搜索、读页不重来；没失败的返回 409。回答提交后还没被用上就出错的（比如重建 agent 时失败），线程仍是 `waiting`、停在原卡上，重新回答即可；agent 已经拿到回答、之后才出错的，原卡作废（再答返回 409），线程是 `failed`，重试会带着已给的回答接着跑。用户的话在交出线程 id 之前就写进检查点，排队时服务重启也查得到（`failed`，可重试）；上一轮没跑完、或停在提问上时不能接着说（409），先重试或回答。

模型服务出错（`app/model_guard.py`，只认服务商的协议信号）：连不上、超时、429、5xx 先由 SDK 重试；重试用完、密钥无效（401/403）、余额不足（402）、生成被打断（`insufficient_system_resource`）算不可用，同一个模型连续 2 次就熔断 180 秒，这期间的调用直接失败（`state.error` 是“模型服务暂时不可用（原因），约 N 分钟后再试”），冷却后放一个调用去试，成功就恢复；原因写进服务日志。请求本身的错误（400 等）不算。这一轮按失败处理，由用户点重试。
内容审核（DeepSeek 拦下请求时返回 400、`error.message` 为 `Content Exists Risk`；输出被截断时 `finish_reason=content_filter`）由 agent 的 `ContentGuard` 中间件处理：新加进来的搜索、读页结果一条条单独送审（只发这段文字、只要 1 个 token），去掉没通过的那条（同一个消息 id 覆盖）接着跑；单条都能过就把这一批结果整个去掉再试；新加进来的是用户的话或回答、去掉后仍被拦、输出被截断时，这一轮停下（`outcome: blocked`，页面显示固定说明），这一轮整轮不再带进之后的上下文。

执行的事件先写进事件日志（`EventLog`，进程内实现，多 worker 时换 Redis Stream），连接只是去读：页面断线或刷新后带着最后一个事件 id 请求 `/{id}/events`，执行还没结束就从断点接着推（没有 id 就从这次执行的开头补发），已经结束就只给当前状态。日志只留在内存里，进程重启就没了，那时以检查点为准。

CLI 演示（`--fake` 用确定性假模型；去掉 `--fake` 需设置 `ARK_API_KEY`，走豆包）：

```bash
uv run python -m app.cli new "某品牌蓝牙耳机黑五预热，5 折，11.20 到 11.27" --fake
# 打印 thread_id 和待确认载荷，之后逐步回传：
uv run python -m app.cli resume <thread_id> '{"values":{"product_name":"蓝牙耳机","offer":"5 折","date_range":"11.20 至 11.27","cta":null,"rules":null}}' --fake
uv run python -m app.cli resume <thread_id> '{"style_id":"s1"}' --fake            # 或 {"custom":"复古胶片感"}
uv run python -m app.cli resume <thread_id> '{"action":"edit","target":"headline","instruction":"更有紧迫感"}' --fake
uv run python -m app.cli resume <thread_id> '{"action":"manual_edit","target":"subhead","content":"我的副标题"}' --fake
uv run python -m app.cli resume <thread_id> '{"action":"change_fact"}' --fake     # 回到事实确认
uv run python -m app.cli resume <thread_id> '{"action":"approve"}' --fake
uv run python -m app.cli state <thread_id>
```

调研场景 CLI（`--scenario research`；和页面走同一个运行器、同一份校验，用法见 `app/cli.py` 顶部注释）：

```bash
uv run python -m app.cli --scenario research --fake new "AI 海报工具在局部修改上的现状（2026）"
uv run python -m app.cli --scenario research --fake say <thread_id> "接着说" [--quote '<json>'] [--action '<json>']
uv run python -m app.cli --scenario research --fake resume <thread_id> '{"answer":"包含"}'          # 回答提问；或 {"skip":true}
uv run python -m app.cli --scenario research --fake retry <thread_id>
uv run python -m app.cli --scenario research state <thread_id>
```

调研给模型看的文字都在 `app/graph/research/prompts.toml`，改文字不用动代码，改完重启后端。调研方法（技能）在 `app/graph/research/skills/<name>/`，文件格式沿用 Agent Skills：`SKILL.md` 的 frontmatter 写 `name`（和目录同名）、`description`（用途，模型靠它判断用不用）、`metadata.title`（页面上显示的名字），正文是查法；`references/report.md` 是报告结构和论断写法的示例。agent 的系统提示词只列目录，由模型决定读不读；读过的方法在整理草稿时带上，按它的报告结构分节。加一类方法就加一个目录，写坏了启动时直接报错。改了提示词、调研方法或调研链路，先跑一遍回放评测（`uv run python -m app.evals.research`）。

对话的检查点默认在 `data/checkpoints_research.sqlite`；配了 `LINGDRAFT_DATABASE_URL` 就存 Postgres。本机连 dev 上的库要先开隧道：`ssh -N -L 15432:127.0.0.1:5432 dev`（加 `-f` 放后台），见 `.env.example`。隧道断了请求会等 10 秒报错，接回来以后不用重启后端。测试不碰这个库；设 `LINGDRAFT_TEST_DATABASE_URL` 能把整套测试放到另一个 Postgres 库上再跑一遍，每个测试前都清空这个库，别指向有用的库。每版报告另写一份 `report_v{n}.md` 留档在 `output/research/<thread_id>/`，流程不读它：页面和 agent 看到的报告都从检查点里的数据生成。

检查点在 `data/checkpoints.sqlite`，每个 thread 的 `v{n}.png / v{n}.html` 和导出的 `final.png / final.html / brief.json` 在 `output/<thread_id>/`。进程重启后 `resume` 可直接续跑。

## 目录

- `app/graph/poster/state.py` 事实 / 元素 / 版本模型与三个中断的回传 schema
- `app/graph/poster/nodes.py` 节点与路由；`graph.py` 建图与检查点
- `app/llm.py` `Planner` 协议：`ArkPlanner`（豆包，json_schema 结构化输出）/ `FakePlanner`
- `app/render/` Jinja2 模板 → Playwright 截图；`checks.py` 事实逐字比对 / 违禁词 / 溢出
- `app/api/` FastAPI；`runner.py` `BaseRunner`（图执行放到工作线程，事件写进 `EventLog` 再转成 SSE；`RunRegistry` 运行占位；两者都是进程内实现，多 worker 时换共享存储）+ 海报 `GraphRunner`（单线程）；`research_runner.py` 调研的执行器（多线程）、状态视图与运行状态、重试、中断整形（带中断 id）、事件摘要
- `app/graph/research/` 调研场景：`state.py`（来源 / 大纲 / 论断 / 逐句正文 / 版本 / 每轮记录）、`agent.py`（`create_agent` 与中间件：提问、调用上限、工具进度、无效工具调用重试、内容审核拦下时去掉资料或停下这一轮、一轮结束时出版和推荐追问）、`workbench.py`（四个报告工具改工作稿，一轮结束落成一版）、`history.py`（从消息里读出用户的话、提问与回答、工具步骤、用量）、`sources.py`（从搜索、读页结果里收集来源，同一网页只给一个 id）、`answers.py`（提问的回答校验，接口 / CLI 共用）、`graph.py`、`report.py`（确定性拼装报告与校验）、`prompts.toml`（给模型看的文字：系统提示词、工具描述、注入的说明；`prompts.py` 只负责渲染）、`skills/`（调研方法，见下）
- `app/research_llm.py` `ResearchPlanner` 协议：`LiveResearchPlanner`（DeepSeek 或豆包 + 真实网关）/ `FakeResearchPlanner`（脚本化的假工具模型 + MockTransport 网关）
- `app/tools/` agent-tools 网关客户端（搜索 / 网页读取）与调研 agent 的工具（`search_web` / `read_page` / `ask_user` / `load_skill`；描述在 `prompts.toml` 的 `[tools.*]`）
- `app/evals/` + `evals/` 回放评测：用真实模型跑 `evals/research/cases.yaml`，按轨迹和裁判模型打分，用法和写用例的约定见 `evals/README.md`
- `scripts/` 对真实模型与网关的验证脚本：`probe_create_agent.py`（假搜索）、`probe_gateway_agent.py`（真实搜索 / 读取）、`PYTHONPATH=. uv run python scripts/<name>.py`；`probe_ask.py` / `probe_autonomy.py` 是早先改调研流程前的探针，结果记在 `../docs/probe/`，现在的代码上跑不了
- `../frontend/` Vite + React 对话式页面（参照 WorkBuddy）。海报：需求气泡 → 漂浮问题卡逐项确认事实 → 风格卡 → 海报 + 元素状态内嵌在助手回复里 → 评审卡 / 自由文本改稿 → 导出。调研（`src/workspace.jsx`，顶栏切换）：左边是对话，每一轮先是思考和工具步骤（跑的时候展开，跑完收起成“思考了 N 秒”），再是带来源角标的回复、“调研报告 · 第 N 版”卡片和推荐追问；右边是交付物，报告 / 论断 / 来源三个页签，在报告里选中文字改、重写一节、改论断、排除来源都作为一句话发回对话
- `tests/` 海报：T1 T2 T4 T5 T6 T8 + 校验重试、风格自定义、cta 绑定等；调研：`test_research.py`（一轮的结构与事件 / 简单问题不写报告 / 后面几轮不改就不出版 / 排除来源、改论断、重写一节、选中文字改 / 两节共用论断时重写和改论断 / 改了报告没写说明也出版 / 提问与跳过 / 无效工具调用重试 / 整理失败是工具错误 / 内容审核拦下资料、用户的话、输出 / 模型服务不可用 / sqlite 往返 / 来源去重与编号 / 时间范围）、`test_research_resume.py`（严格载荷 / 被拒后能继续 / 过期中断 id / 重复提交 / 断线与排队）、`test_research_runs.py`（并行执行 / 提交失败退还占位 / 断线续读与刷新补发 / 保活 / 失败保留内容与重试 / 排队中重启后可重试 / 被掐断的执行可重试）、`test_api_research.py`（接口）；`test_model_guard.py`（模型服务出错的分类与熔断）
