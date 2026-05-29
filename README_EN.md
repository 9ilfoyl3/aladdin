<div align="center">

# Artoo

**Open-Source Agentic RAG Knowledge Base System**

Achieves more accurate knowledge retrieval through agent-orchestrated query routing, rewriting, iterative retrieval, and reflection mechanisms.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-green.svg)](https://fastapi.tiangolo.com/)

English | [中文](./README.md)

</div>

---

## 🔥 Key Features

**Intelligent Retrieval**
- **Agent Orchestration** — Routing → query rewriting → iterative retrieval → reflection, up to 3 iterations
- **Hybrid Retrieval** — Dense semantic + sparse keyword search with RRF fusion and reranking
- **Smart Routing** — Auto-determines query complexity; simple queries take fast path, complex ones go through iterative retrieval
- **Graceful Degradation** — Agent errors fall back to hybrid retrieval; LLM unavailability returns raw text

**Document Processing**
- **Multi-Format** — PDF, Word, Excel, PPT, TXT, Markdown, images
- **Mixed Content** — Auto-extracts embedded images with OCR, inserts recognized text at page positions
- **Structure-Aware Chunking** — Splits by document logical structure with parent-child chunk mapping
- **Async Pipeline** — Redis Stream task queue + independent Worker process, API decoupled from processing

**Flexible Configuration**
- **Multi-Model Management** — Multiple LLM configs with frontend dynamic switching
- **Embedding / Rerank** — Unified remote service calls, visual frontend configuration, hot-swappable
- **OCR Service Management** — Visual management with default + fallback auto-switching
- **OpenAI Compatible** — SSE streaming, OpenAI API format, API Key authentication

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Access Layer                            │
│   Chat API (OpenAI Compatible)  │  Admin API (RESTful)      │
├─────────────────────────────────────────────────────────────┤
│                  Agent Orchestration Layer                   │
│   QueryRouter → QueryRewriter → Executor → Reflector        │
├─────────────────────────────────────────────────────────────┤
│                     Retrieval Tool Layer                     │
│   DenseRetriever │ SparseRetriever │ HybridRetriever        │
│                      Reranker                               │
├─────────────────────────────────────────────────────────────┤
│                    Index / Storage Layer                     │
│   Milvus (Dense + Sparse Vectors)  │  PostgreSQL (Metadata) │
├─────────────────────────────────────────────────────────────┤
│                   Data Processing Layer                      │
│   Loader → Chunker → Embedder → Indexer                     │
│   Worker (Redis Stream Consumer)                            │
├─────────────────────────────────────────────────────────────┤
│                  Model Service Layer (External)              │
│   LLM API  │  Embedding API  │  Rerank API  │  OCR API     │
└─────────────────────────────────────────────────────────────┘
```

> All AI inference (LLM / Embedding / Rerank / OCR) is handled via HTTP calls to external services. The backend is lightweight to deploy.

---

## 🚀 Quick Start

### Prerequisites

| Dependency | Version | Notes |
|-----------|---------|-------|
| Docker & Docker Compose | 20.10+ | For Milvus, PostgreSQL, Redis |
| Python | 3.12 | ⚠️ 3.13+ has compatibility issues |
| Node.js | 18+ | Frontend build |
| LLM API | - | Any OpenAI-compatible API |
| Embedding Service | - | TEI / Infinity / vLLM providing `/v1/embeddings` |
| Rerank Service | - | TEI / Jina (optional, retrieval works without it) |

### 1. Clone

```bash
git clone <repo-url>
cd artoo
```

### 2. Start Infrastructure

```bash
docker compose up -d
# Starts Milvus + PostgreSQL + Redis + etcd + MinIO
```

### 3. Install Dependencies

<details>
<summary><b>macOS / Linux</b></summary>

```bash
make install
```

</details>

<details>
<summary><b>Windows (PowerShell)</b></summary>

```powershell
conda create -n artoo python=3.12 -y
conda activate artoo

pip install --upgrade pip
pip install -r backend/requirements-base.txt

cd frontend && npm install && cd ..
```

</details>

### 4. Configure

```bash
cp backend/.env.example backend/.env
```

Edit `backend/.env`:

```env
# === LLM (required) ===
LLM_PROVIDER=vllm
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
LLM_API_KEY=sk-your-key

# === Embedding (optional, can be configured via frontend after startup) ===
EMBED_BASE_URL=http://your-embedding-server/v1
EMBED_MODEL=BAAI/bge-m3
EMBED_API_KEY=

# === Rerank (optional, can be configured via frontend after startup) ===
RERANK_BASE_URL=http://your-rerank-server/v1
RERANK_MODEL=BAAI/bge-reranker-v2-m3
RERANK_API_KEY=
```

> **Embedding / Rerank can be configured after startup.** Add remote service URLs via the frontend "Embedding & Rerank Config" page.

### 5. Launch

<details>
<summary><b>macOS / Linux</b></summary>

```bash
make dev
# Starts API server + Worker + frontend dev server
```

</details>

<details>
<summary><b>Windows (PowerShell) — three terminals</b></summary>

```powershell
# Terminal 1: API server
cd backend
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# Terminal 2: Worker
cd backend
python -m app.worker_main

# Terminal 3: Frontend
cd frontend
npm run dev
```

</details>

### 6. Access

| Service | URL |
|---------|-----|
| Frontend UI | http://localhost:5173 |
| API Docs | http://localhost:8000/docs |

### 7. Usage

1. **Embedding Config** → Add remote Embedding service URL → Test connectivity → Enable
2. **Model Management** → Add LLM config → Test connectivity → Set as default
3. **Knowledge Base** → Create → Upload documents → Wait for processing
4. **Chat** → Select knowledge base → Ask questions

---

## 🐳 Docker Deployment (Production)

```bash
mkdir -p /opt/artoo && cd /opt/artoo

docker load -i app.tar
docker load -i infra.tar    # First deployment only

cp .env.example .env && vim .env

docker compose up -d
```

See [Deployment & Operations Guide](./DEPLOY_OPERATIONS.md) for details.

---

## 🔍 Retrieval Modes

| Mode | Flow | Use Case |
|------|------|----------|
| **Smart** (agent) | Router → query rewriting → iterative retrieval + reflection | Complex multi-hop queries |
| **Fast** (hybrid) | Dense + sparse parallel → RRF fusion → reranking | General purpose, low latency |

---

## 🔧 Tech Stack

| Component | Choice |
|-----------|--------|
| Backend | FastAPI + Uvicorn |
| Vector Database | Milvus 2.4+ |
| Relational Database | PostgreSQL 16 |
| Task Queue | Redis Stream |
| Frontend | React 18 + TypeScript + Tailwind CSS v4 |
| Document Parsing | PyMuPDF / python-docx / openpyxl / python-pptx |
| LLM | Any OpenAI-compatible API |
| Embedding / Rerank | Any OpenAI-compatible remote service (TEI / Infinity / vLLM) |

---

## 📂 Project Structure

```
artoo/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI entry point
│   │   ├── config.py            # Configuration
│   │   ├── worker_main.py       # Worker entry point
│   │   ├── api/                 # API routes
│   │   ├── agent/               # Agent orchestration
│   │   ├── retrieval/           # Retrieval tools
│   │   ├── pipeline/            # Document processing pipeline
│   │   ├── models/              # Model abstraction layer (LLM/Embedding/Rerank Provider)
│   │   ├── storage/             # Storage layer (Milvus/PostgreSQL)
│   │   └── schema/              # Data models
│   ├── Dockerfile
│   └── requirements-base.txt    # Python dependencies
├── frontend/                    # React frontend
├── docker-compose.yml           # Local dev infrastructure
├── docker-compose-production.yml # Production deployment
├── Makefile                     # Dev commands
└── scripts/                     # Packaging & deployment scripts
```

---

## 🛠️ Commands

```bash
make install            # Install all dependencies
make dev                # Start frontend + backend + Worker
make infra              # Start infrastructure
make infra-down         # Stop infrastructure
make test               # Run tests
```

---

## 📖 Documentation

| Document | Description |
|----------|-------------|
| [Deployment & Operations](./DEPLOY_OPERATIONS.md) | Production deployment, operations, configuration |
| [Technical Architecture](./ARCHITECTURE_EN.md) | Agent orchestration, chunking strategy, OCR extension details |
| [macOS Packaging Guide](./DEPLOYMENT_GUIDE_MAC.md) | Docker image packaging for intranet deployment |

---

## 🤝 Contributing

Issues and Pull Requests are welcome.

## 📄 License

[MIT License](./LICENSE)
