# 实验 01：灵稿的对话图挂到 langgraph dev 上

`chat_graph.py` 用 `build_agent` 原样建对话图，用的是假模型和假网关。它只为能挂到 Agent Server 上补了两处，原因见笔记 01 的第 6、7 条：

- context 给默认值；
- 同步 middleware 外面包了一层异步桥。

搜索词里带“慢”时，假网关会慢 4 秒，用来试停止和排队。

起服务（不改项目依赖，`--with` 临时装 CLI）：

```sh
cd docs/LangGraph研究/实验/01-agent-server
LANGGRAPH_CLI_NO_ANALYTICS=1 PYTHONPATH=../../../../backend LINGDRAFT_FAKE=1 \
  uv run --project ../../../../backend --with "langgraph-cli[inmem]" langgraph dev --port 2024 --no-browser --no-reload
```

跑实验（在 backend 下，用项目里装好的 langgraph-sdk）：

```sh
uv run python "../docs/LangGraph研究/实验/01-agent-server/exp.py" e1 e2 e3:interrupt e3:rollback e4:reject e4:enqueue e4:interrupt e4:rollback e5 e6
```
