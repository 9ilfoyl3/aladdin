#!/usr/bin/env bash
# ============================================================
# Artoo 服务器端部署（在内网服务器上、dist/ 目录内执行）
#
# 自动完成：加载镜像 -> 引导 .env -> 起中间件 -> 等就绪 -> 起应用。
#
#   ./install.sh
# ============================================================
set -euo pipefail

cd "$(dirname "$0")"

# 自动授权：Windows 打包会丢失 Linux 执行权限，这里统一补回
chmod -R 755 . 2>/dev/null || true

echo "=== Artoo 部署 ==="

echo "[1/4] 加载 Docker 镜像..."
for f in *.tar; do
  [[ -f "$f" ]] || continue
  echo "  load $f"
  docker load -i "$f"
done

echo "[2/4] 检查配置..."
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "  已从 .env.example 生成 .env（含示例默认值，可直接启动）。"
  echo "  ⚠️ 生产环境建议修改 JWT_SECRET 与 SUPER_ADMIN_PASSWORD 后重新部署。"
fi

echo "[3/4] 启动中间件，等待就绪..."
docker compose -f docker-compose.yml --profile infra up -d
echo "  等待中间件 healthy..."
for i in $(seq 1 30); do
  if ! docker compose -f docker-compose.yml --profile infra ps --format '{{.Health}}' | grep -qiE 'starting|unhealthy'; then
    break
  fi
  sleep 5
done
docker compose -f docker-compose.yml --profile infra ps

echo "[4/4] 启动应用..."
docker compose -f docker-compose.yml --profile app up -d

# 读取端口用于提示（缺省与 .env.example 对齐）
FRONTEND_PORT=$(grep -E '^FRONTEND_PORT=' .env | cut -d= -f2 || true)
BACKEND_PORT=$(grep -E '^BACKEND_PORT=' .env | cut -d= -f2 || true)
IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "服务器IP")

echo ""
echo "=== 部署完成 ==="
echo "前端: http://${IP}:${FRONTEND_PORT:-8888}"
echo "后端: http://${IP}:${BACKEND_PORT:-8000}"
echo ""
echo "查看日志: docker compose -f docker-compose.yml --profile app logs -f"
echo "停止应用: docker compose -f docker-compose.yml --profile app down"
echo "停止中间件: docker compose -f docker-compose.yml --profile infra down  # 数据卷保留"
