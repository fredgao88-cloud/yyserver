# WireGuard 密钥命名卷升级迁移与回退指南 (DEPLOY-KEY-001)

为防止 `./wireguard/config` 内的服务端私钥与客户端配置文件被 `Dockerfile` 构建上下文或 `api` 容器挂载访问，系统已将 WireGuard 配置存储由项目内目录迁移至独立 Docker 命名卷 `wireguard_config`（并在 `.dockerignore` 与 `docker-compose.yml` 中彻底隔离 `api` 服务）。

## 1. 已有部署升级步骤（零密钥重置 / 客户端不失联）

1. **先备份旧目录至仓库外部**（勿留在项目目录内）：
   ```bash
   sudo mkdir -p /root/backups/wireguard-config
   sudo cp -a ./wireguard/config/. /root/backups/wireguard-config/
   ```
2. **将旧配置迁入命名卷 `wireguard_config`**：
   ```bash
   sudo sh wireguard/migrate_to_volume.sh --source-dir ./wireguard/config --volume wireguard_config
   ```
   > `wireguard/entrypoint.sh` 检测到卷内已存在 `/etc/wireguard/wg0.conf` 时会直接沿用原私钥、公钥与全部 `[Peer]` 配置，不会重新生成密钥。
3. **重建并启动服务**：
   ```bash
   docker compose -f docker-compose.yml build api
   docker compose -f docker-compose.yml up -d --force-recreate api wg nginx
   ```
4. **验证隔离与连通性**：
   - 验证 `wg` 容器公钥及对端：`docker exec hospital_agent_wg wg show wg0`
   - 验证 `api` 镜像与运行时容器无密钥挂载：`docker exec hospital_agent_api ls /opt/hospital-agent/wireguard` 应提示不存在。
5. **确认无误后清理项目目录内旧密钥文件**：
   ```bash
   sudo rm -rf ./wireguard/config
   ```

## 2. 回退步骤

如升级后需紧急回退至旧版宿主目录：
```bash
sudo mkdir -p ./wireguard/config
sudo cp -a /root/backups/wireguard-config/. ./wireguard/config/
# 将 docker-compose.yml 中 wg.volumes 改回 ./wireguard/config:/etc/wireguard 后执行：
docker compose -f docker-compose.yml up -d --force-recreate wg
```
