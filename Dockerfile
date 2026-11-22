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

# 中文字体：导出 PDF 行程单要用。slim 镜像里一个中文字体都没有，
# 缺了它 /trips/export 会直接抛「找不到中文字体」，而接口其余部分全正常。
# 选文泉驿而不是 Noto CJK：Noto 是 CFF（PostScript）轮廓，reportlab 注册不了。
#
# 放在 COPY 代码之前：字体和源码无关，放后面的话每次改一行代码都会重跑 apt，
# 国内网络下一慢就 exit 100，整个镜像重建被卡住。
# Acquire::Retries / Timeout：拉 Debian 包经常半路超时，不设就只能重试整个构建。
RUN apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=60 update \
    && apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=60 install -y --no-install-recommends fonts-wqy-zenhei \
    && rm -rf /var/lib/apt/lists/*

# 先只装依赖，代码改动不会让这一层失效
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

# 再装项目本身
COPY app ./app
COPY web ./web
# 知识库语料随仓库分发，检索靠它。漏掉这一行不会报错，只会让
# search_knowledge 永远返回「知识库为空」——打包版踩过一次，这里别再踩
COPY knowledge ./knowledge
# 技能目录同理：漏了不报错，只是 /api/v1/health 的 skills 变 0、技能全不可用
COPY skills ./skills
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
