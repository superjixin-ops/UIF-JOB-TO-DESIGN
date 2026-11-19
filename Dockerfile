# 生产镜像
FROM python:3.12-slim AS base
WORKDIR /app

# uv 按 lock 文件精确安装依赖
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

RUN mkdir -p /app/out

# 第一阶段：只装第三方依赖。
#
# 为什么必须加 --no-install-project：
#   这个仓库既是一个应用，本身也是一个可安装的包（pyproject 里 packages = ["pdc"]）。
#   只拷 pyproject.toml 就 uv sync，hatchling 会去找 pdc/ 目录，
#   而这时源码还没拷进来 → 构建失败。CI 上踩过两次。
#
#   分成两阶段后，依赖层仍能命中缓存：只要 uv.lock 没变，这层不会重建。
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# 第二阶段：源码就位后再装项目本身
#
# README.md 必须一起拷：pyproject 里 readme = "README.md"，
# hatchling 打包项目时要读它，缺了会报 "Readme file does not exist"。
# 第一阶段加了 --no-install-project 才把这个问题暴露出来。
COPY README.md ./
COPY pdc ./pdc
COPY eval ./eval
# 样例数据：容器里没有 ~/Documents/CRD-7，必须自带
COPY samples ./samples
RUN uv sync --frozen --no-dev

# 容器里默认走 BM25：避免启动时下载 100MB 向量模型
ENV PDC_RAG_MODE=bm25 \
    PDC_MAX_CONCURRENCY=4 \
    PDC_REPO=/app/samples/pipelines \
    PDC_RULES_DIR=/app/samples/rules \
    PDC_SKILLS_DIR=/app/samples/skills \
    PDC_OUT_DIR=/app/out

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')"

CMD ["uv", "run", "uvicorn", "pdc.api:app", "--host", "0.0.0.0", "--port", "8000"]
