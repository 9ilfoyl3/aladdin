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
