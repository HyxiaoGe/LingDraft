# LingDraft backend（海报：M1 图 + M2 接口；对话：agent + CLI + 接口）

黑五海报单场景的 LangGraph 实现：`intake → confirm_facts(中断) → 风格 → plan_layout → render → check → review(中断) → revise / manual_edit / change_fact / export`。
设计见 `../docs/黑五海报PoC技术方案.md`。

对话场景（模块和接口路径沿用 research）：一条线程就是一段对话。用户每说一句，同一个 `create_agent` 接着上下文往下做：搜索、读网页、查天气、查地点、比路线、查航班火车、排行程、画图、取当前时间，也可以用 `task` 把一件查资料的事交给子 agent（它在自己的上下文里搜、读，交回一份报告），最后给一段带来源角标的回复。之前的报告、提问卡都已经拿掉。模型默认 DeepSeek：主 agent 用 V4 Pro、开思考；子 agent 用 Flash；起标题用 Flash、不开思考，和第一次调模型同时跑，不让回答等它；推荐问题在这一轮答完后起，不开思考。对话太长时，发给模型的那份会压一压；关于用户的长期记忆每天整理一次，放进系统提示词（见下面的“上下文压缩和长期记忆”）。交互约定见 `../docs/调研交互约定.md`。

## 运行

先把 `.env.example` 复制为 `.env` 并填入 `ARK_API_KEY`（不填只能用 `--fake` / `LINGDRAFT_FAKE=1`）。对话还要配 `LINGDRAFT_DATABASE_URL`（必须配，见下面的“存储”），真实模型要 `DEEPSEEK_API_KEY` 和 `AGENT_TOOLS_API_KEY`。

```bash
uv sync
uv run playwright install chromium
uv run pytest -q            # 不联网（FakePlanner / FakeResearchPlanner + MockTransport 网关），对话存在内存里
```

Web 方式（M2）：

```bash
LINGDRAFT_FAKE=1 uv run uvicorn app.api.main:app --port 8000   # 去掉 LINGDRAFT_FAKE 走真实模型；没配 LINGDRAFT_DATABASE_URL 起不来
cd ../frontend && npm install && npm run dev                    # http://localhost:5173，/api 代理到 8000
```

部署（dev，192.168.1.11）：仓库根目录的 `Dockerfile` 先构建前端，再装后端，一个容器（`lingdraft`，单进程 uvicorn）同时出接口和页面（`LINGDRAFT_STATIC_DIR`，没有扩展名的地址给 `index.html`）。`docker-compose.yml` 接入 `middleware_default`（nginx-proxy 按容器名转过来）、`postgres_default`（用 `lingdraft` 库）和 `agent-tools-internal`；`.env` 放在 dev 的 `~/project/LingDraft/` 下，画的图存在那里的 `data/`。PR 上 CI 只跑测试（内存和 Postgres 各跑一遍）；合进 master 后，dev 上的 self-hosted runner 拉代码、`docker compose up -d --build`，然后等 `/api/health` 变健康（返回里的 `version` 是部署的提交号）。

环境变量（开关型的认 1/0、true/false、yes/no、on/off，不分大小写，写成别的值启动时直接报错）：

- 通用：`LINGDRAFT_FAKE=1` 用假模型；`LINGDRAFT_DATA_DIR`、`LINGDRAFT_OUTPUT_DIR` 改数据和产出目录；`LINGDRAFT_STATIC_DIR` 由后端出前端页面（镜像里配好了）；`LINGDRAFT_VERSION` 是部署的提交号（镜像构建时带进来）。
- 海报：`ARK_API_KEY`、`ARK_BASE_URL`、`ARK_MODEL`、`ARK_MODEL_LITE`、`ARK_IMAGE_MODEL`；`LINGDRAFT_BACKGROUND=0` 关闭背景图生成。
- 对话的存储：`LINGDRAFT_DATABASE_URL`，必须配。
- 对话的模型：
  - `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL`（默认 `deepseek-v4-pro`）、`DEEPSEEK_FLASH_MODEL`。
  - 主 agent：`RESEARCH_PROVIDER`（deepseek / ark）、`RESEARCH_MODEL`、`RESEARCH_THINKING`、`RESEARCH_EFFORT`。
    思考统一按 LangChain 的 content blocks 读（`history.thinking_of`），哪家模型都一样。DeepSeek 的思考原样传回（同一轮不传报 400）；豆包 2.x 给的是思考摘要（页面显示它）和加密的思考原文，两样都原样传回（不传加密原文推理效果会下降）。
  - 子 agent：`SUBAGENT_MODEL`（默认 Flash，豆包时是 lite）、`SUBAGENT_THINKING`、`SUBAGENT_EFFORT`。`SUBAGENT_SEARCH_BUDGET`、`SUBAGENT_READ_BUDGET` 是一个子 agent 最多搜几次、读几页，默认都是 8。
  - 整理记忆：`DREAM_MODEL`（默认和主 agent 一样）、`DREAM_THINKING`、`DREAM_EFFORT`。
- 对话的其他：
  - `LINGDRAFT_RESEARCH_WORKERS`：同时跑几段对话，默认 4，多出来的排队。
  - `COMPACT_TRIGGER_TOKENS`、`COMPACT_TARGET_TOKENS`：上下文压缩的触发线和压到多少，默认 20 万、10 万。
  - `LINGDRAFT_DREAM=0` 关掉每天按时整理记忆；`LINGDRAFT_DREAM_HOUR` 是北京时间几点整理，默认 3 点。
- 外部服务：
  - `AGENT_TOOLS_URL`、`AGENT_TOOLS_API_KEY`：agent-tools 网关，管搜索、读网页、天气、地图、航班火车、生图。都走网关的 MCP（`/search/mcp`、`/reader/mcp`、`/weather/mcp`、`/maps/mcp`、`/flyai/mcp`、`/image/v2/mcp`），起服务时连上取工具；没连上的先这样起来（搜索、读网页、画图、地图、航班火车是同名的占位工具，调了说服务暂时连不上；天气先不带），后台每分钟再试，连上一个换一个。画的图存在 image-service 里，按编号经网关取、删（`/image/v1/images/{id}`）。
  - `FUSION_URL`：新对话页的“今日灵感”从 Fusion 取。

海报的接口：`POST /api/threads`（multipart：request，image 可选）和 `POST /api/threads/{id}/resume`（`{"payload": ...}`）都返回 SSE：`thread` / `started` / `progress` / `node` / `interrupt` / `error` 事件，`state` 事件是完整状态，最后以 `end` 收尾；每个事件带 `id`（“执行-序号”），安静 15 秒发一行注释保活；`GET /api/threads/{id}/state`；`GET /api/threads/{id}/files/{name}`；`GET /api/threads/{id}/export` 打包 zip。

## 对话接口

前缀 `/api/research/threads`。请求体是 json `{"text"}`，说的话为空返回 400；线程不存在返回 404；正在跑、或者上一轮没跑完时，大多数操作返回 409（各接口另有的 409 写在后面）。

- 说话：
  - `POST`：开一段新对话。
  - `/{id}/messages`：接着说。
  - `/{id}/retry`：失败的线程接着跑；没失败的返回 409。
  - `/{id}/regenerate`：最后一轮重新回答。
  - `/{id}/edit`：改了最后那句话，按新的话重新回答。

  这几个 POST 都返回 SSE。
- 回答进行中又说的话（没发出去的话最多一句，已经有一句的返回 409）：
  - `POST /{id}/queue`：排着，这一轮答完或停下就接着发成新的一轮。这一轮刚好答完了就直接发，返回 `{"queued": null}`，页面去接 `/events`。
  - `POST /{id}/guide`：把排着的话插进这一轮，主 agent 下一次调模型之前放进对话。这一轮没再调模型就结束了的，照常排着发。
  - `DELETE /{id}/queue`、`DELETE /{id}/guide`：撤回，返回原文，页面放回输入框。
- 管理对话：
  - `POST /{id}/stop`：停下正在跑或排队的这一轮。点了马上取消，在等的模型、搜索、画图一起断开；已经写出来的思考和正文留着。没在跑的返回 409。
  - `PATCH /{id}`（json `{"title"}`）：改标题。
  - `DELETE /{id}`：删掉整段对话，找不回来。检查点、运行记录、画的图、整理记忆的记录一起删，从这段对话记下的记忆不删。
- 查看：
  - `GET /{id}/state`：当前状态。
  - `GET /{id}/events?after=<事件 id>`：断线、刷新后接上事件流。
  - `GET /{id}/images/{image_id}`：对话里画的图，后端经网关从 image-service 取来转给页面。这一轮还在跑、图刚画好时页面就来取，那时检查点里还没有它，所以不查图是不是这段对话的（服务只给灵稿的图，编号是随机的 uuid）。编号格式不对、服务里已经没有的返回 404，网关、服务出问题返回 502。
  - `GET /{id}/runs` 和 `GET /api/research/runs`：运行记录，见下面的“运行记录”。
  - `GET /api/memory`：长期记忆，只能看。

SSE 事件：

- `thread`：`{thread_id, queued}`，排队时也先给页面。
- `started`：开始跑了。
- `title`：标题起好了。
- `phase`：在等什么。`model` 是等模型，`retry` 是无效工具调用让模型重发，`guard` 是内容审核拦下后重试，`queued` 是子 agent 在排队，`suggest` 是在起推荐问题。
- `step`：工具步骤，开始和做完各来一次，做完的带着它得到的来源。
- `delta`：`{id, kind: thinking|text, text}`，逐字的思考和正文，id 和存下来的消息一致。
- `guide`：插进这一轮的话放进了对话。
- `node`、`error`、`end`；`state` 是完整状态。

子 agent 的事件带 `task`（派它的那次 `task` 调用的 id）。每个事件带 `id`（“执行-序号”），安静 15 秒发一行注释保活。

`state` 是整段对话：

- `status`：`queued` / `running` 是有执行占着位；`idle` 是这一轮跑完了，可以接着说；`failed` 是某一步出错，或者执行被掐断（比如服务重启），原因在 `error`。
- `stopping`：点了停止，还没停下。
- `suggesting`：答完了，推荐问题还在起，页面这时打开会接上事件流等着。
- `title`、`running_since`。
- `turns`：每一轮用户说的话，加上模型的思考、正文、工具步骤、插进来的话，按先后排；子任务那一步带着子 agent 的条目。`record` 是这一轮的用时、推荐问题、用量（主 agent 和子 agent 分开记）和结果（`ok` / `no_reply` / `blocked` / `stopped`）。
- `sources`：来源。
- `queued_message` / `guided_message`：排着的、等着插进这一轮的话。

同一个线程同时只接受一次执行：请求被接受时占位，执行完才释放，浏览器断开不影响。

失败不会抹掉已有内容。`retry` 从最后一个检查点接着跑：失败的那一步重做，已经做过的搜索、读页不重来。

检查点只在一次执行的开头（用户的话）和结束时（包括出错、点停止）存，中间每一步不存，省得每步都存一份整段对话。所以执行被掐断（服务重启、进程被杀）时，这一轮做到一半的没存下来，重试从头跑这一轮。执行中页面要的来源、标题都跟着事件来（做完的那一步带着它得到的来源），不从库里取。

用户的话在交出线程 id 之前就写进检查点，排队时服务重启也查得到（`failed`，可重试）。上一轮没跑完时不能接着说（409），先重试。

模型服务出错（`app/model_guard.py`，只认服务商的协议信号）：

- 连不上、超时、429、5xx 先由 SDK 重试。异步流式调用 120 秒收不到一个片段，也算超时。
- 算不可用的：重试完还是连不上、超时、5xx，密钥无效（401/403），余额不足（402），生成被打断（`insufficient_system_resource`）。
- 熔断：同一个模型连续 2 次不可用，就断开 180 秒。这期间的调用直接失败，`state.error` 是“模型服务暂时不可用（原因），约 N 分钟后再试”。冷却后放一个调用去试，成功就恢复。原因写进服务日志。
- 不算不可用的：请求本身的错误（400 等）。429 重试完还是限流，这次调用失败，但不算进熔断，不然请求一密就把所有对话一起断开。
- 起标题单独记熔断：它的超时很短，超时只说明不想等，不说明服务坏了。断开期间标题直接用原话，不连累同一个模型上的子 agent 和写短版。
- 这一轮按失败处理，由用户点重试。

内容审核由 agent 的 `ContentGuard` 中间件处理（DeepSeek 拦下请求时返回 400、`error.message` 为 `Content Exists Risk`；输出被截断时 `finish_reason=content_filter`）：

- 新加进来的搜索、读页结果一条条单独送审（只发这段文字、只要 1 个 token），去掉没通过的那条（同一个消息 id 覆盖）接着跑。单条都能过的，把这一批结果整个去掉再试。
- 新加进来的是用户的话（包括插进这一轮的），或者去掉资料后仍被拦，或者输出被截断：这一轮停下（`outcome: blocked`，页面显示固定说明），这一轮整轮不再带进之后的上下文。

执行的事件先写进事件日志（`EventLog`，进程内实现，多 worker 时换 Redis Stream），连接只是去读：页面断线或刷新后带着最后一个事件 id 请求 `/{id}/events`，执行还没结束就从断点接着推（没有 id 就从这次执行的开头补发），已经结束就只给当前状态。日志只留在内存里，进程重启就没了，那时以检查点为准。

## 上下文压缩和长期记忆

上下文压缩（`app/graph/research/compaction.py`）：

- 发给主 agent 的对话估算超过 20 万 token 时，从最早的一轮往后压，压到 10 万以下。
- 先把之前几轮的搜索、读页结果换成一行说明；还不够，再把整轮换成 Flash 写的短版。
- state 里的消息不改，页面照常显示原文。模型要细节时用 `recall` 工具取回。

长期记忆（`app/graph/research/memory.py`）：

- 每天北京时间 3 点在后台整理一次，把有新的几轮的对话整理成关于用户的记忆，存在 LangGraph 的 Store 里。
- 每次调主 agent，都整份放进系统提示词。
- 没有登录，所有人共用一份。页面只能看。

## CLI

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

对话场景 CLI（`--scenario research`）和页面走同一个运行器，所以也要能连上库。运行时逐字打印思考和正文，以及每一步工具。用法见 `app/cli.py` 顶部注释：

```bash
uv run python -m app.cli --scenario research --fake new "杭州这周末天气怎么样"
uv run python -m app.cli --scenario research --fake say <thread_id> "接着说"
uv run python -m app.cli --scenario research --fake retry <thread_id>
uv run python -m app.cli --scenario research --fake regenerate <thread_id>
uv run python -m app.cli --scenario research state <thread_id>
uv run python -m app.cli --scenario research dream          # 马上整理一次长期记忆
```

对话给模型看的文字（系统提示词、工具描述、系统注入的说明）都在 `app/graph/research/prompts.toml`，改文字不用动代码，改完重启后端。改了提示词或对话链路，先跑一遍回放评测（`uv run python -m app.evals.research`，用真实模型，要花钱，见 `evals/README.md`）。

## 存储

对话的检查点、长期记忆和运行记录都存在 Postgres 里（`LINGDRAFT_DATABASE_URL`），三样共用一个连接池。这个变量必须配：没配、连不上，服务启动时就报错退出。

本机连 dev 上的库要经过隧道：`ssh -N -L 15432:127.0.0.1:5432 dev`，见 `.env.example`。

- Sean 的机器上这条隧道交给 launchd 守着，配置在 `~/Library/LaunchAgents/org.seanfield.lingdraft-pg-tunnel.plist`。开机就起，断了自己重连，日志在 `~/Library/Logs/lingdraft-pg-tunnel.log`。
- 看状态用 `launchctl print gui/$(id -u)/org.seanfield.lingdraft-pg-tunnel`，停掉用 `launchctl bootout gui/$(id -u)/org.seanfield.lingdraft-pg-tunnel`。
- 隧道断着的时候，请求会等 10 秒报错；接回来以后不用重启后端。

测试不碰这个库，默认存在内存里（同一个测试里再建一个运行器，就像服务重启）。设了 `LINGDRAFT_TEST_DATABASE_URL`，整套测试就放到那个 Postgres 库上跑，每个测试前都清空这个库，别指向有用的库。CI 两种都跑。

对话里画的图存在 image-service 里、归灵稿（只有灵稿的密钥取得到、删得掉），消息里只记编号、大小这些。删对话、重新回答时按编号删掉不要的图，删不掉只记日志。2026-11-04 以前灵稿自己存在数据目录里的图没有搬过去，那些对话里的图显示“这张图已经不在了”。

运行记录（`app/api/run_store.py`，跟 LangGraph Agent Server 的 run 学的）：

- 每次执行一行：新对话、接着说、重试、重新回答、改了原话都算一次。
- 记着属于哪一轮、状态（queued / running / success / stopped / error / lost）、进来和开始和结束的时间、出错的类型和原因连同堆栈、跑它的版本（部署的提交号）。
- 和检查点存在一处，是 Postgres 的 `runs` 表。服务启动时，上次还在排队、在跑的记成 lost。删掉对话时一起删。
- 查看：`GET /api/research/threads/{id}/runs` 看一段对话的，`GET /api/research/runs?status=error&limit=50` 看所有对话里最近的。接口不给堆栈；堆栈在库里，出错时也打进日志（Loki 能搜到）。
- 写记录失败只打日志，不影响执行。

海报的检查点还在 SQLite 里（`data/checkpoints.sqlite`），每个 thread 的 `v{n}.png / v{n}.html` 和导出的 `final.png / final.html / brief.json` 在 `output/<thread_id>/`。进程重启后 `resume` 可直接续跑。

## 目录

- `app/graph/poster/state.py` 事实 / 元素 / 版本模型与三个中断的回传 schema
- `app/graph/poster/nodes.py` 节点与路由；`graph.py` 建图与检查点
- `app/llm.py` `Planner` 协议：`ArkPlanner`（豆包，json_schema 结构化输出）/ `FakePlanner`
- `app/render/` Jinja2 模板 → Playwright 截图；`checks.py` 事实逐字比对 / 违禁词 / 溢出
- `app/api/` FastAPI：
  - `runner.py`：两种场景共用的部分，即运行占位 `RunRegistry` 和事件日志 `EventLog`（转成 SSE；两者都是进程内实现，多 worker 时换共享存储）；还有海报的 `GraphRunner`（同步的图放进一个工作线程）。
  - `research_runner.py`：对话的运行器，在 `app/aio.py` 的事件循环里异步地跑。管排队、停止、排着的话和插话、重试和重新回答、状态视图；`open_storage` 打开存储。
  - `run_store.py`：运行记录和排着的话。
  - `favicon.py`：来源网站的图标。
  - `inspiration.py`：今日灵感。
- `app/graph/research/` 对话场景：
  - `agent.py`：`create_agent` 和 5 个中间件：
    - TurnMiddleware：起标题、记录每一轮、换系统提示词（带上长期记忆）、取插进来的话；
    - ContentGuard：内容审核；
    - CompactionMiddleware：上下文压缩；
    - ToolProgressMiddleware：工具进度；
    - InvalidToolCallRetryMiddleware：无效工具调用让模型重发。
  - `subagent.py`：`task` 工具和子 agent。每轮最多派 5 个，同一个对话同时最多跑 3 个；每个最多调 12 次模型。
  - `compaction.py`：上下文压缩和 `recall` 工具。
  - `memory.py`：长期记忆。
  - `history.py`：从消息里读出用户的话、工具步骤、画好的图、用量。
  - `sources.py`：从搜索、读页结果里收集来源，同一网页只给一个 id。
  - `state.py`。
  - `graph.py`：存储（共用的连接池、线程列表）。
  - `prompts.toml`：给模型看的文字；`prompts.py` 只负责渲染。
- `app/research_llm.py` `ResearchPlanner` 协议：`LiveResearchPlanner`（DeepSeek 或豆包 + 真实网关，各步用哪个模型见它的构造函数）/ `FakeResearchPlanner`（脚本化的假工具模型 + 进程内的假搜索、读页、天气、地图、航班火车、生图服务，假生图服务也管按编号取图、删图）
- `app/tools/`：
  - `gateway.py`：agent-tools 网关客户端（按编号取图、删图），和网关出错时的几种异常。
  - `research_tools.py`：agent 自己的工具（`get_current_time`），描述在 `prompts.toml` 的 `[tools.*]`。
  - `mcp.py`：MCP 工具共用的部分：经网关连、取工具、认出网关的 401/403（让这一轮失败）和 429（今天的上限到了）。
  - `web_mcp.py`：搜索、读网页。`search_web`、`read_page` 是 agent-tools 的 MCP 工具（怎么搜、怎么读、结果的样子、失败时给模型的话都在 search-service、reader-service 里，和 Fusion 共用）；这里接上灵稿自己的一段说明（什么时候搜、搜够没有、来源 id），中间件给结果带上来源 id、把失败整理成页面上的中文，没连上时用同名的占位工具。
  - `image_mcp.py`：画图、改图。`generate_image` 是 agent-tools 的 MCP 工具（出图、存图、改图找原图、比例、给模型的说明都在 image-service 里，和 Fusion 共用）；这里接上灵稿自己的一段说明，中间件管每轮最多画 3 张、改图只认这段对话里的图，把结果整理成图片卡片要的东西，没连上时用同名的占位工具。
  - `weather_mcp.py`：查天气。`get_weather` 是 agent-tools 的 MCP 工具（挑城市、整理预报、缓存、给模型的说明都在 weather-service 里，和 Fusion 共用），用 LangChain 的 MCP 适配器接进来；这里接上灵稿自己的一段说明，中间件把结果整理成模型看的文字，网关出问题算没查到、密钥不对让这一轮失败。
  - `maps_mcp.py`：查地点、比路线。`place_search`、`route_compare` 是 agent-tools 的 MCP 工具（先高德后腾讯、地名怎么定、重名怎么问、给模型的说明都在 maps-service 里，和 Fusion 共用）；这里接上灵稿自己的一段说明（卡片署了服务商不用重复、拿不到用户位置要问），中间件把整份结果留给地点卡、路线卡，网关今天的上限到了算额度用完，没连上时用同名的占位工具。
  - `flyai_mcp.py`：查航班、火车，排行程。`search_flights`、`search_trains` 是 agent-tools flyai 的 MCP 工具（日期定不了先问、只要高铁、飞猪认不出的地名、结果的样子都在服务里，和 Fusion 共用）；这里接上灵稿自己的一段说明（卡片上都列了、别贴预订链接、接站换乘用查到的站名和城市），中间件把整份结果留给航班卡、火车卡，每轮最多查 4 次；比路线的起终点是查到的车站时，城市没填对就不交给地图、告诉模型该填哪个。`present_itinerary` 是灵稿自己的：模型按这段对话查到的班次编号排方案，核对编号和先后后画成行程卡，一个回答最多一张。
  - `docs_mcp.py`：查文档。`resolve-library-id`（按库名找库）、`query-docs`（查这个库的文档）是 Context7 的 MCP 工具，经 agent-tools 的 context7 服务（注入密钥、只放这两个工具、限流时标出来）；这里接上灵稿自己的一段说明（什么时候用、引用来源 id），中间件给每段文档的出处带上来源 id，带来源的那几段记在 artifact 里给来源列表和压缩后取回。Context7 免费版每月 1000 次（每月初重置，和 Fusion 共用），网关给灵稿设了每天 30 次；限流、上限到了、连不上都告诉模型改用搜索和读官方文档。连上了才带（和天气一样）。
- `app/model_guard.py` 模型服务出错的分类和熔断；`app/aio.py` 对话的事件循环
- `app/evals/` + `evals/` 回放评测：用真实模型跑 `evals/research/cases.yaml`，按轨迹和裁判模型打分，用法和写用例的约定见 `evals/README.md`
- `../frontend/` Vite + React 对话式页面（参照 WorkBuddy）。
  - 海报（`#poster`）：需求气泡 → 漂浮问题卡逐项确认事实 → 风格卡 → 海报 + 元素状态内嵌在助手回复里 → 评审卡 / 自由文本改稿 → 导出。
  - 对话页：`src/workspace.jsx` 左边是对话列表（改名、导出、删除）和记忆页入口，右边是对话。`timeline.jsx` 里每一轮先是过程（思考、搜索、读页、子任务按先后，跑的时候展开，跑完收起），然后是天气卡片、地点卡、路线卡、航班卡、火车卡、行程卡、画的图，再是带来源角标的正文和这条回答用到的来源，最后一轮下面是推荐问题；回答时又说的话贴在输入框上沿。
- `tests/`：
  - 海报：T1 T2 T4 T5 T6 T8 + 校验重试、风格自定义、cta 绑定等。
  - 对话（都只用假模型）：
    - `test_research.py`：多轮对话、流式事件、停止和重新生成、内容审核、检查点往返、来源。
    - `test_research_runs.py`：并行执行、运行占位、断线续读、失败后重试、停止。
    - `test_research_queue.py`：排着的话和插话。
    - `test_research_subagent.py`：子 agent。
    - `test_compaction.py`、`test_memory.py`、`test_images.py`、`test_weather.py`、`test_web_tools.py`（搜索、读页）。
    - `test_run_records.py`：运行记录、没配库启动报错。
    - `test_api_research.py`：接口。
    - `test_research_eval.py`：评测的离线部分。
  - `test_model_guard.py`：模型服务出错的分类和熔断。
