<div align="center">

# Aladdin

**开源 Agentic RAG 知识库系统**

通过 Agent 编排实现智能路由、查询改写、迭代检索与反思，让知识检索更精准。

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-green.svg)](https://fastapi.tiangolo.com/)

[English](./README_EN.md) | 中文

</div>

---

## 🔥 核心特性

**智能检索**
- **Agent 编排** — 路由判定 → 查询改写 → 迭代检索 → 反思评估，最多 3 轮迭代
- **混合检索** — 稠密语义 + 稀疏关键词，RRF 融合 + Rerank 精排
- **智能路由** — 自动判断查询复杂度，简单问题走快路径，复杂问题走迭代检索
- **容错降级** — Agent 异常自动回退 hybrid，LLM 不可用返回原文

**文档处理**
- **多格式支持** — PDF、Word、Excel、PPT、TXT、Markdown、图片
- **图文混排** — 自动提取嵌入图片并 OCR，按页位置插入识别文本
- **结构感知切片** — 按文档逻辑结构切分，父子块映射，兼顾精度与上下文
- **异步 Pipeline** — Redis Stream 任务队列 + 独立 Worker 进程，API 与文档处理解耦

**灵活配置**
- **多模型管理** — 支持多个 LLM / Embedding / Rerank 配置，前端动态切换
- **OCR 服务管理** — 可视化管理多个 OCR 服务，默认 + Fallback 自动切换
- **OpenAI 兼容** — SSE 流式输出，兼容 OpenAI API 格式，支持 API Key 认证

---

## 🏗️ 架构概览

```
┌─────────────────────────────────────────────────────────────┐
│                        接入层                                │
│   Chat API (OpenAI 兼容)  │  Admin API (RESTful)            │
├─────────────────────────────────────────────────────────────┤
│                     Agent 编排层                             │
│   QueryRouter → QueryRewriter → Executor → Reflector        │
├─────────────────────────────────────────────────────────────┤
│                      检索工具层                              │
│   DenseRetriever │ SparseRetriever │ HybridRetriever        │
│                      Reranker                               │
├─────────────────────────────────────────────────────────────┤
│                    索引 / 存储层                             │
│   Milvus (稠密+稀疏向量)  │  PostgreSQL (元数据+配置)        │
├─────────────────────────────────────────────────────────────┤
│                    数据处理层                                │
│   Loader → Chunker → Embedder → Indexer                     │
│   Worker (Redis Stream Consumer)                            │
├─────────────────────────────────────────────────────────────┤
│                    模型服务层（外部）                         │
│   LLM API  │  Embedding API  │  Rerank API  │  OCR API     │
└─────────────────────────────────────────────────────────────┘
```

---

## 🚀 快速开始

### 环境要求

| 依赖 | 版本 | 说明 |
|------|------|------|
| Docker & Docker Compose | 20.10+ | 运行 Milvus、PostgreSQL、Redis |
| Python | 3.12 | ⚠️ 3.13+ 存在兼容问题 |
| Node.js | 18+ | 前端构建 |
| LLM API | - | 任意 OpenAI 兼容 API（DeepSeek / 通义 / OpenAI 等） |
| Embedding 服务 | - | TEI / Infinity / vLLM 等提供 `/v1/embeddings` 接口 |

### 1. 克隆项目

```bash
git clone <repo-url>
cd aladdin
```

### 2. 启动基础设施

```bash
docker compose up -d
# 启动 Milvus + PostgreSQL + Redis + etcd + MinIO
```

> macOS / Linux 也可以使用 `make infra`

验证服务就绪：
```bash
docker compose ps
# 所有容器状态应为 running / healthy
```

### 3. 安装依赖

<details>
<summary><b>macOS / Linux</b></summary>

```bash
make install
# 自动创建 Python venv + 安装后端依赖 + 安装前端依赖
```

</details>

<details>
<summary><b>Windows (PowerShell)</b></summary>

```powershell
# 创建 Python 环境（必须 3.12，3.13+ 不兼容）
conda create -n aladdin python=3.12 -y
conda activate aladdin

# 安装后端依赖
pip install --upgrade pip
pip install -r backend/requirements-base.txt

# 安装前端依赖
cd frontend
npm install
cd ..
```

</details>

### 4. 配置环境变量

```bash
cp backend/.env.example backend/.env
```

编辑 `backend/.env`，配置必要服务：

```env
# === LLM（必填）===
LLM_PROVIDER=vllm
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
LLM_API_KEY=sk-your-key

# === Embedding 服务（必填）===
EMBED_PROVIDER=remote
EMBED_BASE_URL=http://your-embedding-server/v1
EMBED_MODEL=BAAI/bge-m3
EMBED_API_KEY=your-token

# === Rerank 服务（必填）===
RERANK_PROVIDER=remote
RERANK_BASE_URL=http://your-rerank-server/v1
RERANK_MODEL=BAAI/bge-reranker-v2-m3
RERANK_API_KEY=your-token

# === 基础设施（默认值适用于本地 Docker）===
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/aladdin
MILVUS_HOST=localhost
MILVUS_PORT=19530
REDIS_URL=redis://localhost:6379/0
```

> 💡 Embedding/Rerank 也支持在启动后通过前端页面动态配置，环境变量仅作为初始默认值。

### 5. 启动服务

<details>
<summary><b>macOS / Linux</b></summary>

```bash
make dev
# 同时启动 API 服务 + Worker + 前端开发服务器
```

</details>

<details>
<summary><b>Windows (PowerShell) — 分三个终端</b></summary>

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

</details>

### 6. 访问

| 服务 | 地址 |
|------|------|
| 前端界面 | http://localhost:5173 |
| API 文档 | http://localhost:8000/docs |

### 7. 使用流程

1. **模型管理** → 添加 LLM 配置 → 测试连通性 → 设为默认
2. **知识库** → 创建知识库 → 上传文档 → 等待处理完成
3. **对话** → 选择知识库 → 提问

---

## 🐳 Docker 部署（生产环境）

### 快速部署

```bash
# 1. 准备部署目录
mkdir -p /opt/aladdin && cd /opt/aladdin

# 2. 加载镜像
docker load -i app.tar
docker load -i infra.tar    # 首次部署

# 3. 配置环境变量
cp .env.example .env
vim .env

# 4. 启动
docker compose up -d
```

### 服务架构

```
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│   frontend   │───▶│   backend    │───▶│    worker    │
│   (nginx)    │    │  (API only)  │    │ (Pipeline)   │
└──────────────┘    └──────┬───────┘    └──────┬───────┘
                           │                    │
                    ┌──────▼───────┐            │
                    │ Redis Stream │◀───────────┘
                    └──────────────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
         PostgreSQL     Milvus       Redis
```

- **backend** — API 服务，处理请求，文档上传后入队
- **worker** — 独立进程，消费队列执行文档解析、Embedding、索引
- **frontend** — nginx 静态资源 + API 反向代理

### 镜像打包

```bash
# macOS / Linux
make docker-package-arm-update     # ARM64 迭代更新包
make docker-package-amd64-update   # AMD64 迭代更新包
make docker-package-arm            # ARM64 首次完整包（含基础设施镜像）
make docker-package-amd64          # AMD64 首次完整包
```

详细部署流程参见 [部署运维手册](./DEPLOY_OPERATIONS.md)。

---

## 🔍 检索模式

| 模式 | 流程 | 适用场景 |
|------|------|----------|
| **智能检索**（agent） | Router 判断 → 查询改写 → 迭代检索 + 反思 | 复杂多跳查询 |
| **快速检索**（hybrid） | 稠密 + 稀疏并行 → RRF 融合 → Rerank 精排 | 通用场景，低延迟 |

### Agent 编排流程

```
用户查询 → Router + Rewriter 并行
  ├─ Router 判定 simple → 直接走 hybrid 快路径
  └─ Router 判定 complex → 等待改写 → Executor 并行检索
       → Reflector 评估（分数快判 / LLM 深度评估）
       → 充分则返回 / 不充分则追加查询（最多 3 轮）
```

---

## 🔧 技术栈

| 组件 | 选型 |
|------|------|
| 后端框架 | FastAPI + Uvicorn |
| 向量数据库 | Milvus 2.4+ |
| 关系数据库 | PostgreSQL 16 |
| 任务队列 | Redis Stream |
| 前端 | React 18 + TypeScript + Tailwind CSS v4 |
| 文档解析 | PyMuPDF / python-docx / openpyxl / python-pptx |
| LLM | 任意 OpenAI 兼容 API |
| Embedding / Rerank | 任意 OpenAI 兼容服务（TEI / Infinity / vLLM） |

---

## 📂 项目结构

```
aladdin/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI 入口
│   │   ├── config.py            # 配置管理
│   │   ├── worker_main.py       # Worker 入口
│   │   ├── api/                 # API 路由层
│   │   ├── agent/               # Agent 编排（Router/Rewriter/Executor/Reflector）
│   │   ├── retrieval/           # 检索工具（向量/稀疏/混合/Rerank）
│   │   ├── pipeline/            # 文档处理管道（Loader/Chunker/Embedder/OCR）
│   │   ├── models/              # 模型抽象层（LLM/Embedding/Rerank）
│   │   ├── storage/             # 存储层（Milvus/PostgreSQL）
│   │   └── schema/              # 数据模型
│   ├── Dockerfile
│   └── requirements.txt
├── frontend/                    # React 前端
├── docker-compose.yml           # 本地开发基础设施
├── docker-compose-production.yml # 生产部署编排
├── Makefile                     # 开发命令
└── scripts/                     # 打包部署脚本
```

---

## 🛠️ 常用命令

```bash
# 开发
make install            # 安装所有依赖
make dev                # 启动前后端 + Worker
make dev-backend        # 仅启动后端 API
make dev-worker         # 仅启动 Worker
make dev-frontend       # 仅启动前端
make test               # 运行测试

# 基础设施
make infra              # 启动 Milvus + PostgreSQL + Redis
make infra-down         # 停止基础设施

# 部署打包
make docker-package-arm-update     # ARM64 更新包
make docker-package-amd64-update   # AMD64 更新包
```

---

## 📖 文档

| 文档 | 说明 |
|------|------|
| [部署运维手册](./DEPLOY_OPERATIONS.md) | 生产部署、运维命令、环境配置 |
| [技术架构详解](./ARCHITECTURE.md) | Agent 编排、切片策略、OCR 扩展等技术细节 |

---

## 🗺️ Roadmap

- [ ] BM25 全文检索（Milvus 原生，第三路召回）
- [ ] Rerank 分数阈值 + 兜底回复（防幻觉）
- [ ] 端到端检索评测体系
- [ ] Chunk 元数据增强 + 过滤检索
- [ ] 数据库迁移管理（Alembic）
- [ ] 知识图谱增强检索（GraphRAG）
- [ ] 数据源连接器（飞书 / Notion）

---

## 🤝 贡献

欢迎提交 Issue 和 Pull Request。

## 📄 许可证

[MIT License](./LICENSE)
