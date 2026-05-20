# Aladdin 打包与部署手册

Aladdin 采用远程模式部署——应用本身不跑模型，通过 API 调用外部的 Embedding/Rerank/LLM 服务。

所有打包在 Windows 机器上执行（`C:\newHLSWorkspace\aladdin`）。

---

## 打包产物

| 包 | 内容 | 大小 | 区分架构 | 什么时候需要 |
|---|---|---|---|---|
| `app.tar` | 后端 + 前端镜像 | ~500MB | ✅ | 每次更新 |
| `infra.tar` | PostgreSQL + Milvus + etcd + MinIO | ~700MB | ✅ | 首次部署 |

另需：`docker-compose.yml` + `.env`

---

## 一、应用打包（app.tar）

### AMD64 服务器

```powershell
docker build -t aladdin-backend:latest backend/
docker build -t aladdin-frontend:latest frontend/
docker save aladdin-backend:latest aladdin-frontend:latest -o app-amd64.tar
```

### ARM64 服务器

```powershell
docker build --platform linux/arm64 -t aladdin-backend:latest backend/
docker build --platform linux/arm64 -t aladdin-frontend:latest frontend/
docker save aladdin-backend:latest aladdin-frontend:latest -o app-arm64.tar
```

---

## 二、中间件打包（infra.tar）

首次部署打一次，以后不用重复。

### AMD64 服务器

```powershell
docker pull postgres:16-alpine
docker pull milvusdb/milvus:v2.4.6
docker pull quay.io/coreos/etcd:v3.5.18
docker pull minio/minio:RELEASE.2023-03-20T20-16-18Z
docker save postgres:16-alpine milvusdb/milvus:v2.4.6 quay.io/coreos/etcd:v3.5.18 minio/minio:RELEASE.2023-03-20T20-16-18Z -o infra-amd64.tar
```

### ARM64 服务器

```powershell
docker pull --platform linux/arm64 postgres:16-alpine
docker pull --platform linux/arm64 milvusdb/milvus:v2.4.6
docker pull --platform linux/arm64 quay.io/coreos/etcd:v3.5.18
docker pull --platform linux/arm64 minio/minio:RELEASE.2023-03-20T20-16-18Z
docker save postgres:16-alpine milvusdb/milvus:v2.4.6 quay.io/coreos/etcd:v3.5.18 minio/minio:RELEASE.2023-03-20T20-16-18Z -o infra-arm64.tar
```

---

## 三、服务器部署

### 首次部署

```bash
mkdir -p /opt/aladdin && cd /opt/aladdin

# 1. 加载镜像
docker load -i infra.tar
docker load -i app.tar

# 2. 放入 docker-compose.yml 和 .env

# 3. 编辑 .env
vim .env

# 4. 启动
docker compose up -d

# 5. 验证
docker compose ps
```

### 更新应用

```bash
cd /opt/aladdin
docker load -i app.tar
docker compose up -d --force-recreate backend frontend
```

---

## 四、.env 配置

```env
# Embedding（远程 API）
EMBED_PROVIDER=remote
EMBED_BASE_URL=http://模型服务地址/v1
EMBED_MODEL=模型名
EMBED_API_KEY=

# Rerank（远程 API）
RERANK_PROVIDER=remote
RERANK_BASE_URL=http://模型服务地址/v1
RERANK_MODEL=模型名
RERANK_API_KEY=

# LLM（启动后也可在前端"模型管理"配置）
LLM_PROVIDER=vllm
LLM_BASE_URL=http://LLM服务地址/v1
LLM_MODEL=模型名
LLM_API_KEY=密钥

# 数据库密码
POSTGRES_PASSWORD=postgres
```

---

## 五、访问

| 地址 | 用途 |
|------|------|
| `http://服务器IP:8888` | 前端管理界面 |
| `http://服务器IP:8000/docs` | API 文档 |

---

## 六、常用命令

```bash
docker compose ps                    # 查看状态
docker compose logs backend -f       # 查看后端日志
docker compose restart backend       # 重启后端
docker compose down                  # 停止（数据保留）
docker compose down -v               # 停止并清除数据（慎用）
```

---

## 七、本地开发模式

本地开发时 Embedding/Rerank 在本机跑（不依赖外部模型服务），需要安装 ML 依赖。

### 前提

- Python 3.12（通过 conda 管理）
- Docker Desktop（跑 PostgreSQL + Milvus）
- 线上 LLM API Key

### 步骤

```powershell
cd C:\newHLSWorkspace\aladdin

# 1. 启动中间件
docker compose up -d

# 2. 激活 Python 环境
conda activate aladdin

# 3. 安装依赖（含 ML）
pip install -r backend/requirements.txt

# 4. 首次需要下载模型（约 3GB，需联网）
$env:HF_HUB_OFFLINE="0"
python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-m3'); print('OK')"
python -c "from sentence_transformers import CrossEncoder; CrossEncoder('BAAI/bge-reranker-v2-m3'); print('OK')"

# 5. 启动后端
cd backend
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# 6. 启动前端（新终端）
cd C:\newHLSWorkspace\aladdin\frontend
npm install
npm run dev
```

### 本地 .env 配置

```env
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/aladdin
MILVUS_HOST=localhost
MILVUS_PORT=19530

# 本地模型
EMBED_PROVIDER=sentence-transformers
EMBED_MODEL=BAAI/bge-m3
EMBED_DEVICE=cpu

RERANK_PROVIDER=sentence-transformers
RERANK_MODEL=BAAI/bge-reranker-v2-m3
RERANK_DEVICE=cpu

# LLM（线上 API）
LLM_PROVIDER=vllm
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
LLM_API_KEY=sk-你的key
```

### 与服务器部署的区别

| | 本地开发 | 服务器部署 |
|---|---|---|
| Embedding/Rerank | 本机 Python 进程跑模型 | 调远程 API |
| 后端 | `uvicorn` 直接运行 | Docker 容器 |
| 前端 | `npm run dev`（端口 3000） | nginx 容器（端口 8888） |
| 中间件 | Docker（暴露端口到 localhost） | Docker（仅容器间通信） |
| Python 版本 | 必须 3.12（3.13+ 有兼容问题） | 不需要装 Python（容器内自带） |
