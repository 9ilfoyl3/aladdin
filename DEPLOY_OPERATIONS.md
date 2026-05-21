# Aladdin 部署手册

---

## 架构概览

```
┌─────────────────────────────────────────────────┐
│              Aladdin 应用容器                     │
│  后端 FastAPI + 前端 nginx                       │
│  通过 API 调用外部服务或本地加载模型：             │
│    → Embedding（远程 API 或本地 FlagEmbedding）   │
│    → Rerank（远程 API 或本地 FlagEmbedding）      │
│    → LLM API                                    │
└────────────────────┬────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────┐
│              中间件容器                           │
│  PostgreSQL 16 │ Milvus 2.4.6 │ etcd 3.5.25    │
│  MinIO 2024-05-28                               │
└─────────────────────────────────────────────────┘
```

---

## 一、打包（Windows / macOS 上执行）

### 快速打包脚本

**Windows (PowerShell)：**
```powershell
cd C:\newHLSWorkspace\aladdin

# === AMD64 服务器 ===
.\scripts\package-amd64.ps1              # 远程模式，首次部署
.\scripts\package-amd64.ps1 -GPU         # GPU 模式（CUDA + FlagEmbedding），首次部署
.\scripts\package-amd64.ps1 -SkipInfra   # 远程模式，更新应用
.\scripts\package-amd64.ps1 -GPU -SkipInfra  # GPU 模式，更新应用

# === ARM64 服务器 ===
.\scripts\package-arm64.ps1              # 远程模式，首次部署
.\scripts\package-arm64.ps1 -GPU         # 本地模型（CPU + FlagEmbedding），首次部署
.\scripts\package-arm64.ps1 -SkipInfra   # 远程模式，更新应用
.\scripts\package-arm64.ps1 -GPU -SkipInfra  # 本地模型，更新应用
```

**macOS / Linux (Shell)：**
```bash
cd /path/to/aladdin

# === AMD64 服务器 ===
./scripts/package-amd64.sh               # 远程模式，首次
./scripts/package-amd64.sh --skip-infra  # 远程模式，更新

# === ARM64 服务器 ===
./scripts/package-arm64.sh               # 远程模式，首次
./scripts/package-arm64.sh --skip-infra  # 远程模式，更新
```

### 参数说明

| 参数 | 作用 |
|------|------|
| 无参数 | 远程模式 + 含中间件（首次部署） |
| `-GPU` | 本地跑模型（AMD64=CUDA, ARM64=CPU） |
| `-SkipInfra` | 跳过中间件（更新应用时用） |

### 打包模型文件（-GPU 模式需要）

```powershell
$HF_CACHE = "$env:USERPROFILE\.cache\huggingface\hub"
tar -czf models.tar.gz -C $HF_CACHE models--BAAI--bge-m3 models--BAAI--bge-reranker-v2-m3
```

不区分架构，通用。

### 输出目录

```
deploy-amd64/ 或 deploy-arm64/
├── app.tar           ← 应用镜像
├── infra.tar         ← 中间件镜像（首次才有）
├── docker-compose.yml
└── .env.example
```

---

## 二、服务器部署

### 首次部署

```bash
mkdir -p /opt/aladdin && cd /opt/aladdin

# 加载镜像
docker load -i infra.tar
docker load -i app.tar

# 如果是 -GPU 模式，解压模型
mkdir -p /opt/models
tar -xzf models.tar.gz -C /opt/models

# 配置
cp .env.example .env
vim .env

# 启动
docker compose up -d
```

### 更新应用

```bash
cd /opt/aladdin
docker load -i app.tar
docker compose up -d --force-recreate backend frontend
docker image prune -f   # 清理旧镜像
```

---

## 三、.env 配置

### 远程模式

```env
EMBED_PROVIDER=remote
EMBED_BASE_URL=http://Embedding服务地址/v1
EMBED_MODEL=模型名
RERANK_PROVIDER=remote
RERANK_BASE_URL=http://Rerank服务地址/v1
RERANK_MODEL=模型名
```

### GPU 模式（AMD64 + NVIDIA）

```env
EMBED_PROVIDER=flag-embedding
EMBED_MODEL=BAAI/bge-m3
EMBED_DEVICE=cuda
RERANK_PROVIDER=flag-embedding
RERANK_MODEL=BAAI/bge-reranker-v2-m3
RERANK_DEVICE=cuda
MODEL_DIR=/opt/models
```

### 本地模型模式（ARM64 CPU）

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
docker compose ps                                       # 查看状态
docker compose logs backend -f                          # 后端日志
docker compose restart backend                          # 重启后端
docker compose down                                     # 停止（数据保留）
docker compose down -v                                  # 清除所有数据（慎用）
docker compose up -d --force-recreate backend frontend  # 更新后重启
docker image prune -f                                   # 清理旧镜像
```

---

## 六、本地开发

```powershell
cd C:\newHLSWorkspace\aladdin

# 启动中间件
docker compose up -d

# 激活环境 + 启动后端
conda activate aladdin
cd backend
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# 启动前端（新终端）
cd C:\newHLSWorkspace\aladdin\frontend
npm run dev
```

**本地 .env（Windows）：**
```env
EMBED_PROVIDER=sentence-transformers
EMBED_DEVICE=cpu
RERANK_PROVIDER=sentence-transformers
RERANK_DEVICE=cpu
```

**本地 .env（macOS）：**
```env
EMBED_PROVIDER=flag-embedding
EMBED_DEVICE=mps
RERANK_PROVIDER=flag-embedding
RERANK_DEVICE=mps
```

> Python 版本必须 3.12（3.13+ 有兼容问题）。

---

## 七、模式速查

| 场景 | 打包命令 | .env 关键配置 |
|------|---------|--------------|
| AMD64 + 远程模型 | `package-amd64.ps1` | `EMBED_PROVIDER=remote` |
| AMD64 + GPU 本地模型 | `package-amd64.ps1 -GPU` + `models.tar.gz` | `EMBED_PROVIDER=flag-embedding` + `EMBED_DEVICE=cuda` + `MODEL_DIR=/opt/models` |
| ARM64 + 远程模型 | `package-arm64.ps1` | `EMBED_PROVIDER=remote` |
| ARM64 + CPU 本地模型 | `package-arm64.ps1 -GPU` + `models.tar.gz` | `EMBED_PROVIDER=flag-embedding` + `EMBED_DEVICE=cpu` + `MODEL_DIR=/opt/models` |
| 更新应用 | 加 `-SkipInfra` | 不变 |
