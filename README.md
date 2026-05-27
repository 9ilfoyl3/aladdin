<div align="center">

# Aladdin

**开源 Agentic RAG 知识库系统**

通过 Agent 编排实现智能路由、查询改写、迭代检索与反思，让知识检索更精准。

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-green.svg)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/React-18-61dafb.svg)](https://react.dev/)
[![Milvus](https://img.shields.io/badge/Milvus-2.4+-00a1ea.svg)](https://milvus.io/)

[English](./README_EN.md) | 中文

</div>

---

#### 概览 • [架构](#-架构概览) • [特性](#-核心特性) • [快速开始](#-快速开始) • [部署](#-docker-部署生产环境) • [文档](#-文档)

---

## 📌 概览

Aladdin 是一个开源的 Agentic RAG 知识库系统，围绕三个核心能力构建：

- **混合检索** — 稠密语义 + 稀疏关键词三路召回，RRF 融合 + Rerank 精排
- **Agent 推理** — ReAct 风格的渐进式多步推理，自主编排检索、改写与反思
- **结构感知处理** — 按文档逻辑结构切分，父子块映射，图文混排 OCR

所有 AI 推理（LLM / Embedding / Rerank / OCR）均通过 HTTP 调用外部服务，部署轻量。

---

## 🔥 核心特性

**智能检索**

| 能力 | 说明 |
|------|------|
| Agent 编排 | 路由判定 → 查询改写 → 迭代检索 → 反思评估，最多 3 轮 |
| 混合检索 | 稠密 + 稀疏并行，RRF 融合 + Rerank 精排 + 父块扩展 |
| 智能路由 | 自动判断查询复杂度，简单走快路径，复杂走迭代 |
| 容错降级 | Agent 异常 → hybrid → 纯检索，多级自动降级 |

**文档处理**

| 能力 | 说明 |
|------|------|
| 多格式支持 | PDF、Word、Excel、PPT、TXT、Markdown、图片 |
| 图文混排 | 自动提取嵌入图片并 OCR，按页位置插入识别文本 |
| 结构感知切片 | 按逻辑结构切分，表格整块保护，父子块映射 |
| 异步 Pipeline | Redis Stream 任务队列 + 独立 Worker，API 与处理解耦 |

**平台能力**

| 能力 | 说明 |
|------|------|
| 多模型管理 | 多 LLM 配置，前端动态切换，Agent 节点独立配模型 |
| Embedding / Rerank | 统一远程服务，前端可视化配置，热切换无需重启 |
| OCR 服务管理 | 多 OCR Provider，默认 + Fallback 自动切换 |
| OpenAI 兼容 | SSE 流式输出，API Key 认证，引用溯源 |

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

全模块化设计，从文档解析、向量化、检索到 LLM 推理，每个组件可独立替换。支持本地 / 私有云部署，数据完全自主可控。

---

## 🚀 快速开始

### 环境要求

- [Docker](https://www.docker.com/) & [Docker Compose](https://docs.docker.com/compose/)（20.10+）
- [Python 3.12](https://www.python.org/)（⚠️ 3.13+ 存在兼容问题）
- [Node.js 18+](https://nodejs.org/)
- 任意 OpenAI 兼容 LLM API
- Embedding 远程服务（TEI / Infinity / vLLM，可启动后在前端配置）

### 安装与启动

```bash
git clone <repo-url>
cd aladdin

# 启动基础设施（Milvus + PostgreSQL + Redis）
docker compose up -d

# 配置环境变量
cp backend/.env.example backend/.env
# 编辑 backend/.env，至少配置 LLM_BASE_URL 和 LLM_API_KEY
```

<details>
<summary><b>macOS / Linux</b></summary>

```bash
# 安装依赖（自动创建 venv）
make install

# 启动服务（API + Worker + 前端）
make dev
```

</details>

<details>
<summary><b>Windows (PowerShell)</b></summary>

```powershell
# 创建 Python 环境（必须 3.12）
conda create -n aladdin python=3.12 -y
conda activate aladdin

# 安装依赖
pip install --upgrade pip
pip install -r backend/requirements-base.txt
cd frontend && npm install && cd ..

# 启动（分三个终端）
# 终端 1：API
cd backend
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# 终端 2：Worker
cd backend
python -m app.worker_main

# 终端 3：前端
cd frontend
npm run dev
```

</details>

启动后访问 http://localhost:5173 开始使用。

> **Embedding / Rerank 可以启动后再配置。** 通过前端「Embedding & Rerank 配置」页面添加远程服务地址即可。

### 服务地址

| 服务 | 地址 |
|------|------|
| 前端界面 | http://localhost:5173 |
| API 文档 | http://localhost:8000/docs |

---

## 🐳 Docker 部署（生产环境）

```bash
mkdir -p /opt/aladdin && cd /opt/aladdin

docker load -i app.tar
docker load -i infra.tar    # 首次部署

cp .env.example .env && vim .env
docker compose up -d
```

迭代更新：

```bash
docker load -i app.tar
docker compose up -d --force-recreate backend worker frontend
docker image prune -f
```

详细部署流程参见 [部署运维手册](./DEPLOY_OPERATIONS.md)。

---

## 🔍 检索模式

| 模式 | 流程 | 适用场景 |
|------|------|----------|
| **智能检索**（agent） | Router → 查询改写 → 迭代检索 + 反思（最多 3 轮） | 复杂多跳查询 |
| **快速检索**（hybrid） | 稠密 + 稀疏并行 → RRF 融合 → Rerank 精排 | 通用场景 |

### Agent 编排流程

```
用户查询 → Router + Rewriter 并行
  ├─ simple → 直接走 hybrid 快路径
  └─ complex → 改写 → Executor 并行检索
       → Reflector 评估（分数快判 / LLM 深度评估）
       → 充分则返回 / 不充分则追加查询（最多 3 轮）
       → 异常 → 降级到 hybrid
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
| Embedding / Rerank | 任意 OpenAI 兼容远程服务 |

---

## 📂 项目结构

```
aladdin/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI 入口
│   │   ├── worker_main.py       # Worker 入口
│   │   ├── api/                 # API 路由
│   │   ├── agent/               # Agent 编排
│   │   ├── retrieval/           # 检索工具
│   │   ├── pipeline/            # 文档处理管道
│   │   ├── models/              # 模型 Provider 抽象
│   │   └── storage/             # 存储层
│   ├── Dockerfile
│   └── requirements-base.txt
├── frontend/                    # React 前端
├── docker-compose.yml           # 开发基础设施
├── docker-compose-production.yml
├── Makefile
└── scripts/
```

---

## 🛠️ 常用命令

```bash
make install            # 安装所有依赖
make dev                # 启动前后端 + Worker
make dev-backend        # 仅后端 API
make dev-worker         # 仅 Worker
make dev-frontend       # 仅前端
make test               # 运行测试
make infra              # 启动基础设施
make infra-down         # 停止基础设施
```

---

## 📖 文档

| 文档 | 说明 |
|------|------|
| [部署运维手册](./DEPLOY_OPERATIONS.md) | 生产部署、运维命令、环境配置 |
| [技术架构详解](./ARCHITECTURE.md) | Agent 编排、切片策略、OCR 扩展 |
| [macOS 打包指南](./DEPLOYMENT_GUIDE_MAC.md) | Docker 镜像打包与内网部署 |
| [Windows 开发指南](./DEPLOYMENT_GUIDE.md) | Windows 本地开发环境搭建 |

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

欢迎提交 [Issue](../../issues) 和 Pull Request。

流程：Fork → 创建分支 → 提交变更 → 发起 PR

---

## 📄 许可证

[MIT License](./LICENSE)
