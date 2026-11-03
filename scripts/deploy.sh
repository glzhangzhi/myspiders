#!/usr/bin/env bash
# 部署：测试 → 构建前端 → 重启服务。
#
#   scripts/deploy.sh              全量（后端改动需要重启）
#   scripts/deploy.sh --web-only   只构建前端（构建产物即时生效，不重启）
#   scripts/deploy.sh --no-test    跳过测试（紧急回滚时用）
#
# 环境变量：SERVICE（默认 myspider）、PYTHON（默认 .venv/bin/python）
set -euo pipefail
cd "$(dirname "$0")/.."

SERVICE="${SERVICE:-myspider}"
PY="${PYTHON:-.venv/bin/python}"
WEB_ONLY=0; TEST=1
for a in "$@"; do
  case "$a" in
    --web-only) WEB_ONLY=1 ;;
    --no-test) TEST=0 ;;
    *) echo "未知参数：$a" >&2; exit 2 ;;
  esac
done

if [[ $TEST == 1 && $WEB_ONLY == 0 ]]; then
  echo "▶ pytest"; "$PY" -m pytest -q
fi

echo "▶ 前端：安装依赖 + 类型检查 + 单测 + 构建"
pnpm -C web install --frozen-lockfile --silent
if [[ $TEST == 1 ]]; then pnpm -C web run -s typecheck; pnpm -C web run -s test; fi
pnpm -C web run -s build

if [[ $WEB_ONLY == 1 ]]; then
  echo "✓ 前端已更新（无需重启）"; exit 0
fi

echo "▶ 重启 $SERVICE"
systemctl restart "$SERVICE"
for i in $(seq 30); do
  if curl -fs -o /dev/null "http://127.0.0.1:${PORT:-12312}/api/health" 2>/dev/null; then
    echo "✓ $SERVICE 已就绪"; exit 0
  fi
  sleep 1
done
echo "✗ $SERVICE 30 秒内没有起来：journalctl -u $SERVICE -n 50" >&2; exit 1
