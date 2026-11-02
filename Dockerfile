# 一个镜像：先构建前端页面，再装后端；后端（uvicorn，单进程）连同页面一起出，接口在 /api 下
# 基础镜像钉到具体版本，别改成 :latest 这类滚动 tag；要升级就改这里的版本号，走 PR
FROM node:22.23.4-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12.16-slim
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev
COPY backend/ ./
COPY --from=web /web/dist /app/static
# 对话里画的图、网站图标存在 /app/data（compose 里挂到宿主机）；对话本身存 Postgres（LINGDRAFT_DATABASE_URL）
ENV PATH="/app/.venv/bin:$PATH" \
    LINGDRAFT_STATIC_DIR=/app/static \
    LINGDRAFT_DATA_DIR=/app/data \
    LINGDRAFT_OUTPUT_DIR=/app/data/output
EXPOSE 8000
# 部署的提交号（CI 构建时传进来），记进运行记录、/api/health 里也看得到；放在最后，换版本不让前面的层失效
ARG LINGDRAFT_VERSION=""
ENV LINGDRAFT_VERSION=${LINGDRAFT_VERSION}
# 只能一个进程：执行登记、事件日志都在进程内（见 app/api/runner.py）
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
