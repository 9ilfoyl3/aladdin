# Aladdin ARM64 部署打包
param(
    [switch]$WithML,       # 加 -WithML 含 ML 依赖（挂载模型模式，CPU）
    [switch]$SkipInfra     # 加 -SkipInfra 跳过中间件（非首次部署）
)

$ErrorActionPreference = "Stop"
$OUT = "deploy-arm64"

Write-Host "=== Aladdin ARM64 打包 ===" -ForegroundColor Green
New-Item -ItemType Directory -Force -Path $OUT | Out-Null

# 后端镜像
Write-Host "`n[1] 构建后端镜像（ARM64）..." -ForegroundColor Cyan
if ($WithML) {
    Write-Host "  模式: 含 ML 依赖（FlagEmbedding + CPU PyTorch）"
    docker build --platform linux/arm64 --build-arg INSTALL_ML=true -t aladdin-backend:latest backend/
} else {
    Write-Host "  模式: 远程（轻量）"
    docker build --platform linux/arm64 -t aladdin-backend:latest backend/
}
if ($LASTEXITCODE -ne 0) { Write-Host "后端构建失败！" -ForegroundColor Red; exit 1 }

Write-Host "`n[2] 构建前端镜像（ARM64）..." -ForegroundColor Cyan
docker build --platform linux/arm64 -t aladdin-frontend:latest frontend/
if ($LASTEXITCODE -ne 0) { Write-Host "前端构建失败！" -ForegroundColor Red; exit 1 }

Write-Host "`n[3] 导出应用镜像..." -ForegroundColor Cyan
docker save aladdin-backend:latest aladdin-frontend:latest -o "$OUT\app.tar"

# 中间件
if (-not $SkipInfra) {
    Write-Host "`n[4] 拉取并导出中间件（ARM64）..." -ForegroundColor Cyan
    docker pull --platform linux/arm64 postgres:16-alpine
    docker pull --platform linux/arm64 milvusdb/milvus:v2.4.6
    docker pull --platform linux/arm64 quay.io/coreos/etcd:v3.5.25
    docker pull --platform linux/arm64 minio/minio:RELEASE.2024-05-28T17-19-04Z
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
Write-Host "  远程模式首次:  .\scripts\package-arm64.ps1"
Write-Host "  挂载模型首次:  .\scripts\package-arm64.ps1 -WithML"
Write-Host "  更新应用:      .\scripts\package-arm64.ps1 -SkipInfra"
Write-Host "  ML更新:        .\scripts\package-arm64.ps1 -WithML -SkipInfra"
