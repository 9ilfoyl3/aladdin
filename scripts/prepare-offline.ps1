# ============================================================
# 内网部署离线打包脚本（在有网的 Windows 机器上执行）
# 执行完后会生成 deploy-package/ 目录，整个拷贝到内网服务器
# ============================================================

$ErrorActionPreference = "Stop"
$DEPLOY_DIR = "deploy-package"

Write-Host "=== Aladdin 内网部署打包 ===" -ForegroundColor Green

# 创建输出目录
New-Item -ItemType Directory -Force -Path $DEPLOY_DIR | Out-Null

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
    Write-Host "  警告: bge-m3 模型未找到，请先运行后端下载模型" -ForegroundColor Yellow
}

# 复制 bge-reranker
$BGE_RERANKER = "$HF_CACHE\models--BAAI--bge-reranker-v2-m3"
if (Test-Path $BGE_RERANKER) {
    Write-Host "  复制 bge-reranker 模型..."
    Copy-Item -Recurse -Force $BGE_RERANKER "$MODEL_DIR\models--BAAI--bge-reranker-v2-m3"
} else {
    Write-Host "  警告: bge-reranker 模型未找到，请先运行后端下载模型" -ForegroundColor Yellow
}

# ============================================================
# 2. 构建后端 Docker 镜像
# ============================================================
Write-Host "`n[2/5] 构建后端镜像..." -ForegroundColor Cyan
docker build -t aladdin-backend:latest -f backend/Dockerfile.production backend/

# ============================================================
# 3. 构建前端 Docker 镜像
# ============================================================
Write-Host "`n[3/5] 构建前端镜像..." -ForegroundColor Cyan
docker build -t aladdin-frontend:latest frontend/

# ============================================================
# 4. 导出所有 Docker 镜像
# ============================================================
Write-Host "`n[4/5] 导出 Docker 镜像..." -ForegroundColor Cyan

$images = @(
    "aladdin-backend:latest",
    "aladdin-frontend:latest",
    "postgres:16-alpine",
    "milvusdb/milvus:v2.4.6",
    "quay.io/coreos/etcd:v3.5.18",
    "minio/minio:RELEASE.2023-03-20T20-16-18Z"
)

foreach ($img in $images) {
    $filename = ($img -replace "[/:]", "_") + ".tar"
    Write-Host "  导出 $img -> $filename"
    docker save $img -o "$DEPLOY_DIR\$filename"
}

# ============================================================
# 5. 复制部署文件
# ============================================================
Write-Host "`n[5/5] 复制部署配置..." -ForegroundColor Cyan

Copy-Item docker-compose-production.yml "$DEPLOY_DIR\docker-compose.yml"
Copy-Item backend\.env.example "$DEPLOY_DIR\.env.example"

# 生成部署说明
@"
# Aladdin 内网部署步骤

## 1. 加载 Docker 镜像
```bash
for f in *.tar; do docker load -i `$f; done
```

## 2. 配置环境变量
```bash
cp .env.example .env
# 编辑 .env，配置 LLM 地址和密钥
```

## 3. 启动所有服务
```bash
docker compose up -d
```

## 4. 访问
- 前端: http://服务器IP
- 后端 API: http://服务器IP:8000
- API 文档: http://服务器IP:8000/docs

## 5. 停止
```bash
docker compose down
```

## 环境变量说明
| 变量 | 默认值 | 说明 |
|------|--------|------|
| POSTGRES_PASSWORD | postgres | 数据库密码 |
| EMBED_DEVICE | cpu | 嵌入模型设备 (cpu/cuda) |
| RERANK_DEVICE | cpu | 重排序设备 (cpu/cuda) |
| LLM_PROVIDER | vllm | LLM 提供方 |
| LLM_BASE_URL | - | LLM API 地址 |
| LLM_MODEL | - | LLM 模型名 |
| LLM_API_KEY | - | LLM 密钥 |

如果服务器有 NVIDIA GPU，设置 EMBED_DEVICE=cuda 和 RERANK_DEVICE=cuda 可加速 3-5 倍。
"@ | Out-File -Encoding utf8 "$DEPLOY_DIR\README.md"

# ============================================================
Write-Host "`n=== 打包完成 ===" -ForegroundColor Green
Write-Host "输出目录: $DEPLOY_DIR\"
Write-Host "将整个 $DEPLOY_DIR 目录拷贝到内网服务器即可部署"

# 显示大小
$size = (Get-ChildItem -Recurse $DEPLOY_DIR | Measure-Object -Property Length -Sum).Sum / 1GB
Write-Host "总大小: $([math]::Round($size, 2)) GB"
