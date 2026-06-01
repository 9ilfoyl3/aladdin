# Artoo ARM64 部署打包
param(
    [switch]$SkipInfra,    # 跳过中间件（非首次部署）
    [switch]$BackendOnly,  # 只构建后端
    [switch]$FrontendOnly, # 只构建前端
    [switch]$NoCache       # 不使用缓存（分支切换或依赖变更时用）
)

$ErrorActionPreference = "Stop"
$OUT = "deploy-arm64"
$env:DOCKER_BUILDKIT = "1"

Write-Host "=== Artoo ARM64 打包 ===" -ForegroundColor Green
New-Item -ItemType Directory -Force -Path $OUT | Out-Null

$cacheFlag = if ($NoCache) { "--no-cache" } else { "" }
$buildBoth = -not $BackendOnly -and -not $FrontendOnly

# 后端镜像
if ($buildBoth -or $BackendOnly) {
    Write-Host "`n[1] 构建后端镜像（ARM64）..." -ForegroundColor Cyan
    $cmd = "docker build --platform linux/arm64 $cacheFlag -t artoo-backend:latest backend/"
    Invoke-Expression $cmd
    if ($LASTEXITCODE -ne 0) { Write-Host "后端构建失败！" -ForegroundColor Red; exit 1 }
}

# 前端镜像
if ($buildBoth -or $FrontendOnly) {
    Write-Host "`n[2] 构建前端镜像（ARM64）..." -ForegroundColor Cyan
    $cmd = "docker build --platform linux/arm64 $cacheFlag -t artoo-frontend:latest frontend/"
    Invoke-Expression $cmd
    if ($LASTEXITCODE -ne 0) { Write-Host "前端构建失败！" -ForegroundColor Red; exit 1 }
}

# 导出应用镜像
Write-Host "`n[3] 导出应用镜像..." -ForegroundColor Cyan
if ($BackendOnly) {
    docker save artoo-backend:latest -o "$OUT\app.tar"
} elseif ($FrontendOnly) {
    docker save artoo-frontend:latest -o "$OUT\app.tar"
} else {
    docker save artoo-backend:latest artoo-frontend:latest -o "$OUT\app.tar"
}

# 中间件
if (-not $SkipInfra -and $buildBoth) {
    Write-Host "`n[4] 拉取并导出中间件（ARM64）..." -ForegroundColor Cyan
    docker pull --platform linux/arm64 postgres:16-alpine
    docker pull --platform linux/arm64 milvusdb/milvus:v2.5.4
    docker pull --platform linux/arm64 quay.io/coreos/etcd:v3.5.25
    docker pull --platform linux/arm64 minio/minio:RELEASE.2024-05-28T17-19-04Z
    docker pull --platform linux/arm64 redis:7-alpine
    docker save postgres:16-alpine milvusdb/milvus:v2.5.4 quay.io/coreos/etcd:v3.5.25 `
        minio/minio:RELEASE.2024-05-28T17-19-04Z redis:7-alpine -o "$OUT\infra.tar"
}

# 配置文件
Write-Host "`n[5] 复制配置文件..." -ForegroundColor Cyan
Copy-Item docker-compose-production.yml "$OUT\docker-compose.yml"
Copy-Item backend\.env.example "$OUT\.env.example"
Copy-Item frontend\nginx.conf "$OUT\nginx.conf"
# 中间件 compose 单独放 middleware/ 子目录，便于独立启停管理
New-Item -ItemType Directory -Force -Path "$OUT\middleware" | Out-Null
Copy-Item deploy-middleware\docker-compose.yml "$OUT\middleware\docker-compose.yml"

Write-Host "`n=== 完成 ===" -ForegroundColor Green
$size = [math]::Round((Get-ChildItem -Recurse $OUT | Measure-Object -Property Length -Sum).Sum / 1GB, 2)
Write-Host "输出: $OUT\ ($size GB)"
Write-Host ""
Write-Host "产出结构:" -ForegroundColor Yellow
Write-Host "  $OUT\app.tar               应用镜像（backend+frontend）"
Write-Host "  $OUT\infra.tar             基础设施镜像（仅完整包）"
Write-Host "  $OUT\docker-compose.yml    应用编排（backend/worker/frontend）"
Write-Host "  $OUT\middleware\docker-compose.yml  中间件编排（独立启停）"
Write-Host "  $OUT\nginx.conf / .env.example"
Write-Host ""
Write-Host "部署（服务器）:" -ForegroundColor Yellow
Write-Host "  docker network create arag-network"
Write-Host "  (cd middleware; docker compose --env-file ../.env up -d)   # 先起中间件，等 healthy"
Write-Host "  docker compose up -d                                       # 再起应用"
Write-Host ""
Write-Host "打包用法:" -ForegroundColor Yellow
Write-Host "  首次部署:      .\scripts\package-arm64.ps1"
Write-Host "  更新应用:      .\scripts\package-arm64.ps1 -SkipInfra"
Write-Host "  只更新后端:    .\scripts\package-arm64.ps1 -SkipInfra -BackendOnly"
Write-Host "  只更新前端:    .\scripts\package-arm64.ps1 -SkipInfra -FrontendOnly"
Write-Host "  切换分支/依赖变更: .\scripts\package-arm64.ps1 -SkipInfra -NoCache"
