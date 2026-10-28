# LingDraft backend（海报：M1 图 + M2 接口；调研：R3 图 + CLI + R4 接口）

黑五海报单场景的 LangGraph 实现：`intake → confirm_facts(中断) → 风格 → plan_layout → render → check → review(中断) → revise / manual_edit / change_fact / export`。
设计见 `../docs/黑五海报PoC技术方案.md`。

调研场景（图 + CLI + 接口 + 前端）：`intake → confirm_scope(只在需要澄清时中断) → research（全局 create_agent，`ask_user` 会中断；无文字答案时无工具受控收尾）→ consolidate（无工具结构化整理，关键缺口回 research 补一轮；补查没拿到新答案或整理失败就保留上一轮的结果）→ write（每版连同下载包一起写好，来源状态按这版正文重算）→ check（只是说明，不拦交付）→ review(可选修改：exclude_source / edit_claim / rewrite_section 改完出新一版；approve 只是结束)`。每一版报告写出来就是交付物；没答完的会在报告开头写明还缺什么。模型默认 DeepSeek V4 Pro（调研 agent 开思考，结构化步骤不开）。设计见 `../docs/调研场景与多场景架构设计.md` 第 17 节。

## 运行

先把 `.env.example` 复制为 `.env` 并填入 `ARK_API_KEY`（不填只能用 `--fake` / `LINGDRAFT_FAKE=1`）。

```bash
uv sync
uv run playwright install chromium
uv run pytest -q            # 95 个用例，不联网（FakePlanner / FakeResearchPlanner + MockTransport 网关）
```

Web 方式（M2）：

```bash
LINGDRAFT_FAKE=1 uv run uvicorn app.api.main:app --port 8000   # 去掉 LINGDRAFT_FAKE 走豆包，需要 ARK_API_KEY
cd ../frontend && npm install && npm run dev                    # http://localhost:5173，/api 代理到 8000
```

环境变量：`ARK_API_KEY`、`ARK_MODEL`、`ARK_MODEL_LITE`、`ARK_IMAGE_MODEL`、`LINGDRAFT_BACKGROUND=0` 关闭背景图生成、`LINGDRAFT_FAKE=1` 用假模型、`LINGDRAFT_RESEARCH_WORKERS` 同时跑几个调研线程（默认 4，多出来的排队）；调研场景另需 `AGENT_TOOLS_URL` / `AGENT_TOOLS_API_KEY`（agent-tools 网关）与 `DEEPSEEK_API_KEY`；`RESEARCH_PROVIDER`（deepseek / ark）、`RESEARCH_MODEL`、`RESEARCH_THINKING`、`RESEARCH_EFFORT` 可改调研模型。

接口：`POST /api/threads`（multipart：request，image 可选）和 `POST /api/threads/{id}/resume`（`{"payload": ...}`）都返回 SSE：`thread` / `started` / `progress` / `node` / `interrupt` / `error` 事件，`state` 事件是完整状态，最后以 `end` 收尾；每个事件带 `id`（“执行-序号”），安静 15 秒发一行注释保活；`GET /api/threads/{id}/state`；`GET /api/threads/{id}/files/{name}`；`GET /api/threads/{id}/export` 打包 zip。

调研接口同构，前缀 `/api/research/threads`：`POST`（json `{"request"}`）、`/{id}/resume`、`/{id}/retry`、`/{id}/events?after=<事件 id>`、`/{id}/state`、`/{id}/files/report_v{n}.md`、`/{id}/package/{report.md|items.json|sources.json|manifest.json}`、`/{id}/export`。agent 的动态提问在接口上统一成 `{"type":"research.ask", title, options, why}`，回传 `{"answer"}` 或 `{"skip":true}`（二选一），由 `ResearchRunner` 转成中间件的 decisions。

调研的 resume 要带上当前这张卡的中断 id：`{"interrupt_id": "...", "payload": {...}}`，id 在 `state.interrupt.id` 和 `interrupt` 事件里。回答先过 `app/graph/research/answers.py` 的校验（CLI 也用同一份）再交给图：不合法返回 400，线程停在原卡上；卡已过期（线程往前走了）或这一步还在排队 / 运行返回 409。同一个线程同时只接受一次执行：请求被接受时占位，执行完才释放，浏览器断开不影响；新线程排队时也能马上查到状态。

调研线程的 `state.status`：`queued` / `running`（有执行占着位）、`waiting`（停在卡上等用户）、`done`（走完了）、`failed`（某一步出错，或执行被掐断、比如服务重启；原因在 `state.error`）。失败不会抹掉已有内容，`POST /{id}/retry` 从最后一个检查点接着跑：失败的那一步重做，agent 里已经做过的搜索、读页不重来；没失败的返回 409。回答提交后还没被用上就出错的（比如重建 agent 时失败），线程仍是 `waiting`、停在原卡上，重新回答即可；agent 已经拿到回答、之后才出错的，原卡作废（再答返回 409），线程是 `failed`，重试会带着已给的回答接着跑。新线程在把 id 交给页面之前就把需求写进检查点，排队时服务重启也查得到（`failed`，可重试）。

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

调研场景 CLI（`--scenario research`；回传格式见 `app/cli.py` 顶部注释）：

```bash
uv run python -m app.cli --scenario research --fake new "AI 海报工具在局部修改上的现状（2026）"
uv run python -m app.cli --scenario research --fake resume <thread_id> '{"answers":{"q1":"改文字"}}'                          # 澄清问题（只在 intake 提了问题时出现）
uv run python -m app.cli --scenario research --fake resume <thread_id> '{"answer":"包含"}'                                   # agent 动态提问；或 {"skip":true}
uv run python -m app.cli --scenario research --fake resume <thread_id> '{"action":"exclude_source","target":"s2"}'            # 终审：排除来源，依赖它的论断退回草稿并从报告移除
uv run python -m app.cli --scenario research --fake resume <thread_id> '{"action":"approve"}'                                 # 不用改了，结束
uv run python -m app.cli --scenario research state <thread_id>
```

调研的检查点在 `data/checkpoints_research.sqlite`，每版报告 `report_v{n}.md` 和最新一版的下载包 `package/{manifest,items,sources}.json + report.md`（`items.json` 只含正文用到的论断，`sources.json` 只含它们引用的来源）在 `output/research/<thread_id>/`。“导出”只指下载：`state.package` 给出最新一版的下载地址，`/{id}/export` 打成 zip；改动前留下的旧线程，包和最新一版对不上的不给下载地址。

修复前留下的卡死线程：旧版节点拒绝回答后，坏回答会留在检查点里，之后每次提交正确回答都报同一个错（比如一直“回答的不是当前问题”）。停在澄清卡或终审卡上的这种线程，用 `PYTHONPATH=. uv run python scripts/unstick_research_thread.py <thread_id> ...` 手工解卡（不调模型；先确认线程没在跑），然后刷新页面重新回答。

检查点在 `data/checkpoints.sqlite`，每个 thread 的 `v{n}.png / v{n}.html` 和导出的 `final.png / final.html / brief.json` 在 `output/<thread_id>/`。进程重启后 `resume` 可直接续跑。

## 目录

- `app/graph/poster/state.py` 事实 / 元素 / 版本模型与三个中断的回传 schema
- `app/graph/poster/nodes.py` 节点与路由；`graph.py` 建图与检查点
- `app/llm.py` `Planner` 协议：`ArkPlanner`（豆包，json_schema 结构化输出）/ `FakePlanner`
- `app/render/` Jinja2 模板 → Playwright 截图；`checks.py` 事实逐字比对 / 违禁词 / 溢出
- `app/api/` FastAPI；`runner.py` `BaseRunner`（图执行放到工作线程，事件写进 `EventLog` 再转成 SSE；`RunRegistry` 运行占位；两者都是进程内实现，多 worker 时换共享存储）+ 海报 `GraphRunner`（单线程）；`research_runner.py` 调研的执行器（多线程）、状态视图与运行状态、重试、中断整形（带中断 id）、事件摘要
- `app/graph/research/` 调研场景：`state.py`（范围 / 大纲 / 来源 / 论断 / 逐句正文）、`agent.py`（`create_agent` + HITL / 调用上限中间件、`Findings` 结构化产出、从工具消息里解析候选来源）、`answers.py`（三种确认卡的回答校验，接口 / CLI / 节点共用）、`nodes.py` / `graph.py`、`report.py`（确定性拼装报告与六项校验）、`prompts.py`
- `app/research_llm.py` `ResearchPlanner` 协议：`ArkResearchPlanner`（豆包 + 真实网关）/ `FakeResearchPlanner`（脚本化的假工具模型 + MockTransport 网关）
- `app/tools/` agent-tools 网关客户端（搜索 / 网页读取）与调研 agent 的工具（`search_web` / `read_page` / `ask_user`）
- `scripts/` 对真实模型与网关的验证脚本：`probe_create_agent.py`（假搜索）、`probe_gateway_agent.py`（真实搜索 / 读取）、`probe_research_llm.py`（调研场景三种结构化输出），`PYTHONPATH=. uv run python scripts/<name>.py`
- `../frontend/` Vite + React 对话式页面（参照 WorkBuddy）。海报：需求气泡 → 漂浮问题卡逐项确认事实 → 风格卡 → 海报 + 元素状态内嵌在助手回复里 → 评审卡 / 自由文本改稿 → 导出。调研（`src/research.jsx`，顶栏切换）：需求清楚直接开始，模糊才弹澄清卡 → agent 提问卡（带“不问会怎样”，可跳过）→ 大纲 + 论断 + 假设 → 报告 / 论断 / 来源三个页签，点论断或来源单独处理 → 终审卡 / 自由文本重写某章 → 导出确认物包
- `tests/` 海报：T1 T2 T4 T5 T6 T8 + 校验重试、风格自定义、cta 绑定等；调研：`test_research.py`（全流程可追溯 / 排除来源传播 / 改论断 / 重写章节 / 改大纲只补调研新章 / 跳过提问 / sqlite 跨实例恢复 / 同批夹提问也按额度 / 没呈现的提问不占额度 / 回补失败保留第一轮 / 来源状态随报告重算 / 重写带用户约束 / 无效报告照样交付）、`test_research_resume.py`（路径穿越与符号链接 / 严格载荷 / 被拒后能继续 / 过期中断 id / 重复提交 / 断线与排队）、`test_research_runs.py`（并行执行 / 提交失败退还占位 / 断线续读与刷新补发 / 保活 / 失败保留内容与重试 / 回答已用上后出错不再让人重答 / 排队中重启后可重试 / 被掐断的执行可重试）
