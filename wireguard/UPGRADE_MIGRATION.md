# WireGuard 密钥命名卷升级迁移与回退指南 (DEPLOY-KEY-001)

为防止 `./wireguard/config` 内的服务端私钥与客户端配置文件被 `Dockerfile` 构建上下文或 `api` 容器挂载访问，系统已将 WireGuard 配置存储由项目内目录迁移至独立 Docker 命名卷 `wireguard_config`（注意：Docker Compose 会按 `<project_name>_wireguard_config` 命名实际物理卷）。

## 1. 已有部署升级步骤（自动解析项目物理卷并校验一致性）

1. **先备份旧目录至仓库外部**（勿留在项目目录内）：
   ```bash
   sudo mkdir -p /root/backups/wireguard-config
   sudo cp -a ./wireguard/config/. /root/backups/wireguard-config/
   ```
2. **使用迁移脚本自动解析当前 Compose 项目的实际物理卷并迁移/核验**：
   - 脚本会通过 `docker compose [-p <项目名>] config --format json` 解析 `wg` 服务挂载 `/etc/wireguard` 的真实目标物理卷（例如 `<project>_wireguard_config`），拷贝全部密钥与 `clients/*.conf`，并在结束前核验卷内 `wg0.conf`、`server.pub` 与源目录完全一致：
   ```bash
   sudo sh wireguard/migrate_to_volume.sh --source-dir ./wireguard/config --compose-file docker-compose.yml
   # 若指定了自定义 Compose 项目名（-p）：
   sudo sh wireguard/migrate_to_volume.sh --source-dir ./wireguard/config --compose-file docker-compose.yml -p my_proj
   ```
   > 仅验证目标物理卷与源目录一致性（不覆盖写入）可执行：`sudo sh wireguard/migrate_to_volume.sh --verify-only ...`。若目标卷不一致，脚本会直接退出阻断重建。
3. **重建并启动服务**：
   ```bash
   docker compose -f docker-compose.yml build api
   docker compose -f docker-compose.yml up -d --force-recreate api wg nginx
   ```
4. **验证隔离与连通性**：
   - 验证 `wg` 容器公钥及对端：`docker exec hospital_agent_wg wg show wg0`（服务端公钥与升级前一致，原客户端可直接重连）
   - 验证 `api` 镜像与运行时容器无密钥挂载：`docker exec hospital_agent_api ls /opt/hospital-agent/wireguard` 应提示不存在。
5. **确认无误后清理项目目录内旧密钥文件**：
   ```bash
   sudo rm -rf ./wireguard/config
   ```

## 2. 回退步骤

如升级后需紧急回退至旧版宿主目录：
```bash
sudo sh wireguard/migrate_to_volume.sh --rollback --target-dir ./wireguard/config --compose-file docker-compose.yml
# 或从备份目录恢复：sudo cp -a /root/backups/wireguard-config/. ./wireguard/config/
# 将 docker-compose.yml 中 wg.volumes 改回 ./wireguard/config:/etc/wireguard 后执行：
docker compose -f docker-compose.yml up -d --force-recreate wg
```
