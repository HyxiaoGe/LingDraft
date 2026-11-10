# LingDraft backend（海报：M1 图 + M2 接口；对话：agent + CLI + 接口）

黑五海报单场景的 LangGraph 实现：`intake → confirm_facts(中断) → 风格 → plan_layout → render → check → review(中断) → revise / manual_edit / change_fact / export`。
设计见 `../docs/黑五海报PoC技术方案.md`。

对话场景（模块和接口路径沿用 research）：一条线程就是一段对话。用户每说一句，同一个 `create_agent` 接着上下文往下做：搜索、读网页、查文档、查天气、查地点、比路线、查航班火车、排行程、画图、取当前时间，读用户发的文件、看用户发的图、查用户自己的资料库、写文件（Artifact），也可以用 `task` 把一件查资料的事交给子 agent（它在自己的上下文里搜、读，交回一份报告），最后给一段带来源角标的回复。之前的报告、提问卡都已经拿掉。模型能选（见下面的“对话的模型”），默认是 Auto：第一句话判断难易，复杂的用 DeepSeek V4 Pro、开思考，简单的用免费额度上的模型；Auto 的子 agent 用 Flash；起标题用 Flash、不开思考，和第一次调模型同时跑，不让回答等它；推荐问题在这一轮答完后起，不开思考。对话太长时，发给模型的那份会压一压；关于用户的长期记忆每天整理一次，放进系统提示词（见下面的“上下文压缩和长期记忆”）。交互约定见 `../docs/调研交互约定.md`。

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

部署：仓库根目录的 `Dockerfile` 先构建前端，再装后端，一个容器（`lingdraft`，单进程 uvicorn，内存上限 512 MB）同时出接口和页面（`LINGDRAFT_STATIC_DIR`，没有扩展名的地址给 `index.html`）。`docker-compose.yml` 接入 `middleware_default`（nginx-proxy 按容器名转过来）、`postgres_default`（用 `lingdraft` 库）和 `agent-tools-internal`；`.env` 放在部署机的仓库目录下，那里的 `data/` 挂成数据目录（用户传的文件原件、网站图标；画的图、用户发的图在 image-service 里）。PR 上 CI 只跑测试（内存和 Postgres 各跑一遍，Postgres 用带 pgvector 的镜像）；合进 master 后，部署机上的 self-hosted runner 拉代码、先只建镜像（旧容器照常服务），再等 `/api/health` 里的 `busy`（排着队、在跑的回答有几个）变成 0，最多等 10 分钟，然后 `docker compose up -d` 换容器，等 `/api/health` 变健康（返回里的 `version` 是部署的提交号）。不等的话，重启会把正在跑的回答记成 lost，用户得重问。

环境变量（开关型的认 1/0、true/false、yes/no、on/off，不分大小写，写成别的值启动时直接报错）：

- 通用：`LINGDRAFT_FAKE=1` 用假模型；`LINGDRAFT_DATA_DIR`、`LINGDRAFT_OUTPUT_DIR` 改数据和产出目录；`LINGDRAFT_STATIC_DIR` 由后端出前端页面（镜像里配好了）；`LINGDRAFT_VERSION` 是部署的提交号（镜像构建时带进来）。
- 登录（见 `app/identity.py`）：`AUTH_URL`（单独部署的 auth 服务的公开地址，也是 JWT 的签发者，默认值见 `app/config.py`）、`AUTH_CLIENT_ID`（灵稿在它那里登记的应用；不配就没有登录，所有人都是游客）、`AUTH_INTERNAL_URL`（后端取验签公钥走的内网地址，不配就用公开地址）、`AUTH_REDIS_URL`（auth 服务的 Redis，查别处退出登录的吊销标记；不配就等 token 自己过期）；`LINGDRAFT_GUEST_SECRET` 签游客令牌的密钥（部署必配，不配每次启动随机一个，重启后游客的对话找不回）、`LINGDRAFT_GUEST_PER_IP_DAILY` 一个地址一天最多领几个游客身份（默认 5，配合游客的总额度防刷）。登录页面跳到 auth 服务再回来（redirect URI 必须是 https 或 localhost），所以内网 http 地址只能当游客用。
- 海报：`ARK_API_KEY`、`ARK_BASE_URL`、`ARK_MODEL`、`ARK_MODEL_LITE`、`ARK_IMAGE_MODEL`；`LINGDRAFT_BACKGROUND=0` 关闭背景图生成。
- 对话的存储：`LINGDRAFT_DATABASE_URL`，必须配。
- 对话的模型：env 里只放各家的 key 和地址，用哪些模型写在 `app/models.toml`。现在接了 DeepSeek、豆包、Kimi、Grok、千问、小米 MiMo、智谱 GLM、腾讯混元（TokenHub）、MiniMax，key 是 `DEEPSEEK_API_KEY`、`ARK_API_KEY`、`MOONSHOT_API_KEY`、`XAI_API_KEY`、`DASHSCOPE_API_KEY`、`MIMO_API_KEY`、`ZHIPUAI_API_KEY`、`HUNYUAN_API_KEY`、`MINIMAX_API_KEY`；地址默认是国内站（见 `app/config.py`），换地址设同名的 `*_BASE_URL`。TokenHub 上的免费额度（厂商写 `tokenhub`）和混元用同一个地址和 key，记账、熔断分开记；百炼上的（`dashscope`）用千问的 key。
  - 选项（`[[options]]`）是输入框旁边能选的，Auto 在第一个。模型写成 LangChain `init_chat_model` 的“厂商:模型”，可以另配子 agent 用的模型、思考开关（`toggle` 用户能开关、`always`、`never`）和思考强度（不给用户看）。`vision = true` 是看得见图（用户发的图直接给它看，看不见的用看图工具，见下面的“图片和文件”）。没配 key 的那家不出现在列表里。`status = "hidden"` 是新对话不再提供、老对话照常用；`"retired"` 是厂商也停了，老对话从下一轮起改用 Auto。`access` 是最低哪一档能选（`guest` 游客、`user` 登录用户、`admin` 管理员；不写是 `user`）：游客能选 Auto 和轻量模型，要登录的照样列出来、页面上锁着，直接调接口选它返回 403。Auto 必须给游客。
  - 对话第一句话时定下选项，之后不能换（Auto 例外，由系统在里面换）。每一轮开始时定下这一轮主 agent、子 agent 用的模型，记进这一轮的记录（`model_catalog.Catalog.plan`）；之前几轮是别的模型答的，发过去时不带那几轮的思考。
  - `max_output`：主 agent、子 agent 每次最多输出多少 token（思考也算），现在是 65536。不带的话有的厂商默认只有几千，模型写长文件会被截断。
  - 内部用途（`[roles]`）：起标题（`title`）、推荐问题（`suggest`）、单条送审（`probe`）、写短版（`summarize`）、整理记忆（`dream`）、评测的裁判（`judge`）、Auto 判断难易（`route`）、看图工具（`vision`）各用哪个，不跟着选项走。除了单条送审，都先走同一个模型的免费额度。
  - Auto（`[auto]`，`Catalog.pick`）：对话第一句话由 `route` 判断难易（最多等 10 秒，等不到、出错当复杂），定下以后整段沿用。复杂的用 Auto 选项的模型（`deepseek:deepseek-v4-pro`），游客用备用；简单的按到期日从早到晚挑第一个还能用的免费额度（没有 `same_as` 的那种），都用不了用备用。备用是 `fallback`（选项 `deepseek-flash`）。
  - 免费额度（`[[free]]`）：各家给的试用额度，现在是百炼（`dashscope`）和 TokenHub（`tokenhub`）的。`until` 那天北京时间 0 点起不再用。写了 `same_as` 的和某个选项是同一个模型：用到那个模型的（选了它、Auto 判为复杂、子 agent、内部用途、备用 Flash）都先走免费额度，用完、到期、出错再走直连，页面上还是那个选项。没有 `same_as` 的只给 Auto 判为简单的用，要写 `name`、`icon`（回答开头显示，见 `/api/models` 的 `names`）。免费模型不能同时是选项或内部用途。厂商报额度用完（`model_guard.FREE_TIER_CODES`，百炼还有 403 `insufficient_quota`）就记进 `free_used_up` 表，重启以后也不再用；续了额度改 `until` 就重新能用。
  - 换备用（`agent.ModelSelect`）：这次调模型不可用（包括首字超时、免费额度用完；走免费额度时认不出来的出错也算），按“免费额度 → 直连的同一个模型 → 备用 Flash 的免费额度 → 直连的 Flash”往下换，试过的不再试，换到头照常失败。换的时候发 `model` 事件，流出了半截的先发 `restart`。Auto 的对话以后几轮都用换上的那个；用户自己选的只这一轮换。子 agent 只做“免费额度 → 直连”这一步，这个子任务剩下的都用直连。
  - 接新的一家：OpenAI 兼容接口的在 `research_llm.COMPAT` 里加一行（怎么开关思考，先实测），别的在 `research_llm.PROVIDERS` 里加一个建模型的函数：思考怎么开关、思考怎么传回去各家不一样。思考统一按 LangChain 的 content blocks 读（`history.thinking_of`），哪家模型都一样。DeepSeek 的思考原样传回（同一轮不传报 400）；豆包 2.x 给的是思考摘要（页面显示它）和加密的思考原文，两样都原样传回（不传加密原文推理效果会下降）。别的几家思考都在 `reasoning_content` 里（MiniMax 要带 `reasoning_split`，不然 M3 的思考夹在正文的 `<think>` 里），也原样传回；GLM-5.3、MiniMax M3.1 Flash、Grok 关不掉思考（选项写 `always`），Grok 的思考多半不返回（所以它的选项先 `hidden`，页面上不出现）。
  - 价格（`[prices]`、`[peak]`）：各模型命中缓存、没命中、输出的单价（元 / 百万 token，国内站按量付费），手动照各家官网维护。DeepSeek 分峰谷（工作日 9–12、14–18 点是高峰，周末和法定节假日全天空闲，节假日表每年补一次），混元 Hy4 有限时优惠（写着到期时间），MiniMax M3 按输入长度分档。用户能选的、内部用途用的模型都要有价格，没有的启动就报错。成本怎么记见下面的“存储”。
  - `SUBAGENT_SEARCH_BUDGET`、`SUBAGENT_READ_BUDGET` 是一个子 agent 最多搜几次、读几页，默认都是 8。
- 对话的其他：
  - `LINGDRAFT_RESEARCH_WORKERS`：同时跑几段对话，默认 4，多出来的排队。
  - `COMPACT_TRIGGER_TOKENS`、`COMPACT_TARGET_TOKENS`：上下文压缩的触发线和压到多少，默认 20 万、10 万。
  - `LINGDRAFT_DREAM=0` 关掉每天按时整理记忆；`LINGDRAFT_DREAM_HOUR` 是北京时间几点整理，默认 3 点。
  - `LINGDRAFT_FILES_DIR`：用户传的文件原件放哪，默认数据目录下的 `files/`；`FILE_PARSE_TIMEOUT_S`：解析一个文件最多多少秒，默认 60。
  - `LANGSMITH_TRACING=true` 加上 `LANGSMITH_API_KEY`：对话的过程传到 LangSmith（`app/tracing.py`），`LANGSMITH_PROJECT` 是传到哪个项目。每轮对话一条 trace，带着 thread_id；中间件那几层只留名字和用时。本机的 `.env` 只放 key、不开 tracing，给评测传实验用（见 `evals/README.md`）。
- 外部服务：
  - `AGENT_TOOLS_URL`、`AGENT_TOOLS_API_KEY`：agent-tools 网关，管搜索、读网页、查文档、天气、地图、航班火车、生图。都走网关的 MCP（`/search/mcp`、`/reader/mcp`、`/context7/mcp`、`/weather/mcp`、`/maps/mcp`、`/flyai/mcp`、`/image/v2/mcp`），起服务时连上取工具；没连上的先这样起来（搜索、读网页、画图、地图、航班火车是同名的占位工具，调了说服务暂时连不上；天气、查文档先不带），后台每分钟再试，连上一个换一个。画的图存在 image-service 里，按编号经网关取、删（`/image/v1/images/{id}`）。
  - `FUSION_URL`：新对话页的“今日灵感”从 Fusion 取。
  - 资料库的向量、重排都调百炼，用 `DASHSCOPE_API_KEY`；重排的地址可以用 `KB_RERANK_URL` 换。

海报的接口：`POST /api/threads`（multipart：request，image 可选）和 `POST /api/threads/{id}/resume`（`{"payload": ...}`）都返回 SSE：`thread` / `started` / `progress` / `node` / `interrupt` / `error` 事件，`state` 事件是完整状态，最后以 `end` 收尾；每个事件带 `id`（“执行-序号”），安静 15 秒发一行注释保活；`GET /api/threads/{id}/state`；`GET /api/threads/{id}/files/{name}`；`GET /api/threads/{id}/export` 打包 zip。

## 对话接口

前缀 `/api/research/threads`。除了取图，接口都要带 `Authorization: Bearer <令牌>`（游客令牌或 auth 服务的 access token，见下面的“身份和其他接口”），没带、不对、过期都是 401。线程不存在、不归这个人，一律 404。请求体是 json `{"text", "thinking", "work", "files"}`（`thinking` 是思考开关，不给按模型的默认；`work` 是这句话是不是在 Work 模式下发的，不给是 Chat，见下面；`files` 见下面的“图片和文件”），说的话为空返回 400；正在跑、或者上一轮没跑完时，大多数操作返回 409（各接口另有的 409 写在后面）；开始一轮之前额度用完了返回 429（见“存储”里的额度）。

- 说话：
  - `GET /api/models`：新对话能选的模型（`models`：`{id, name, description, thinking, model, locked}`，Auto 在第一个；`model` 是主 agent 用的“厂商:模型”，页面按它给每轮找名字；这一档选不了的也列出来，`locked` 为 true）。`names` 是选项以外、回答开头可能出现的模型（Auto 用的免费额度）的名字和图标。
  - `POST`：开一段新对话，可以带 `model`（选项的 id，不给是 Auto），没有这个、停用了的返回 400，这一档选不了的返回 403。
  - `/{id}/messages`：接着说。
  - 新对话和接着说可以带图：请求改成 `multipart/form-data`，`body` 字段是同样的 json，每张图一个 `image` 字段，一句话最多 4 张（多了 400）。图先传进 image-service 再开始这一轮：每张超过 4 MB 返回 413，不是 PNG / JPEG / WebP 返回 415，传不上去返回 502；有一张传不上去整句不发，这一轮没开始（409、模型不对）就把传上去的删掉。
  - 也可以带文件：先传到 `/api/files`，解析好了把编号放进 `files`（最多 5 个）。还在解析、没读成、过期了返回 400（`detail` 是 `{code: "files_not_sendable", reason, message}`），什么都没开始。
  - 排着的话、插进这一轮的话不能带图、带文件（`/queue` 只收 json，带了 `files` 返回 400）。
  - `/{id}/retry`：失败的线程接着跑；没失败的返回 409。
  - `/{id}/regenerate`：最后一轮重新回答，可以带 `{"thinking", "work"}`（没带的照原来那句话的）。
  - Chat 和 Work 按每句话算（输入框里的两段开关）：Work 的那一轮系统提示词多一段（`prompts.toml` 的 `[agent].work`：计划、调研、写报告这类任务先问用户再动手），
    并且给模型问用户的工具 `ask_user`；Chat 的轮两样都没有，硬调了工具也不停下来。排着的话带着发的时候的模式；每轮的记录里有 `work`。
  - `/{id}/edit`：改了最后那句话，按新的话重新回答。

  这几个 POST 都返回 SSE。
- 回答进行中又说的话（没发出去的话最多一句，已经有一句的返回 409）：
  - `POST /{id}/queue`：排着，这一轮答完或停下就接着发成新的一轮。这一轮刚好答完了就直接发，返回 `{"queued": null}`，页面去接 `/events`。
  - `POST /{id}/guide`：把排着的话插进这一轮，主 agent 下一次调模型之前放进对话。这一轮没再调模型就结束了的，照常排着发。
  - `DELETE /{id}/queue`、`DELETE /{id}/guide`：撤回，返回原文，页面放回输入框。
- 管理对话：
  - `POST /{id}/stop`：停下正在跑或排队的这一轮。点了马上取消，在等的模型、搜索、画图一起断开；已经写出来的思考和正文留着。没在跑的返回 409。
  - `PATCH /{id}`（json `{"title"}`）：改标题，最多 40 个字，空白并成一个空格。返回列表里的这一行。
  - `POST /{id}/pin`、`DELETE /{id}/pin`：置顶、取消，运行中也行。返回列表里的这一行（`pinned_at` 是置顶的时间，没置顶是 null）。
  - `DELETE /{id}`：删掉整段对话，找不回来。检查点、运行记录、画的图、用户发的图、用户发的文件和模型写的文件、整理记忆的记录一起删，从这段对话记下的记忆不删。
- 查看：
  - `GET`（前缀本身）：这个人的对话列表，每行是 `thread_id`、`title`、`status`、`updated_at`、`running_since`、`pinned_at`，最近有动静的在前。登录以前没归谁的老对话谁都看不到。
  - `GET /{id}/state`：当前状态。
  - `GET /{id}/events?after=<事件 id>`：断线、刷新后接上事件流。
  - `GET /{id}/images/{image_id}`：对话里画的图、用户发的图，后端经网关从 image-service 取来转给页面。按文件表认是不是这段对话的（图发出去、画好时就登记了，不靠检查点）。页面用 `<img src>` 取、带不了令牌，所以这个接口不认人，对话 id 和图的编号都是随机的。不是这段对话的、编号格式不对、服务里已经没有的返回 404，网关、服务出问题返回 502。
  - `GET /{id}/files/{file_id}`：下载用户发的文件（原件）；`GET /{id}/files/{file_id}/parts`：右栏预览，抽出来的文字按段给（`{id, name, type, parts: [{title, text}], more}`，太长的只给前面一部分，`more` 为 true）。
  - `GET /{id}/runs` 和 `GET /api/research/runs`：运行记录，见下面的“运行记录”。`/api/research/runs` 跨所有人的对话，只有管理员能看（别人 403）。

SSE 事件：

- `thread`：`{thread_id, queued}`，排队时也先给页面。
- `started`：开始跑了。
- `title`：标题起好了。
- `phase`：在等什么。`model` 是等模型，`retry` 是无效工具调用让模型重发，`guard` 是内容审核拦下后重试，`queued` 是子 agent 在排队，`suggest` 是在起推荐问题。
- `step`：工具步骤，开始和做完各来一次，做完的带着它得到的来源。
- `delta`：`{id, kind: thinking|text, text}`，逐字的思考和正文，id 和存下来的消息一致。思考结束时那一段带着 `ms`（想了多久）。
- `draft`：模型正在写的文件（`create_file`、`edit_file` 的参数边流边解）：`{id, name, mid, title, type, file_id, change_note, text}`，`id` 是那次工具调用的 id，第一条带 `name`（哪个工具）和 `mid`（模型消息的 id），别的字段收完一个给一个，`text` 是内容新增的那一截。和 `delta` 一起每 0.08 秒攒一批发。
- `model`：`{ref}`，这一轮主 agent 用的模型（“厂商:模型”）。开始时来一次，换了备用再来一次。
- `restart`：`{id}`，模型生成到一半断了、要重新生成（再调一次或换备用）。`id` 是要去掉的那条半截消息，没流出东西的是 null。
- `guide`：插进这一轮的话放进了对话。
- `error`：`{message, kind}`，`kind` 同下面状态里的 `error_kind`。
- `node`、`end`；`state` 是完整状态。

子 agent 的事件带 `task`（派它的那次 `task` 调用的 id）。每个事件带 `id`（“执行-序号”），安静 15 秒发一行注释保活。

`state` 是整段对话：

- `status`：`queued` / `running` 是有执行占着位；`idle` 是这一轮跑完了，可以接着说；`failed` 是某一步出错，或者执行被掐断（比如服务重启），原因在 `error`（转写过的说法）。
- `error_kind`：没跑完的原因分几类，页面按类别说下一步能做什么：`model` 模型服务不可用，`blocked` 内容审核没通过，`cut_short` 半路停了、没有错误记录（进程重启），`tool` 工具服务（网关）出错，`internal` 别的。
- `stopping`：点了停止，还没停下。
- `suggesting`：答完了，推荐问题还在起，页面这时打开会接上事件流等着。
- `title`、`running_since`、`updated_at`（最近一个检查点的时间）。
- `model`：对话定下的模型（选项的 id）；还没定的老对话、定的那个停了的是 `auto`。
- `model_ref`：正在跑的这一轮主 agent 用的“厂商:模型”，页面在这一轮的记录出来之前按它显示；没在跑是 null。
- `turns`：每一轮用户说的话（带着这句话发的图 `images`、文件 `files`），加上模型的思考（带 `ms`）、正文、工具步骤、插进来的话，按先后排；子任务那一步带着子 agent 的条目。`record` 是这一轮的用时、推荐问题、用量（主 agent 和子 agent 分开记）、结果（`ok` / `no_reply` / `blocked` / `stopped`）、用的模型（`option`、`model`、`subagent_model`，模型里带着开没开思考）和这一轮花的 `credits`。
- `sources`：来源。
- `queued_message` / `guided_message`：排着的、等着插进这一轮的话。

同一个线程同时只接受一次执行：请求被接受时占位，执行完才释放，浏览器断开不影响。

失败不会抹掉已有内容。`retry` 从最后一个检查点接着跑：失败的那一步重做，已经做过的搜索、读页不重来。

检查点只在一次执行的开头（用户的话）和结束时（包括出错、点停止）存，中间每一步不存，省得每步都存一份整段对话。所以执行被掐断（服务重启、进程被杀）时，这一轮做到一半的没存下来，重试从头跑这一轮。执行中页面要的来源、标题都跟着事件来（做完的那一步带着它得到的来源），不从库里取。

用户的话在交出线程 id 之前就写进检查点，排队时服务重启也查得到（`failed`，可重试）。上一轮没跑完时不能接着说（409），先重试。

模型服务出错（`app/model_guard.py`，只认服务商的协议信号）：

- 连不上、超时、429、5xx 先由 SDK 重试。异步流式调用 120 秒收不到一个片段，也算超时。
- 算不可用的：重试完还是连不上、超时、5xx，密钥无效（401），没有权限（403），余额不足（402），生成被打断（`insufficient_system_resource`）。
- 首字超时：对话 agent 的流式调用发出去 30 秒还没收到第一个有内容的片段（思考、正文、工具调用都算，保活注释不算），也算不可用，断开这次请求。
- 免费额度用完（`FreeTierUsedUp`）：服务答复了，不算进熔断；记下以后不再用这个免费额度，这次换备用。
- 熔断：同一个“厂商:模型”连续 2 次不可用，就断开 180 秒（直连的和托管在百炼、TokenHub 上的同一个模型各断各的）。这期间的调用直接失败，`state.error` 是“模型服务暂时不可用（原因），约 N 分钟后再试”。冷却后放一个调用去试，成功就恢复。原因写进服务日志。
- 不算不可用的：请求本身的错误（400 等）。429 重试完还是限流，这次调用失败，但不算进熔断，不然请求一密就把所有对话一起断开。上下文超长也不算，见下面的“上下文压缩”。
- 起标题、Auto 判断难易、看图工具单独记熔断：起标题、判断难易的超时很短，超时只说明不想等，不说明服务坏了；看图坏了不该拦着别的用 Flash 的。起标题断开期间标题直接用原话，不连累同一个模型上的子 agent 和写短版。
- 生成到一半断了（流读到一半断开、流里来了错误、资源不足、太久没片段）：等 2 秒把这次调用整个再发一次，只再发一次（`InterruptedRetryMiddleware`，主 agent、子 agent 都挂）。
- 不可用的（包括再发一次还是断了的），主 agent 换备用（见上面的“换备用”）；换到头了这一轮按失败处理，由用户点重试。

内容审核由 agent 的 `ContentGuard` 中间件处理（DeepSeek 拦下请求时返回 400、`error.message` 为 `Content Exists Risk`；输出被截断时 `finish_reason=content_filter`）：

- 新加进来的搜索、读页结果一条条单独送审（只发这段文字、只要 1 个 token），去掉没通过的那条（同一个消息 id 覆盖）接着跑。单条都能过的，把这一批结果整个去掉再试。
- 新加进来的是用户的话（包括插进这一轮的），或者去掉资料后仍被拦，或者输出被截断：这一轮停下（`outcome: blocked`，页面显示固定说明），这一轮整轮不再带进之后的上下文。

执行的事件先写进事件日志（`EventLog`，进程内实现，多 worker 时换 Redis Stream），连接只是去读：页面断线或刷新后带着最后一个事件 id 请求 `/{id}/events`，执行还没结束就从断点接着推（没有 id 就从这次执行的开头补发），已经结束就只给当前状态。日志只留在内存里，进程重启就没了，那时以检查点为准。

## 上下文压缩和长期记忆

上下文压缩（`app/graph/research/compaction.py`）：

- 发给主 agent 的对话估算超过 20 万 token 时，从最早的一轮往后压，压到 10 万以下。
- 先把之前几轮的搜索、读页、查文档、取回的结果换成一行说明；用户发的文件只留目录和开头，读文件的结果换成一句说明（要原文用 `read_file` 再读）；模型写文件的调用里整篇的内容、改的片段换成一句说明。还不够，再把整轮换成 Flash 写的短版。
- state 里的消息不改，页面照常显示原文。模型要细节时用 `recall` 工具取回。
- 估算没到触发线、模型服务却说超过了它的上下文窗口（`model_guard.is_context_overflow`：langchain-openai 认出来的 `ContextOverflowError`，400/413/422 里各家的固定说法，TokenHub 的 400003、智谱的 1261）：把之前几轮压到这次估算的一半（最多压到 10 万），再发一次，只再发一次。之前几轮没有能再压的，照常失败。走的是免费额度的，先换直连再试，直连也说超长才压。
- MiMo 直连超长不报错：把输入截到窗口那么长，一个字没生成就以 `length` 结束。模型守卫把这种换成 `InputTruncated`，也当超长压了再发；它不是服务出错，走免费额度的也不先换直连。
- 只压主 agent，子 agent 不压。

长期记忆（`app/graph/research/memory.py`）：

- 每天北京时间 3 点在后台整理一次，把有新的几轮的对话整理成关于用户的记忆，存在 LangGraph 的 Store 里。
- 每次调主 agent，都整份放进系统提示词。
- 按人分（对话归谁就记在谁名下）。游客的对话不整理，登录并进账号以后再整理。
- `GET /api/memory`：这个人的全部记忆和整理的时间，每条带 `thread_id`（从哪段对话记下的，页面能点回去）。`PATCH /api/memory/{id}`（json `{"text"}`）改一条，空白并成一个空格，空的、超过 200 字返回 400；`DELETE /api/memory/{id}` 删一条，以后回答不再参考。不能手动新加。游客、不是这个人的、没有这一条一律 404。

## 图片和文件

用户发的图（`app/tools/vision.py`）：

- 一句话最多 4 张。发的时候传进 image-service，编号记在那句话上（`additional_kwargs` 的 `lingdraft_image`），同时登记进文件表。
- 看得见图的模型（`models.toml` 里 `vision = true`）：那句话后面接上图（base64，从 image-service 取来），最多带最近 10 张，更早的、服务里没了的换成一句说明。
- 看不见的：接一段说明（图的编号、看不见、要知道图里有什么就调 `view_image`），由 `[roles]` 的 `vision`（Flash，开思考）看图、用文字回答。看得见图的模型调模型时去掉 `view_image`。
- 改图、照着画：说明里都给编号，模型当 `generate_image` 的 `source_image_id`。
- 存着的消息不变，每次调模型按这次用的模型组织，一轮里换了备用也跟着换。

用户发的文件（`app/files/`、`app/tools/files.py`）：

- 先传：`POST /api/files`（`multipart/form-data`，一个 `file` 字段），存好原件、记一行就回编号（`status` 是 `parsing`），页面接着问 `GET /api/files/{id}`（`parsing` / `ready` / `failed`，`failed` 带 `reason`）。不收的 `detail` 是 `{code: "file_rejected", reason, message, …}`：超过 20 MB 返回 413，认不出、不收的 415，今天传满了 429（游客每天 20 个、登录用户 100 个，管理员不限）。`DELETE /api/files/{id}` 去掉还没发出去的。
- 认类型看内容，不看扩展名：PDF、Word（docx）、Excel（xlsx）、PPT（pptx）、CSV、Markdown、代码和别的文字。老格式（doc、xls、ppt）、加了密码的 Office 文件、图片不收；扫描版的 PDF 读不了。
- 解析在单独的子进程里跑，一次一个，超时是 `FILE_PARSE_TIMEOUT_S`。子进程把自己设成内存不够时最先被杀的，被杀了算这个文件读不了，对话的进程不受影响。
- 抽出来的文字按段存（`file_parts`）：PDF 一页一段，PPT 一张一段，Excel、CSV 按行切、每段带表头（每张表最多 2000 行），Word、Markdown 按标题和长度切（约 4000 字一段）。
- 发出去时绑到这段对话；传上来一天还没发出去的清掉。
- 给模型：每次调模型时拼进那句话前面。不超过 2 万字的整篇放进去（一句话里整篇放的加起来不超过 5 万字）；长的、放不下的只给目录和开头 2000 字，模型用 `read_file` 按段读、在文件里找（一次最多回 8000 字）。
- `read_file` 认这段对话里用户发的文件、模型写的文件（最新一版）和这个人资料库里建好了的文件。不收钱，只给主 agent。

模型写的文件（Artifact，`app/tools/write_file.py`、`app/files/artifacts.py`）：

- `create_file` 新建，`edit_file` 出新一版。三种：文档（Markdown）、网页（HTML）、代码，后端一样存。每一版是文件表里的一行、存全文，一份最多 30 万字。各版用第一版的编号串起来，读、改的总是这段对话里最新的一版。
- 改用片段替换（每段 `old_text` 要在全文里恰好出现一次，不做模糊匹配）或者整篇重写。
- 一轮里一份文件只出一版：同一轮里又改了这一轮写的那版，新的顶替它、版本号不变。
- 输出被截断（`finish_reason=length`）的那次不存。用户发的文件只读。
- 不收钱，只给主 agent。写的过程边流边推给页面（SSE 的 `draft`），写成了的在 `turns` 的工具步骤里。

图片也登记在文件表里（`kind` 是 `image`，画的图记着哪一轮），页面取图按表认是不是这段对话的。表里没登记的老对话的图要补一次：`uv run python -m app.files.backfill_images` 只数，加 `--write` 才写；可以重跑，登记过的跳过；只看每段对话最后存下的状态。

## 资料库

用户把文件传进自己的资料库，以后任何对话都能查，回答标出用的是哪个文件的哪一段（`app/knowledge/`、`app/tools/knowledge.py`）。游客没有。

- 一个人最多 20 个资料库，各有名字（最多 30 字，同一个人不重名，不分大小写）和说明（最多 300 字，模型拿它判断查哪个）。几个资料库加起来最多 100 个文件、1000 万字；单个文件和对话里一样不超过 20 MB，不算每天上传的个数。
- 一个资料库就是文件表里的一个工作区（`workspace_id` 是 `kb:<资料库编号>`）：解析、原件、按段的文字、预览、`read_file` 都和对话里的文件一样。
- 上传 → 解析 → 切块（约 800 字一块，前后重叠 100 字）、算向量、写进 `kb_chunks` → 能用。建好索引之前一直是 `parsing`。
- 查：向量（`qwen3.7-text-embedding-flash`，1024 维）和关键词（jieba 分好词交给 Postgres 的 tsvector，词之间是“或”）各取前 20 块，RRF（k=60）合并，`qwen3.7-text-rerank` 重排取前 8 块。查询最多 500 字。向量、重排没调成的少一路照样给结果。没配 `DASHSCOPE_API_KEY`、用假模型时，向量用本地按词算的 `HashEmbedder`，不重排。
- 工具 `search_knowledge(query, library?)`：只给主 agent，只在这段对话第一轮时资料库里有文件才有（工具一直在列表里，没有的调模型时去掉）。说明里列着那时每个资料库的名字、说明和文件名（最多 50 个文件）。默认查全部，模型可以给 `library`（名字）只查一个；之后新传的、新建的资料库照样查得到。来源 id 按“文件的第几段”算，和网页来源一起编号。
- 记账：建索引按实际 token 扣用户的额度（用途 `kb_index`，没建成的记 `kb_index_failed`）；查一次按 `[tools]` 扣，查询时调向量、重排的只记账（`kb_search`），不算给用户。
- `kb:<账号>` 工作区里的老文件（每人一个库时传的），Postgres 建表时一次搬进一个叫“我的资料”的资料库。

接口（游客：`GET` 的 `available` 是 false，建资料库、传文件返回 403 `login_required`；别人的、没有的 404）：

- `GET /api/knowledge`：几个资料库（每个带文件和状态）、上限（`limits`）、用了多少（`used`，按人算）。
- `POST /api/knowledge/libraries`（json `{name, description}`）建；`PATCH /api/knowledge/libraries/{id}` 改，没给的不改；`DELETE` 删，里面的文件一起删。不收的 `detail` 是 `{code: "library_rejected", reason, message, …}`：名字、说明不对 422（`bad_name` / `bad_description`），满了 409（`libraries_full`），重名 409（`name_taken`）。
- `POST /api/knowledge/libraries/{id}/files`：传进一个资料库。不收的和 `/api/files` 一样是 `file_rejected`：资料库没有 404（`no_library`），满了 409（`kb_full`），太大 413，类型不对 415。
- `GET /api/knowledge/files/{id}`：状态，同 `/api/files/{id}`，`failed` 的原因还有 `kb_chars`（字数到上限）、`index_failed`（索引没建成）、`no_text`（没有文字）。`DELETE` 删，删了马上搜不到，正在处理的也能删。
- `GET /api/knowledge/files/{id}/parts`：右栏预览；`GET /api/knowledge/files/{id}/original`：下载原件。

## 身份和其他接口

- 身份（`app/identity.py`）：每个请求带 `Authorization: Bearer`，是灵稿自己签的游客令牌（`sub` 带 `guest:` 前缀），或 auth 服务的 access token（按 JWKS 验签，锁定签发者、audience 和 `type=access`；验过再查 auth 的 Redis 里的吊销标记，Redis 不通就放行）。三档：游客、登录用户、管理员。auth 服务的公钥取不到返回 503。
  - `GET /api/auth/config`：`{login, auth_url, client_id}`，`login` 为 false 是没开登录（没配 `AUTH_CLIENT_ID`），页面不显示登录入口。
  - `POST /api/auth/guest`：领一个游客身份，`{token, expires_at}`。一个地址一天领太多返回 429（`detail.code` 是 `guests_per_ip`，带每个地址一天几个、登录后的额度）。
  - `GET /api/auth/me`：`{sub, tier, email, guest}`。
  - `POST /api/auth/claim`（json `{guest_token}`）：登录以后把游客期间的对话并进账号，返回 `{moved}`。游客令牌不对 400，自己还是游客 403。
- `GET /api/credits`：额度，见“存储”里的额度。
- `GET /api/inspirations`：新对话页的“今日灵感”，一次都没取到返回 502。
- `GET /api/favicon/{host}`：来源网站的图标，后端代取、按域名缓存，取不到 404。
- `GET /api/health`：`{ok, version, busy}`，不碰数据库和外部服务；`busy` 是排着队、在跑的回答有几个，部署时等它变成 0。

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

对话用的都存在 Postgres 里（`LINGDRAFT_DATABASE_URL`），共用一个连接池（成本账另用一个连接写，见下面的“成本”）。这个变量必须配：没配、连不上，服务启动时就报错退出。表都在启动时建（`CREATE TABLE IF NOT EXISTS`）：

- 检查点、长期记忆：LangGraph 的 `AsyncPostgresSaver`、`AsyncPostgresStore` 自己建的表。
- `runs`、`pending_messages`、`thread_owners`（`app/api/run_store.py`）：运行记录、排着的话、每段对话归谁和置顶时间（`pinned_at`）。
- `files`、`file_parts`（`app/files/store.py`）：用户发的文件、模型写的文件（一版一行）、图片的登记，一行一个；`workspace_id` 是对话 id 或 `kb:<资料库编号>`。`file_parts` 是抽出来的文字，按段。
- `kb_libraries`、`kb_chunks`（`app/knowledge/`）：资料库的名字和说明；块的内容、1024 维向量（pgvector）、tsvector，跟着文件删。库里要有 pgvector 扩展：建表时跑 `CREATE EXTENSION IF NOT EXISTS vector`，扩展已经装好的，连库的账号不是超级用户也不报错。
- `model_costs`、`quota_resets`、`free_used_up`：成本账、管理员重置额度的时间、用完的免费额度（按模型和到期日）。

库在别的机器上、端口不对外时，可以用 SSH 隧道转到本机（见 `.env.example`）。隧道断着的时候，请求会等 10 秒报错；接回来以后不用重启后端。

测试不碰这个库，默认存在内存里（同一个测试里再建一个运行器，就像服务重启）。设了 `LINGDRAFT_TEST_DATABASE_URL`，整套测试就放到那个 Postgres 库上跑，每个测试前都清空这个库，别指向有用的库。CI 两种都跑。

对话里画的图存在 image-service 里、归灵稿（只有灵稿的密钥取得到、删得掉），消息里只记编号、大小这些，文件表里登记一行。删对话、重新回答时按编号删掉不要的图，删不掉只记日志。
用户发的图也传进 image-service（`POST /image/v1/images`，和画的图放在同一个私有目录、同样不自动清理），编号记在那句话上（`additional_kwargs` 的 `lingdraft_image`），跟着对话删；重新回答、改那句话都留着。给模型的说明、图怎么给模型看，见上面的“图片和文件”。消息里记着 `file` 的老格式的图（灵稿自己存在数据目录里的，不在 image-service 里），页面上显示“这张图已经不在了”。

用户发的文件、模型写的每一版，原件都存在 `LINGDRAFT_FILES_DIR`，文字按段在 `file_parts`。删对话时按工作区一起删，原件没删掉只记日志。

运行记录（`app/api/run_store.py`，跟 LangGraph Agent Server 的 run 学的）：

- 每次执行一行：新对话、接着说、重试、重新回答、改了原话都算一次。
- 记着属于哪一轮、状态（queued / running / success / stopped / error / lost）、进来和开始和结束的时间、出错的类型和原因连同堆栈、跑它的版本（部署的提交号）。
- 和检查点存在一处，是 Postgres 的 `runs` 表。服务启动时，上次还在排队、在跑的记成 lost。删掉对话时一起删。
- 查看：`GET /api/research/threads/{id}/runs` 看一段对话的，`GET /api/research/runs?status=error&limit=50` 看所有对话里最近的。接口不给堆栈；堆栈在库里，出错时也打进日志（Loki 能搜到）。
- 写记录失败只打日志，不影响执行。

成本（`app/costs.py`）：1 credit = ¥0.001 的 API 成本，每次调模型记一行账，额度按账扣（见下面的额度）。工具调用也算钱：画图、看图、搜索、读页、查资料库这些按次计价（`models.toml` 的 `[tools]`，画图 0.2 元，看图、搜索、读页、查资料库 0.005 元……），只算做成了的，没列的（task、recall、排行程、读文件、写文件）不收。

- 每次调模型一行，记在 `model_costs` 表：调用开始的时间、厂商和模型、用途（主 agent、子 agent、起标题、推荐问题、压缩、整理记忆、评测的裁判、资料库建索引和查询……）、哪段对话哪一轮、归谁、三种 token 数、按调用开始那一刻的单价算出来的钱（`yuan`）和用的单价（`rate`）。不算给用户的调用也记，对账才对得上；删对话时不删。
- `billed` 是算给用户的钱。走免费额度的调用 `yuan` 记 0（`rate` 是 `free_tier`），`billed` 按没有免费额度时会用的那个模型的价格算（有 `same_as` 的按那个选项的模型，没有的按 Auto 的备用，`model_catalog.Catalog.billed_as`），所以免费额度用没用完，同样的问题扣的差不多。别的调用 `billed` 就是 `yuan`。
- 钱数也写进那条模型消息，每轮的用量里加起来（`usage.cost`、`subagent_usage.cost`），页面在模型名后面显示这一轮的 credits（主 agent 加子 agent）。

额度（`app/quota.py`）：北京时间的自然日、自然周（周一起）。游客每天 1000 credits；登录用户每天 10000、每周 50000；管理员不限；所有游客合计每天 50000（约 50 元，防脚本批量领游客身份刷）。数字在 `LINGDRAFT_CREDITS_GUEST_DAILY` / `LINGDRAFT_CREDITS_USER_DAILY` / `LINGDRAFT_CREDITS_USER_WEEKLY` / `LINGDRAFT_CREDITS_GUESTS_DAILY`；`LINGDRAFT_CREDITS_PER_ASK`（默认 200）是页面上“约可问 N 次”按一次多少 credits 换算。

- 怎么算：账上（`model_costs`）这个人在窗口里算给用户的几样（主 agent、子 agent 的模型调用，做成了的工具调用，资料库建索引）按 `billed` 加起来；出错、丢了的那一轮不算（用户没拿到回答）；点了停止的照算。游客期间花的记在游客身份上，登录并进账号后不跟过去。
- 只在开始前查：说话、排话、重新回答、改话、重试时用完了回 429（`detail` 是对象：`code` 是 `credits_exhausted`，带什么时候重置、是不是游客），正在跑的那一轮不管。
- `GET /api/credits`：`{unlimited, warn_at, per_ask, windows: [{kind, limit, used, resets_at}]}`，`kind` 是 `day`、`week`（登录用户才有）、`guests`（所有游客今天合计，页面不显示）；游客多一个 `login: {day, week}`（登录后每天、每周多少）。页面在账号菜单里显示今天、这周还剩多少，用了八成（`warn_at`）在输入框上面提醒，用完了提示里带登录入口。
- 管理员重置：`uv run python -m app.quota reset <owner>`（owner 是 `thread_owners` 里的，登录用户的 id 或 `guest:xxx`），从那一刻起重新数；`show <owner>` 看用了多少。
- 流到一半停了、断了的调用厂商照样收钱，接口却不给用量：记一行“不知道”，钱数是空的，不估算。还没流出东西就失败的不记。
- 后台一个线程用自己的连接写库，写不上只打日志。服务、命令行、评测起来时开（配了库才开），单元测试不开。
- 对账：`uv run python -m app.costs --days 7`，按北京时间的天、厂商、模型汇总，和各家后台的账单比。本机跑评测记在本机连的库里，对账时和线上的加起来。

海报的检查点还在 SQLite 里（`data/checkpoints.sqlite`），每个 thread 的 `v{n}.png / v{n}.html` 和导出的 `final.png / final.html / brief.json` 在 `output/<thread_id>/`。进程重启后 `resume` 可直接续跑。

## 目录

- `app/graph/poster/state.py` 事实 / 元素 / 版本模型与三个中断的回传 schema
- `app/graph/poster/nodes.py` 节点与路由；`graph.py` 建图与检查点
- `app/llm.py` `Planner` 协议：`ArkPlanner`（豆包，json_schema 结构化输出）/ `FakePlanner`
- `app/render/` Jinja2 模板 → Playwright 截图；`checks.py` 事实逐字比对 / 违禁词 / 溢出
- `app/api/` FastAPI：
  - `runner.py`：两种场景共用的部分，即运行占位 `RunRegistry` 和事件日志 `EventLog`（转成 SSE；两者都是进程内实现，多 worker 时换共享存储）；还有海报的 `GraphRunner`（同步的图放进一个工作线程）。
  - `research_runner.py`：对话的运行器，在 `app/aio.py` 的事件循环里异步地跑。管排队、停止、排着的话和插话、重试和重新回答、状态视图；`open_storage` 打开存储。
  - `run_store.py`：运行记录、排着的话、对话归谁和置顶。
  - `drafts.py`：模型写文件时，把流出来的参数边收边解成 SSE 的 `draft`。
  - `main.py`：接口；`favicon.py`：来源网站的图标；`inspiration.py`：今日灵感。
- `app/graph/research/` 对话场景：
  - `agent.py`：`create_agent` 和它的中间件，从外到里：
    - TurnMiddleware：起标题、定下这一轮的模型（Auto 第一句话判断难易）、记录每一轮、换系统提示词（带上长期记忆）、取插进来的话；
    - ContentGuard：内容审核；
    - CompactionMiddleware：上下文压缩，模型说超长时压了再发；
    - ToolChargeMiddleware：做成了的工具调用记账；
    - ToolProgressMiddleware：工具进度；
    - WeatherMiddleware、TravelMiddleware、MapsMiddleware、WebToolsMiddleware、DocsMiddleware、ImageMiddleware：各个工具的结果整理（见下面的 `app/tools/`）；
    - InvalidToolCallRetryMiddleware：无效工具调用让模型重发；
    - ModelCallCapMiddleware：一轮最多调 20 次模型，最后一次不许调工具；
    - ModelSelect：每次调模型换上这一轮的模型，拼进用户发的文件和图，去掉用不上的工具（`view_image`、`search_knowledge`），出问题换备用；
    - InterruptedRetryMiddleware：生成到一半断了再调一次。
  - `subagent.py`：`task` 工具和子 agent。每轮最多派 5 个，同一个对话同时最多跑 3 个；每个最多调 12 次模型。
  - `compaction.py`：上下文压缩和 `recall` 工具。
  - `memory.py`：长期记忆。
  - `history.py`：从消息里读出用户的话、发的图和文件、工具步骤、画好的图、写的文件、用量。
  - `sources.py`：从搜索、读页、查文档、查资料库的结果里收集来源，同一网页只给一个 id。
  - `state.py`。
  - `graph.py`：存储（共用的连接池、线程列表）。
  - `prompts.toml`：给模型看的文字；`prompts.py` 只负责渲染（读 toml 的在 `app/prompting.py`）。
- `app/research_llm.py` `ResearchPlanner` 协议：`LiveResearchPlanner`（各家模型按 `models.toml` 建，内部用途走 `RoleModel`，先走免费额度；真实网关）/ `FakeResearchPlanner`（脚本化的假工具模型 + 进程内的假搜索、读页、天气、地图、航班火车、生图服务，假生图服务也管按编号取图、删图）
- `app/model_catalog.py` + `app/models.toml`：模型选项、内部用途、Auto、免费额度、价格。`app/free_used_up.py`：用完的免费额度记进库。
- `app/identity.py` 身份；`app/quota.py` 额度；`app/costs.py` 成本记账；`app/tracing.py` LangSmith；`app/clock.py` 模型看得到的当前时间（评测回放时挪到录像的那时）。
- `app/files/`：文件表（`store.py`）、上传和解析（`service.py`、`parse.py`、子进程 `child.py`）、模型写的文件用到的切段和按片段改（`artifacts.py`）、补登记图片（`backfill_images.py`）。
- `app/knowledge/`：资料库（`libraries.py`）、切块和分词（`text.py`）、块和查询（`store.py`）、向量和重排（`remote.py`）、建索引和查（`service.py`）。
- `app/tools/`：
  - `gateway.py`：agent-tools 网关客户端（传图、按编号取图、删图），和网关出错时的几种异常。
  - `files.py`：用户发的文件怎么给模型，`read_file`。`write_file.py`：`create_file`、`edit_file`。`vision.py`：用户发的图怎么给模型，`view_image`。`knowledge.py`：`search_knowledge`。
  - `research_tools.py`：agent 自己的工具（`get_current_time`），描述在 `prompts.toml` 的 `[tools.*]`。
  - `mcp.py`：MCP 工具共用的部分：经网关连、取工具、认出网关的 401/403（让这一轮失败）和 429（今天的上限到了）。
  - `web_mcp.py`：搜索、读网页。`search_web`、`read_page` 是 agent-tools 的 MCP 工具（怎么搜、怎么读、结果的样子、失败时给模型的话都在 search-service、reader-service 里，和 Fusion 共用）；这里接上灵稿自己的一段说明（什么时候搜、搜够没有、来源 id），中间件给结果带上来源 id、把失败整理成页面上的中文，没连上时用同名的占位工具。
  - `image_mcp.py`：画图、改图。`generate_image` 是 agent-tools 的 MCP 工具（出图、存图、改图找原图、比例、给模型的说明都在 image-service 里，和 Fusion 共用）；这里接上灵稿自己的一段说明，中间件管每轮最多画 3 张、改图只认这段对话里画的和用户发的图、给带图的话接上说明，把结果整理成图片卡片要的东西，没连上时用同名的占位工具。
  - `weather_mcp.py`：查天气。`get_weather` 是 agent-tools 的 MCP 工具（挑城市、整理预报、缓存、给模型的说明都在 weather-service 里，和 Fusion 共用），用 LangChain 的 MCP 适配器接进来；这里接上灵稿自己的一段说明，中间件把结果整理成模型看的文字，网关出问题算没查到、密钥不对让这一轮失败。
  - `maps_mcp.py`：查地点、比路线。`place_search`、`route_compare` 是 agent-tools 的 MCP 工具（先高德后腾讯、地名怎么定、重名怎么问、给模型的说明都在 maps-service 里，和 Fusion 共用）；这里接上灵稿自己的一段说明（卡片署了服务商不用重复、拿不到用户位置要问），中间件把整份结果留给地点卡、路线卡，网关今天的上限到了算额度用完，没连上时用同名的占位工具。
  - `flyai_mcp.py`：查航班、火车，排行程。`search_flights`、`search_trains` 是 agent-tools flyai 的 MCP 工具（日期定不了先问、只要高铁、飞猪认不出的地名、结果的样子都在服务里，和 Fusion 共用）；这里接上灵稿自己的一段说明（卡片上都列了、别贴预订链接、接站换乘用查到的站名和城市），中间件把整份结果留给航班卡、火车卡，每轮最多查 4 次；比路线的起终点是查到的车站时，城市没填对就不交给地图、告诉模型该填哪个。`present_itinerary` 是灵稿自己的：模型按这段对话查到的班次编号排方案，核对编号和先后后画成行程卡，一个回答最多一张。
  - `docs_mcp.py`：查文档。`resolve-library-id`（按库名找库）、`query-docs`（查这个库的文档）是 Context7 的 MCP 工具，经 agent-tools 的 context7 服务（注入密钥、只放这两个工具、限流时标出来）；这里接上灵稿自己的一段说明（什么时候用、引用来源 id），中间件给每段文档的出处带上来源 id，带来源的那几段记在 artifact 里给来源列表和压缩后取回。Context7 免费版每月 1000 次（每月初重置，和 Fusion 共用），网关给灵稿设了每天 30 次；限流、上限到了、连不上都告诉模型改用搜索和读官方文档。连上了才带（和天气一样）。
- `app/model_guard.py` 模型服务出错的分类和熔断；`app/aio.py` 对话的事件循环
- `app/evals/` + `evals/` 回放评测：用真实模型跑 `evals/research/cases.yaml`，按轨迹和裁判模型打分，用法和写用例的约定见 `evals/README.md`
- `../frontend/` Vite + React 对话式页面（参照 WorkBuddy）。
  - 海报（`#poster`）：需求气泡 → 漂浮问题卡逐项确认事实 → 风格卡 → 海报 + 元素状态内嵌在助手回复里 → 评审卡 / 自由文本改稿 → 导出。
  - 对话页：`src/workspace.jsx`。左栏（`sidebar.jsx`）是对话列表（能搜标题，每条能置顶、改名、导出、删除）、资料库入口、额度和账号；中间是对话；右栏看模型写的文件（`artifact.jsx`）、这段对话的文件列表和用户发的文件（`workspace-files.jsx`）。`timeline.jsx` 里每一轮先是过程（思考、搜索、读页、子任务按先后，跑的时候展开，跑完收起），然后是天气卡片、地点卡、路线卡、航班卡、火车卡、行程卡、画的图，再是带来源角标的正文、写的文件的卡片和这条回答用到的来源，最后一轮下面是推荐问题；回答时又说的话贴在输入框上沿。设置窗口（`settings.jsx`）里有记忆（`memory.jsx`）；资料库是单独一页（`/library`，`knowledge.jsx`）。
- `tests/`：
  - 海报：T1 T2 T4 T5 T6 T8 + 校验重试、风格自定义、cta 绑定等。
  - 对话（都只用假模型）：
    - `test_research.py`：多轮对话、流式事件、停止和重新生成、内容审核、检查点往返、来源。
    - `test_research_runs.py`：并行执行、运行占位、断线续读、失败后重试、停止。
    - `test_research_queue.py`：排着的话和插话。
    - `test_research_subagent.py`：子 agent。
    - `test_compaction.py`（含超长压了再发）、`test_memory.py`。
    - 工具：`test_images.py`（画图）、`test_weather.py`、`test_web_tools.py`（搜索、读页）、`test_maps.py`、`test_travel.py`（航班火车、行程）、`test_docs.py`（查文档）、`test_gateway_tools.py`（网关客户端）。
    - `test_vision.py`：用户发的图怎么给模型、`view_image`。`test_files.py`：用户发的文件、`read_file`。`test_artifacts.py`：模型写的文件。`test_knowledge.py`：资料库。
    - `test_model_options.py`：选模型、思考开关。`test_auto_routing.py`：Auto、免费额度、换备用。
    - `test_costs.py`：成本记账。`test_quota.py`：额度。`test_identity.py`：身份、对话和记忆按人分、游客并进账号。
    - `test_run_records.py`：运行记录、没配库启动报错。
    - `test_api_research.py`：接口。
    - `test_research_eval.py`、`test_eval_decisions.py`、`test_eval_smith.py`：评测的离线部分、决策点回放、传 LangSmith。`test_tracing.py`：线上 tracing。
    - `test_favicon.py`、`test_inspiration.py`、`test_prompts.py`。
  - `test_model_guard.py`：模型服务出错的分类和熔断。
