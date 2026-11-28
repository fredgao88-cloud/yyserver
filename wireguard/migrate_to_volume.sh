#!/bin/sh
# WireGuard 密钥与配置迁移脚本（从旧宿主机目录迁入 Docker 命名卷 wireguard_config）
# 用法：
#   sh wireguard/migrate_to_volume.sh [--source-dir ./wireguard/config] [--volume wireguard_config] [--force]
# 回退方法：
#   1. 将 docker-compose.yml 中 wg 挂载暂时改回旧目录（或使用备份目录）；
#   2. 执行 docker compose up -d --force-recreate wg 即可恢复原密钥与对端列表。
set -eu

SOURCE_DIR="./wireguard/config"
VOLUME_NAME="wireguard_config"
FORCE=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --source-dir)
      SOURCE_DIR="$2"
      shift 2
      ;;
    --volume)
      VOLUME_NAME="$2"
      shift 2
      ;;
    --force)
      FORCE=1
      shift 1
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
done

if [ ! -f "${SOURCE_DIR}/wg0.conf" ]; then
  echo "ERROR: ${SOURCE_DIR}/wg0.conf not found; nothing to migrate." >&2
  exit 1
fi

ABS_SRC="$(cd "${SOURCE_DIR}" && pwd)"
docker volume create "${VOLUME_NAME}" >/dev/null

docker run --rm \
  -v "${ABS_SRC}:/src:ro" \
  -v "${VOLUME_NAME}:/dst" \
  -e FORCE="${FORCE}" \
  alpine:3.20 sh -eu -c '
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

echo "下一步建议："
echo "  1) docker compose up -d --force-recreate wg"
echo "  2) 核对客户端配置公钥未变更后，将宿主机 ${ABS_SRC} 备份并移出项目目录（如 /root/backups/wireguard-config/），避免密钥残留仓库树内。"
