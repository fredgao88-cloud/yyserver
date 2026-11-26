#!/usr/bin/env bash
# 院内 DIP 在院患者定时巡检脚本（防重入锁 + 脱敏日志 + 钉钉推送）
# 推荐 crontab 配置（每 15 分钟执行一次）：
# */15 * * * * /opt/hospital-agent/scripts/run_inpatient_scan.sh >> /var/log/hospital_agent_scan.log 2>&1

set -euo pipefail

LOCK_FILE="/tmp/hospital_agent_scan.lock"
CONTAINER_NAME="${API_CONTAINER_NAME:-hospital_agent_api}"

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') [SKIP] 上一次在院巡检尚未结束，本次跳过"
  exit 0
fi

echo "$(date '+%Y-%m-%d %H:%M:%S') [START] 触发在院 DIP 定时巡检 (--notify)"
if docker exec "${CONTAINER_NAME}" python -m tools.inpatient_scanner --notify; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') [DONE] 在院 DIP 定时巡检完成"
else
  rc=$?
  echo "$(date '+%Y-%m-%d %H:%M:%S') [ERROR] 在院 DIP 定时巡检异常退出 code=${rc}"
  exit "${rc}"
fi
