# multi-agents

## 后端启动（Windows PowerShell）
```powershell
cd d:\Projects\multi-agents\backend
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
alembic upgrade head
python -m uvicorn app.main:app --reload --port 8000
```

## 前端启动
```powershell
cd d:\Projects\multi-agents\frontend
npm install
npm run dev
```

## 环境变量文件
- `backend/.env.example`：已提交到仓库的模板文件
- `backend/.env`：本地运行配置（不要提交）

创建本地配置：
```powershell
cd d:\Projects\multi-agents\backend
Copy-Item .env.example .env
```

## Relay API 配置（yunwu.ai）
`backend/.env` 推荐配置：
```env
LLM_PROVIDER=openai_compatible
AI_API_KEY=your_token_here
AI_BASE_URL=https://yunwu.ai/v1
AGENT_MODEL=selected_model_name
DEBUG=true
```

说明：
- `AI_BASE_URL` 需要使用带 `/v1` 的地址，以兼容 OpenAI 风格 SDK 调用。
- 如果你误填为 `https://yunwu.ai` 或 `https://yunwu.ai/v1/chat/completions`，
  代码现在会自动规范化为 `https://yunwu.ai/v1`。

## Relay 连通性快速测试
```powershell
cd d:\Projects\multi-agents\backend
$env:RUN_RELAY_API_TEST='1'
python -m pytest tests/integration/test_relay_api_connection.py -q
```

期望结果：
- 出现 `1 passed` 表示 Relay 连通性和流式生成功能都正常。

## 本机 / 局域网快速切换
1. 本机模式

在项目根目录同时启动后端和前端：

```powershell
cd d:\Projects\multi-agents
.\start-dev.ps1
```

访问：

- `http://localhost:5173/`

2. 局域网模式

在项目根目录同时启动后端和前端：

```powershell
cd d:\Projects\multi-agents
.\start-dev.ps1 -Lan
```

访问：

- `http://192.168.3.119:5173`

你也可以分别启动后端和前端：

```powershell
# backend
cd d:\Projects\multi-agents\backend
.\start.ps1
.\start.ps1 -Lan

# frontend
cd d:\Projects\multi-agents\frontend
npm run dev
npm run dev:lan
```

## Encoding Guard

This repo now includes a UTF-8 guard to reduce encoding corruption:

1. `.editorconfig` enforces `charset = utf-8`.
2. `.gitattributes` normalizes text file handling in Git.
3. `scripts/check-utf8.ps1` validates tracked text files are UTF-8.

Run check:

```powershell
cd d:\Projects\multi-agents
.\scripts\check-utf8.ps1
```

Strict mode (also fail on UTF-8 BOM):

```powershell
cd d:\Projects\multi-agents
.\scripts\check-utf8.ps1 -FailOnBom
```

Auto-fix UTF-8 BOM for tracked text files:

```powershell
cd d:\Projects\multi-agents
.\scripts\check-utf8.ps1 -FixBom
```

## 服务器部署脚本（deploy.ps1）

项目根目录提供了 `deploy.ps1`，用于打包代码并上传到服务器后触发远端部署脚本。

脚本位置：

```powershell
d:\Projects\multi-agents\deploy.ps1
```

执行方式：

```powershell
cd d:\Projects\multi-agents
.\deploy.ps1
```

脚本会自动执行以下步骤：

1. 在本地打包项目（排除 `.git`、`backend/.venv`、`backend/.env`、`frontend/node_modules`、`frontend/dist`）。
2. 上传压缩包到服务器：`/opt/multi-agents-src.tar.gz`。
3. 通过 SSH 执行远端部署脚本：`/opt/deploy-multi-agents.sh`。

## 服务器重启手段（运维）

服务器侧提供两种重启脚本，可用于快速恢复服务：

1. 常规一键重启（推荐日常使用）

```bash
bash /opt/restart-multi-agents.sh
```

- 作用：重启后端服务、校验并重载 Nginx、做本地与公网健康检查。

2. 强力重启（Redis 状态异常时使用）

```bash
bash /opt/restart-multi-agents-hard.sh
```

- 作用：先重启 Redis，再重启后端并重载 Nginx，随后做状态与健康检查。
- 注意：会清掉在线状态、房间临时状态和部分缓存；不会删除数据库中的用户、房间、消息等持久数据。

---

# 服务器数据备份、下载、恢复流程

服务器：

```text
root@121.43.104.192
```

当前项目：

```text
/opt/multi-agents
```

备份目录：

```text
/opt/backups/multi-agents
```

数据库：

```text
PostgreSQL: collab_db
Redis: /var/lib/redis/dump.rdb
```

---

# 一、打包当前服务器数据

先登录服务器：

```powershell
ssh root@121.43.104.192
```

在服务器执行：

```bash
cat > /tmp/backup_multi_agents.sh <<'BASH'
#!/usr/bin/env bash

APP_DIR="/opt/multi-agents"
BACKUP_DIR="/opt/backups/multi-agents"
TS=$(date +%Y%m%d-%H%M%S)
STAGE="/tmp/multi-agents-backup-$TS"
FINAL="$BACKUP_DIR/multi-agents-backup-$TS.tar.gz"

mkdir -p "$BACKUP_DIR" "$STAGE"

echo "[1/5] 导出 PostgreSQL 数据库 collab_db..."
cd /tmp
sudo -u postgres pg_dump -Fc collab_db > "$STAGE/postgres-collab_db.dump"
sudo -u postgres pg_dump collab_db > "$STAGE/postgres-collab_db.sql"

echo "[2/5] 保存并复制 Redis dump.rdb..."
redis-cli SAVE
cp /var/lib/redis/dump.rdb "$STAGE/redis-dump.rdb"
redis-cli INFO persistence > "$STAGE/redis-persistence-info.txt"

echo "[3/5] 打包当前项目文件..."
tar --ignore-failed-read -czf "$STAGE/app-files.tar.gz" "$APP_DIR" 2>/dev/null || true

echo "[4/5] 写入备份说明..."
cat > "$STAGE/backup-manifest.txt" <<EOF
backup_time=$TS
active_app_dir=$APP_DIR
postgres_database=collab_db
redis_dump=/var/lib/redis/dump.rdb
backend_env=$APP_DIR/backend/.env
EOF

echo "[5/5] 生成最终备份包和 sha256..."
tar -czf "$FINAL" -C /tmp "multi-agents-backup-$TS"
sha256sum "$FINAL" > "$FINAL.sha256"

echo ""
echo "备份完成："
ls -lh "$FINAL" "$FINAL.sha256"
echo ""
echo "下载文件名："
basename "$FINAL"
BASH

bash /tmp/backup_multi_agents.sh
```

执行完成后会输出类似：

```text
multi-agents-backup-20260510-180454.tar.gz
```

记住这个文件名。

退出服务器：

```bash
exit
```

---

# 二、下载备份到本地

进入本地保存目录，例如：

```powershell
cd "D:\实验数据\2026年5月10日-17点53分"
```

下载备份包：

```powershell
scp root@121.43.104.192:/opt/backups/multi-agents/multi-agents-backup-20260510-180454.tar.gz .
```

下载校验文件：

```powershell
scp root@121.43.104.192:/opt/backups/multi-agents/multi-agents-backup-20260510-180454.tar.gz.sha256 .
```

或者一次性下载两个：

```powershell
scp root@121.43.104.192:/opt/backups/multi-agents/multi-agents-backup-20260510-180454.tar.gz* .
```

校验文件完整性：

```powershell
Get-FileHash .\multi-agents-backup-20260510-180454.tar.gz -Algorithm SHA256
```

查看服务器生成的校验值：

```powershell
Get-Content .\multi-agents-backup-20260510-180454.tar.gz.sha256
```

两个 SHA256 值一致，说明下载完整。

---

# 三、恢复服务器数据

先把本地备份上传回服务器。

在本地执行：

```powershell
cd "D:\实验数据\2026年5月10日-17点53分"
```

```powershell
ssh root@121.43.104.192 "mkdir -p /opt/backups/multi-agents"
```

```powershell
scp .\multi-agents-backup-20260510-180454.tar.gz root@121.43.104.192:/opt/backups/multi-agents/
```

```powershell
scp .\multi-agents-backup-20260510-180454.tar.gz.sha256 root@121.43.104.192:/opt/backups/multi-agents/
```

登录服务器：

```powershell
ssh root@121.43.104.192
```

校验备份：

```bash
cd /opt/backups/multi-agents
sha256sum -c multi-agents-backup-20260510-180454.tar.gz.sha256
```

如果显示：

```text
multi-agents-backup-20260510-180454.tar.gz: OK
```

再执行恢复：

```bash
cat > /tmp/restore_multi_agents.sh <<'BASH'
#!/usr/bin/env bash
set -e

BACKUP="/opt/backups/multi-agents/multi-agents-backup-20260510-180454.tar.gz"
RESTORE_DIR="/tmp/restore-multi-agents-20260510-180454"

echo "[1/8] 解压备份..."
rm -rf "$RESTORE_DIR"
mkdir -p "$RESTORE_DIR"
tar -xzf "$BACKUP" -C "$RESTORE_DIR"

INNER_DIR=$(find "$RESTORE_DIR" -maxdepth 1 -type d -name "multi-agents-backup-*" | head -1)

if [ -z "$INNER_DIR" ]; then
  echo "ERROR: cannot find inner backup directory"
  exit 1
fi

echo "[2/8] 停止后端服务..."
systemctl stop multi-agents-backend || true

echo "[3/8] 备份当前旧目录..."
if [ -d /opt/multi-agents ]; then
  mv /opt/multi-agents "/opt/multi-agents.before-restore-$(date +%Y%m%d-%H%M%S)"
fi

echo "[4/8] 恢复项目文件..."
tar -xzf "$INNER_DIR/app-files.tar.gz" -C /

echo "[5/8] 恢复 PostgreSQL 数据库 collab_db..."
sudo -u postgres dropdb --if-exists collab_db
sudo -u postgres createdb collab_db
sudo -u postgres pg_restore -d collab_db "$INNER_DIR/postgres-collab_db.dump"

echo "[6/8] 恢复 Redis..."
systemctl stop redis-server || true
cp "$INNER_DIR/redis-dump.rdb" /var/lib/redis/dump.rdb
chown redis:redis /var/lib/redis/dump.rdb
chmod 660 /var/lib/redis/dump.rdb
systemctl start redis-server

echo "[7/8] 启动后端和 nginx..."
systemctl start multi-agents-backend
systemctl reload nginx || systemctl restart nginx || true

echo "[8/8] 检查服务状态..."
systemctl status redis-server --no-pager -l | head -20
systemctl status multi-agents-backend --no-pager -l | head -30
systemctl status nginx --no-pager -l | head -20

echo ""
echo "恢复完成。"
BASH

bash /tmp/restore_multi_agents.sh
```

恢复后检查端口：

```bash
ss -lntp | egrep "8001|6379|5432|80|443"
```

恢复后网页测试：

```text
1. 打开平台
2. 进入或创建 single 房间
3. 发送普通消息
4. 发送 @socratic
5. 确认智能体能正常回复
```

---

# 最常用三条命令

## 打包

```bash
bash /tmp/backup_multi_agents.sh
```

## 下载

```powershell
scp root@121.43.104.192:/opt/backups/multi-agents/multi-agents-backup-时间.tar.gz* .
```

## 恢复

```bash
bash /tmp/restore_multi_agents.sh
```
