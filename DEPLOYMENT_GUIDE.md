# Aladdin 本地部署指南（Windows）

基于实际部署经验总结，适用于 Windows + Docker Desktop + 线上 LLM 的场景。

---

## 前置要求

| 依赖 | 版本 | 说明 |
|------|------|------|
| Docker Desktop | 最新版 | 运行 Milvus + PostgreSQL |
| Conda (Anaconda/Miniconda) | 最新版 | 管理 Python 环境 |
| Node.js | 18+ | 前端构建 |
| 线上 LLM API Key | - | DeepSeek / 通义 / 火山引擎等 |

**硬件要求（CPU 模式）：** 8GB+ 内存，无需显卡  
**硬件要求（GPU 模式）：** NVIDIA 显卡 + 驱动 + 8GB+ 显存

---

## 第一步：启动基础设施

```powershell
cd C:\newHLSWorkspace\aladdin
docker compose up -d
```

验证所有容器正常运行：

```powershell
docker compose ps
```

应看到 etcd、minio、milvus、postgres 四个容器状态为 running。

验证 PostgreSQL：
```powershell
docker exec -it aladdin-postgres-1 psql -U postgres -d aladdin -c "SELECT 1"
```

验证 Milvus：
```powershell
curl http://localhost:9091/healthz
```

---

## 第二步：创建 Python 环境

> ⚠️ **关键：必须使用 Python 3.12**。Python 3.13+ 的 tokenizers 库存在 segfault 问题。

```powershell
# 如果 conda 提示接受服务条款，按提示执行 conda tos accept 命令
conda create -n aladdin python=3.12 -y
conda activate aladdin
```

验证：
```powershell
python --version
# 应输出 Python 3.12.x
```

---

## 第三步：安装依赖

```powershell
cd C:\newHLSWorkspace\aladdin

# 升级 pip
python -m pip install --upgrade pip

# 安装 PyTorch
# 有 NVIDIA 显卡 + 驱动：
pip install torch --index-url https://download.pytorch.org/whl/cu124
# 没有显卡：
# pip install torch --index-url https://download.pytorch.org/whl/cpu

# 安装项目依赖
pip install -r backend/requirements.txt
```

---

## 第四步：下载 Embedding 和 Rerank 模型

模型约 3GB，首次需要从 HuggingFace 下载。

```powershell
cd C:\newHLSWorkspace\aladdin\backend

# 如果能直连 HuggingFace（有代理/VPN）：
python -c "from sentence_transformers import SentenceTransformer; m = SentenceTransformer('BAAI/bge-m3'); print('bge-m3 OK')"
python -c "from sentence_transformers import CrossEncoder; m = CrossEncoder('BAAI/bge-reranker-v2-m3'); print('reranker OK')"

# 如果需要镜像加速：
# $env:HF_ENDPOINT="https://hf-mirror.com"
# 然后执行上面两条命令

# 如果镜像也不行，设置代理：
# $env:HTTP_PROXY="http://127.0.0.1:7890"
# $env:HTTPS_PROXY="http://127.0.0.1:7890"
# 然后执行上面两条命令
```

两条都输出 OK 后，模型已缓存到本地，后续启动不再需要网络。

---

## 第五步：配置环境变量

编辑 `backend/.env` 文件：

```env
# 数据库（Docker 里的 PostgreSQL）
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/aladdin

# Milvus（Docker 里的）
MILVUS_HOST=localhost
MILVUS_PORT=19530

# LLM → 线上模型（以下为示例，替换为你的配置）
LLM_PROVIDER=vllm
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
LLM_API_KEY=sk-你的key

# Embedding 和 Rerank → 本地模型
# 没有 NVIDIA 显卡或未装驱动用 cpu，有显卡用 cuda
EMBED_MODEL=BAAI/bge-m3
EMBED_DEVICE=cpu
RERANK_MODEL=BAAI/bge-reranker-v2-m3
RERANK_DEVICE=cpu

# Agent 参数
AGENT_MAX_ITERATIONS=3
AGENT_TIMEOUT=30.0

# 切片参数
PARENT_CHUNK_SIZE=1500
CHILD_CHUNK_SIZE=300
CHUNK_OVERLAP=50
```

> ⚠️ **注意：** 如果 `.env` 修改后不生效，同时修改 `backend/app/config.py` 中对应字段的默认值，
> 然后删除 `__pycache__` 缓存：
> ```powershell
> Get-ChildItem -Recurse -Filter "__pycache__" | Remove-Item -Recurse -Force
> ```

### 各厂商 LLM 配置示例

| 厂商 | LLM_BASE_URL | LLM_MODEL |
|------|-------------|-----------|
| DeepSeek | https://api.deepseek.com/v1 | deepseek-chat |
| 通义千问 | https://dashscope.aliyuncs.com/compatible-mode/v1 | qwen-plus |
| 火山引擎 | https://ark.cn-beijing.volces.com/api/v3 | 你的推理接入点ID |
| OpenAI | https://api.openai.com/v1 | gpt-4o |
| 智谱 GLM | https://open.bigmodel.cn/api/paas/v4 | glm-4-flash |

---

## 第六步：启动后端

```powershell
cd C:\newHLSWorkspace\aladdin\backend
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

首次启动会自动在 PostgreSQL 中创建表。看到以下输出表示成功：

```
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8000
```

验证：浏览器打开 http://localhost:8000 ，看到：
```json
{"message": "Agentic RAG System is running"}
```

---

## 第七步：启动前端

新开一个终端窗口：

```powershell
cd C:\newHLSWorkspace\aladdin\frontend
npm install
npm run dev
```

看到 `Local: http://localhost:3000/` 表示成功。

---

## 第八步：验证完整流程

1. 浏览器打开 http://localhost:3000
2. **模型管理** → 添加 LLM 配置 → 测试连通性 → 设为默认
3. **知识库** → 创建知识库
4. 进入知识库 → 上传文档（PDF/DOCX/TXT 等）
5. 等待文档状态变为 completed
6. **对话** → 选择知识库 → 提问

---

## 日常启动流程

每次开发/使用时：

```powershell
# 1. 确保 Docker 容器在运行
docker compose ps
# 如果没运行：docker compose up -d

# 2. 激活环境，启动后端
conda activate aladdin
cd C:\newHLSWorkspace\aladdin\backend
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# 3. 新终端，启动前端
cd C:\newHLSWorkspace\aladdin\frontend
npm run dev
```

---

## 停止服务

```powershell
# 前端/后端：在对应终端按 Ctrl+C

# 停止 Docker 容器（数据会保留）
cd C:\newHLSWorkspace\aladdin
docker compose down

# 如果要彻底清除数据（包括数据库和向量）
docker compose down -v
```

---

## 切换到 GPU 模式

前提：已安装 NVIDIA 显卡驱动（`nvidia-smi` 能执行）。

1. 修改 `backend/.env`：
   ```env
   EMBED_DEVICE=cuda
   RERANK_DEVICE=cuda
   ```

2. 同步修改 `backend/app/config.py` 中的默认值（如果 .env 不生效）

3. 清缓存重启：
   ```powershell
   Get-ChildItem -Recurse -Filter "__pycache__" | Remove-Item -Recurse -Force
   python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```

GPU 模式下 Embedding 和 Rerank 速度提升 3-5 倍。

---

## 常见问题

### Q: Python 进程无输出直接退出

**原因：** Python 版本过高（3.13+），tokenizers 库 segfault。  
**解决：** 使用 Python 3.12：`conda create -n aladdin python=3.12 -y`

### Q: `RuntimeError: Found no NVIDIA driver`

**原因：** 配置了 `EMBED_DEVICE=cuda` 但没有 NVIDIA 驱动。  
**解决：** 改为 `cpu`，或安装 NVIDIA 驱动。

### Q: 模型下载失败 / 连不上 HuggingFace

**解决：** 设置代理或镜像：
```powershell
$env:HTTP_PROXY="http://127.0.0.1:7890"
$env:HTTPS_PROXY="http://127.0.0.1:7890"
```

### Q: `.env` 配置不生效

**原因：** pydantic-settings 的 `.env` 加载路径问题 + `__pycache__` 缓存。  
**解决：** 直接修改 `backend/app/config.py` 中的默认值，然后：
```powershell
Get-ChildItem -Recurse -Filter "__pycache__" | Remove-Item -Recurse -Force
```

### Q: `ModelManager has no attribute 'llm'`

**原因：** 数据库中没有配置 LLM，且代码回退逻辑有 bug。  
**解决：** 在前端"模型管理"页面添加一个 LLM 配置并设为默认。

### Q: PostgreSQL 连接失败

**解决：** 确认 Docker 容器在运行：
```powershell
docker compose ps
docker compose up -d  # 如果没运行
```

---

## 架构总览

```
Docker Desktop:
  ├─ PostgreSQL (端口 5432) ← 元数据存储
  ├─ Milvus    (端口 19530) ← 向量存储
  ├─ etcd                    ← Milvus 依赖
  └─ MinIO                   ← Milvus 依赖

本地进程:
  ├─ Python 后端 (端口 8000) ← FastAPI + Embedding/Rerank 模型
  └─ Node 前端   (端口 3000) ← React 管理界面

线上服务:
  └─ LLM API (DeepSeek/通义/火山等) ← 对话生成
```
