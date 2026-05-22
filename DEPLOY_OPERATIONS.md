# Aladdin 部署手册

---

## 部署模式

| 模式 | Embedding/Rerank | 需要模型文件 | 镜像大小 |
|------|-----------------|:---:|---------|
| **远程模式** | 调外部 API | ❌ | ~500MB |
| **挂载模型模式** | 容器内加载，模型从宿主机挂载 | ✅ | ~3GB (GPU) / ~1.5GB (CPU) |

---

## 一、打包

### 脚本用法

**Windows (PowerShell)：**
```powershell
cd C:\newHLSWorkspace\aladdin

# AMD64
.\scripts\package-amd64.ps1              # 远程模式，首次
.\scripts\package-amd64.ps1 -GPU         # 挂载模型 + GPU，首次
.\scripts\package-amd64.ps1 -SkipInfra   # 远程模式，更新
.\scripts\package-amd64.ps1 -GPU -SkipInfra  # 挂载模型 + GPU，更新

# ARM64
.\scripts\package-arm64.ps1              # 远程模式，首次
.\scripts\package-arm64.ps1 -GPU         # 挂载模型 + CPU，首次
.\scripts\package-arm64.ps1 -SkipInfra   # 远程模式，更新
.\scripts\package-arm64.ps1 -GPU -SkipInfra  # 挂载模型 + CPU，更新
```

**macOS / Linux (Shell)：**
```bash
./scripts/package-amd64.sh              # 远程模式，首次
./scripts/package-amd64.sh --gpu        # 挂载模型 + GPU，首次
./scripts/package-amd64.sh --skip-infra # 更新
./scripts/package-arm64.sh              # ARM64 远程模式
./scripts/package-arm64.sh --gpu        # ARM64 挂载模型
```

### 参数

| 参数 | 作用 |
|------|------|
| 无 | 远程模式 + 含中间件 |
| `-GPU` / `--gpu` | 挂载模型模式（AMD64=CUDA, ARM64=CPU） |
| `-SkipInfra` / `--skip-infra` | 跳过中间件（更新时用） |

### 打包模型（挂载模式需要）

```powershell
$HF_CACHE = "$env:USERPROFILE\.cache\huggingface\hub"
tar -czf models.tar.gz -C $HF_CACHE models--BAAI--bge-m3 models--BAAI--bge-reranker-v2-m3
```

### 输出

```
deploy-amd64/ 或 deploy-arm64/
├── app.tar            ← 应用镜像（backend + frontend）
├── infra.tar          ← 中间件镜像（首次才有）
├── nginx.conf         ← nginx 配置（挂载到 frontend 容器）
├── docker-compose.yml
└── .env.example
```

---

## 二、服务器部署

### 首次

```bash
mkdir -p /opt/aladdin && cd /opt/aladdin

docker load -i infra.tar
docker load -i app.tar

# 挂载模式：解压模型
mkdir -p /opt/models
tar -xzf models.tar.gz -C /opt/models

cp .env.example .env
vim .env                                # 必须配置 EMBED/RERANK 相关项
docker compose up -d
```

> **注意：** 首次启动后需等待所有服务 healthy（约 30 秒），Worker 会自动检测 Embedding 服务可用后开始消费。

### 更新

```bash
cd /opt/aladdin
docker load -i app.tar
docker compose up -d --force-recreate backend worker frontend
docker image prune -f
```

---

## 三、.env 配置

### 远程模式

```env
EMBED_PROVIDER=remote
EMBED_BASE_URL=http://Embedding服务地址/v1
EMBED_MODEL=模型名
EMBED_API_KEY=密钥
RERANK_PROVIDER=remote
RERANK_BASE_URL=http://Rerank服务地址/ranking_score
RERANK_MODEL=模型名
RERANK_API_KEY=密钥
```

> `.env` 中的 Embedding/Rerank 配置是兜底值。启动后可在前端"Embedding"页面管理，数据库配置优先级更高。

### 挂载模型 — GPU（AMD64 + NVIDIA）

```env
EMBED_PROVIDER=flag-embedding
EMBED_MODEL=BAAI/bge-m3
EMBED_DEVICE=cuda
RERANK_PROVIDER=flag-embedding
RERANK_MODEL=BAAI/bge-reranker-v2-m3
RERANK_DEVICE=cuda
MODEL_DIR=/opt/models
```

### 挂载模型 — CPU（ARM64）

```env
EMBED_PROVIDER=flag-embedding
EMBED_MODEL=BAAI/bge-m3
EMBED_DEVICE=cpu
RERANK_PROVIDER=flag-embedding
RERANK_MODEL=BAAI/bge-reranker-v2-m3
RERANK_DEVICE=cpu
MODEL_DIR=/opt/models
```

### LLM

```env
LLM_PROVIDER=vllm
LLM_BASE_URL=http://LLM服务地址/v1
LLM_MODEL=模型名
LLM_API_KEY=密钥
```

> 也可启动后在前端"模型管理"配置。

### 其他

```env
POSTGRES_PASSWORD=postgres
FRONTEND_PORT=8888
```

---

## 四、访问

- 前端：`http://服务器IP:8888`
- API 文档：`http://服务器IP:8000/docs`

---

## 五、运维

```bash
docker compose ps                                       # 状态
docker compose logs backend -f                          # API 日志
docker compose logs worker -f                           # Worker 日志（文档处理）
docker compose restart backend                          # 重启 API
docker compose restart worker                           # 重启 Worker
docker compose down                                     # 停止
docker compose down -v                                  # 清数据（慎用）
docker image prune -f                                   # 清旧镜像
```

### 架构说明

```
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│   frontend   │───▶│   backend    │───▶│    worker    │
│   (nginx)    │    │  (API only)  │    │ (Pipeline)   │
└──────────────┘    └──────┬───────┘    └──────┬───────┘
                           │                    │
                    ┌──────▼───────┐            │
                    │ Redis Stream │◀───────────┘
                    └──────────────┘
```

- **backend**：仅处理 API 请求，文档上传后入队 Redis Stream
- **worker**：独立进程消费队列，执行文档解析、Embedding、索引
- Worker 卡死不影响 API 响应，可独立重启

---

## 六、本地开发

### Windows（conda 环境）

```powershell
cd C:\newHLSWorkspace\aladdin
docker compose up -d                    # 启动中间件
conda activate aladdin

# 终端 1：API 服务
cd backend
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# 终端 2：Worker（文档处理）
cd backend
python -m app.worker_main

# 终端 3：前端
cd frontend
npm run dev
```

### macOS / Linux（venv 环境）

```bash
cd aladdin
docker compose up -d
source .venv/bin/activate

# 终端 1：API
cd backend && uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# 终端 2：Worker
cd backend && python -m app.worker_main

# 终端 3：前端
cd frontend && npm run dev
```

或使用 Makefile（macOS/Linux）：
```bash
make infra          # 启动中间件
make dev-backend    # 启动 API
make dev-worker     # 启动 Worker（新终端）
make dev-frontend   # 启动前端（新终端）
```

**Windows .env：**
```env
EMBED_PROVIDER=sentence-transformers
EMBED_DEVICE=cpu
RERANK_PROVIDER=sentence-transformers
RERANK_DEVICE=cpu
```

**macOS .env：**
```env
EMBED_PROVIDER=flag-embedding
EMBED_DEVICE=mps
RERANK_PROVIDER=flag-embedding
RERANK_DEVICE=mps
```

> Python 必须 3.12。
