#!/usr/bin/env bash
# ============================================================
# Artoo 离线部署包构建（在有网的开发机执行）
#
# 产物 dist/ 目录可整体拷到内网服务器，再执行 install.sh 部署。
#
# 用法：
#   deploy/build.sh                  # 当前架构，含中间件镜像（首次部署）
#   deploy/build.sh --arch arm64     # 指定目标架构（amd64 | arm64）
#   deploy/build.sh --app-only       # 只打应用镜像（迭代更新，不含中间件）
#   deploy/build.sh --with-graph     # 后端镜像内额外安装知识图谱依赖（Neo4j 驱动）
# ============================================================
set -euo pipefail

cd "$(dirname "$0")/.."   # 切到项目根目录

ARCH=""           # 留空 = 跟随本机架构
APP_ONLY=false
WITH_GRAPH=false  # 是否在后端镜像内安装知识图谱依赖（Neo4j 驱动）

while [[ $# -gt 0 ]]; do
  case "$1" in
    --arch) ARCH="$2"; shift 2 ;;
    --app-only) APP_ONLY=true; shift ;;
    --with-graph) WITH_GRAPH=true; shift ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
done

PLATFORM_ARG=""
[[ -n "$ARCH" ]] && PLATFORM_ARG="--platform linux/${ARCH}"

OUT="dist"
mkdir -p "$OUT"

echo "=== Artoo 离线包构建 (${ARCH:-本机架构}) ==="

echo "[1/4] 构建应用镜像..."
GRAPH_BUILD_ARG=""
[[ "$WITH_GRAPH" == true ]] && GRAPH_BUILD_ARG="--build-arg WITH_GRAPH=true" && echo "  （含知识图谱依赖：Neo4j 驱动）"
docker build $PLATFORM_ARG $GRAPH_BUILD_ARG -t artoo-backend:latest backend/
docker build $PLATFORM_ARG -t artoo-frontend:latest frontend/

echo "[2/4] 导出应用镜像..."
docker save artoo-backend:latest artoo-frontend:latest -o "$OUT/app-images.tar"

if [[ "$APP_ONLY" == false ]]; then
  echo "[3/4] 拉取并导出中间件镜像..."
  INFRA_IMAGES=(
    postgres:16-alpine
    redis:7-alpine
    milvusdb/milvus:v2.5.4
    quay.io/coreos/etcd:v3.5.25
    minio/minio:RELEASE.2024-05-28T17-19-04Z
  )
  # 开图谱时额外导出 Neo4j 镜像（与 docker-compose.yml graph profile 一致）。
  [[ "$WITH_GRAPH" == true ]] && INFRA_IMAGES+=(neo4j:5-community)
  for img in "${INFRA_IMAGES[@]}"; do
    docker pull $PLATFORM_ARG "$img"
  done
  docker save "${INFRA_IMAGES[@]}" -o "$OUT/infra-images.tar"
else
  echo "[3/4] 跳过中间件镜像（--app-only）"
fi

echo "[4/4] 复制部署文件..."
cp docker-compose.yml "$OUT/"
cp .env.example "$OUT/"
mkdir -p "$OUT/deploy"
cp deploy/milvus-user.yaml "$OUT/deploy/"
cp deploy/install.sh "$OUT/"
chmod +x "$OUT/install.sh"

echo ""
echo "=== 完成 ==="
du -sh "$OUT"
echo "将 $OUT/ 整体拷到服务器，执行 ./install.sh"
