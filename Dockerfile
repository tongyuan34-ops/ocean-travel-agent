# syntax=docker/dockerfile:1
# 单镜像双用途：默认跑后端（uvicorn），compose 里用 command 覆盖跑前端反代。
FROM python:3.13-slim AS runtime

# uv 负责装依赖：读 uv.lock 精确还原，比 pip resolve 快一个数量级
COPY --from=ghcr.io/astral-sh/uv:0.13.3 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    VIRTUAL_ENV="/app/.venv"

WORKDIR /app

# 先只装依赖，代码改动不会让这一层失效
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

# 再装项目本身
COPY app ./app
COPY web ./web
RUN uv sync --frozen --no-dev

# 运行期需要可写的目录：媒体产物与会话落盘
RUN useradd --create-home --uid 10001 ocean \
    && mkdir -p /app/media /app/data /app/cookies \
    && chown -R ocean:ocean /app
USER ocean

EXPOSE 8000 3001

HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=2)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
