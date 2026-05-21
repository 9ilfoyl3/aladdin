#!/bin/bash
# Aladdin AMD64 部署打包
# 用法:
#   ./scripts/package-amd64.sh              # 远程模式，首次
#   ./scripts/package-amd64.sh --gpu        # GPU 模式（CUDA + FlagEmbedding），首次
#   ./scripts/package-amd64.sh --skip-infra # 远程模式，更新
#   ./scripts/package-amd64.sh --gpu --skip-infra  # GPU 模式，更新

set -e

GPU=false
SKIP_INFRA=false
OUT="deploy-amd64"

for arg in "$@"; do
    case $arg in
        --gpu) GPU=true ;;
        --skip-infra) SKIP_INFRA=true ;;
    esac
done

echo "=== Aladdin AMD64 打包 ==="
mkdir -p "$OUT"

echo ""
echo "[1] 构建后端镜像..."
if [ "$GPU" = true ]; then
    echo "  模式: GPU（CUDA + FlagEmbedding）"
    docker build -t aladdin-backend:latest -f backend/Dockerfile.production backend/
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

if [ "$SKIP_INFRA" = false ]; then
    echo ""
    echo "[4] 拉取并导出中间件..."
    docker pull postgres:16-alpine
    docker pull milvusdb/milvus:v2.4.6
    docker pull quay.io/coreos/etcd:v3.5.25
    docker pull minio/minio:RELEASE.2024-05-28T17-19-04Z
    docker save postgres:16-alpine milvusdb/milvus:v2.4.6 quay.io/coreos/etcd:v3.5.25 minio/minio:RELEASE.2024-05-28T17-19-04Z -o "$OUT/infra.tar"
fi

echo ""
echo "[5] 复制配置文件..."
cp docker-compose-production.yml "$OUT/docker-compose.yml"
cp backend/.env.example "$OUT/.env.example"

echo ""
echo "=== 完成 ==="
du -sh "$OUT"
