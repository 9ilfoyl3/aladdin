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
EMBED_PROVIDER=sentence-transformers
EMBED_MODEL=BAAI/bge-m3
EMBED_DEVICE=cpu
RERANK_PROVIDER=sentence-transformers
RERANK_MODEL=BAAI/bge-reranker-v2-m3
RERANK_DEVICE=cpu

# 或使用远程 Embedding/Rerank 服务（无需本地模型）：
# EMBED_PROVIDER=remote
# EMBED_BASE_URL=http://embedding-server:8080/v1
# EMBED_MODEL=model-name
# EMBED_API_KEY=your-token
# RERANK_PROVIDER=remote
# RERANK_BASE_URL=http://rerank-server:8001/ranking_score
# RERANK_MODEL=
# RERANK_API_KEY=

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

可选外部服务:
  └─ OCR 服务 (如 http://10.30.1.2:8909/parse) ← 图片/扫描件识别
```

## 支持的文件格式

| 格式 | 处理方式 |
|------|---------|
| pdf | PyMuPDF 提取文本，空文本自动走 OCR |
| docx | python-docx |
| xlsx | openpyxl |
| pptx | python-pptx |
| txt/md | 直接读取 |
| jpg/jpeg/png | OCR 服务识别（需配置 OCR 服务） |

## 检索模式

对话时由用户选择，不绑定知识库：

| 前端显示 | 实际模式 | 说明 |
|---------|---------|------|
| 智能检索（默认） | agent | Router 自动判断复杂度，简单走 hybrid，复杂走迭代 |
| 快速检索 | hybrid | 直接混合检索，跳过 Router |

## Agent 节点模型配置

可在前端"模型管理"页面为 Agent 各节点配置独立 LLM：

| 节点 | 推荐模型 | 作用 |
|------|---------|------|
| Router | 轻量模型 | 判断 simple/complex |
| Rewriter | 轻量模型 | 查询改写 |
| Reflector | 轻量模型 | 评估检索质量 |
| 最终回答 | 强模型（对话选择的模型） | 生成回答 |

不配置时，所有节点使用对话选择的模型。

## 内网全 Docker 化部署

### 方式一：使用 PowerShell 打包脚本（旧方式）

在有网机器上执行打包脚本：

```powershell
cd C:\newHLSWorkspace\aladdin
.\scripts\prepare-offline.ps1
```

生成 `deploy-package/` 目录，拷贝到内网服务器后执行：

```bash
chmod +x deploy-intranet.sh
./deploy-intranet.sh
```

### 方式二：使用 Makefile 打包（推荐）

Windows 上需要安装 Make（通过 `choco install make` 或 WSL2），然后使用与 macOS 相同的命令：

```powershell
# 远程 Embedding 模式（不含 ML 依赖，镜像约 500MB）
make docker-package-arm-update      # ARM64 目标服务器
make docker-package-amd64-update    # AMD64 目标服务器

# 本地模型模式（含 CPU 版 PyTorch，镜像约 1.5GB）
make docker-build-arm-ml            # ARM64
make docker-build-amd64-ml          # AMD64

# 首次完整部署（服务 + 模型 + 基础设施镜像）
make docker-package-arm             # ARM64
make docker-package-amd64           # AMD64
```

### Dockerfile 构建参数

后端 Dockerfile 通过 `INSTALL_ML` 参数控制是否安装 ML 依赖：

```powershell
# 远程模式（轻量，约 500MB，适用于有独立 Embedding/Rerank 服务的环境）
docker build -t aladdin-backend:latest -f backend/Dockerfile backend/

# 本地模型模式（含 CPU 版 PyTorch，约 1.5GB）
docker build --build-arg INSTALL_ML=true -t aladdin-backend:latest -f backend/Dockerfile backend/
```

### Embedding/Rerank 配置

系统支持本地模型和远程服务两种模式，可通过环境变量设置初始默认值，也可在前端 **Embedding** 页面动态切换。

**远程服务示例（`.env`）：**
```env
# Embedding：OpenAI 兼容接口，地址填到 /v1
EMBED_PROVIDER=remote
EMBED_BASE_URL=http://10.30.1.4:8902/v1
EMBED_MODEL=Qwen3-Embedding-0.6B
EMBED_API_KEY=your-token

# Rerank：自定义接口，地址填完整端点
RERANK_PROVIDER=remote
RERANK_BASE_URL=http://10.30.1.3:8001/ranking_score
RERANK_MODEL=
RERANK_API_KEY=
```

**本地模型（`.env`）：**
```env
EMBED_PROVIDER=sentence-transformers
EMBED_MODEL=BAAI/bge-m3
EMBED_DEVICE=cpu                    # 有 GPU 改为 cuda

RERANK_PROVIDER=sentence-transformers
RERANK_MODEL=BAAI/bge-reranker-v2-m3
RERANK_DEVICE=cpu
```

#### 远程服务地址填写规则

| 接口类型 | 地址填写方式 | 示例 |
|---------|-------------|------|
| OpenAI 兼容（TEI/Infinity/vLLM） | 填到 `/v1`，系统自动拼接 `/embeddings` 或 `/rerank` | `http://server:8080/v1` |
| 自定义接口 | 填完整端点路径 | `http://server:8001/ranking_score` |

#### Provider 选择

| Provider | 特点 | 推荐场景 |
|----------|------|---------|
| `sentence-transformers` | 跨平台兼容，稀疏向量为 BM25 近似 | 本地开发、Windows |
| `flag-embedding` | 原生稠密+稀疏向量，检索质量更高 | 生产环境、有 GPU |
| `remote` | 调用外部 API，不加载本地模型 | 有独立 Embedding/Rerank 服务 |

生产环境默认使用 `flag-embedding` provider + `cuda` 设备（本地模型模式），或 `remote` provider（远程服务模式）。
