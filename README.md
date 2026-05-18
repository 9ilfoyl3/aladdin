# Agentic RAG 知识库系统

基于 Agent 编排的 RAG（检索增强生成）知识库问答系统。通过路由判定、查询改写、迭代检索与反思机制，实现比传统 RAG 更精准的知识检索与回答生成。

## 快速开始

### 前置要求

| 依赖 | 版本要求 | 说明 |
|------|----------|------|
| Python | 3.12+ | 推荐 3.12 或 3.14 |
| Node.js | 18+ | 前端构建，推荐 LTS 版本 |
| Docker | 20.10+ | 用于运行 Milvus 向量数据库 |
| Git | 最新 | 代码管理 |

### 1. 克隆项目

```bash
git clone <repo-url>
cd aladdin
```

### 2. 环境配置（按平台）

#### macOS（Intel / Apple Silicon）

```bash
# 安装 Python（如未安装）
brew install python@3.12

# 安装 Node.js（如未安装）
brew install node

# 安装 Docker Desktop
# 下载：https://www.docker.com/products/docker-desktop/
# 安装后启动 Docker Desktop

# 创建虚拟环境并安装后端依赖
python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r backend/requirements.txt

# 安装前端依赖
cd frontend && npm install && cd ..
```

环境变量关键配置：
```bash
EMBED_PROVIDER=sentence-transformers   # 或 flag-embedding（需额外安装 FlagEmbedding）
EMBED_DEVICE=cpu                       # macOS 不支持 CUDA
RERANK_PROVIDER=sentence-transformers  # 或 flag-embedding
RERANK_DEVICE=cpu
```

#### Windows

推荐两种方式：

**方式 A：WSL2（推荐，体验与 Linux 一致）**

```powershell
# 1. 安装 WSL2
wsl --install

# 2. 在 WSL2 中按 Linux 步骤操作（见下方）
```

**方式 B：原生 Windows**

```powershell
# 安装 Python 3.12+（从 https://www.python.org/downloads/ 下载）
# 安装时勾选 "Add Python to PATH"

# 安装 Node.js（从 https://nodejs.org/ 下载 LTS 版本）

# 安装 Docker Desktop for Windows
# 下载：https://www.docker.com/products/docker-desktop/

# 创建虚拟环境
python -m venv .venv
.venv\Scripts\pip install --upgrade pip
.venv\Scripts\pip install -r backend\requirements.txt

# 安装前端依赖
cd frontend
npm install
cd ..
```

环境变量关键配置：
```bash
EMBED_PROVIDER=sentence-transformers   # Windows 必须用此选项，flag-embedding 兼容性差
EMBED_DEVICE=cpu                       # 无 NVIDIA GPU 时用 cpu
RERANK_PROVIDER=sentence-transformers  # Windows 必须用此选项
RERANK_DEVICE=cpu
```

> ⚠️ Windows 上 `FlagEmbedding` 库安装困难（peft、accelerate 等依赖编译问题），Embedding 和 Rerank 均请使用 `sentence-transformers` provider。

#### Linux（有 NVIDIA GPU）

```bash
# 安装 Python
sudo apt install python3.12 python3.12-venv

# 安装 Node.js（通过 NodeSource）
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs

# 安装 Docker
# 参考：https://docs.docker.com/engine/install/ubuntu/

# 创建虚拟环境并安装依赖
python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r backend/requirements.txt

# 安装前端依赖
cd frontend && npm install && cd ..
```

环境变量关键配置：
```bash
EMBED_PROVIDER=sentence-transformers   # 或 flag-embedding（GPU 环境推荐，支持稀疏向量）
EMBED_DEVICE=cuda                      # 有 GPU 时用 cuda
RERANK_PROVIDER=sentence-transformers  # 或 flag-embedding
RERANK_DEVICE=cuda
```

### 3. 下载模型（必须）

项目运行在 HuggingFace 离线模式（`HF_HUB_OFFLINE=1`），不会自动联网下载模型。启动前必须将模型下载到本地缓存目录。

```bash
# 方式一：使用 Makefile 命令（推荐）
make download-models

# 方式二：国内网络使用镜像源
make download-models-cn

# 方式三：手动指定本地路径（已有模型文件时）
# 在 .env 中直接指向本地目录：
# EMBED_MODEL=/path/to/local/bge-m3
# RERANK_MODEL=/path/to/local/bge-reranker-v2-m3
```

模型默认下载到 `~/.cache/huggingface/hub/` 目录，代码通过模型名称自动定位，无需额外配置路径。

> ⚠️ bge-m3 约 2.2GB，bge-reranker-v2-m3 约 2.2GB，请确保磁盘空间充足。

### 4. 配置环境变量

```bash
cp backend/.env.example backend/.env
```

编辑 `backend/.env`，必须配置的项：

```bash
# LLM 配置（必填，选一种）
# 方式一：本地 Ollama
LLM_PROVIDER=ollama
LLM_BASE_URL=http://localhost:11434
LLM_MODEL=qwen2.5:7b

# 方式二：远端 API（OpenAI 兼容格式）
LLM_PROVIDER=vllm
LLM_BASE_URL=https://your-api-endpoint
LLM_MODEL=your-model-name
LLM_API_KEY=your-api-key

# Embedding Provider（根据平台选择）
EMBED_PROVIDER=sentence-transformers   # 跨平台兼容，推荐本地开发
# EMBED_PROVIDER=flag-embedding        # 支持稀疏向量，macOS/Linux 可用，Windows 不推荐

# Rerank Provider（根据平台选择）
RERANK_PROVIDER=sentence-transformers  # 跨平台兼容，推荐本地开发
# RERANK_PROVIDER=flag-embedding       # FlagReranker，macOS/Linux 可用，Windows 不推荐
```

### 5. 启动基础设施

```bash
# 启动 Milvus 向量数据库（etcd + minio + milvus）
make infra

# 验证 Milvus 是否就绪
curl http://localhost:9091/healthz
```

### 6. 启动服务

```bash
# 同时启动前后端
make dev
```

或分别启动：

```bash
# 终端 1：后端
make dev-backend

# 终端 2：前端
make dev-frontend
```

启动成功后访问：
- 前端界面：http://localhost:5173
- 后端 API：http://localhost:8000
- API 文档：http://localhost:8000/docs

### Embedding / Rerank Provider 选择指南

| 配置项 | `sentence-transformers` | `flag-embedding` |
|--------|------------------------|------------------|
| 跨平台 | ✅ Mac / Windows / Linux | ⚠️ Mac / Linux 可用，Windows 困难 |
| Embedding 稠密向量 | ✅ 支持 | ✅ 支持 |
| Embedding 稀疏向量 | ❌ 占位值（不影响运行） | ✅ 原生 lexical weights |
| Rerank 精排 | ✅ CrossEncoder | ✅ FlagReranker |
| 混合检索效果 | 仅稠密检索生效，稀疏部分不贡献召回 | 稠密+稀疏双路召回，效果最佳 |
| 安装难度 | 低，pip install 即可 | 中，依赖 peft/accelerate 等 |
| 推荐场景 | 本地开发、Windows 环境、快速验证 | 生产部署、追求最佳检索效果 |

两种 provider 使用相同的模型文件（`BAAI/bge-m3` 和 `BAAI/bge-reranker-v2-m3`），切换时无需重新下载模型，只需修改 `EMBED_PROVIDER` 和 `RERANK_PROVIDER` 配置即可。

### 常用命令

```bash
make install          # 安装所有依赖（后端 + 前端）
make download-models  # 下载模型到本地（必须，离线模式）
make download-models-cn  # 通过国内镜像下载模型
make dev              # 启动前后端开发服务
make dev-backend      # 仅启动后端
make dev-frontend     # 仅启动前端
make infra            # 启动 Milvus
make infra-down       # 停止 Milvus
make test             # 运行后端测试
make clean            # 清理缓存
```

### 常见问题

**Q: 启动后端报 `OSError: We couldn't connect to 'https://huggingface.co'` 或模型加载失败**

A: 项目强制离线模式运行，模型必须提前下载。执行 `make download-models`（国内用 `make download-models-cn`）下载模型到本地缓存。

**Q: 启动后端报 `ModuleNotFoundError: No module named 'FlagEmbedding'`**

A: 你的 `EMBED_PROVIDER` 设置为 `flag-embedding` 但未安装 FlagEmbedding 库。解决方式二选一：
- 改为 `EMBED_PROVIDER=sentence-transformers`（推荐）
- 安装 FlagEmbedding：`.venv/bin/pip install FlagEmbedding`

**Q: Windows 上安装 FlagEmbedding 失败**

A: 这是已知问题，FlagEmbedding 的依赖（peft、accelerate）在 Windows 上编译困难。请将 Embedding 和 Rerank 都设为 `sentence-transformers`：
```bash
EMBED_PROVIDER=sentence-transformers
RERANK_PROVIDER=sentence-transformers
```
功能完全正常，仅稀疏检索部分不生效。无需 WSL2。

**Q: 模型下载超时或失败**

A: 国内网络访问 HuggingFace 不稳定，使用镜像：
```bash
make download-models-cn
```
或手动下载后在 `.env` 中指定本地路径。

**Q: Milvus 启动失败**

A: 确认 Docker 已启动，且端口 19530 未被占用：
```bash
docker ps                          # 查看容器状态
docker compose logs milvus         # 查看 Milvus 日志
lsof -i :19530                     # 检查端口占用（macOS/Linux）
```

**Q: `sentence-transformers` 模式下混合检索效果是否受影响？**

A: 稠密检索正常工作，稀疏检索部分返回占位值不贡献实际召回。对于大多数场景，稠密检索 + Rerank 已经能提供很好的效果。如需完整的混合检索能力，切换到 `flag-embedding` 即可。

## 整体架构

```
┌─────────────────────────────────────────────────────────────┐
│                        接入层                                │
│   Chat API (OpenAI 兼容)  │  Admin API (RESTful)            │
├─────────────────────────────────────────────────────────────┤
│                     Agent 编排层                             │
│   QueryRouter → QueryRewriter → Executor → Reflector        │
├─────────────────────────────────────────────────────────────┤
│                      检索工具层                              │
│   VectorRetriever │ SparseRetriever │ HybridRetriever       │
│                      Reranker                               │
├─────────────────────────────────────────────────────────────┤
│                    索引/存储层                               │
│   Milvus (稠密+稀疏向量)  │  SQLite (元数据)                 │
├─────────────────────────────────────────────────────────────┤
│                    数据处理层                                │
│   Loader → Chunker → Enricher → Embedder → Indexer         │
├─────────────────────────────────────────────────────────────┤
│                    模型抽象层                                │
│   LLMProvider  │  EmbedProvider  │  RerankProvider          │
└─────────────────────────────────────────────────────────────┘
```

## 核心流程

### 文档处理管道

```
上传文件 → Loader 解析（PDF/DOCX/XLSX/PPTX/TXT/MD）
         → 文本为空时自动触发 OCR（支持多 Provider + Fallback）
         → Chunker 结构感知切分（父块 1500 字 / 子块 300 字，表格整块保护）
         → Embedder 生成稠密向量(1024维) + 稀疏向量
         → 写入 Milvus（向量）+ SQLite（元数据）
```

切片策略采用**结构感知的父子 chunk 切分**：
- 优先识别文档结构标记（条款编号、法律文书关键词、Markdown 标题等）按逻辑段落切分
- 无结构标记时回退到段落边界切分
- 子块用于精准检索（语义集中），父块用于上下文返回（信息完整）
- 子块切分同样感知结构标记，确保每个子块是一个完整的逻辑单元
- HTML 表格（`<table>...</table>`）整块保护，不会被切断到两个 chunk 中
- 识别 VL 模型特有标记（`[Non-Text]`、`[Image]` 等）作为分段点

**为什么这样设计：**

传统 RAG 按固定字符数切分，容易把一个完整的逻辑段落（如"关于误工费的反驳"）切成两半，导致 embedding 向量表示的是混合语义，检索精度下降。结构感知切分保证每个子块是一个独立的语义单元，embedding 精确表示该主题，检索命中率更高。

**不会丢失召回：**

- 检索命中子块后，通过父子映射返回完整的父块内容，LLM 获得充分上下文
- 跨段落的复杂问题由 Agent 模式处理——查询改写生成多个子查询，迭代检索命中多个子块，合并返回多个父块
- 结构切分 + 父块扩展 + Agent 迭代三者配合，召回率和精度同时提升

### 三档检索模式

| 模式 | 流程 | 适用场景 |
|------|------|----------|
| direct | 稠密向量 ANN 检索 | 简单查询、低延迟 |
| hybrid | 稠密+稀疏并行 → RRF 融合 → Rerank 精排 → 父块扩展 | 通用场景 |
| agent | 路由判定 → 查询改写 → 迭代检索+反思（最多3轮） | 复杂多跳查询 |

**相比传统 RAG 的核心差异：**

| 能力 | 传统 RAG | 本系统 |
|------|---------|--------|
| 切片方式 | 固定字符数切分 | 结构感知切分，保持逻辑完整性 |
| 检索方式 | 单次向量检索 | 稠密+稀疏混合检索 + RRF 融合 + Rerank 精排 |
| 查询理解 | 原始 query 直接检索 | LLM 路由判定 + 查询改写（多角度检索） |
| 迭代能力 | 无 | 检索→反思→补充检索，最多 3 轮迭代 |
| 上下文返回 | 返回命中的小块 | 子块命中后扩展为父块，上下文完整 |
| 容错能力 | 无 | 多级降级（Agent异常→hybrid→纯检索） |
| 性能优化 | 无 | 查询去重、分数快判减少 60% LLM 调用、批量 Rerank 消除锁争用 |

### Agent 编排流程（agent 模式）

```
用户查询
  │
  ├─ Router + Rewriter 并行执行
  │    ├─ Router 判定 simple → 取消改写，直接走 hybrid 快路径
  │    └─ Router 判定 complex → 等待改写结果 ↓
  │
  ├─ Executor 并行检索
  │    ├─ 查询级去重（embedding cosine similarity > 0.92 跳过）
  │    ├─ 子查询跳过 rerank（纯向量+RRF，完全并行无锁）
  │    └─ 合并去重后统一 rerank + 父块扩展（只调一次，消除锁争用）
  │
  ├─ Reflector 两级评估
  │    ├─ 快速判定（无 LLM）：top-3 均分 ≥ 0.7 → 充分 / top-5 均分 < 0.3 → 不充分
  │    ├─ LLM 深度评估：分数中间地带，多维度评分（相关性/覆盖度/一致性）
  │    ├─ 充分 → 返回结果
  │    ├─ 覆盖度增幅 < 10% → 提前终止（继续迭代无意义）
  │    └─ 不充分 → 生成追加查询，回到 Executor（最多 3 轮）
  │
  └─ 异常 → 降级到 hybrid 快路径
```

### 容错降级机制

- **Agent 异常降级**：编排过程中任何异常自动回退到 hybrid 检索
- **LLM 不可用降级**：流式生成失败时直接返回检索到的原文
- **Reranker 异常降级**：跳过重排序，返回 RRF 融合结果
- 响应中 `metadata.degraded` 字段标识是否发生降级，`metadata.llm_degraded` 标识 LLM 是否降级

## 技术选型

| 组件 | 选型 | 原因 |
|------|------|------|
| Web 框架 | FastAPI | 原生异步、自动 OpenAPI 文档、高性能 |
| 向量数据库 | Milvus | 同时支持稠密+稀疏向量、HNSW 索引、生产级可靠性 |
| 元数据存储 | SQLite + aiosqlite | 零配置、异步支持、适合中小规模部署 |
| Embedding | BAAI/bge-m3 | 多语言、同时输出稠密(1024维)+稀疏向量、开源免费 |
| Reranker | BAAI/bge-reranker-v2-m3 | 多语言交叉编码器、精排效果好、可本地部署 |
| LLM | Ollama / vLLM (OpenAI 兼容) | 灵活切换本地/远端模型、支持流式生成 |
| 前端 | React 18 + TypeScript + Tailwind | 类型安全、组件化、快速开发 |
| 数据请求 | TanStack Query | 自动缓存、后台刷新、乐观更新 |
| 文档解析 | PyMuPDF / python-docx / openpyxl / python-pptx | 覆盖主流办公文档格式 |
| 融合算法 | RRF (Reciprocal Rank Fusion) | 无需训练、对不同分数尺度鲁棒 |

## 支持的能力

- **多格式文档**：PDF、Word、Excel、PPT、TXT、Markdown
- **混合检索**：稠密语义检索 + 稀疏关键词检索，RRF 融合
- **智能路由**：自动判断查询复杂度，简单问题走快路径（路由与改写并行，零等待）
- **查询改写**：多策略扩展（关键词提取、假设文档生成 HyDE、视角转换），生成 2-4 个检索查询
- **查询去重**：基于 embedding 余弦相似度跨迭代去重，避免重复检索
- **迭代反思**：两级评估（分数快判 + LLM 深度评估），覆盖度增幅不足时提前终止
- **结构性碎片惩罚**：Rerank 阶段对标题/目录等无实质信息的短文本施加分数惩罚
- **多模型管理**：数据库持久化多个 LLM 配置，支持创建/编辑/删除/设为默认/连通性测试，对话时动态切换
- **OCR 服务管理**：可视化管理多个 OCR 服务（PaddleOCR/TextIn/通用API），支持默认+Fallback 自动切换，抽象基类+工厂模式易于扩展
- **Markdown 切片优化**：VL 模型返回的 Markdown 内容（含表格、标题）智能切分，表格整块保护不切断；切片预览支持 Markdown 渲染（标题、表格、列表等正确展示）
- **上下文窗口管理**：可配置每个模型的最大上下文 token 数，按 chunk 相关性智能截断，适配不同窗口大小的模型
- **流式响应**：SSE 流式输出，兼容 OpenAI API 格式，Agent 模式实时推送思考进度事件；支持按模型配置开关流式
- **引用溯源**：回答附带引用来源（文件名、子块内容、父块上下文、相关性分数）
- **API Key 认证**：SHA256 哈希存储，支持创建/撤销/调用统计，仅 `/v1/` 路径需认证
- **检索测试**：独立的检索测试页面，对比不同模式效果

## OCR 服务管理

系统支持可配置的 OCR 服务，用于处理扫描件 PDF 等无文本层的文档。通过前端管理页面（`/ocr-services`）可视化维护多个 OCR 服务配置，支持设置默认服务和 Fallback 自动切换。

### 支持的 OCR Provider

| Provider 类型 | 说明 | 配置要点 |
|--------------|------|----------|
| `paddleocr` | PaddleOCR 本地服务 | 需安装 PaddleOCR 依赖，通过 `extra_config` 配置 `lang`（语言）和 `use_gpu` |
| `textin` | 合合信息 TextIn OCR | 响应格式 `{code, message, data: [{page, content}]}`，填写 API 地址和密钥 |
| `external_api` | 通用外部 API（兼容模式） | 自动识别常见响应格式，适合快速接入未专门适配的服务 |

### 架构设计

采用抽象基类 + 多实现 + 工厂模式，每个第三方 OCR 服务对应一个独立的 Provider 类：

```
OCRProvider (抽象基类)
├── PaddleOCRProvider          # 本地 PaddleOCR
├── BaseExternalAPIProvider    # 外部 HTTP API 抽象基类（通用上传逻辑）
│   ├── TextInProvider         # TextIn OCR 适配
│   └── ExternalAPIProvider    # 通用兼容（自动识别响应格式）
└── 新增 Provider...           # 继承 BaseExternalAPIProvider 即可
```

### 接入新的 OCR 服务

1. 在 `backend/app/pipeline/ocr/` 下新建 `xxx_provider.py`
2. 继承 `BaseExternalAPIProvider`，实现 `_adapt_response` 方法解析该服务的响应格式
3. 在 `backend/app/pipeline/ocr/manager.py` 的 `_create_provider` 工厂方法中注册新类型
4. 在 `backend/app/api/ocr_config.py` 的校验逻辑中添加新的 `provider_type`
5. 在前端 `OcrServices.tsx` 的 Select 中添加选项

### 默认服务与 Fallback

- 同一时刻最多一个默认服务、一个 Fallback 服务
- 同一配置不能同时为默认和 Fallback
- 文档处理时优先使用默认服务，失败后自动切换到 Fallback 重试一次
- 数据库中无 OCR 配置时，Pipeline 正常运行（跳过 OCR 步骤）

### API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/ocr-configs` | 获取所有配置（api_key 脱敏） |
| POST | `/api/ocr-configs` | 创建配置 |
| PUT | `/api/ocr-configs/{id}` | 更新配置（部分更新） |
| DELETE | `/api/ocr-configs/{id}` | 删除配置 |
| POST | `/api/ocr-configs/test` | 临时配置连通性测试 |
| POST | `/api/ocr-configs/{id}/test` | 已保存配置连通性测试 |

## 项目结构

```
aladdin/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI 入口
│   │   ├── config.py            # 配置管理（pydantic-settings）
│   │   ├── api/                 # API 路由层
│   │   │   ├── chat.py          # Chat API（OpenAI 兼容，流式 Agent 进度推送）
│   │   │   ├── knowledge_base.py
│   │   │   ├── document.py
│   │   │   ├── retrieval.py     # 检索测试
│   │   │   ├── llm_config.py    # 多模型配置管理（CRUD + 连通性测试）
│   │   │   ├── ocr_config.py   # OCR 服务配置管理（CRUD + 连通性测试）
│   │   │   ├── api_key.py
│   │   │   ├── auth.py          # API Key 验证逻辑
│   │   │   ├── middleware.py    # 认证中间件（仅 /v1/ 路径）
│   │   │   └── system.py        # 健康检查/系统配置
│   │   ├── agent/               # Agent 编排层
│   │   │   ├── orchestrator.py  # 编排主控（路由+改写并行，迭代+反思，异常降级）
│   │   │   ├── router.py        # 查询路由（simple/complex）
│   │   │   ├── rewriter.py      # 查询改写（关键词/HyDE/视角转换）
│   │   │   ├── executor.py      # 并行检索（查询去重 + 批量 Rerank）
│   │   │   └── reflector.py     # 两级反思（分数快判 + LLM 深度评估）
│   │   ├── retrieval/           # 检索工具层
│   │   │   ├── vector.py        # 稠密向量检索
│   │   │   ├── sparse.py        # 稀疏向量检索
│   │   │   ├── hybrid.py        # 混合检索 + RRF + Rerank + 结构碎片惩罚 + 父块扩展
│   │   │   └── reranker.py      # Reranker 封装
│   │   ├── pipeline/            # 文档处理管道
│   │   │   ├── pipeline.py      # 管道编排
│   │   │   ├── chunker.py       # 结构感知切片器
│   │   │   ├── embedder.py      # 批量向量化
│   │   │   ├── enricher.py      # 富化器（预留）
│   │   │   ├── loaders/         # 文档解析器
│   │   │   └── ocr/             # OCR 服务模块
│   │   │       ├── provider.py          # 抽象基类 + 统一数据结构
│   │   │       ├── manager.py           # Provider 管理器（工厂 + fallback）
│   │   │       ├── paddleocr_provider.py # PaddleOCR 本地
│   │   │       ├── textin_provider.py    # TextIn OCR 适配
│   │   │       └── external_api_provider.py # 通用外部 API
│   │   ├── models/              # 模型抽象层
│   │   │   ├── provider.py      # Provider 接口定义
│   │   │   ├── manager.py       # 模型统一管理器（单例）
│   │   │   ├── llm/             # Ollama / vLLM 实现
│   │   │   ├── embedding/       # bge-m3 本地推理
│   │   │   └── rerank/          # bge-reranker 本地推理
│   │   ├── storage/             # 存储层
│   │   │   ├── milvus.py        # Milvus 操作封装
│   │   │   └── database.py      # SQLite 异步会话
│   │   └── schema/              # 数据模型
│   │       ├── db.py            # ORM 模型（KnowledgeBase/Document/Chunk/ApiKey/LLMConfig）
│   │       └── api.py           # API 请求/响应模型
│   ├── Dockerfile
│   └── requirements.txt
├── frontend/                    # React 前端
│   └── src/
│       ├── pages/               # 知识库/对话/检索测试/模型管理/OCR服务/API Key/设置
│       ├── components/          # UI 组件
│       └── lib/api.ts           # API 客户端
├── docker-compose.yml           # Milvus 基础设施
└── Makefile                     # 开发命令
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LLM_PROVIDER` | ollama | LLM 提供者（ollama / vllm） |
| `LLM_BASE_URL` | http://localhost:11434 | LLM 服务地址 |
| `LLM_MODEL` | qwen2.5:7b | LLM 模型名称 |
| `LLM_API_KEY` | - | API 密钥（远端服务需要） |
| `EMBED_PROVIDER` | sentence-transformers | Embedding 后端（sentence-transformers / flag-embedding） |
| `EMBED_MODEL` | BAAI/bge-m3 | Embedding 模型名称或本地路径 |
| `EMBED_DEVICE` | cuda | 推理设备（cuda / cpu / mps） |
| `RERANK_PROVIDER` | sentence-transformers | Rerank 后端（sentence-transformers / flag-embedding） |
| `RERANK_MODEL` | BAAI/bge-reranker-v2-m3 | Rerank 模型 |
| `RERANK_DEVICE` | cuda | 推理设备（cuda / cpu / mps） |
| `MILVUS_HOST` | localhost | Milvus 地址 |
| `MILVUS_PORT` | 19530 | Milvus 端口 |
| `AGENT_MAX_ITERATIONS` | 3 | Agent 最大迭代次数 |
| `AGENT_TIMEOUT` | 30.0 | Agent 超时时间（秒） |
| `PARENT_CHUNK_SIZE` | 1500 | 父块大小（字符） |
| `CHILD_CHUNK_SIZE` | 300 | 子块大小（字符） |
| `CHUNK_OVERLAP` | 50 | 子块重叠（字符） |

## 未来扩展方向

- **语义切分**：基于 embedding 相似度变化点切分，进一步提升 chunk 质量
- **LLM Rerank**：用大模型做精排，替代小模型 Reranker，提升专业领域排序精度
- **Chunk 富化**：启用 Enricher，为每个 chunk 生成摘要和关键词，提升检索召回
- **多模态支持**：图片/表格识别与检索
- **对话记忆**：多轮对话上下文管理，支持指代消解
- **知识图谱**：从文档中抽取实体关系，支持图谱增强检索
- **评估体系**：集成 RAGAS 等评估框架，量化检索和生成质量
- **分布式部署**：支持多 Worker 水平扩展，文档处理队列化
- **权限管理**：知识库级别的访问控制
- **增量更新**：文档修改后仅重新处理变更部分
