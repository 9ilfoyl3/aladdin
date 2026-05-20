# Aladdin 打包与部署手册

所有打包在 Windows 机器上执行（`C:\newHLSWorkspace\aladdin`）。
三个包独立打包，按需组合部署。

---

## 打包产物

| 包 | 内容 | 大小 | 区分架构 |
|---|---|---|---|
| `infra.tar` | PostgreSQL + Milvus + etcd + MinIO | ~700MB | ✅ 区分 |
| `models.tar.gz` | bge-m3 + bge-reranker 模型文件 | ~3GB | ❌ 通用 |
| `app.tar` | 后端 + 前端应用镜像 | ~500MB | ✅ 区分 |

另需配置文件：`docker-compose.yml` + `.env`

---

## 一、中间件打包（infra.tar）

首次部署时打一次，以后不用重复。

### AMD64

```powershell
docker pull postgres:16-alpine
docker pull milvusdb/milvus:v2.4.6
docker pull quay.io/coreos/etcd:v3.5.18
docker pull minio/minio:RELEASE.2023-03-20T20-16-18Z

docker save postgres:16-alpine milvusdb/milvus:v2.4.6 quay.io/coreos/etcd:v3.5.18 minio/minio:RELEASE.2023-03-20T20-16-18Z -o infra.tar
```

### ARM64

```powershell
docker pull --platform linux/arm64 postgres:16-alpine
docker pull --platform linux/arm64 milvusdb/milvus:v2.4.6
docker pull --platform linux/arm64 quay.io/coreos/etcd:v3.5.18
docker pull --platform linux/arm64 minio/minio:RELEASE.2023-03-20T20-16-18Z

docker save postgres:16-alpine milvusdb/milvus:v2.4.6 quay.io/coreos/etcd:v3.5.18 minio/minio:RELEASE.2023-03-20T20-16-18Z -o infra.tar
```

---

## 二、模型打包（models.tar.gz）

不区分架构，打一次通用。本地模式才需要，远程模式跳过。

```powershell
$HF_CACHE = "$env:USERPROFILE\.cache\huggingface\hub"
tar -czf models.tar.gz -C $HF_CACHE models--BAAI--bge-m3 models--BAAI--bge-reranker-v2-m3
```

---

## 三、应用打包（app.tar）

每次代码更新后重新打包。

### AMD64 — 远程模式（不含 ML 依赖）

```powershell
docker build -t aladdin-backend:latest backend/
docker build -t aladdin-frontend:latest frontend/
docker save aladdin-backend:latest aladdin-frontend:latest -o app.tar
```

### AMD64 — 本地模式（含 ML 依赖）

```powershell
docker build --build-arg INSTALL_ML=true -t aladdin-backend:latest backend/
docker build -t aladdin-frontend:latest frontend/
docker save aladdin-backend:latest aladdin-frontend:latest -o app.tar
```

### ARM64 — 远程模式

```powershell
docker build --platform linux/arm64 -t aladdin-backend:latest backend/
docker build --platform linux/arm64 -t aladdin-frontend:latest frontend/
docker save aladdin-backend:latest aladdin-frontend:latest -o app.tar
```

### ARM64 — 本地模式

```powershell
docker build --platform linux/arm64 --build-arg INSTALL_ML=true -t aladdin-backend:latest backend/
docker build --platform linux/arm64 -t aladdin-frontend:latest frontend/
docker save aladdin-backend:latest aladdin-frontend:latest -o app.tar
```

---

## 四、服务器部署

### 首次部署

```bash
mkdir -p /opt/aladdin && cd /opt/aladdin

# 1. 加载中间件镜像
docker load -i infra.tar

# 2. 加载应用镜像
docker load -i app.tar

# 3. 如果是本地模式，解压模型
mkdir -p /opt/models
tar -xzf models.tar.gz -C /opt/models

# 4. 放入配置文件（docker-compose.yml + .env）
# 5. 编辑 .env
vim .env

# 6. 启动
docker compose up -d
```

### 更新应用

```bash
cd /opt/aladdin
docker load -i app.tar
docker compose up -d --force-recreate backend frontend
```

---

## 五、.env 配置

### 远程模式（Embedding/Rerank 调外部 API）

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

### 本地模式（容器内跑模型）

```env
EMBED_PROVIDER=sentence-transformers
EMBED_MODEL=BAAI/bge-m3
EMBED_DEVICE=cpu

RERANK_PROVIDER=sentence-transformers
RERANK_MODEL=BAAI/bge-reranker-v2-m3
RERANK_DEVICE=cpu
```

docker-compose.yml 的 backend volumes 加模型挂载：
```yaml
volumes:
  - /opt/models:/root/.cache/huggingface/hub
```

### LLM（启动后也可在前端配置）

```env
LLM_PROVIDER=vllm
LLM_BASE_URL=http://LLM服务地址/v1
LLM_MODEL=模型名
LLM_API_KEY=密钥
```

---

## 六、组合速查

| 我要部署... | 需要的包 |
|---|---|
| 远程模式（首次） | `infra.tar` + `app.tar` + 配置文件 |
| 本地模式（首次） | `infra.tar` + `app.tar` + `models.tar.gz` + 配置文件 |
| 更新应用 | `app.tar` |
| 从远程切换到本地 | `models.tar.gz` + 改 `.env` + 加 volume 挂载 |
