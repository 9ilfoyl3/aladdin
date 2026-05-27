# Aladdin 本地开发指南（Windows）

适用于 Windows + Docker Desktop + 远程 LLM/Embedding/Rerank 服务的开发环境搭建。

---

## 环境要求

| 依赖 | 版本 | 说明 |
|------|------|------|
| Docker Desktop | 最新版 | 运行 Milvus + PostgreSQL + Redis |
| Conda (Anaconda/Miniconda) | 最新版 | 管理 Python 环境 |
| Node.js | 18+ | 前端构建 |
| LLM API Key | - | DeepSeek / 通义 / OpenAI 等 |
| Embedding 服务 | - | TEI / Infinity / vLLM 等（可选，启动后在前端配置） |

**硬件要求：** 8GB+ 内存，无需显卡（所有 AI 推理由远程服务承担）

---

## 第一步：启动基础设施

```powershell
cd C:\your-workspace\aladdin
docker compose up -d
```

验证：
```powershell
docker compose ps
# 应看到 etcd、minio、milvus、postgres、redis 状态为 running
```

---

## 第二步：创建 Python 环境

> ⚠️ **必须使用 Python 3.12**，3.13+ 存在兼容问题。

```powershell
conda create -n aladdin python=3.12 -y
conda activate aladdin
```

---

## 第三步：安装依赖

```powershell
# 后端
pip install --upgrade pip
pip install -r backend/requirements-base.txt

# 前端
cd frontend
npm install
cd ..
```

---

## 第四步：配置环境变量

```powershell
cp backend/.env.example backend/.env
```

编辑 `backend/.env`：

```env
# 数据库
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/aladdin

# Milvus
MILVUS_HOST=localhost
MILVUS_PORT=19530

# Redis
REDIS_URL=redis://localhost:6379/0

# LLM（必填）
LLM_PROVIDER=vllm
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
LLM_API_KEY=sk-your-key

# Embedding 远程服务（可选，也可启动后在前端配置）
EMBED_BASE_URL=http://your-embedding-server/v1
EMBED_MODEL=BAAI/bge-m3
EMBED_API_KEY=

# Rerank 远程服务（可选，也可启动后在前端配置）
RERANK_BASE_URL=http://your-rerank-server/v1
RERANK_MODEL=BAAI/bge-reranker-v2-m3
RERANK_API_KEY=
```

### LLM 厂商配置参考

| 厂商 | LLM_BASE_URL | LLM_MODEL |
|------|-------------|-----------|
| DeepSeek | https://api.deepseek.com/v1 | deepseek-chat |
| 通义千问 | https://dashscope.aliyuncs.com/compatible-mode/v1 | qwen-plus |
| 火山引擎 | https://ark.cn-beijing.volces.com/api/v3 | 推理接入点 ID |
| OpenAI | https://api.openai.com/v1 | gpt-4o |
| 智谱 GLM | https://open.bigmodel.cn/api/paas/v4 | glm-4-flash |

---

## 第五步：启动服务

```powershell
# 终端 1：API 服务
cd backend
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# 终端 2：Worker（文档处理）
cd backend
python -m app.worker_main

# 终端 3：前端
cd frontend
npm run dev
```

首次启动会自动在 PostgreSQL 中创建表结构。

---

## 第六步：验证

1. 浏览器打开 http://localhost:5173
2. **模型管理** → 添加 LLM 配置 → 测试连通性 → 设为默认
3. **Embedding 配置** → 确认远程服务连通
4. **知识库** → 创建 → 上传文档 → 等待处理完成
5. **对话** → 选择知识库 → 提问

---

## 日常启动

```powershell
# 1. 确保 Docker 容器运行
docker compose ps
# 如果没运行：docker compose up -d

# 2. 激活环境，启动后端
conda activate aladdin
cd backend
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# 3. 新终端，启动 Worker
cd backend
python -m app.worker_main

# 4. 新终端，启动前端
cd frontend
npm run dev
```

---

## 停止服务

```powershell
# 前端/后端：Ctrl+C

# 停止 Docker（数据保留）
docker compose down

# 彻底清除数据
docker compose down -v
```

---

## 常见问题

### Q: Python 进程无输出直接退出

Python 版本过高（3.13+）。使用 3.12：`conda create -n aladdin python=3.12 -y`

### Q: `.env` 配置不生效

清理缓存后重启：
```powershell
Get-ChildItem -Recurse -Filter "__pycache__" | Remove-Item -Recurse -Force
```

### Q: PostgreSQL 连接失败

确认 Docker 容器在运行：
```powershell
docker compose ps
docker compose up -d
```

### Q: Worker 不处理文档

确认 Redis 容器正常运行，检查 Worker 终端是否有错误输出。
