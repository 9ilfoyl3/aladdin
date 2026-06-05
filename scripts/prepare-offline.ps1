# ============================================================
# 内网部署离线打包脚本（在有网的 Windows 机器上执行）
# 执行完后会生成 deploy-package/ 目录，整个拷贝到内网服务器
# ============================================================

$ErrorActionPreference = "Stop"
$DEPLOY_DIR = "deploy-package"

Write-Host "=== Artoo 内网部署打包 ===" -ForegroundColor Green

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
docker build -t artoo-backend:latest -f backend/Dockerfile.production backend/

# ============================================================
# 3. 构建前端 Docker 镜像
# ============================================================
Write-Host "`n[3/5] 构建前端镜像..." -ForegroundColor Cyan
docker build -t artoo-frontend:latest frontend/

# ============================================================
# 4. 导出所有 Docker 镜像
# ============================================================
Write-Host "`n[4/5] 导出 Docker 镜像..." -ForegroundColor Cyan

$images = @(
    "artoo-backend:latest",
    "artoo-frontend:latest",
    "postgres:16-alpine",
    "milvusdb/milvus:v2.5.4",
    "quay.io/coreos/etcd:v3.5.25",
    "minio/minio:RELEASE.2024-05-28T17-19-04Z",
    "redis:7-alpine"
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
Copy-Item frontend\nginx.conf "$DEPLOY_DIR\nginx.conf"
Copy-Item scripts\deploy-intranet.sh "$DEPLOY_DIR\"
# 中间件 compose + Milvus mmap 配置：放 middleware/ 子目录，与 package-amd64 产物一致。
# compose 内以 ./milvus-user.yaml 挂载，二者须同目录。
New-Item -ItemType Directory -Force -Path "$DEPLOY_DIR\middleware" | Out-Null
Copy-Item deploy-middleware\docker-compose.yml "$DEPLOY_DIR\middleware\docker-compose.yml"
Copy-Item deploy-middleware\milvus-user.yaml "$DEPLOY_DIR\middleware\milvus-user.yaml"

# 生成部署说明
@"
# Artoo 内网部署步骤

> 推荐直接执行 ``bash deploy-intranet.sh``，它会自动完成：加载镜像 → 引导编辑 .env →
> 建网络 → 起中间件（含 Milvus mmap）→ 等就绪 → 起应用（backend 多进程）。
> 下面是手动分步等价命令。

## 1. 加载 Docker 镜像
```bash
for f in *.tar; do docker load -i `$f; done
```

## 2. 配置环境变量
```bash
cp .env.example .env
# 必填：JWT_SECRET、SUPER_ADMIN_USERNAME/PASSWORD、LLM_*、EMBED_BASE_URL、RERANK_BASE_URL
# 可调：BACKEND_WORKERS（默认 2）、POSTGRES_PASSWORD
```

## 3. 创建共享网络（仅首次）
```bash
docker network create arag-network
```

## 4. 启动中间件（etcd/minio/milvus/postgres/redis），等全部 healthy
```bash
cd middleware && docker compose --env-file ../.env up -d && cd ..
# 查看状态：cd middleware && docker compose --env-file ../.env ps
```

## 5. 启动应用（backend / worker / frontend）
```bash
docker compose up -d
```

## 6. 访问
- 前端: http://服务器IP:8888
- 后端 API: http://服务器IP:8088

## 7. 停止
```bash
docker compose down                                          # 停应用
cd middleware && docker compose --env-file ../.env down       # 停中间件（数据卷保留）
```

## 关键环境变量
| 变量 | 默认值 | 说明 |
|------|--------|------|
| JWT_SECRET | （必填） | JWT 签名密钥，缺失则启动失败 |
| SUPER_ADMIN_USERNAME/PASSWORD | （必填） | 初始超管账号 |
| POSTGRES_PASSWORD | postgres | 数据库密码 |
| BACKEND_WORKERS | 2 | backend 进程数（单机建议 2~4） |
| LLM_BASE_URL / LLM_MODEL / LLM_API_KEY | - | LLM 服务 |
| EMBED_BASE_URL / RERANK_BASE_URL | - | Embedding / Rerank 远程服务 |

> Milvus 已默认开启 mmap（middleware/milvus-user.yaml）控常驻内存；内存仍紧张可把
> 其中 vectorIndex 也设为 true。
"@ | Out-File -Encoding utf8 "$DEPLOY_DIR\README.md"

# ============================================================
Write-Host "`n=== 打包完成 ===" -ForegroundColor Green
Write-Host "输出目录: $DEPLOY_DIR\"
Write-Host "将整个 $DEPLOY_DIR 目录拷贝到内网服务器即可部署"

# 显示大小
$size = (Get-ChildItem -Recurse $DEPLOY_DIR | Measure-Object -Property Length -Sum).Sum / 1GB
Write-Host "总大小: $([math]::Round($size, 2)) GB"
