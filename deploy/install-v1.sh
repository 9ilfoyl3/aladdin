#!/usr/bin/env bash
# ============================================================
# Artoo 服务管理脚本（docker-compose V1 兼容版）
#
# 用法：
#   bash install-v1.sh              # 首次部署（加载镜像 + 启动全部）
#   bash install-v1.sh start        # 启动全部服务
#   bash install-v1.sh stop         # 停止全部服务（保留数据）
#   bash install-v1.sh restart      # 重启应用（加载新镜像 + 重建应用容器）
#   bash install-v1.sh down         # 停止并删除容器（数据卷保留）
#   bash install-v1.sh down-all     # 停止并删除容器 + 数据卷（⚠️ 清除所有数据）
#   bash install-v1.sh logs [服务名] [条数]  # 查看日志（默认全部应用，100条）
#   bash install-v1.sh status       # 查看服务状态
# ============================================================
set -euo pipefail

cd "$(dirname "$0")"

# 自动授权：Windows 打包会丢失 Linux 执行权限，这里统一补回（含本脚本所在目录）
chmod -R 755 . 2>/dev/null || true

COMPOSE_CMD="docker-compose"
COMPOSE_FILE="docker-compose.yml"
ACTION="${1:-install}"

# ============================================================
# 公共函数
# ============================================================

ensure_compose_compat() {
  # 去掉 profiles 行（V1 不支持）
  if grep -q "profiles:" "$COMPOSE_FILE" 2>/dev/null; then
    sed -i '/profiles:/d' "$COMPOSE_FILE"
  fi

  # 处理网络
  local NETWORK_NAME="arag-network"
  if docker network inspect "$NETWORK_NAME" >/dev/null 2>&1; then
    if ! grep -q "external: true" "$COMPOSE_FILE"; then
      sed -i "s/name: ${NETWORK_NAME}/name: ${NETWORK_NAME}\n    external: true/" "$COMPOSE_FILE"
    fi
  else
    sed -i '/external: true/d' "$COMPOSE_FILE"
  fi
}

check_env() {
  if [[ ! -f .env ]]; then
    cp .env.example .env
    echo "  已从 .env.example 生成 .env（含示例默认值，可直接启动）。"
    echo "  ⚠️ 生产环境建议修改 JWT_SECRET 与 SUPER_ADMIN_PASSWORD 后重新部署。"
  fi

  # 用 grep 直接从文件提取值（避免 source 解析注释或特殊字符的问题）
  _get_env_val() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' ; }

  local MISSING=()

  if [[ -z "$(_get_env_val JWT_SECRET)" ]]; then
    MISSING+=("JWT_SECRET（JWT 签名密钥，生成：python3 -c \"import secrets; print(secrets.token_urlsafe(48))\"）")
  fi
  if [[ -z "$(_get_env_val SUPER_ADMIN_USERNAME)" ]]; then
    MISSING+=("SUPER_ADMIN_USERNAME（初始超管用户名，如 admin）")
  fi
  if [[ -z "$(_get_env_val SUPER_ADMIN_PASSWORD)" ]]; then
    MISSING+=("SUPER_ADMIN_PASSWORD（初始超管密码，如 Admin@123456）")
  fi

  if [[ ${#MISSING[@]} -gt 0 ]]; then
    echo ""
    echo "  ❌ 以下必填配置项为空，服务无法启动："
    for item in "${MISSING[@]}"; do
      echo "    - $item"
    done
    echo ""
    echo "  请编辑 .env 填写后重新执行："
    echo "    vi .env"
    echo ""
    echo "  选填项（不填也能启动，后续可在前端配置）："
    echo "    - LLM_BASE_URL / LLM_MODEL（大模型服务地址）"
    echo "    - EMBED_BASE_URL（Embedding 服务地址）"
    echo "    - RERANK_BASE_URL（Rerank 服务地址）"
    exit 1
  fi
  echo "  配置校验通过 ✓"
}

wait_infra_healthy() {
  echo "  等待中间件 healthy（最多 150 秒）..."
  for i in $(seq 1 30); do
    # 检查是否所有中间件容器都在运行
    NOT_RUNNING=$(docker ps --filter "name=arag-" --format '{{.Names}} {{.Status}}' | grep -E 'etcd|minio|milvus|postgres|redis' | grep -cvE 'Up' || true)
    if [[ "$NOT_RUNNING" -eq 0 ]]; then
      # 再检查有没有 health: starting 的
      STARTING=$(docker ps --filter "name=arag-" --format '{{.Status}}' | grep -ci 'health: starting' || true)
      if [[ "$STARTING" -eq 0 ]]; then
        echo "  中间件全部就绪 ✓"
        return 0
      fi
    fi
    echo "  等待中... ($i/30)"
    sleep 5
  done
  echo "  ⚠️ 部分中间件可能未就绪，请检查: docker ps --filter name=arag-"
}

show_info() {
  local FRONTEND_PORT BACKEND_PORT IP
  FRONTEND_PORT=$(grep -E '^FRONTEND_PORT=' .env | cut -d= -f2 || true)
  BACKEND_PORT=$(grep -E '^BACKEND_PORT=' .env | cut -d= -f2 || true)
  IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "服务器IP")
  echo ""
  echo "  前端: http://${IP}:${FRONTEND_PORT:-8888}"
  echo "  后端: http://${IP}:${BACKEND_PORT:-8000}"
}

# ============================================================
# 子命令
# ============================================================

do_install() {
  echo "=== Artoo 首次部署 ==="

  echo "[1/5] 加载 Docker 镜像..."
  for f in *.tar; do
    [[ -f "$f" ]] || continue
    echo "  load $f"
    docker load -i "$f"
  done

  echo "[2/5] 检查配置..."
  check_env

  echo "[3/5] 处理 compose 文件兼容性..."
  ensure_compose_compat

  echo "[4/5] 启动中间件..."
  $COMPOSE_CMD -f "$COMPOSE_FILE" up -d etcd minio milvus postgres redis
  wait_infra_healthy

  echo "[5/5] 启动应用..."
  $COMPOSE_CMD -f "$COMPOSE_FILE" up -d backend worker frontend

  echo ""
  echo "=== 部署完成 ==="
  show_info
}

do_start() {
  echo "=== 启动全部服务 ==="
  ensure_compose_compat
  $COMPOSE_CMD -f "$COMPOSE_FILE" up -d
  wait_infra_healthy
  echo "=== 启动完成 ==="
  show_info
}

do_stop() {
  echo "=== 停止全部服务（数据保留）==="
  $COMPOSE_CMD -f "$COMPOSE_FILE" stop
  echo "=== 已停止 ==="
}

do_restart() {
  echo "=== 重启应用（加载新镜像 + 重建容器）==="

  echo "[1/3] 加载镜像..."
  for f in *.tar; do
    [[ -f "$f" ]] || continue
    echo "  load $f"
    docker load -i "$f"
  done

  echo "[2/3] 处理 compose 文件兼容性..."
  ensure_compose_compat

  echo "[3/3] 重建应用容器..."
  $COMPOSE_CMD -f "$COMPOSE_FILE" up -d --force-recreate backend worker frontend

  echo ""
  echo "=== 重启完成 ==="
  show_info
}

do_down() {
  echo "=== 停止并删除容器（数据卷保留）==="
  $COMPOSE_CMD -f "$COMPOSE_FILE" down
  echo "=== 已清理 ==="
}

do_down_all() {
  echo "⚠️  即将删除所有容器和数据卷（数据库、向量库、上传文件全部清除）"
  read -p "确认？(y/N): " confirm
  if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
    echo "已取消"
    exit 0
  fi
  $COMPOSE_CMD -f "$COMPOSE_FILE" down -v
  echo "=== 已清除所有数据 ==="
}

do_logs() {
  # 参数：$1=服务名（可选），$2=条数（可选，默认100）
  local SERVICE="${2:-}"
  local LINES="${3:-100}"

  if [[ -n "$SERVICE" ]]; then
    echo "=== 查看 $SERVICE 日志（最近 $LINES 条，实时跟踪）==="
    $COMPOSE_CMD -f "$COMPOSE_FILE" logs -f --tail="$LINES" "$SERVICE"
  else
    echo "=== 查看应用日志（最近 $LINES 条，实时跟踪）==="
    $COMPOSE_CMD -f "$COMPOSE_FILE" logs -f --tail="$LINES" backend worker frontend
  fi
}

do_status() {
  $COMPOSE_CMD -f "$COMPOSE_FILE" ps
}

# ============================================================
# 路由
# ============================================================

case "$ACTION" in
  install)   do_install ;;
  start)     do_start ;;
  stop)      do_stop ;;
  restart)   do_restart ;;
  down)      do_down ;;
  down-all)  do_down_all ;;
  logs)      do_logs "$@" ;;
  status)    do_status ;;
  *)
    echo "未知命令: $ACTION"
    echo "可用命令: start | stop | restart | down | down-all | logs [服务] [条数] | status"
    exit 1
    ;;
esac
