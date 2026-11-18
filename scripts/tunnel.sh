#!/usr/bin/env bash
# 用 SSH 反向隧道把本地服务暴露到公网。零安装、零账号。
#
# 为什么不用 cloudflared / ngrok：
#   GitHub 下载被墙，二进制下不来；ngrok 还要注册拿 token。
#   SSH 是系统自带的，localhost.run 支持免密钥隧道。
#
# 用法：
#   ./scripts/serve.sh &          # 先起服务
#   ./scripts/tunnel.sh 8000      # 再开隧道
#
# 注意：
#   - 域名会轮换，重启就变。面试前重跑一次，把新链接发过去。
#   - 进程活着隧道才在。关终端即失效。
#   - 这是演示通道，不要放生产数据。
set -euo pipefail

PORT="${1:-8000}"
HOST="${TUNNEL_HOST:-localhost.run}"

echo "把 localhost:${PORT} 暴露到公网（${HOST}）…"
echo "Ctrl+C 结束"
echo

if [ "$HOST" = "localhost.run" ]; then
  exec ssh -o StrictHostKeyChecking=no -o ServerAliveInterval=30 \
    -R 80:localhost:"$PORT" nokey@localhost.run
fi

# 备用：serveo
exec ssh -o StrictHostKeyChecking=no -R 80:localhost:"$PORT" serveo.net
