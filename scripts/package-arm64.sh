#!/bin/bash
# Aladdin ARM64 部署打包
# 用法:
#   ./scripts/package-arm64.sh              # 远程模式，首次
#   ./scripts/package-arm64.sh --gpu        # 本地模型（CPU + FlagEmbedding），首次
#   ./scripts/package-arm64.sh --skip-infra # 远程模式，更新
#   ./scripts/package-arm64.sh --gpu --skip-infra  # 本地模型，更新

set -e

GPU=false
SKIP_INFRA=false
OUT="deploy-arm64"

for arg in "$@"; do
    case $arg in
        --gpu) GPU=true ;;
        --skip-infra) SKIP_INFRA=true ;;
    esac
done

echo "=== Aladdin ARM64 打包 ==="
mkdir -p "$OUT"

echo ""
echo "[1] 构建后端镜像（ARM64）..."
if [ "$GPU" = true ]; then
    echo "  模式: 本地模型（FlagEmbedding + CPU PyTorch）"
    docker build --platform linux/arm64 --build-arg INSTALL_ML=true -t aladdin-backend:latest backend/
else
    echo "  模式: 远程（轻量）"
    docker build --platform linux/arm64 -t aladdin-backend:latest backend/
fi

echo ""
echo "[2] 构建前端镜像（ARM64）..."
docker build --platform linux/arm64 -t aladdin-frontend:latest frontend/

echo ""
echo "[3] 导出应用镜像..."
docker save aladdin-backend:latest aladdin-frontend:latest -o "$OUT/app.tar"

if [ "$SKIP_INFRA" = false ]; then
    echo ""
    echo "[4] 拉取并导出中间件（ARM64）..."
    docker pull --platform linux/arm64 postgres:16-alpine
    docker pull --platform linux/arm64 milvusdb/milvus:v2.4.6
    docker pull --platform linux/arm64 quay.io/coreos/etcd:v3.5.25
    docker pull --platform linux/arm64 minio/minio:RELEASE.2024-05-28T17-19-04Z
    docker pull --platform linux/arm64 redis:7-alpine
    docker save postgres:16-alpine milvusdb/milvus:v2.4.6 quay.io/coreos/etcd:v3.5.25 minio/minio:RELEASE.2024-05-28T17-19-04Z redis:7-alpine -o "$OUT/infra.tar"
fi

echo ""
echo "[5] 复制配置文件..."
cp docker-compose-production.yml "$OUT/docker-compose.yml"
cp backend/.env.example "$OUT/.env.example"

echo ""
echo "=== 完成 ==="
du -sh "$OUT"

