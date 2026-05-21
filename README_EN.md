
# Aladdin
<div align="center">

**Agentic RAG Knowledge Base System**

An agent-orchestrated RAG (Retrieval-Augmented Generation) system that achieves more accurate knowledge retrieval through query routing, rewriting, iterative retrieval, and reflection mechanisms.

English | [中文](./README.md)

</div>

---

## ✨ Features

- **Intelligent Routing** — Automatically determines query complexity; simple queries take the fast path, complex ones go through iterative retrieval
- **Hybrid Retrieval** — Dense semantic + sparse keyword retrieval with RRF fusion and reranking
- **Iterative Reflection** — Retrieve → evaluate → supplement retrieval, up to 3 iterations to ensure answer quality
- **Structure-Aware Chunking** — Splits documents by logical structure with parent-child chunk mapping for both precision and context completeness
- **Multi-Format Documents** — PDF, Word, Excel, PPT, TXT, Markdown, images
- **Mixed Content Processing** — Automatically extracts embedded images and performs OCR, inserting recognized text at page positions
- **Multi-Model Management** — Multiple LLM / Embedding / Rerank configurations with frontend dynamic switching
- **OCR Service Management** — Visual management of multiple OCR services with default + fallback auto-switching
- **Streaming Response** — SSE streaming output, compatible with OpenAI API format
- **Graceful Degradation** — Agent errors fall back to hybrid retrieval; LLM unavailability returns raw retrieved text

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
│   VectorRetriever │ SparseRetriever │ HybridRetriever       │
│                      Reranker                               │
├─────────────────────────────────────────────────────────────┤
│                    Index / Storage Layer                     │
│   Milvus (Dense + Sparse Vectors)  │  PostgreSQL (Metadata) │
├─────────────────────────────────────────────────────────────┤
│                   Data Processing Layer                      │
│   Loader → Chunker → Enricher → Embedder → Indexer         │
├─────────────────────────────────────────────────────────────┤
│                    Model Abstraction Layer                   │
│   LLMProvider  │  EmbedProvider  │  RerankProvider          │
└─────────────────────────────────────────────────────────────┘
```

## 🚀 Quick Start

### Prerequisites

| Dependency | Version | Notes |
|-----------|---------|-------|
| Python | 3.12 | ⚠️ 3.13+ has compatibility issues |
| Node.js | 18+ | Frontend build |
| Docker | 20.10+ | For Milvus + PostgreSQL |

### Installation

```bash
git clone <repo-url>
cd aladdin

# Install all dependencies
make install

# Download Embedding / Rerank models (required for local mode, skip for remote mode)
make download-models
# For users in China:
# make download-models-cn
```

### Configuration

```bash
cp backend/.env.example backend/.env
```

Edit `backend/.env`, at minimum configure the LLM:

```env
# LLM (required, OpenAI-compatible format)
LLM_PROVIDER=vllm
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
LLM_API_KEY=sk-your-key

# Embedding (choose one: sentence-transformers / flag-embedding / remote)
EMBED_PROVIDER=sentence-transformers
EMBED_MODEL=BAAI/bge-m3
EMBED_DEVICE=cpu

# Rerank (choose one: sentence-transformers / flag-embedding / remote)
RERANK_PROVIDER=sentence-transformers
RERANK_MODEL=BAAI/bge-reranker-v2-m3
RERANK_DEVICE=cpu
```

### Launch

```bash
# 1. Start infrastructure
make infra

# 2. Start frontend and backend
make dev
```

Access:
- Frontend UI: http://localhost:3000
- API Docs: http://localhost:8000/docs

### Usage

1. **Model Management** → Add LLM config → Test connectivity → Set as default
2. **Knowledge Base** → Create knowledge base → Upload documents
3. **Chat** → Select knowledge base → Ask questions

## 📖 Documentation

| Document | Description |
|----------|-------------|
| [Deployment Guide (Windows)](./DEPLOYMENT_GUIDE.md) | Local development setup on Windows with detailed steps |
| [Deployment Guide (macOS)](./DEPLOYMENT_GUIDE_MAC.md) | Build Docker images on macOS for offline intranet deployment |
| [Deployment Operations](./DEPLOY_OPERATIONS.md) | Production deployment, operations commands, and configuration |
| [Technical Architecture](./ARCHITECTURE_EN.md) | Agent orchestration, chunking strategy, environment variables, OCR extension details |

## 🔧 Tech Stack

| Component | Choice |
|-----------|--------|
| Backend | FastAPI |
| Vector Database | Milvus |
| Metadata Storage | PostgreSQL |
| Embedding | BAAI/bge-m3 |
| Reranker | BAAI/bge-reranker-v2-m3 |
| LLM | Ollama / vLLM / Any OpenAI-compatible API |
| Frontend | React 18 + TypeScript + Tailwind CSS |
| Document Parsing | PyMuPDF / python-docx / openpyxl / python-pptx |

## 📂 Project Structure

```
aladdin/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI entry point
│   │   ├── config.py            # Configuration management
│   │   ├── api/                 # API routes
│   │   ├── agent/               # Agent orchestration (Router/Rewriter/Executor/Reflector)
│   │   ├── retrieval/           # Retrieval tools (Vector/Sparse/Hybrid/Rerank)
│   │   ├── pipeline/            # Document processing pipeline (Loader/Chunker/Embedder/OCR)
│   │   ├── models/              # Model abstraction layer (LLM/Embedding/Rerank)
│   │   ├── storage/             # Storage layer (Milvus/Database)
│   │   └── schema/              # Data models
│   ├── Dockerfile
│   └── requirements.txt
├── frontend/                    # React frontend
├── docker-compose.yml           # Infrastructure orchestration
├── Makefile                     # Development commands
├── DEPLOYMENT_GUIDE.md          # Windows deployment guide
├── DEPLOYMENT_GUIDE_MAC.md      # macOS packaging & deployment guide
└── DEPLOY_OPERATIONS.md         # Production deployment & operations
```

## 🛠️ Common Commands

```bash
make install            # Install all dependencies (backend + frontend)
make dev                # Start frontend and backend dev servers
make dev-backend        # Start backend only
make dev-frontend       # Start frontend only
make infra              # Start infrastructure (Milvus + PostgreSQL)
make infra-down         # Stop infrastructure
make download-models    # Download models
make download-models-cn # Download models via China mirror
make test               # Run tests
make clean              # Clean caches
```

## 🔍 Retrieval Modes

| Mode | Description | Use Case |
|------|-------------|----------|
| Smart (agent) | Router auto-determines → query rewriting → iterative retrieval + reflection | Complex multi-hop queries |
| Fast (hybrid) | Dense + sparse parallel → RRF fusion → reranking | General purpose, low latency |

## 🤝 Contributing

Issues and Pull Requests are welcome.

## 📄 License

[MIT License](./LICENSE)