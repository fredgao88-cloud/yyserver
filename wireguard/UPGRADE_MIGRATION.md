# WireGuard 密钥命名卷升级迁移与回退指南 (DEPLOY-KEY-001)

为防止 `./wireguard/config` 内的服务端私钥与客户端配置文件被 `Dockerfile` 构建上下文或 `api` 容器挂载访问，系统已将 WireGuard 配置存储由项目内目录迁移至独立 Docker 命名卷 `wireguard_config`（注意：Docker Compose 会按 `<project_name>_wireguard_config` 命名实际物理卷）。

## 1. 已有部署升级步骤（自动解析项目物理卷并校验完整性）

1. **先备份旧目录至仓库外部**（勿留在项目目录内）：
   ```bash
   sudo mkdir -p /root/backups/wireguard-config
   sudo cp -a ./wireguard/config/. /root/backups/wireguard-config/
   ```
2. **使用迁移脚本解析当前 Compose 项目的实际物理卷并迁移/核验**：
   - 脚本会强制调用 `docker compose -f <compose> [-p <项目名>] config --format json` 核验 `wg` 服务 `/etc/wireguard` 挂载类型为命名卷（named volume）并提取真实物理卷名；若配置读取或解析失败、挂载非命名卷、文件缺失或权限（目录 `700`、私密文件 `600`）不符，脚本立即中止退出，绝不猜测卷名。
   - **默认项目名示例**：
   ```bash
   sudo sh wireguard/migrate_to_volume.sh --source-dir ./wireguard/config --compose-file docker-compose.yml
   sudo sh wireguard/migrate_to_volume.sh --source-dir ./wireguard/config --compose-file docker-compose.yml --verify-only
   ```
   - **自定义项目名（`-p my_proj`）示例**（迁移、校验、重建与回退均须使用同一 `-p my_proj`）：
   ```bash
   sudo sh wireguard/migrate_to_volume.sh --source-dir ./wireguard/config --compose-file docker-compose.yml -p my_proj
   sudo sh wireguard/migrate_to_volume.sh --source-dir ./wireguard/config --compose-file docker-compose.yml -p my_proj --verify-only
   ```
3. **使用与上一步完全一致的 `-f` 与 `-p` 参数重建并启动服务**：
   ```bash
   # 默认项目：
   docker compose -f docker-compose.yml build api
   docker compose -f docker-compose.yml up -d --force-recreate api wg nginx

   # 若前一步使用了自定义项目名 -p my_proj：
   docker compose -f docker-compose.yml -p my_proj build api
   docker compose -f docker-compose.yml -p my_proj up -d --force-recreate api wg nginx
   ```
4. **验证隔离与连通性**：
   - 验证 `wg` 容器公钥及对端：`docker exec hospital_agent_wg wg show wg0`（服务端公钥与升级前一致，原客户端可直接重连）
   - 验证 `api` 镜像与运行时容器无密钥挂载：`docker exec hospital_agent_api ls /opt/hospital-agent/wireguard` 应提示不存在。
5. **确认无误后清理项目目录内旧密钥文件**：
   ```bash
   sudo rm -rf ./wireguard/config
   ```

## 2. 回退步骤

如升级后需紧急回退至旧版宿主目录（同样保持 `-f` 与 `-p` 一致）：
```bash
sudo sh wireguard/migrate_to_volume.sh --rollback --target-dir ./wireguard/config --compose-file docker-compose.yml
# 若使用了自定义项目名：
# sudo sh wireguard/migrate_to_volume.sh --rollback --target-dir ./wireguard/config --compose-file docker-compose.yml -p my_proj
# 将 docker-compose.yml 中 wg.volumes 改回 ./wireguard/config:/etc/wireguard 后按原 -p 参数重建：
docker compose -f docker-compose.yml up -d --force-recreate wg
```
