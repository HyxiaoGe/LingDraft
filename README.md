# 灵稿 · LingDraft

从一个想法，到一份拿得出手的作品。

能搜、能读、回答会标来源的 AI 对话，基于 LangGraph 和 DeepSeek。

## 功能

- 联网搜索、读网页，回答带来源角标
- 复杂问题拆给子 agent 并行去查
- 天气卡片，画图和改图
- 思考过程可见；回答进行中可以接着说
- 长期记忆，长对话自动压缩上下文

## 本地运行

```bash
cd backend
cp .env.example .env    # 填数据库地址和密钥；LINGDRAFT_FAKE=1 用假模型
uv sync
uv run uvicorn app.api.main:app --port 8000

# 另开一个终端
cd frontend
npm install
npm run dev             # http://localhost:5173
```

测试：`cd backend && uv run pytest -q`

接口、环境变量、存储等开发说明见 [backend/README.md](backend/README.md)。
