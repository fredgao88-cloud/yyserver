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
  if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    echo "ERROR: docker compose CLI is required to resolve effective WireGuard target volume safely; refusing to guess." >&2
    return 1
  fi
  if [ -n "${PROJECT_NAME}" ]; then
    CFG_JSON="$(docker compose -f "${COMPOSE_FILE}" -p "${PROJECT_NAME}" config --format json 2>/dev/null)" || {
      echo "ERROR: failed to read Compose config (${COMPOSE_FILE}, project=${PROJECT_NAME}); aborting before touching any volume." >&2
      return 1
    }
  else
    CFG_JSON="$(docker compose -f "${COMPOSE_FILE}" config --format json 2>/dev/null)" || {
      echo "ERROR: failed to read Compose config (${COMPOSE_FILE}); aborting before touching any volume." >&2
      return 1
    }
  fi
  if [ -z "${CFG_JSON}" ]; then
    echo "ERROR: empty Compose config JSON for ${COMPOSE_FILE}; aborting." >&2
    return 1
  fi
  RESOLVED="$(printf '%s' "${CFG_JSON}" | python3 -c "
import json, sys
cfg = json.load(sys.stdin)
proj = str(cfg.get('name') or '').strip()
vols = cfg.get('volumes') or {}
wg = (cfg.get('services') or {}).get('wg') or {}
matches = []
for m in (wg.get('volumes') or []):
    if isinstance(m, dict) and m.get('target') == '/etc/wireguard':
        matches.append(m)
if len(matches) != 1:
    sys.stderr.write('ERROR: wg service must have exactly one /etc/wireguard mount.\n')
    sys.exit(2)
m = matches[0]
if m.get('type') != 'volume':
    sys.stderr.write(f\"ERROR: wg /etc/wireguard mount must be named volume, got type={m.get('type')!r}.\n\")
    sys.exit(3)
src_key = str(m.get('source') or '').strip()
if not src_key or src_key not in vols:
    sys.stderr.write(f'ERROR: named volume definition missing for {src_key!r}.\n')
    sys.exit(4)
vdef = vols.get(src_key) or {}
phys = str(vdef.get('name') or (f'{proj}_{src_key}' if proj else src_key)).strip()
if not phys:
    sys.stderr.write('ERROR: resolved physical volume name is empty.\n')
    sys.exit(5)
print(phys)
")" || {
    echo "ERROR: Compose WireGuard named volume resolution/validation failed; aborting before write." >&2
    return 1
  }
  printf '%s' "${RESOLVED}"
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

# 若同时显式指定了预期卷名，拦截任何不一致，避免向错误物理卷写入后触发服务重建重新生成密钥
if [ -n "${EXPLICIT_VOLUME}" ] && [ "${EXPLICIT_VOLUME}" != "${VOLUME_NAME}" ]; then
  echo "ERROR: volume mismatch before recreate: --volume=${EXPLICIT_VOLUME} != Compose target=${VOLUME_NAME}. Aborting." >&2
  exit 1
fi

VERIFY_SNIPPET='
  [ "$(stat -c "%a" /dst)" = "700" ] || { echo "ERROR: /dst mode is not 700" >&2; exit 1; }
  [ -f /dst/wg0.conf ] || { echo "ERROR: target missing wg0.conf" >&2; exit 1; }
  [ "$(stat -c "%a" /dst/wg0.conf)" = "600" ] || { echo "ERROR: wg0.conf mode is not 600" >&2; exit 1; }
  cmp -s /src/wg0.conf /dst/wg0.conf || { echo "ERROR: wg0.conf content mismatch" >&2; exit 1; }
  if [ -f /src/server.key ]; then
    [ -f /dst/server.key ] || { echo "ERROR: target missing server.key" >&2; exit 1; }
    [ "$(stat -c "%a" /dst/server.key)" = "600" ] || { echo "ERROR: server.key mode is not 600" >&2; exit 1; }
    cmp -s /src/server.key /dst/server.key || { echo "ERROR: server.key content mismatch" >&2; exit 1; }
  fi
  if [ -f /src/server.pub ]; then
    [ -f /dst/server.pub ] || { echo "ERROR: target missing server.pub" >&2; exit 1; }
    cmp -s /src/server.pub /dst/server.pub || { echo "ERROR: server.pub content mismatch" >&2; exit 1; }
  fi
  SRC_LIST=""
  DST_LIST=""
  if [ -d /src/clients ]; then
    SRC_LIST="$(for p in /src/clients/*.conf; do [ -f "$p" ] && basename "$p"; done | sort)"
  fi
  if [ -d /dst/clients ]; then
    DST_LIST="$(for p in /dst/clients/*.conf; do [ -f "$p" ] && basename "$p"; done | sort)"
  fi
  if [ "${SRC_LIST}" != "${DST_LIST}" ]; then
    echo "ERROR: clients/*.conf file list mismatch between source and target." >&2
    exit 1
  fi
  if [ -n "${SRC_LIST}" ]; then
    [ -d /dst/clients ] && [ "$(stat -c "%a" /dst/clients)" = "700" ] || { echo "ERROR: /dst/clients mode is not 700" >&2; exit 1; }
    for p in /src/clients/*.conf; do
      [ -f "$p" ] || continue
      base="$(basename "$p")"
      [ "$(stat -c "%a" "/dst/clients/${base}")" = "600" ] || { echo "ERROR: clients/${base} mode is not 600" >&2; exit 1; }
      cmp -s "$p" "/dst/clients/${base}" || { echo "ERROR: clients/${base} content mismatch" >&2; exit 1; }
    done
  fi
  echo "VERIFIED: wg0.conf, keys, clients/*.conf, and permissions (700/600) match source exactly."
'

HELPER_IMAGE="$(pick_helper_image)"

if [ "${ROLLBACK}" = "1" ]; then
  mkdir -p "${TARGET_DIR}/clients"
  ABS_DST="$(cd "${TARGET_DIR}" && pwd)"
  docker run --rm --entrypoint sh -v "${VOLUME_NAME}:/src:ro" -v "${ABS_DST}:/dst" "${HELPER_IMAGE}" -eu -c "
    [ -f /src/wg0.conf ] || { echo 'ERROR: /src/wg0.conf missing in volume' >&2; exit 1; }
    mkdir -p /dst/clients && chmod 700 /dst /dst/clients
    rm -f /dst/clients/*.conf
    cp -p /src/wg0.conf /dst/wg0.conf
    [ -f /src/server.key ] && cp -p /src/server.key /dst/server.key
    [ -f /src/server.pub ] && cp -p /src/server.pub /dst/server.pub
    if [ -d /src/clients ]; then
      for p in /src/clients/*.conf; do [ -f \"\$p\" ] && cp -p \"\$p\" /dst/clients/; done
    fi
    chmod 600 /dst/wg0.conf /dst/server.key /dst/clients/*.conf 2>/dev/null || true
    ${VERIFY_SNIPPET}
    echo 'OK: rolled back WireGuard keys/configs to host dir.'
  "
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
      rm -f /dst/clients/*.conf
      cp -p /src/wg0.conf /dst/wg0.conf
      [ -f /src/server.key ] && cp -p /src/server.key /dst/server.key
      [ -f /src/server.pub ] && cp -p /src/server.pub /dst/server.pub
      if [ -d /src/clients ]; then
        for p in /src/clients/*.conf; do [ -f "$p" ] && cp -p "$p" /dst/clients/; done
      fi
      chmod 600 /dst/wg0.conf /dst/server.key /dst/clients/*.conf 2>/dev/null || true
      echo "OK: migrated WireGuard keys and peer configs into Docker volume."
    '
fi

# 强制重建前一致性校验：确保目标物理卷内 wg0.conf、keys、clients/*.conf 文件集合与内容及权限（700/600）与源目录完全一致
docker run --rm --entrypoint sh -v "${ABS_SRC}:/src:ro" -v "${VOLUME_NAME}:/dst:ro" "${HELPER_IMAGE}" -eu -c "${VERIFY_SNIPPET}"

echo "验证通过！目标 Compose 挂载卷 ${VOLUME_NAME} 已就绪，可执行："
echo "  docker compose -f ${COMPOSE_FILE}${PROJECT_NAME:+ -p ${PROJECT_NAME}} up -d --force-recreate wg"
