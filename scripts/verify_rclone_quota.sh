#!/usr/bin/env bash
# 验证 rclone 是否已在用「自己的 Google Cloud OAuth 项目」，并压测配额是否真的变宽。
#
# 用法:  bash scripts/verify_rclone_quota.sh
# 背景:  client_id 为空 → 走 rclone 内置公共项目的共享配额 → av_images(3万+小文件)
#        必然撞 403 Quota exceeded。见 app/services/backup_service.py 注释。

set -uo pipefail

CONF="${RCLONE_CONFIG:-/root/.config/rclone/rclone.conf}"
REMOTE="google drive:myspiders-backup"

echo "=== 1) 凭据检查 ==="
cid=$(python3 - "$CONF" <<'PY'
import configparser,sys
c=configparser.ConfigParser(); c.read(sys.argv[1])
print((c['google drive'].get('client_id') or '').strip())
PY
)
if [ -z "$cid" ]; then
  echo "❌ client_id 仍为空 —— 还在用 rclone 公共共享配额，问题没治本。"
  exit 1
fi
echo "✅ client_id 已设置: ${cid:0:18}…  (项目号 ${cid%%-*})"

echo
echo "=== 2) 连通性 / 授权是否有效 ==="
if ! rclone about "$REMOTE" 2>&1 | head -6; then
  echo "❌ about 失败:授权可能没换成功或已过期"
  exit 1
fi

echo
echo "=== 3) 配额压测(高并发递归列目录 —— 旧配额下这步必 403) ==="
start=$(date +%s)
out=$(rclone lsjson "$REMOTE/av_images" --recursive --fast-list \
        --checkers 16 --transfers 16 2>&1 >/dev/null)
rc=$?
dur=$(( $(date +%s) - start ))

if echo "$out" | grep -qi "quota\|rate.*exceed\|403"; then
  echo "❌ 仍然撞配额(${dur}s):"
  echo "$out" | head -3
  exit 1
fi
if [ $rc -ne 0 ]; then
  echo "⚠️  非配额类错误(rc=$rc):"; echo "$out" | head -5; exit 1
fi

echo "✅ 高并发递归列目录通过,用时 ${dur}s,无 403。"

echo
echo "=== 4) 本地 vs 远端文件数 ==="
loc=$(find /root/myspiders/downloads/av_images -type f 2>/dev/null | wc -l)
rem=$(rclone size "$REMOTE/av_images" --json --fast-list 2>/dev/null \
        | python3 -c "import json,sys; print(json.load(sys.stdin)['count'])")
echo "本地 $loc / 远端 $rem"
[ "$loc" = "$rem" ] && echo "✅ 完全一致" || echo "⚠️  有差异(下次 backup 会补齐)"

echo
echo "全部通过。可以把 backup_service.py 里的限流放宽了(见文件内注释)。"
