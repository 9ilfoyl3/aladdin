# ============================================================
# 内网部署离线打包脚本 - ARM64 架构（aarch64）
# 在有网的 Windows 机器上执行，通过 Docker buildx 交叉编译
# ============================================================

$ErrorActionPreference = "Stop"
$DEPLOY_DIR = "deploy-package-arm64"

Write-Host "=== Aladdin 内网部署打包 (ARM64) ===" -ForegroundColor Green

# 创建输出目录
New-Item -ItemType Directory -Force -Path $DEPLOY_DIR | Out-Null

# ============================================================
# 0. 确保 buildx 支持 ARM64
# ============================================================
Write-Host "`n[0/5] 检查 Docker buildx 多架构支持..." -ForegroundColor Cyan
$null = docker buildx create --name arm-builder --use 2>&1
$null = docker buildx inspect --bootstrap 2>&1
Write-Host "  buildx 就绪"

# ============================================================
# 1. 准备模型文件
# ============================================================
Write-Host "`n[1/5] 准备模型文件..." -ForegroundColor Cyan

$HF_CACHE = "$env:USERPROFILE\.cache\huggingface\hub"
$MODEL_DIR = "backend\models"

New-Item -ItemType Directory -Force -Path $MODEL_DIR | Out-Null

# 复制 bge-m3
$BGE_M3 = "$HF_CACHE\models--BAAI--bge-m3"
if (Test-Path $BGE_M3) {
    Write-Host "  复制 bge-m3 模型..."
    Copy-Item -Recurse -Force $BGE_M3 "$MODEL_DIR\models--BAAI--bge-m3"
} else {
    Write-Host "  错误: bge-m3 模型未找到，请先运行后端下载模型" -ForegroundColor Red
    exit 1
}

# 复制 bge-reranker
$BGE_RERANKER = "$HF_CACHE\models--BAAI--bge-reranker-v2-m3"
if (Test-Path $BGE_RERANKER) {
    Write-Host "  复制 bge-reranker 模型..."
    Copy-Item -Recurse -Force $BGE_RERANKER "$MODEL_DIR\models--BAAI--bge-reranker-v2-m3"
} else {
    Write-Host "  错误: bge-reranker 模型未找到，请先运行后端下载模型" -ForegroundColor Red
    exit 1
}

# ============================================================
# 2. 构建后端 Docker 镜像 (ARM64)
# ============================================================
Write-Host "`n[2/5] 构建后端镜像 (ARM64)..." -ForegroundColor Cyan
docker buildx build --platform linux/arm64 -t aladdin-backend:latest-arm64 -f backend/Dockerfile.production-arm64 --load backend/
if ($LASTEXITCODE -ne 0) {
    Write-Host "  错误: 后端镜像构建失败！" -ForegroundColor Red
    exit 1
}

# ============================================================
# 3. 构建前端 Docker 镜像 (ARM64)
# ============================================================
Write-Host "`n[3/5] 构建前端镜像 (ARM64)..." -ForegroundColor Cyan
docker buildx build --platform linux/arm64 -t aladdin-frontend:latest-arm64 --load frontend/
if ($LASTEXITCODE -ne 0) {
    Write-Host "  错误: 前端镜像构建失败！" -ForegroundColor Red
    exit 1
}

# ============================================================
# 4. 拉取并导出所有 Docker 镜像 (ARM64)
# ============================================================
Write-Host "`n[4/5] 拉取并导出 Docker 镜像 (ARM64)..." -ForegroundColor Cyan

# ARM64 版本的基础设施镜像
$infraImages = @(
    "postgres:16-alpine",
    "milvusdb/milvus:v2.4.6",
    "quay.io/coreos/etcd:v3.5.18",
    "minio/minio:RELEASE.2023-03-20T20-16-18Z"
)

# 拉取 ARM64 版本
foreach ($img in $infraImages) {
    Write-Host "  拉取 $img (arm64)..."
    docker pull --platform linux/arm64 $img
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  错误: 拉取 $img 失败！" -ForegroundColor Red
        exit 1
    }
}

# 导出所有镜像
$allImages = @(
    @{name="aladdin-backend:latest-arm64"; file="aladdin-backend_latest-arm64.tar"},
    @{name="aladdin-frontend:latest-arm64"; file="aladdin-frontend_latest-arm64.tar"},
    @{name="postgres:16-alpine"; file="postgres_16-alpine.tar"},
    @{name="milvusdb/milvus:v2.4.6"; file="milvusdb_milvus_v2.4.6.tar"},
    @{name="quay.io/coreos/etcd:v3.5.18"; file="quay.io_coreos_etcd_v3.5.18.tar"},
    @{name="minio/minio:RELEASE.2023-03-20T20-16-18Z"; file="minio_minio_RELEASE.2023-03-20T20-16-18Z.tar"}
)

foreach ($img in $allImages) {
    Write-Host "  导出 $($img.name) -> $($img.file)"
    docker save $img.name -o "$DEPLOY_DIR\$($img.file)"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  错误: 导出 $($img.name) 失败！" -ForegroundColor Red
        exit 1
    }
}

# ============================================================
# 5. 复制部署文件
# ============================================================
Write-Host "`n[5/5] 复制部署配置..." -ForegroundColor Cyan

# 生成 ARM64 专用的 docker-compose（镜像名带 -arm64 后缀）
$composeContent = Get-Content docker-compose-production.yml -Raw
$composeContent = $composeContent -replace "aladdin-backend:latest", "aladdin-backend:latest-arm64"
$composeContent = $composeContent -replace "aladdin-frontend:latest", "aladdin-frontend:latest-arm64"
# ARM 服务器通常没有 NVIDIA GPU，默认 cpu
$composeContent = $composeContent -replace '\$\{EMBED_DEVICE:-cuda\}', '${EMBED_DEVICE:-cpu}'
$composeContent = $composeContent -replace '\$\{RERANK_DEVICE:-cuda\}', '${RERANK_DEVICE:-cpu}'
$composeContent | Out-File -Encoding utf8 "$DEPLOY_DIR\docker-compose.yml"

Copy-Item backend\.env.example "$DEPLOY_DIR\.env.example"
Copy-Item backend\.env.production "$DEPLOY_DIR\.env"

# 修改 .env 默认设备为 cpu（ARM 通常无 NVIDIA GPU）
$envContent = Get-Content "$DEPLOY_DIR\.env" -Raw
$envContent = $envContent -replace "EMBED_DEVICE=cuda", "EMBED_DEVICE=cpu"
$envContent = $envContent -replace "RERANK_DEVICE=cuda", "RERANK_DEVICE=cpu"
$envContent | Out-File -Encoding utf8 "$DEPLOY_DIR\.env"

Copy-Item scripts\deploy-intranet.sh "$DEPLOY_DIR\deploy-intranet.sh"

# 生成部署说明
@"
# Aladdin 内网部署步骤 (ARM64)

## 1. 加载 Docker 镜像
```bash
for f in *.tar; do docker load -i `$f; done
```

## 2. 检查/编辑环境变量
```bash
vim .env
# LLM 可以启动后在前端配置，也可以在这里预配置
```

## 3. 启动所有服务
```bash
docker compose up -d
```

## 4. 访问
- 前端: http://服务器IP:8888
- 后端 API: http://服务器IP:8000 (仅容器内部)
- API 文档: 通过前端 nginx 代理访问

## 5. 首次使用
1. 打开 http://服务器IP:8888
2. 进入"模型管理" -> 添加内网 LLM 配置 -> 测试连通性 -> 设为默认
3. 创建知识库 -> 上传文档 -> 对话

## 6. 停止
```bash
docker compose down
```

## 注意事项
- 本包为 ARM64 (aarch64) 架构，适用于华为鲲鹏、飞腾、Apple Silicon 等
- 默认使用 CPU 推理（ARM 服务器通常无 NVIDIA GPU）
- 如有 GPU 支持，修改 .env 中 EMBED_DEVICE=cuda 和 RERANK_DEVICE=cuda
"@ | Out-File -Encoding utf8 "$DEPLOY_DIR\README.md"

# ============================================================
Write-Host "`n=== 打包完成 (ARM64) ===" -ForegroundColor Green
Write-Host "输出目录: $DEPLOY_DIR\"
Write-Host "将整个 $DEPLOY_DIR 目录拷贝到 ARM64 内网服务器即可部署"

# 显示大小
$size = (Get-ChildItem -Recurse $DEPLOY_DIR | Measure-Object -Property Length -Sum).Sum / 1GB
Write-Host "总大小: $([math]::Round($size, 2)) GB"
