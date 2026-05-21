# Aladdin
<div align="center">

**基于 Agent 编排的 RAG 知识库问答系统**

通过路由判定、查询改写、迭代检索与反思机制，实现比传统 RAG 更精准的知识检索与回答生成。

[English](./README_EN.md) | 中文

</div>

---

## ✨ 特性

- **智能路由** — 自动判断查询复杂度，简单问题走快路径，复杂问题走迭代检索
- **混合检索** — 稠密语义 + 稀疏关键词检索，RRF 融合 + Rerank 精排
- **迭代反思** — 检索→评估→补充检索，最多 3 轮迭代，确保回答质量
- **结构感知切片** — 按文档逻辑结构切分，父子块映射，兼顾检索精度与上下文完整性
- **多格式文档** — PDF、Word、Excel、PPT、TXT、Markdown、图片
- **图文混排处理** — 自动提取嵌入图片并 OCR 识别，按页位置插入文本
- **多模型管理** — 支持多个 LLM / Embedding / Rerank 配置，前端动态切换
- **OCR 服务管理** — 可视化管理多个 OCR 服务，支持默认 + Fallback 自动切换
- **流式响应** — SSE 流式输出，兼容 OpenAI API 格式
- **容错降级** — Agent 异常自动回退 hybrid，LLM 不可用返回原文

## 🏗️ 架构

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
│   Milvus (稠密+稀疏向量)  │  PostgreSQL (元数据)             │
├─────────────────────────────────────────────────────────────┤
│                    数据处理层                                │
│   Loader → Chunker → Enricher → Embedder → Indexer         │
├─────────────────────────────────────────────────────────────┤
│                    模型抽象层                                │
│   LLMProvider  │  EmbedProvider  │  RerankProvider          │
└─────────────────────────────────────────────────────────────┘
```

## 🚀 快速开始

### 前置要求

| 依赖 | 版本 | 说明 |
|------|------|------|
| Python | 3.12 | ⚠️ 3.13+ 存在兼容问题 |
| Node.js | 18+ | 前端构建 |
| Docker | 20.10+ | 运行 Milvus + PostgreSQL |

### 安装

```bash
git clone <repo-url>
cd aladdin

# 安装所有依赖
make install

# 下载 Embedding / Rerank 模型（本地模式必须，远程模式跳过）
make download-models
# 国内网络使用镜像：
# make download-models-cn
```

### 配置

```bash
cp backend/.env.example backend/.env
```

编辑 `backend/.env`，至少配置 LLM：

```env
# LLM（必填，OpenAI 兼容格式）
LLM_PROVIDER=vllm
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
LLM_API_KEY=sk-your-key

# Embedding（三选一：sentence-transformers / flag-embedding / remote）
EMBED_PROVIDER=sentence-transformers
EMBED_MODEL=BAAI/bge-m3
EMBED_DEVICE=cpu

# Rerank（三选一：sentence-transformers / flag-embedding / remote）
RERANK_PROVIDER=sentence-transformers
RERANK_MODEL=BAAI/bge-reranker-v2-m3
RERANK_DEVICE=cpu
```

### 启动

```bash
# 1. 启动基础设施
make infra

# 2. 启动前后端
make dev
```

访问：
- 前端界面：http://localhost:3000
- API 文档：http://localhost:8000/docs

### 使用流程

1. **模型管理** → 添加 LLM 配置 → 测试连通性 → 设为默认
2. **知识库** → 创建知识库 → 上传文档
3. **对话** → 选择知识库 → 提问

## 📖 文档

| 文档 | 说明 |
|------|------|
| [部署指南 (Windows)](./DEPLOYMENT_GUIDE.md) | Windows 本地开发环境搭建，含详细步骤 |
| [部署指南 (macOS)](./DEPLOYMENT_GUIDE_MAC.md) | macOS 打包 Docker 镜像，离线部署到内网服务器 |
| [部署运维手册](./DEPLOY_OPERATIONS.md) | 生产环境部署、运维命令、配置说明 |
| [技术架构详解](./ARCHITECTURE.md) | Agent 编排流程、切片策略、环境变量、OCR 扩展等技术细节 |

## 🔧 技术栈

| 组件 | 选型 |
|------|------|
| 后端框架 | FastAPI |
| 向量数据库 | Milvus |
| 元数据存储 | PostgreSQL |
| Embedding | BAAI/bge-m3 |
| Reranker | BAAI/bge-reranker-v2-m3 |
| LLM | Ollama / vLLM / 任意 OpenAI 兼容 API |
| 前端 | React 18 + TypeScript + Tailwind CSS |
| 文档解析 | PyMuPDF / python-docx / openpyxl / python-pptx |

## 📂 项目结构

```
aladdin/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI 入口
│   │   ├── config.py            # 配置管理
│   │   ├── api/                 # API 路由层
│   │   ├── agent/               # Agent 编排（Router/Rewriter/Executor/Reflector）
│   │   ├── retrieval/           # 检索工具（向量/稀疏/混合/Rerank）
│   │   ├── pipeline/            # 文档处理管道（Loader/Chunker/Embedder/OCR）
│   │   ├── models/              # 模型抽象层（LLM/Embedding/Rerank）
│   │   ├── storage/             # 存储层（Milvus/Database）
│   │   └── schema/              # 数据模型
│   ├── Dockerfile
│   └── requirements.txt
├── frontend/                    # React 前端
├── docker-compose.yml           # 基础设施编排
├── Makefile                     # 开发命令
├── DEPLOYMENT_GUIDE.md          # Windows 部署指南
├── DEPLOYMENT_GUIDE_MAC.md      # macOS 打包部署指南
└── DEPLOY_OPERATIONS.md         # 生产部署运维手册
```

## 🛠️ 常用命令

```bash
make install            # 安装所有依赖（后端 + 前端）
make dev                # 启动前后端开发服务
make dev-backend        # 仅启动后端
make dev-frontend       # 仅启动前端
make infra              # 启动基础设施（Milvus + PostgreSQL）
make infra-down         # 停止基础设施
make download-models    # 下载模型
make download-models-cn # 通过国内镜像下载模型
make test               # 运行测试
make clean              # 清理缓存
```

## 🔍 检索模式

| 模式 | 说明 | 适用场景 |
|------|------|----------|
| 智能检索（agent） | Router 自动判断 → 查询改写 → 迭代检索 + 反思 | 复杂多跳查询 |
| 快速检索（hybrid） | 稠密 + 稀疏并行 → RRF 融合 → Rerank 精排 | 通用场景，低延迟 |

## 🤝 贡献

欢迎提交 Issue 和 Pull Request。

## 📄 许可证

[MIT License](./LICENSE)