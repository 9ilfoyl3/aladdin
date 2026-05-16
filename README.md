# Agentic RAG 知识库系统

基于 Agent 编排的 RAG（检索增强生成）知识库问答系统。通过路由判定、查询改写、迭代检索与反思机制，实现比传统 RAG 更精准的知识检索与回答生成。

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
         → Chunker 结构感知切分（父块 1500 字 / 子块 300 字）
         → Embedder 生成稠密向量(1024维) + 稀疏向量
         → 写入 Milvus（向量）+ SQLite（元数据）
```

切片策略采用**结构感知的父子 chunk 切分**：
- 优先识别文档结构标记（条款编号、法律文书关键词、Markdown 标题等）按逻辑段落切分
- 无结构标记时回退到段落边界切分
- 子块用于精准检索（语义集中），父块用于上下文返回（信息完整）
- 子块切分同样感知结构标记，确保每个子块是一个完整的逻辑单元

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
- **上下文窗口管理**：可配置每个模型的最大上下文 token 数，按 chunk 相关性智能截断，适配不同窗口大小的模型
- **流式响应**：SSE 流式输出，兼容 OpenAI API 格式，Agent 模式实时推送思考进度事件；支持按模型配置开关流式
- **引用溯源**：回答附带引用来源（文件名、子块内容、父块上下文、相关性分数）
- **API Key 认证**：SHA256 哈希存储，支持创建/撤销/调用统计，仅 `/v1/` 路径需认证
- **检索测试**：独立的检索测试页面，对比不同模式效果

## 快速启动

### 前置要求

| 依赖 | 版本要求 | 说明 |
|------|----------|------|
| Python | 3.12+ | 推荐 3.12 或 3.14，macOS 可通过 Homebrew 安装 |
| Node.js | 18+ | 前端构建，推荐 LTS 版本 |
| Docker | 20.10+ | 用于运行 Milvus 向量数据库 |
| pip | 最新 | Python 包管理 |

**平台说明：**
- **macOS**：`EMBED_DEVICE=cpu`、`RERANK_DEVICE=cpu`（Apple Silicon 暂不支持 CUDA）
- **Windows**：建议使用 WSL2 运行 Docker 和后端，或直接在 Windows 上运行（需 Python 3.12+）
- **Linux（有 GPU）**：`EMBED_DEVICE=cuda`、`RERANK_DEVICE=cuda`

### 安装与启动

```bash
# 1. 启动 Milvus 向量数据库
make infra

# 2. 安装依赖
make install

# 3. 配置环境变量
cp backend/.env.example backend/.env
# 编辑 backend/.env，配置 LLM 地址和 API Key

# 4. 启动前后端
make dev
```

- 前端：http://localhost:5173
- 后端 API：http://localhost:8000
- API 文档：http://localhost:8000/docs

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
│   │   │   └── loaders/         # 文档解析器
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
│       ├── pages/               # 知识库/对话/检索测试/模型管理/API Key/设置
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
| `EMBED_MODEL` | BAAI/bge-m3 | Embedding 模型 |
| `EMBED_DEVICE` | cuda | 推理设备（cuda / cpu） |
| `RERANK_MODEL` | BAAI/bge-reranker-v2-m3 | Rerank 模型 |
| `RERANK_DEVICE` | cuda | 推理设备（cuda / cpu） |
| `MILVUS_HOST` | localhost | Milvus 地址 |
| `MILVUS_PORT` | 19530 | Milvus 端口 |
| `AGENT_MAX_ITERATIONS` | 3 | Agent 最大迭代次数 |
| `AGENT_TIMEOUT` | 30.0 | Agent 超时时间（秒） |
| `PARENT_CHUNK_SIZE` | 1500 | 父块大小（字符） |
| `CHILD_CHUNK_SIZE` | 300 | 子块大小（字符） |
| `CHUNK_OVERLAP` | 50 | 子块重叠（字符） |

## 未来扩展方向

- **扫描件 OCR 支持**：集成 PaddleOCR / pytesseract，自动识别扫描件 PDF 并提取文本（当前扫描件会提示不支持）
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
