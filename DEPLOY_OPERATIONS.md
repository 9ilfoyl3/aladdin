# Aladdin 打包与部署操作手册

所有打包操作在你的 Windows 机器上执行（`C:\newHLSWorkspace\aladdin`）。

---

## 一、模式说明

| 模式 | Embedding/Rerank | 镜像大小 | 适用场景 |
|------|-----------------|---------|---------|
| **远程模式** | 调外部 API | ~500MB | 服务器上已有模型服务 |
| **本地模式** | 容器内跑模型 | ~1.5GB (CPU) / ~5GB (GPU) | 无独立模型服务 |

---

## 二、打包命令

### 远程模式 — AMD64 服务器

```powershell
cd C:\newHLSWorkspace\aladdin

# 构建应用镜像
docker build -t aladdin-backend:latest backend/
docker build -t aladdin-frontend:latest frontend/

# 首次部署：导出全量（应用 + 中间件）
docker save aladdin-backend:latest aladdin-frontend:latest -o app.tar
docker save postgres:16-alpine milvusdb/milvus:v2.4.6 quay.io/coreos/etcd:v3.5.18 minio/minio:RELEASE.2023-03-20T20-16-18Z -o infra.tar

# 后续更新：只导出应用
docker save aladdin-backend:latest aladdin-frontend:latest -o app.tar
```

### 远程模式 — ARM64 服务器

```powershell
cd C:\newHLSWorkspace\aladdin

# 构建应用镜像（交叉编译）
docker build --platform linux/arm64 -t aladdin-backend:latest backend/
docker build --platform linux/arm64 -t aladdin-frontend:latest frontend/

# 首次部署：拉取 ARM64 中间件 + 导出全量
docker pull --platform linux/arm64 postgres:16-alpine
docker pull --platform linux/arm64 milvusdb/milvus:v2.4.6
docker pull --platform linux/arm64 quay.io/coreos/etcd:v3.5.18
docker pull --platform linux/arm64 minio/minio:RELEASE.2023-03-20T20-16-18Z

docker save aladdin-backend:latest aladdin-frontend:latest -o app.tar
docker save postgres:16-alpine milvusdb/milvus:v2.4.6 quay.io/coreos/etcd:v3.5.18 minio/minio:RELEASE.2023-03-20T20-16-18Z -o infra.tar

# 后续更新：只导出应用
docker save aladdin-backend:latest aladdin-frontend:latest -o app.tar
```

### 本地模式（CPU）— AMD64 服务器

```powershell
cd C:\newHLSWorkspace\aladdin

# 构建（含 sentence-transformers + CPU PyTorch）
docker build --build-arg INSTALL_ML=true -t aladdin-backend:latest backend/
docker build -t aladdin-frontend:latest frontend/

# 导出（同上）
docker save aladdin-backend:latest aladdin-frontend:latest -o app.tar
docker save postgres:16-alpine milvusdb/milvus:v2.4.6 quay.io/coreos/etcd:v3.5.18 minio/minio:RELEASE.2023-03-20T20-16-18Z -o infra.tar
```

### 本地模式（CPU）— ARM64 服务器

```powershell
cd C:\newHLSWorkspace\aladdin

# 构建（交叉编译 + ML 依赖）
docker build --platform linux/arm64 --build-arg INSTALL_ML=true -t aladdin-backend:latest backend/
docker build --platform linux/arm64 -t aladdin-frontend:latest frontend/

# 导出（同上）
docker save aladdin-backend:latest aladdin-frontend:latest -o app.tar
# 中间件同 ARM64 远程模式
```

### 本地模式（GPU）— AMD64 + NVIDIA 服务器

```powershell
cd C:\newHLSWorkspace\aladdin

# 准备模型文件
$HF_CACHE = "$env:USERPROFILE\.cache\huggingface\hub"
New-Item -ItemType Directory -Force -Path backend/models
Copy-Item -Recurse -Force "$HF_CACHE\models--BAAI--bge-m3" backend/models/
Copy-Item -Recurse -Force "$HF_CACHE\models--BAAI--bge-reranker-v2-m3" backend/models/

# 构建（CUDA PyTorch + FlagEmbedding + 模型打包进镜像）
docker build -t aladdin-backend:latest -f backend/Dockerfile.production backend/
docker build -t aladdin-frontend:latest frontend/

# 导出
docker save aladdin-backend:latest aladdin-frontend:latest -o app.tar
docker save postgres:16-alpine milvusdb/milvus:v2.4.6 quay.io/coreos/etcd:v3.5.18 minio/minio:RELEASE.2023-03-20T20-16-18Z -o infra.tar
```

---

## 三、传输到服务器

三个独立包，按需组合：

| 包 | 内容 | 大小 | 什么时候需要 |
|---|---|---|---|
| `app.tar` | 后端 + 前端镜像 | ~500MB | **每次更新** |
| `infra.tar` | PostgreSQL + Milvus + etcd + MinIO | ~700MB | **首次部署** |
| `models.tar.gz` | bge-m3 + bge-reranker 模型文件 | ~3GB | **本地模式才需要**（远程模式不需要） |

### 打包命令

```powershell
cd C:\newHLSWorkspace\aladdin

# ===== 应用包（每次更新都要重新打） =====
docker save aladdin-backend:latest aladdin-frontend:latest -o app.tar

# ===== 中间件包（首次打一次，以后不用） =====
# AMD64 服务器：
docker save postgres:16-alpine milvusdb/milvus:v2.4.6 quay.io/coreos/etcd:v3.5.18 minio/minio:RELEASE.2023-03-20T20-16-18Z -o infra.tar
# ARM64 服务器（需要先 pull ARM 版）：
docker pull --platform linux/arm64 postgres:16-alpine
docker pull --platform linux/arm64 milvusdb/milvus:v2.4.6
docker pull --platform linux/arm64 quay.io/coreos/etcd:v3.5.18
docker pull --platform linux/arm64 minio/minio:RELEASE.2023-03-20T20-16-18Z
docker save postgres:16-alpine milvusdb/milvus:v2.4.6 quay.io/coreos/etcd:v3.5.18 minio/minio:RELEASE.2023-03-20T20-16-18Z -o infra.tar

# ===== 模型包（通用，不区分架构，打一次就行） =====
$HF_CACHE = "$env:USERPROFILE\.cache\huggingface\hub"
tar -czf models.tar.gz -C $HF_CACHE models--BAAI--bge-m3 models--BAAI--bge-reranker-v2-m3
```

### 按场景选择传输哪些文件

| 场景 | 需要传的文件 |
|------|------------|
| 首次部署（远程模式） | `app.tar` + `infra.tar` + `docker-compose.yml` + `.env` |
| 首次部署（本地模式） | `app.tar` + `infra.tar` + `models.tar.gz` + `docker-compose.yml` + `.env` |
| 后续更新应用 | `app.tar` |
| 补装模型（远程改本地） | `models.tar.gz` |

---

## 四、服务器部署

### 首次部署

```bash
# 创建部署目录
mkdir -p /opt/aladdin && cd /opt/aladdin

# 加载镜像
docker load -i infra.tar
docker load -i app.tar

# 如果是本地模式，解压模型
mkdir -p /opt/models
tar -xzf models.tar.gz -C /opt/models

# 配置环境变量
cp .env.example .env
vim .env
```

**`.env` 必须配置的项：**

远程模式：
```env
EMBED_PROVIDER=remote
EMBED_BASE_URL=http://你的Embedding服务地址/v1
RERANK_PROVIDER=remote
RERANK_BASE_URL=http://你的Rerank服务地址/v1
```

本地模式：
```env
EMBED_PROVIDER=sentence-transformers
EMBED_DEVICE=cpu    # 有 GPU 改 cuda
RERANK_PROVIDER=sentence-transformers
RERANK_DEVICE=cpu   # 有 GPU 改 cuda
```

本地模式还需要在 `docker-compose.yml` 的 backend volumes 中加模型挂载：
```yaml
volumes:
  - upload_data:/app/data
  - /opt/models:/root/.cache/huggingface/hub
```

LLM（启动后也可在前端配置）：
```env
LLM_PROVIDER=vllm
LLM_BASE_URL=http://你的LLM服务地址/v1
LLM_MODEL=模型名
LLM_API_KEY=密钥
```

**启动：**
```bash
docker compose up -d

# 验证
docker compose ps          # 全部 Up
docker compose logs backend --tail 20  # 无报错
```

**访问：** `http://服务器IP:8888`

---

### 后续更新

```bash
cd /opt/aladdin

# 加载新镜像
docker load -i app.tar

# 重启应用（不影响数据库和向量库）
docker compose up -d --force-recreate backend frontend

# 验证
docker compose logs backend --tail 20
```

---

## 五、常用运维命令

```bash
# 查看状态
docker compose ps

# 查看日志
docker compose logs backend -f
docker compose logs frontend -f

# 重启单个服务
docker compose restart backend

# 停止（数据保留）
docker compose down

# 停止并清除所有数据（慎用！）
docker compose down -v
```

---

## 六、快速参考

| 我要... | 命令 |
|---------|------|
| ARM + 远程模式打包 | `docker build --platform linux/arm64 -t aladdin-backend:latest backend/` |
| AMD + 远程模式打包 | `docker build -t aladdin-backend:latest backend/` |
| AMD + GPU 本地模式打包 | `docker build -t aladdin-backend:latest -f backend/Dockerfile.production backend/` |
| 只更新应用 | `docker save aladdin-backend:latest aladdin-frontend:latest -o app.tar` |
| 服务器加载更新 | `docker load -i app.tar && docker compose up -d --force-recreate backend frontend` |
