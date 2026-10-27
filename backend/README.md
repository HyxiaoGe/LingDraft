# LingDraft backend（海报：M1 图 + M2 接口；调研：R3 图 + CLI + R4 接口）

黑五海报单场景的 LangGraph 实现：`intake → confirm_facts(中断) → 风格 → plan_layout → render → check → review(中断) → revise / manual_edit / change_fact / export`。
设计见 `../docs/黑五海报PoC技术方案.md`。

调研场景（图 + CLI + 接口 + 前端）：`intake → confirm_scope(中断) → plan → research_section × N（内嵌 create_agent，`ask_user` 会中断）→ confirm_outline(中断) → write → check → review(中断：approve / exclude_source / edit_claim / rewrite_section / reopen_outline) → export`。设计见 `../docs/调研场景与多场景架构设计.md`。

## 运行

先把 `.env.example` 复制为 `.env` 并填入 `ARK_API_KEY`（不填只能用 `--fake` / `LINGDRAFT_FAKE=1`）。

```bash
uv sync
uv run playwright install chromium
uv run pytest -q            # 45 个用例，不联网（FakePlanner / FakeResearchPlanner + MockTransport 网关）
```

Web 方式（M2）：

```bash
LINGDRAFT_FAKE=1 uv run uvicorn app.api.main:app --port 8000   # 去掉 LINGDRAFT_FAKE 走豆包，需要 ARK_API_KEY
cd ../frontend && npm install && npm run dev                    # http://localhost:5173，/api 代理到 8000
```

环境变量：`ARK_API_KEY`、`ARK_MODEL`、`ARK_MODEL_LITE`、`ARK_IMAGE_MODEL`、`LINGDRAFT_BACKGROUND=0` 关闭背景图生成、`LINGDRAFT_FAKE=1` 用假模型；调研场景另需 `AGENT_TOOLS_URL` / `AGENT_TOOLS_API_KEY`（agent-tools 网关）。

接口：`POST /api/threads`（multipart：request，image 可选）和 `POST /api/threads/{id}/resume`（`{"payload": ...}`）都返回 SSE：`progress` / `node` / `interrupt` / `error` 事件，最后一个 `state` 事件是完整状态；`GET /api/threads/{id}/state`；`GET /api/threads/{id}/files/{name}`；`GET /api/threads/{id}/export` 打包 zip。

调研接口同构，前缀 `/api/research/threads`：`POST`（json `{"request"}`）、`/{id}/resume`、`/{id}/state`、`/{id}/files/report_v{n}.md`、`/{id}/package/{report.md|items.json|sources.json|manifest.json}`、`/{id}/export`。agent 的动态提问在接口上统一成 `{"type":"research.ask", title, options, why}`，回传 `{"answer"}` 或 `{"skip":true}`，由 `ResearchRunner` 转成中间件的 decisions。

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
uv run python -m app.cli --scenario research --fake resume <thread_id> '{"values":{"question":"……","time_range":"2026"}}'   # 范围确认
uv run python -m app.cli --scenario research --fake resume <thread_id> '{"answer":"包含"}'                                   # agent 动态提问；或 {"skip":true}
uv run python -m app.cli --scenario research --fake resume <thread_id> '{"action":"approve"}'                                 # 大纲确认（或 action=edit + nodes）
uv run python -m app.cli --scenario research --fake resume <thread_id> '{"action":"exclude_source","target":"s2"}'            # 终审：排除来源，依赖它的论断退回草稿并从报告移除
uv run python -m app.cli --scenario research --fake resume <thread_id> '{"action":"approve"}'                                 # 导出确认物包
uv run python -m app.cli --scenario research state <thread_id>
```

调研的检查点在 `data/checkpoints_research.sqlite`，每版报告 `report_v{n}.md` 和导出的 `package/{manifest,items,sources}.json + report.md` 在 `output/research/<thread_id>/`。

检查点在 `data/checkpoints.sqlite`，每个 thread 的 `v{n}.png / v{n}.html` 和导出的 `final.png / final.html / brief.json` 在 `output/<thread_id>/`。进程重启后 `resume` 可直接续跑。

## 目录

- `app/graph/poster/state.py` 事实 / 元素 / 版本模型与三个中断的回传 schema
- `app/graph/poster/nodes.py` 节点与路由；`graph.py` 建图与检查点
- `app/llm.py` `Planner` 协议：`ArkPlanner`（豆包，json_schema 结构化输出）/ `FakePlanner`
- `app/render/` Jinja2 模板 → Playwright 截图；`checks.py` 事实逐字比对 / 违禁词 / 溢出
- `app/api/` FastAPI；`runner.py` `BaseRunner`（图执行放到单工作线程并转成 SSE）+ 海报 `GraphRunner`；`research_runner.py` 调研的状态视图 / 中断整形 / 事件摘要
- `app/graph/research/` 调研场景：`state.py`（范围 / 大纲 / 来源 / 论断 / 逐句正文）、`agent.py`（`create_agent` + HITL / 调用上限中间件、`Findings` 结构化产出、从工具消息里解析候选来源）、`nodes.py` / `graph.py`、`report.py`（确定性拼装报告与六项校验）、`prompts.py`
- `app/research_llm.py` `ResearchPlanner` 协议：`ArkResearchPlanner`（豆包 + 真实网关）/ `FakeResearchPlanner`（脚本化的假工具模型 + MockTransport 网关）
- `app/tools/` agent-tools 网关客户端（搜索 / 网页读取）与调研 agent 的工具（`search_web` / `read_page` / `ask_user`）
- `scripts/` 对真实模型与网关的验证脚本：`probe_create_agent.py`（假搜索）、`probe_gateway_agent.py`（真实搜索 / 读取）、`probe_research_llm.py`（调研场景三种结构化输出），`PYTHONPATH=. uv run python scripts/<name>.py`
- `../frontend/` Vite + React 对话式页面（参照 WorkBuddy）。海报：需求气泡 → 漂浮问题卡逐项确认事实 → 风格卡 → 海报 + 元素状态内嵌在助手回复里 → 评审卡 / 自由文本改稿 → 导出。调研（`src/research.jsx`，顶栏切换）：范围逐项确认 → agent 提问卡（带“不问会怎样”，可跳过）→ 大纲 + 论断 + 假设 → 报告 / 论断 / 来源三个页签，点论断或来源单独处理 → 终审卡 / 自由文本重写某章 → 导出确认物包
- `tests/` 海报：T1 T2 T4 T5 T6 T8 + 校验重试、风格自定义、cta 绑定等；调研：`test_research.py`（全流程可追溯 / 排除来源传播 / 改论断 / 重写章节 / 改大纲只补调研新章 / 跳过提问 / sqlite 跨实例恢复）
