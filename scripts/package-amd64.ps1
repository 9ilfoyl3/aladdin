# Aladdin AMD64 + GPU 部署打包
# 使用 Dockerfile.production（CUDA PyTorch + FlagEmbedding）
param(
    [switch]$SkipInfra     # 加 -SkipInfra 跳过中间件（非首次部署）
)

$ErrorActionPreference = "Stop"
$OUT = "deploy-amd64"

Write-Host "=== Aladdin AMD64 + GPU 打包 ===" -ForegroundColor Green
New-Item -ItemType Directory -Force -Path $OUT | Out-Null

# 后端镜像（CUDA + FlagEmbedding，不含 sentence-transformers）
Write-Host "`n[1] 构建后端镜像（Dockerfile.production: CUDA + FlagEmbedding）..." -ForegroundColor Cyan
docker build -t aladdin-backend:latest -f backend/Dockerfile.production backend/
if ($LASTEXITCODE -ne 0) { Write-Host "后端构建失败！" -ForegroundColor Red; exit 1 }

Write-Host "`n[2] 构建前端镜像..." -ForegroundColor Cyan
docker build -t aladdin-frontend:latest frontend/
if ($LASTEXITCODE -ne 0) { Write-Host "前端构建失败！" -ForegroundColor Red; exit 1 }

Write-Host "`n[3] 导出应用镜像..." -ForegroundColor Cyan
docker save aladdin-backend:latest aladdin-frontend:latest -o "$OUT\app.tar"

# 中间件
if (-not $SkipInfra) {
    Write-Host "`n[4] 拉取并导出中间件..." -ForegroundColor Cyan
    docker pull postgres:16-alpine
    docker pull milvusdb/milvus:v2.4.6
    docker pull quay.io/coreos/etcd:v3.5.25
    docker pull minio/minio:RELEASE.2024-05-28T17-19-04Z
    docker save postgres:16-alpine milvusdb/milvus:v2.4.6 quay.io/coreos/etcd:v3.5.25 minio/minio:RELEASE.2024-05-28T17-19-04Z -o "$OUT\infra.tar"
}

# 配置文件
Write-Host "`n[5] 复制配置文件..." -ForegroundColor Cyan
Copy-Item docker-compose-production.yml "$OUT\docker-compose.yml"
Copy-Item backend\.env.example "$OUT\.env.example"

Write-Host "`n=== 完成 ===" -ForegroundColor Green
$size = [math]::Round((Get-ChildItem -Recurse $OUT | Measure-Object -Property Length -Sum).Sum / 1GB, 2)
Write-Host "输出: $OUT\ ($size GB)"
Write-Host ""
Write-Host "用法:" -ForegroundColor Yellow
Write-Host "  首次部署:  .\scripts\package-amd64.ps1"
Write-Host "  更新应用:  .\scripts\package-amd64.ps1 -SkipInfra"
Write-Host ""
Write-Host "服务器 .env 配置:" -ForegroundColor Yellow
Write-Host "  EMBED_PROVIDER=flag-embedding"
Write-Host "  EMBED_DEVICE=cuda"
Write-Host "  RERANK_PROVIDER=flag-embedding"
Write-Host "  RERANK_DEVICE=cuda"
Write-Host "  MODEL_DIR=/opt/models"
