#!/bin/sh
# WireGuard 密钥与配置迁移脚本（自动解析有效 Compose 配置的实际物理卷名并校验完整性）
# 用法：
#   sh wireguard/migrate_to_volume.sh [--source-dir ./wireguard/config] [--compose-file docker-compose.yml] [-p PROJECT] [--force] [--verify-only]
#   回退（将迁移卷或备份目录还原至旧目录后切回宿主目录挂载）：
#   sh wireguard/migrate_to_volume.sh --rollback --target-dir ./wireguard/config [--compose-file docker-compose.yml] [-p PROJECT]
set -eu

SOURCE_DIR="./wireguard/config"
COMPOSE_FILE="docker-compose.yml"
PROJECT_NAME="${COMPOSE_PROJECT_NAME:-}"
EXPLICIT_VOLUME=""
FORCE=0
VERIFY_ONLY=0
ROLLBACK=0
TARGET_DIR="./wireguard/config"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --source-dir)
      SOURCE_DIR="$2"; shift 2 ;;
    --compose-file|-f)
      COMPOSE_FILE="$2"; shift 2 ;;
    --project-name|-p)
      PROJECT_NAME="$2"; shift 2 ;;
    --volume)
      EXPLICIT_VOLUME="$2"; shift 2 ;;
    --target-dir)
      TARGET_DIR="$2"; shift 2 ;;
    --force)
      FORCE=1; shift 1 ;;
    --verify-only)
      VERIFY_ONLY=1; shift 1 ;;
    --rollback)
      ROLLBACK=1; shift 1 ;;
    *)
      echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

resolve_compose_volume() {
  if [ -n "${EXPLICIT_VOLUME}" ]; then
    printf '%s' "${EXPLICIT_VOLUME}"
    return 0
  fi
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    if [ -n "${PROJECT_NAME}" ]; then
      CFG_JSON="$(docker compose -f "${COMPOSE_FILE}" -p "${PROJECT_NAME}" config --format json 2>/dev/null || true)"
    else
      CFG_JSON="$(docker compose -f "${COMPOSE_FILE}" config --format json 2>/dev/null || true)"
    fi
    if [ -n "${CFG_JSON}" ]; then
      RESOLVED="$(printf '%s' "${CFG_JSON}" | python3 -c "
import json, sys
cfg = json.load(sys.stdin)
proj = str(cfg.get('name') or '').strip()
vols = cfg.get('volumes') or {}
wg = (cfg.get('services') or {}).get('wg') or {}
src_key = None
for m in (wg.get('volumes') or []):
    if isinstance(m, dict) and m.get('target') == '/etc/wireguard':
        src_key = m.get('source')
        break
    elif isinstance(m, str) and ':/etc/wireguard' in m:
        src_key = m.split(':', 1)[0]
        break
if not src_key:
    sys.exit(2)
vdef = vols.get(src_key) or {}
phys = vdef.get('name') or (f'{proj}_{src_key}' if proj else src_key)
print(phys)
" 2>/dev/null || true)"
      if [ -n "${RESOLVED}" ]; then
        printf '%s' "${RESOLVED}"
        return 0
      fi
    fi
  fi
  # 无 Docker CLI 离线回退：按 Compose 规则生成 <project>_wireguard_config
  BASE_PROJ="${PROJECT_NAME:-$(basename "$(cd "$(dirname "${COMPOSE_FILE}")" && pwd)")}"
  NORM_PROJ="$(printf '%s' "${BASE_PROJ}" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9_-]//g')"
  printf '%s_wireguard_config' "${NORM_PROJ}"
}

pick_helper_image() {
  if [ -n "${MIGRATE_IMAGE:-}" ]; then
    printf '%s' "${MIGRATE_IMAGE}"
    return 0
  fi
  for img in alpine:3.20 alpine:latest nginx:alpine hospital-agent-node:24-alpine python:3.11-slim; do
    if docker image inspect "${img}" >/dev/null 2>&1; then
      printf '%s' "${img}"
      return 0
    fi
  done
  printf '%s' "nginx:alpine"
}

VOLUME_NAME="$(resolve_compose_volume)"
echo "Resolved effective Compose target physical volume: ${VOLUME_NAME}"

# 若同时显式指定了预期挂载来源，拦截不一致，避免向错误物理卷写入后触发服务重建重新生成密钥
if [ -n "${EXPLICIT_VOLUME}" ]; then
  ACTUAL_COMP_VOL="$(EXPLICIT_VOLUME='' resolve_compose_volume)"
  if [ "${EXPLICIT_VOLUME}" != "${ACTUAL_COMP_VOL}" ]; then
    echo "ERROR: volume mismatch before recreate: --volume=${EXPLICIT_VOLUME} != Compose target=${ACTUAL_COMP_VOL}. Aborting." >&2
    exit 1
  fi
fi

HELPER_IMAGE="$(pick_helper_image)"

if [ "${ROLLBACK}" = "1" ]; then
  mkdir -p "${TARGET_DIR}/clients"
  ABS_DST="$(cd "${TARGET_DIR}" && pwd)"
  docker run --rm --entrypoint sh -v "${VOLUME_NAME}:/src:ro" -v "${ABS_DST}:/dst" "${HELPER_IMAGE}" -eu -c '
    [ -f /src/wg0.conf ] || { echo "ERROR: /src/wg0.conf missing in volume" >&2; exit 1; }
    mkdir -p /dst/clients && chmod 700 /dst /dst/clients
    cp -p /src/wg0.conf /dst/wg0.conf
    [ -f /src/server.key ] && cp -p /src/server.key /dst/server.key
    [ -f /src/server.pub ] && cp -p /src/server.pub /dst/server.pub
    cp -p /src/clients/*.conf /dst/clients/ 2>/dev/null || true
    chmod 600 /dst/wg0.conf /dst/server.key /dst/clients/*.conf 2>/dev/null || true
    echo "OK: rolled back WireGuard keys/configs to host dir."
  '
  exit 0
fi

if [ ! -f "${SOURCE_DIR}/wg0.conf" ]; then
  echo "ERROR: ${SOURCE_DIR}/wg0.conf not found; nothing to migrate." >&2
  exit 1
fi
ABS_SRC="$(cd "${SOURCE_DIR}" && pwd)"

if [ "${VERIFY_ONLY}" != "1" ]; then
  docker volume create "${VOLUME_NAME}" >/dev/null
  docker run --rm --entrypoint sh \
    -v "${ABS_SRC}:/src:ro" \
    -v "${VOLUME_NAME}:/dst" \
    -e FORCE="${FORCE}" \
    "${HELPER_IMAGE}" -eu -c '
      if [ -f /dst/wg0.conf ] && [ "${FORCE}" != "1" ]; then
        echo "ERROR: destination volume already contains wg0.conf (use --force to overwrite)." >&2
        exit 1
      fi
      mkdir -p /dst/clients
      chmod 700 /dst /dst/clients
      cp -p /src/wg0.conf /dst/wg0.conf
      [ -f /src/server.key ] && cp -p /src/server.key /dst/server.key
      [ -f /src/server.pub ] && cp -p /src/server.pub /dst/server.pub
      if [ -d /src/clients ]; then
        cp -p /src/clients/*.conf /dst/clients/ 2>/dev/null || true
      fi
      chmod 600 /dst/wg0.conf /dst/server.key /dst/clients/*.conf 2>/dev/null || true
      echo "OK: migrated WireGuard keys and peer configs into Docker volume."
    '
fi

# 强制重建前一致性校验：确保实际 Compose 挂载物理卷内 wg0.conf 与公钥、对端清单与源目录完全一致
docker run --rm --entrypoint sh -v "${ABS_SRC}:/src:ro" -v "${VOLUME_NAME}:/dst:ro" "${HELPER_IMAGE}" -eu -c '
  [ -f /dst/wg0.conf ] || { echo "ERROR: target volume missing wg0.conf before recreate" >&2; exit 1; }
  cmp -s /src/wg0.conf /dst/wg0.conf || { echo "ERROR: wg0.conf mismatch between source and volume" >&2; exit 1; }
  if [ -f /src/server.pub ]; then
    cmp -s /src/server.pub /dst/server.pub || { echo "ERROR: server.pub mismatch" >&2; exit 1; }
  fi
  echo "VERIFIED: volume wg0.conf & server.pub match source exactly."
'

echo "验证通过！目标 Compose 挂载卷 ${VOLUME_NAME} 已就绪，可执行："
echo "  docker compose -f ${COMPOSE_FILE}${PROJECT_NAME:+ -p ${PROJECT_NAME}} up -d --force-recreate wg"
