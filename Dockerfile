# 生产镜像：多阶段构建，最终镜像不含构建工具
FROM python:3.12-slim AS base
WORKDIR /app

# uv 用来按 lock 文件精确安装依赖
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# 先只拷依赖清单，利用 Docker 层缓存：
# 源码变了但依赖没变时，这一层不会重建
RUN mkdir -p /app/out
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY pdc ./pdc
COPY eval ./eval
# 样例数据：容器里没有 ~/Documents/CRD-7，必须自带
COPY samples ./samples

# 容器里默认走 BM25：避免启动时下载 100MB 向量模型
ENV PDC_RAG_MODE=bm25 \
    PDC_MAX_CONCURRENCY=4 \
    PDC_REPO=/app/samples/pipelines \
    PDC_RULES_DIR=/app/samples/rules \
    PDC_SKILLS_DIR=/app/samples/skills \
    PDC_OUT_DIR=/app/out
EXPOSE 8000

# 健康检查，K8s / docker-compose 都能用
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')"

CMD ["uv", "run", "uvicorn", "pdc.api:app", "--host", "0.0.0.0", "--port", "8000"]
