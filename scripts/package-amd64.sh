#!/bin/bash
# Aladdin AMD64 + GPU 部署打包
# 使用 Dockerfile.production（CUDA PyTorch + FlagEmbedding）
# 用法:
#   ./scripts/package-amd64.sh              # 首次部署
#   ./scripts/package-amd64.sh --skip-infra # 更新应用

set -e

SKIP_INFRA=false
OUT="deploy-amd64"

for arg in "$@"; do
    case $arg in
        --skip-infra) SKIP_INFRA=true ;;
    esac
done

echo "=== Aladdin AMD64 + GPU 打包 ==="
mkdir -p "$OUT"

echo ""
echo "[1] 构建后端镜像（CUDA + FlagEmbedding）..."
docker build -t aladdin-backend:latest -f backend/Dockerfile.production backend/

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
