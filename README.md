<div align="center">

# 🧞 Artoo — 让知识库自己去检索：ReAct Agent 驱动的 Agentic RAG 框架

**开源 · LLM 驱动 · 可私有化部署的智能知识库系统**

Artoo 以 ReAct Agent 为核心，让大模型自主编排关键词检索、语义检索、深度阅读、网页搜索与 MCP 工具，构建"先检索证据、再作答"的可追溯问答体验。

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-green.svg)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/React-18-61dafb.svg)](https://react.dev/)
[![Milvus](https://img.shields.io/badge/Milvus-2.4+-00a1ea.svg)](https://milvus.io/)

| **简体中文** | [English](./README_EN.md) |

</div>

<p align="center">
  <a href="#-项目介绍">项目介绍</a> •
  <a href="#%EF%B8%8F-架构设计">架构设计</a> •
  <a href="#-功能概览">功能概览</a> •
  <a href="#-快速开始">快速开始</a> •
  <a href="#-文档">文档</a> •
  <a href="#-开发指南">开发指南</a>
</p>

---

## 📌 项目介绍

**Artoo** 是一款开源的、基于大语言模型（LLM）的 Agentic RAG 知识库框架，专为企业级文档理解与可追溯问答场景打造。

框架围绕三大核心能力构建：**ReAct Agent 智能推理**让大模型在 Think → Act → Observe 循环中自主决定检索策略与停止时机；**三路混合检索**通过稠密语义、稀疏向量与 BM25 全文检索并行召回，经 RRF 融合、Rerank 精排、MMR 去冗余与父块扩展输出高质量上下文；**结构感知文档处理**按文档逻辑结构切分、父子块映射，并对图文混排文档做并发 OCR。配合可视化的多模型管理、Embedding / Rerank / OCR 服务热切换、MCP 工具集成、Agent 技能（Skills）与三层递进式上下文管理，Artoo 把分散文档沉淀为可查询、可推理、可溯源的知识资产。

所有 AI 推理（LLM / Embedding / Rerank / OCR）均通过 HTTP 调用外部服务，后端轻量、易于私有化离线部署，数据完全自主可控。Agent 推理过程通过 EventBus 实时流式推送到前端，思考、工具调用、引用来源、上下文 Token 占用全程可视。

## ✨ 核心亮点

- **真正的 ReAct Agent** —— 大模型通过 function calling 自主调用工具、分析结果、决定继续检索还是提交答案，而非固定流水线编排。
- **Evidence-First 检索纪律** —— 内置 Progressive RAG 系统提示词（Assess-Reconnaissance-Plan-Execute 工作流），强制"先检索、深读 chunk、再作答"，杜绝凭记忆臆造。
- **三路混合检索** —— Dense + Sparse + BM25 并行召回，RRF 融合 + Rerank 精排 + 复合评分 + MMR 去冗余 + 父块扩展。
- **三层递进式上下文管理** —— BPE Token 估算 + API Usage 增量追踪 + LLM 摘要合并 + 分组截断兜底，长对话不超窗。
- **可扩展工具生态** —— 内置知识检索、关键词匹配、深度阅读、网页搜索、思考、技能加载等工具，并支持接入远程 MCP Server。
- **全程可视化** —— Agent 思考、工具调用、引用溯源、Token 占用通过 SSE 实时推送，前端逐 token 渲染。

## 🏗️ 架构设计

```
┌─────────────────────────────────────────────────────────────┐
│                          接入层                              │
│   Chat API (OpenAI 兼容 · SSE)  │  Admin API (RESTful)       │
│   MCP Server (对外暴露知识库能力)                            │
├─────────────────────────────────────────────────────────────┤
│                     ReAct Agent 引擎                         │
│   Think(流式LLM) → Analyze(终止判定) → Act(工具) → Observe   │
│   EventBus 事件总线  │  三层递进式上下文管理  │  Skills 技能  │
├─────────────────────────────────────────────────────────────┤
│                         工具层                               │
│  knowledge_search │ grep_chunks │ list_knowledge_chunks      │
│  thinking │ web_search │ final_answer │ MCP Tools            │
├─────────────────────────────────────────────────────────────┤
│                        检索工具层                            │
│   Dense + Sparse + BM25 → RRF 融合 → Rerank → MMR → 父块扩展 │
├─────────────────────────────────────────────────────────────┤
│                      索引 / 存储层                           │
│   Milvus (稠密 + 稀疏向量)  │  PostgreSQL (元数据 + 配置)     │
├─────────────────────────────────────────────────────────────┤
│                       数据处理层                             │
│   Loader → OCR → Chunker → Embedder → Indexer                │
│   Worker (Redis Stream 消费者，异步处理)                     │
├─────────────────────────────────────────────────────────────┤
│                    模型服务层（外部 HTTP）                    │
│   LLM API  │  Embedding API  │  Rerank API  │  OCR API       │
└─────────────────────────────────────────────────────────────┘
```

从文档解析、向量化、检索到大模型推理，全流程模块化解耦，组件可灵活替换与扩展。支持本地 / 私有云部署，数据完全自主可控，零门槛 Web UI 快速上手。

## 🧩 功能概览

**智能对话**

| 能力 | 详情 |
|------|------|
| ReAct 推理 | 大模型在 Think → Act → Observe 循环中自主决策，调用工具、分析结果、决定停止时机 |
| 工具调用 | 内置知识检索、关键词匹配、深度阅读、网页搜索、思考、技能加载，支持远程 MCP 工具 |
| Evidence-First | Progressive RAG 提示词强制"先检索、深读 chunk、再作答"，引用溯源、拒绝臆造 |
| Agent 预设 | 内置「快速问答」（hybrid 单轮）与「智能推理」（agent 多步），系统提示词可在线 AI 改写 |
| 上下文管理 | 三层递进式压缩（Token 估算 + Usage 追踪 + LLM 摘要 + 分组截断），长对话不超窗 |
| 流式可视化 | 思考、工具调用、引用、Token 占用通过 SSE 实时推送，逐 token 渲染 |

**知识管理**

| 能力 | 详情 |
|------|------|
| 文档格式 | PDF / Word / Excel / PPT / TXT / Markdown / 图片 |
| 图文混排 | 自动提取嵌入图片并并发 OCR，按页位置插入识别文本，内容 hash 去重 |
| 结构感知切片 | 按文档逻辑结构切分，表格整块保护，父块（上下文）/ 子块（精检）映射 |
| 三路混合检索 | Dense 语义 + Sparse 稀疏 + BM25 全文，RRF 融合 + Rerank + MMR + 父块扩展 |
| 异步 Pipeline | Redis Stream 任务队列 + 独立 Worker，API 与处理解耦，断点续处理 |
| 检索测试 | 独立检索测试页，对比 direct / hybrid / agent 三档模式效果 |

**集成与扩展**

| 能力 | 详情 |
|------|------|
| LLM | 任意 OpenAI 兼容 API（vLLM / DeepSeek / Qwen / …）/ Ollama |
| Embedding / Rerank | 任意 OpenAI 兼容远程服务（TEI / Infinity / vLLM），前端可视化热切换 |
| OCR 服务 | PaddleOCR 本地 / TextIn / 通用外部 API，多 Provider + 默认 + Fallback 自动切换 |
| 向量数据库 | Milvus 2.4+（稠密 + 稀疏向量） |
| MCP | 对外暴露知识库能力（MCP Server），对内接入远程 MCP 工具（服务发现 + 自动注册） |
| 技能 Skills | Progressive Disclosure 渐进式加载，按需读取 SKILL.md 完整指令 |

**平台能力**

| 能力 | 详情 |
|------|------|
| 部署 | 本地 / Docker / 内网离线部署（ARM64 + AMD64 镜像打包） |
| 界面 | Web UI / RESTful API / OpenAI 兼容 API / MCP Server |
| 多模型管理 | 数据库持久化多 LLM 配置，前端创建 / 编辑 / 设默认 / 连通性测试，对话动态切换 |
| 安全 | API Key 认证（SHA256 哈希存储，仅 `/v1/` 路径鉴权），MCP 外部工具输出标记为不可信 |
| 容错降级 | Agent 异常 → hybrid → 纯检索；LLM 不可用 → 返回检索原文；Reranker 异常 → RRF 结果 |
| 可观测性 | Agent 思考 / 工具 / Token 用量 SSE 事件，会话历史持久化 agent_steps |

## 🚀 快速开始

### 🛠 环境要求

- [Docker](https://www.docker.com/) & [Docker Compose](https://docs.docker.com/compose/)（20.10+，用于 Milvus / PostgreSQL / Redis）
- [Python 3.12](https://www.python.org/)（⚠️ 3.13+ 存在兼容问题）
- [Node.js 18+](https://nodejs.org/)
- 任意 OpenAI 兼容 LLM API
- Embedding 远程服务（TEI / Infinity / vLLM，可启动后在前端配置）

### 📦 安装与启动

```bash
git clone <repo-url>
cd artoo

# 启动基础设施（Milvus + PostgreSQL + Redis）
docker compose up -d

# 配置环境变量
cp backend/.env.example backend/.env
# 编辑 backend/.env，至少配置 LLM_BASE_URL、LLM_MODEL、LLM_API_KEY
```

<details>
<summary><b>macOS / Linux</b></summary>

```bash
# 安装依赖（自动创建 .venv）
make install

# 启动服务（API + Worker + 前端）
make dev
```

</details>

<details>
<summary><b>Windows (PowerShell)</b></summary>

```powershell
# 创建 Python 环境（必须 3.12）
conda create -n artoo python=3.12 -y
conda activate artoo

# 安装依赖
pip install --upgrade pip
pip install -r backend/requirements.txt
cd frontend && npm install && cd ..

# 分三个终端启动
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

启动后访问 **http://localhost:5173** 即可使用。

> **Embedding / Rerank / LLM 都可以启动后再配置。** 通过前端「Embedding & Rerank 配置」「模型管理」页面添加远程服务地址，即时生效无需重启。

### 🌐 服务地址

| 服务 | 地址 |
|------|------|
| 前端界面 | `http://localhost:5173` |
| API 文档 | `http://localhost:8000/docs` |
| MCP Server | `http://localhost:8000/mcp` |

### 🧭 使用流程

1. **Embedding 配置** → 添加远程 Embedding 服务地址 → 连通性测试 → 启用
2. **模型管理** → 添加 LLM 配置 → 连通性测试 → 设为默认
3. **知识库** → 创建 → 上传文档 → 等待 Worker 处理完成
4. **对话** → 选择知识库与 Agent 预设 → 提问

## 🐳 Docker 部署（生产 / 内网）

```bash
mkdir -p /opt/artoo && cd /opt/artoo

docker load -i artoo-backend.tar
docker load -i artoo-frontend.tar
# 首次部署还需加载基础设施镜像与模型包

cp .env.example .env && vim .env
docker compose up -d
```

镜像打包（ARM64 / AMD64）与内网离线部署详见 [部署运维手册](./DEPLOY_OPERATIONS.md) 与 [macOS 打包指南](./DEPLOYMENT_GUIDE_MAC.md)。

## 🔍 检索模式

| 模式 | 流程 | 适用场景 |
|------|------|----------|
| **direct** | 稠密向量 ANN 检索 | 简单查询、低延迟 |
| **hybrid**（快速问答） | Dense + Sparse + BM25 并行 → RRF 融合 → Rerank 精排 → MMR → 父块扩展 | 通用场景 |
| **agent**（智能推理） | ReAct 循环：自主调用 grep / 语义检索 / 深读 / 思考 / 网搜，迭代直至提交答案 | 复杂多跳查询、需推理综合 |

### Agent ReAct 循环

```
用户查询 →（有历史时）改写指代、脱敏历史检索结果强制重检
  │
  └─ while 未完成 且 未达 max_iterations：
       ├─ Think：流式调用 LLM，实时发射 THOUGHT 事件
       ├─ 上下文管理：Usage 估算 →（>50%）LLM 摘要合并 →（>80%）分组截断
       ├─ Analyze：判定终止
       │    ├─ final_answer 工具 → 提交答案
       │    ├─ 自然停止 → 引导调用 final_answer
       │    └─ stuck loop / 空响应 → 重试或合成答案
       ├─ Act：执行工具调用（可并行），发射 TOOL_CALL / TOOL_RESULT 事件
       └─ Observe：工具结果追加到消息，进入下一轮
  │
  └─ 异常 → 降级到 hybrid 快路径
```

更多关于工具、提示词、上下文管理与切片策略的细节，见 [技术架构详解](./ARCHITECTURE.md)。

## 🔧 技术栈

| 组件 | 选型 |
|------|------|
| 后端框架 | FastAPI + Uvicorn |
| 向量数据库 | Milvus 2.4+ |
| 关系数据库 | PostgreSQL 16 |
| 任务队列 | Redis Stream |
| 前端 | React 18 + TypeScript + Tailwind CSS v4 |
| 文档解析 | PyMuPDF / python-docx / openpyxl / python-pptx |
| Token 估算 | tiktoken（cl100k_base） |
| LLM | 任意 OpenAI 兼容 API / Ollama |
| Embedding / Rerank | 任意 OpenAI 兼容远程服务 |

## 📂 项目结构

```
artoo/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI 入口
│   │   ├── worker_main.py       # Worker 入口
│   │   ├── config.py            # 配置
│   │   ├── api/                 # API 路由（chat / kb / document / *config …）
│   │   ├── agent/               # ReAct Agent 引擎
│   │   │   ├── engine.py        #   核心 ReAct 循环
│   │   │   ├── events.py        #   EventBus 事件总线
│   │   │   ├── tools/           #   工具层（检索 / 深读 / 网搜 / MCP …）
│   │   │   ├── memory/          #   三层递进式上下文管理
│   │   │   ├── skills/          #   技能（Progressive Disclosure）
│   │   │   └── prompts/         #   Progressive RAG 系统提示词
│   │   ├── retrieval/           # 检索工具（hybrid / vector / sparse / bm25 …）
│   │   ├── pipeline/            # 文档处理管道（loader / ocr / chunker …）
│   │   ├── models/              # 模型 Provider 抽象（LLM / Embedding / Rerank）
│   │   └── storage/             # 存储层（Milvus / PostgreSQL）
│   ├── Dockerfile
│   └── requirements.txt
├── frontend/                    # React 前端
├── docker-compose.yml           # 开发基础设施
├── docker-compose-production.yml
├── Makefile
└── scripts/
```

## 🛠️ 常用命令

```bash
make install            # 安装所有依赖（自动创建 .venv）
make dev                # 启动前后端 + Worker
make dev-backend        # 仅后端 API
make dev-worker         # 仅 Worker
make dev-frontend       # 仅前端
make infra              # 启动基础设施（Milvus + Redis + PostgreSQL）
make infra-down         # 停止基础设施
make download-models    # 下载 Embedding / Rerank 模型到本地
make test               # 运行后端测试
```

## 📘 文档

| 文档 | 说明 |
|------|------|
| [技术架构详解](./ARCHITECTURE.md) | ReAct 引擎、工具层、上下文管理、切片策略、OCR 扩展 |
| [部署运维手册](./DEPLOY_OPERATIONS.md) | 生产部署、运维命令、环境配置 |
| [macOS 打包指南](./DEPLOYMENT_GUIDE_MAC.md) | Docker 镜像打包与内网部署 |
| [Windows 开发指南](./DEPLOYMENT_GUIDE.md) | Windows 本地开发环境搭建 |

## 🗺️ Roadmap

- [ ] 端到端检索评测体系（RAGAS，量化召回与生成质量）
- [ ] Chunk 元数据增强（Enricher 生成摘要 / 关键词）+ 过滤检索
- [ ] 数据库迁移管理（Alembic）
- [ ] 知识图谱增强检索（GraphRAG）
- [ ] 数据源连接器（飞书 / Notion）
- [ ] 多 Worker 水平扩展

## 🧭 开发指南

快速开发模式无需每次重建 Docker 镜像：`make infra` 启动基础设施后，分别运行 `make dev-backend`、`make dev-worker`、`make dev-frontend`，后端支持 `--reload` 热重载，前端 Vite 自动热更新。详见 [Windows 开发指南](./DEPLOYMENT_GUIDE.md)。

## 🤝 贡献

欢迎提交 [Issue](../../issues) 和 Pull Request。

**流程：** Fork → 创建分支 → 提交变更 → 发起 PR

## 📄 许可证

本项目基于 [MIT](./LICENSE) 协议发布。你可以自由使用、修改和分发本项目代码，但需保留原始版权声明。
