# Aladdin ARM64 离线打包（一键执行）
$ErrorActionPreference = "Stop"
$DEPLOY_DIR = "deploy-package-arm64"

Write-Host "=== Aladdin ARM64 打包 ===" -ForegroundColor Green
New-Item -ItemType Directory -Force -Path $DEPLOY_DIR | Out-Null

# 构建镜像
Write-Host "`n[1/4] 构建后端镜像（ARM64，约30-60分钟）..." -ForegroundColor Cyan
docker build --platform linux/arm64 -t aladdin-backend:latest -f backend/Dockerfile.production-arm64 backend/
if ($LASTEXITCODE -ne 0) { Write-Host "后端构建失败！" -ForegroundColor Red; exit 1 }

Write-Host "`n[2/4] 构建前端镜像（ARM64）..." -ForegroundColor Cyan
docker build --platform linux/arm64 -t aladdin-frontend:latest frontend/
if ($LASTEXITCODE -ne 0) { Write-Host "前端构建失败！" -ForegroundColor Red; exit 1 }

# 拉取基础设施镜像
Write-Host "`n[3/4] 拉取并导出镜像..." -ForegroundColor Cyan
$images = @("aladdin-backend:latest", "aladdin-frontend:latest", "postgres:16-alpine", "milvusdb/milvus:v2.4.6", "quay.io/coreos/etcd:v3.5.18", "minio/minio:RELEASE.2023-03-20T20-16-18Z")
foreach ($img in $images) {
    if ($img -notlike "aladdin-*") { docker pull --platform linux/arm64 $img }
    $file = ($img -replace "[/:]", "_") + ".tar"
    Write-Host "  导出 $img"
    docker save $img -o "$DEPLOY_DIR\$file"
    if ($LASTEXITCODE -ne 0) { Write-Host "导出 $img 失败！" -ForegroundColor Red; exit 1 }
}

# 复制配置
Write-Host "`n[4/4] 复制配置文件..." -ForegroundColor Cyan
Copy-Item docker-compose-production.yml "$DEPLOY_DIR\docker-compose.yml"
Copy-Item backend\.env.production "$DEPLOY_DIR\.env"

Write-Host "`n=== 完成 ===" -ForegroundColor Green
$size = [math]::Round((Get-ChildItem -Recurse $DEPLOY_DIR | Measure-Object -Property Length -Sum).Sum / 1GB, 2)
Write-Host "输出: $DEPLOY_DIR\ ($size GB)"
Write-Host "拷贝到服务器后: docker compose up -d"
