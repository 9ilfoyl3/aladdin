# Aladdin 部署手册

---

## 架构概览

```
┌─────────────────────────────────────────────────┐
│              Aladdin 应用容器（轻量）              │
│  后端 FastAPI + 前端 nginx                       │
│  通过 API 调用外部服务：                          │
│    → Embedding API                              │
│    → Rerank API                                 │
│    → LLM API                                    │
└────────────────────┬────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────┐
│              中间件容器                           │
│  PostgreSQL │ Milvus │ etcd │ MinIO             │
└─────────────────────────────────────────────────┘
```

---

## 一、打包（Windows 机器上执行）

### 快速打包脚本

**Windows (PowerShell)：**
```powershell
cd C:\newHLSWorkspace\aladdin

.\scripts\package-amd64.ps1              # AMD64 首次部署（远程模式）
.\scripts\package-amd64.ps1 -SkipInfra   # AMD64 更新应用
.\scripts\package-amd64.ps1 -WithML      # AMD64 挂载模型模式

.\scripts\package-arm64.ps1              # ARM64 首次部署
.\scripts\package-arm64.ps1 -SkipInfra   # ARM64 更新应用
.\scripts\package-arm64.ps1 -WithML      # ARM64 挂载模型模式
```

**macOS / Linux (Shell)：**
```bash
cd /path/to/aladdin

./scripts/package-amd64.sh              # AMD64 首次部署（远程模式）
./scripts/package-amd64.sh --skip-infra # AMD64 更新应用
./scripts/package-amd64.sh --with-ml    # AMD64 挂载模型模式

./scripts/package-arm64.sh              # ARM64 首次部署
./scripts/package-arm64.sh --skip-infra # ARM64 更新应用
./scripts/package-arm64.sh --with-ml    # ARM64 挂载模型模式
```

输出目录：`deploy-amd64/` 或 `deploy-arm64/`，整个拷贝到服务器。

| 参数 | 作用 |
|------|------|
| 无参数 | 远程模式 + 含中间件（首次部署） |
| `-SkipInfra` | 只打应用镜像（更新时用） |
| `-WithML` | 含 ML 依赖（挂载模型模式） |
| `-WithML -SkipInfra` | 含 ML + 跳过中间件 |

### 手动打包（如需自定义）

### 1. 应用镜像

| 目标 | 命令 |
|------|------|
| AMD64 远程模式 | `docker build -t aladdin-backend:latest backend/` |
| AMD64 挂载模型 | `docker build --build-arg INSTALL_ML=true -t aladdin-backend:latest backend/` |
| ARM64 远程模式 | `docker build --platform linux/arm64 -t aladdin-backend:latest backend/` |
| ARM64 挂载模型 | `docker build --platform linux/arm64 --build-arg INSTALL_ML=true -t aladdin-backend:latest backend/` |
| 前端（通用） | `docker build -t aladdin-frontend:latest frontend/` |
| 前端（ARM64） | `docker build --platform linux/arm64 -t aladdin-frontend:latest frontend/` |

导出：
```powershell
docker save aladdin-backend:latest aladdin-frontend:latest -o app.tar
```

### 2. 中间件镜像（首次部署打一次）

**AMD64：**
```powershell
docker pull postgres:16-alpine
docker pull milvusdb/milvus:v2.4.6
docker pull quay.io/coreos/etcd:v3.5.25
docker pull minio/minio:RELEASE.2024-05-28T17-19-04Z
docker save postgres:16-alpine milvusdb/milvus:v2.4.6 quay.io/coreos/etcd:v3.5.25 minio/minio:RELEASE.2024-05-28T17-19-04Z -o infra.tar
```

**ARM64：**
```powershell
docker pull --platform linux/arm64 postgres:16-alpine
docker pull --platform linux/arm64 milvusdb/milvus:v2.4.6
docker pull --platform linux/arm64 quay.io/coreos/etcd:v3.5.25
docker pull --platform linux/arm64 minio/minio:RELEASE.2024-05-28T17-19-04Z
docker save postgres:16-alpine milvusdb/milvus:v2.4.6 quay.io/coreos/etcd:v3.5.25 minio/minio:RELEASE.2024-05-28T17-19-04Z -o infra.tar
```

### 3. 模型文件（挂载模型模式才需要）

不区分架构，打一次通用：
```powershell
$HF_CACHE = "$env:USERPROFILE\.cache\huggingface\hub"
tar -czf models.tar.gz -C $HF_CACHE models--BAAI--bge-m3 models--BAAI--bge-reranker-v2-m3
```

---

## 二、部署（服务器上执行）

### 首次部署

```bash
mkdir -p /opt/aladdin && cd /opt/aladdin

# 加载镜像
docker load -i infra.tar
docker load -i app.tar

# 放入 docker-compose.yml 和 .env
# 编辑 .env（见下方配置说明）
vim .env

# 如果是挂载模型模式，解压模型
mkdir -p /opt/models
tar -xzf models.tar.gz -C /opt/models

# 启动
docker compose up -d
```

### 更新应用

```bash
cd /opt/aladdin
docker load -i app.tar
docker compose up -d --force-recreate backend frontend
```

---

## 三、.env 配置

### 镜像配置（使用公司镜像库时填写）

```env
IMAGE_POSTGRES=registry.company.com/aladdin/postgres:16-alpine-arm64
IMAGE_MILVUS=registry.company.com/aladdin/milvus:v2.4.6-arm64
IMAGE_ETCD=registry.company.com/aladdin/etcd:v3.5.25-arm64
IMAGE_MINIO=registry.company.com/aladdin/minio:2023-03-20-arm64
IMAGE_BACKEND=registry.company.com/aladdin/aladdin-backend:latest-arm64
IMAGE_FRONTEND=registry.company.com/aladdin/aladdin-frontend:latest-arm64
```

> 不配置时使用 docker-compose.yml 中的默认镜像名。

### Embedding / Rerank 配置

**方式 A：远程模式（推荐，调外部 API）**

```env
EMBED_PROVIDER=remote
EMBED_BASE_URL=http://模型服务地址/v1
EMBED_MODEL=模型名
EMBED_API_KEY=

RERANK_PROVIDER=remote
RERANK_BASE_URL=http://模型服务地址/v1
RERANK_MODEL=模型名
RERANK_API_KEY=
```

**方式 B：挂载模型目录（服务器无独立模型服务时）**

```env
EMBED_PROVIDER=flag-embedding
EMBED_MODEL=BAAI/bge-m3
EMBED_DEVICE=cpu

RERANK_PROVIDER=flag-embedding
RERANK_MODEL=BAAI/bge-reranker-v2-m3
RERANK_DEVICE=cpu

MODEL_DIR=/opt/models
```

> 需要用 `INSTALL_ML=true` 构建的镜像。
> Linux 服务器推荐 `flag-embedding`，Windows 本地开发用 `sentence-transformers`。

### LLM 配置

```env
LLM_PROVIDER=vllm
LLM_BASE_URL=http://LLM服务地址/v1
LLM_MODEL=模型名
LLM_API_KEY=密钥
```

> 也可启动后在前端"模型管理"页面配置。

### 其他

```env
POSTGRES_PASSWORD=postgres
FRONTEND_PORT=8888
```

---

## 四、访问

| 地址 | 用途 |
|------|------|
| `http://服务器IP:8888` | 前端管理界面 |
| `http://服务器IP:8000/docs` | API 文档 |

---

## 五、运维命令

```bash
docker compose ps                                    # 查看状态
docker compose logs backend -f                       # 后端日志
docker compose logs frontend -f                      # 前端日志
docker compose restart backend                       # 重启后端
docker compose down                                  # 停止（数据保留）
docker compose down -v                               # 清除所有数据（慎用）
docker compose up -d --force-recreate backend frontend  # 更新后重启应用
```

---

## 六、本地开发

本地开发时 Embedding/Rerank 在本机 Python 进程跑，不依赖外部服务。

```powershell
cd C:\newHLSWorkspace\aladdin

# 启动中间件
docker compose up -d

# 激活环境
conda activate aladdin

# 首次下载模型
$env:HF_HUB_OFFLINE="0"
cd backend
python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-m3'); print('OK')"
python -c "from sentence_transformers import CrossEncoder; CrossEncoder('BAAI/bge-reranker-v2-m3'); print('OK')"

# 启动后端
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# 启动前端（新终端）
cd C:\newHLSWorkspace\aladdin\frontend
npm run dev
```

**本地 .env：**

Windows：
```env
EMBED_PROVIDER=sentence-transformers
EMBED_DEVICE=cpu
RERANK_PROVIDER=sentence-transformers
RERANK_DEVICE=cpu
```

macOS：
```env
EMBED_PROVIDER=flag-embedding
EMBED_DEVICE=mps
RERANK_PROVIDER=flag-embedding
RERANK_DEVICE=mps
```

> Python 版本必须 3.12（3.13+ 有兼容问题）。

