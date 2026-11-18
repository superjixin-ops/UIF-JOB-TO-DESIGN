#!/usr/bin/env bash
# 本地起服务。默认只监听 127.0.0.1，配合 tunnel.sh 暴露到公网。
#
# 用法：
#   ./scripts/serve.sh              # 8000 端口
#   PORT=8011 ./scripts/serve.sh    # 换端口
set -euo pipefail

cd "$(dirname "$0")/.."

PORT="${PORT:-8000}"
export PDC_REPO="${PDC_REPO:-samples/pipelines}"
export PDC_RULES_DIR="${PDC_RULES_DIR:-samples/rules}"
export PDC_SKILLS_DIR="${PDC_SKILLS_DIR:-samples/skills}"
export PDC_OUT_DIR="${PDC_OUT_DIR:-out}"
export PDC_RAG_MODE="${PDC_RAG_MODE:-bm25}"   # 容器/演示默认不下载向量模型
export PDC_API_TOKEN="${PDC_API_TOKEN:-}"     # 留空=只允许本机；对外必须设置
export PDC_RUN_TOKEN_BUDGET="${PDC_RUN_TOKEN_BUDGET:-120000}"
export PDC_RATE_PER_MIN="${PDC_RATE_PER_MIN:-6}"

echo "启动 http://127.0.0.1:${PORT}"
echo "  repo : ${PDC_REPO}"
echo "  out  : ${PDC_OUT_DIR}"
echo "  rag  : ${PDC_RAG_MODE}"
echo "  token: ${PDC_API_TOKEN:-（未设置，仅本机可访问）}"
echo "  docs : http://127.0.0.1:${PORT}/docs"

exec uv run uvicorn pdc.api:app --host 127.0.0.1 --port "$PORT"
