#!/bin/bash
# Aladdin AMD64 部署打包（macOS/Linux）
# 用法:
#   ./scripts/package-amd64.sh              # 远程模式 + 中间件
#   ./scripts/package-amd64.sh --skip-infra # 只打应用（更新时）
#   ./scripts/package-amd64.sh --with-ml    # 含 ML 依赖

set -e

WITH_ML=false
SKIP_INFRA=false
OUT="deploy-amd64"

for arg in "$@"; do
    case $arg in
        --with-ml) WITH_ML=true ;;
        --skip-infra) SKIP_INFRA=true ;;
    esac
done

echo "=== Aladdin AMD64 打包 ==="
mkdir -p "$OUT"

# 应用镜像
echo ""
echo "[1] 构建后端镜像..."
if [ "$WITH_ML" = true ]; then
    echo "  模式: 含 ML 依赖"
    docker build --build-arg INSTALL_ML=true -t aladdin-backend:latest backend/
else
    echo "  模式: 远程（轻量）"
    docker build -t aladdin-backend:latest backend/
fi

echo ""
echo "[2] 构建前端镜像..."
docker build -t aladdin-frontend:latest frontend/

echo ""
echo "[3] 导出应用镜像..."
docker save aladdin-backend:latest aladdin-frontend:latest -o "$OUT/app.tar"

# 中间件
if [ "$SKIP_INFRA" = false ]; then
    echo ""
    echo "[4] 拉取并导出中间件..."
    docker pull postgres:16-alpine
    docker pull milvusdb/milvus:v2.4.6
    docker pull quay.io/coreos/etcd:v3.5.18
    docker pull minio/minio:RELEASE.2023-03-20T20-16-18Z
    docker save postgres:16-alpine milvusdb/milvus:v2.4.6 quay.io/coreos/etcd:v3.5.18 minio/minio:RELEASE.2023-03-20T20-16-18Z -o "$OUT/infra.tar"
fi

# 配置文件
echo ""
echo "[5] 复制配置文件..."
cp docker-compose-production.yml "$OUT/docker-compose.yml"
cp backend/.env.example "$OUT/.env.example"

echo ""
echo "=== 完成 ==="
du -sh "$OUT"
