#!/usr/bin/env bash
# ============================================================
# Artoo 服务管理脚本（docker-compose V1 兼容版）
#
# 用法：
#   bash install-v1.sh              # 首次部署（加载镜像 + 启动全部）
#   bash install-v1.sh start [服务] # 启动全部 / 指定服务
#   bash install-v1.sh stop [服务]  # 停止全部 / 指定服务（保留数据）
#   bash install-v1.sh restart [服务] # 重启应用 / 指定服务（不重建、不加载镜像）
#   bash install-v1.sh update [服务]  # 更新应用 / 指定服务（加载新镜像 + 重建容器）
#   bash install-v1.sh down         # 停止并删除容器（数据卷保留）
#   bash install-v1.sh down-all     # 停止并删除容器 + 数据卷（⚠️ 清除所有数据）
#   bash install-v1.sh logs [服务名] [条数]  # 查看日志（默认全部应用，100条）
#   bash install-v1.sh status       # 查看服务状态
#
# 服务名: backend / worker / frontend / postgres / milvus / redis / etcd / minio
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
  echo ""
  echo "  常用命令（在本目录执行，末尾可加服务名指定单个服务）："
  echo "    查看状态: bash install-v1.sh status"
  echo "    查看日志: bash install-v1.sh logs [服务名] [条数]"
  echo "    重启应用: bash install-v1.sh restart [服务名]   # 仅重启，不重建"
  echo "    更新应用: bash install-v1.sh update [服务名]    # 替换 app-images.tar 后执行"
  echo "    停止服务: bash install-v1.sh stop [服务名]"
  echo "    清理容器: bash install-v1.sh down               # 数据卷保留"
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
  local SERVICE="${2:-}"
  ensure_compose_compat
  if [[ -n "$SERVICE" ]]; then
    echo "=== 启动服务: $SERVICE ==="
    $COMPOSE_CMD -f "$COMPOSE_FILE" up -d "$SERVICE"
  else
    echo "=== 启动全部服务 ==="
    $COMPOSE_CMD -f "$COMPOSE_FILE" up -d
    wait_infra_healthy
  fi
  echo "=== 启动完成 ==="
  show_info
}

do_stop() {
  local SERVICE="${2:-}"
  if [[ -n "$SERVICE" ]]; then
    echo "=== 停止服务: $SERVICE（数据保留）==="
    $COMPOSE_CMD -f "$COMPOSE_FILE" stop "$SERVICE"
  else
    echo "=== 停止全部服务（数据保留）==="
    $COMPOSE_CMD -f "$COMPOSE_FILE" stop
  fi
  echo "=== 已停止 ==="
}

do_restart() {
  local SERVICE="${2:-}"
  if [[ -n "$SERVICE" ]]; then
    echo "=== 重启服务: $SERVICE（不重建容器、不加载镜像）==="
    $COMPOSE_CMD -f "$COMPOSE_FILE" restart "$SERVICE"
  else
    echo "=== 重启应用（不重建容器、不加载镜像）==="
    $COMPOSE_CMD -f "$COMPOSE_FILE" restart backend worker frontend
  fi
  echo ""
  echo "=== 重启完成 ==="
  show_info
}

do_update() {
  local SERVICE="${2:-}"
  echo "=== 更新应用（加载新镜像 + 重建容器）==="

  # 记录加载前的应用镜像 ID（仅用于打印变化提示，不作为终止依据）
  local before_backend before_frontend
  before_backend=$(docker images -q artoo-backend:latest 2>/dev/null)
  before_frontend=$(docker images -q artoo-frontend:latest 2>/dev/null)

  echo "[1/3] 加载镜像..."
  local found_tar=0
  for f in *.tar; do
    [[ -f "$f" ]] || continue
    found_tar=1
    local mtime_h
    mtime_h=$(date -d "@$(stat -c %Y "$f" 2>/dev/null || echo 0)" "+%Y-%m-%d %H:%M:%S" 2>/dev/null || echo "未知")
    echo "  load $f（修改于 $mtime_h）"
    docker load -i "$f"
  done
  if [[ "$found_tar" -eq 0 ]]; then
    echo "  ❌ 当前目录没有 .tar 镜像包，无法更新"
    exit 1
  fi

  # 镜像 ID 变化仅作信息提示：手动 docker load 过镜像等场景下 ID 可能不变，
  # 但 update 仍应照常重建容器，不再因“镜像未变化”终止。
  local after_backend after_frontend
  after_backend=$(docker images -q artoo-backend:latest 2>/dev/null)
  after_frontend=$(docker images -q artoo-frontend:latest 2>/dev/null)
  if [[ "$before_backend" == "$after_backend" && "$before_frontend" == "$after_frontend" ]]; then
    echo "  ℹ️ 镜像 ID 未变化（可能已手动 load 过），仍将继续重建容器。"
  else
    echo "  ✓ 镜像已更新："
    [[ "$before_backend" != "$after_backend" ]] && echo "    backend : ${before_backend:-无} → $after_backend"
    [[ "$before_frontend" != "$after_frontend" ]] && echo "    frontend: ${before_frontend:-无} → $after_frontend"
  fi

  echo "[2/3] 处理 compose 文件兼容性..."
  ensure_compose_compat

  if [[ -n "$SERVICE" ]]; then
    echo "[3/3] 重建容器: $SERVICE..."
    $COMPOSE_CMD -f "$COMPOSE_FILE" up -d --force-recreate "$SERVICE"
  else
    echo "[3/3] 重建应用容器..."
    $COMPOSE_CMD -f "$COMPOSE_FILE" up -d --force-recreate backend worker frontend
  fi

  echo ""
  echo "=== 更新完成 ==="
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
  start)     do_start "$@" ;;
  stop)      do_stop "$@" ;;
  restart)   do_restart "$@" ;;
  update)    do_update "$@" ;;
  down)      do_down ;;
  down-all)  do_down_all ;;
  logs)      do_logs "$@" ;;
  status)    do_status ;;
  *)
    echo "未知命令: $ACTION"
    echo "可用命令: start | stop | restart | update | down | down-all | logs [服务] [条数] | status"
    echo "提示: start/stop/restart/update 可在末尾加服务名，如: bash install-v1.sh restart backend"
    exit 1
    ;;
esac
