# Aladdin Technical Architecture

This document provides in-depth technical details about the system's core workflows, chunking strategies, Agent orchestration mechanisms, environment variables, and more.

---

## Core Workflows

### Document Processing Pipeline

```
Upload File → Loader parses (PDF/DOCX/XLSX/PPTX/TXT/MD)
            → Simultaneously extracts text and embedded images (written to temp dir, content hash dedup)
            → If text is empty, triggers full-file OCR (multi-Provider + Fallback)
            → If text is non-empty but has embedded images, concurrent OCR on images
            → Image OCR text inserted after corresponding page text by position
            → Chunker performs structure-aware splitting (parent chunk 1500 chars / child chunk 300 chars, table protection)
            → Embedder generates dense vectors (1024-dim) + sparse vectors
            → Writes to Milvus (vectors) + PostgreSQL (metadata)
            → Cleans up image temp directory
```

### Supported File Formats

| Format | Processing Method |
|--------|------------------|
| PDF | PyMuPDF text extraction, auto-OCR when text is empty |
| DOCX | python-docx |
| XLSX | openpyxl |
| PPTX | python-pptx |
| TXT/MD | Direct read |
| JPG/JPEG/PNG | OCR service recognition (requires OCR service configuration) |

---

## Chunking Strategy

Uses **structure-aware parent-child chunk splitting**:

- Prioritizes document structure markers (clause numbers, legal keywords, Markdown headings) for logical paragraph splitting
- Falls back to paragraph boundary splitting when no structure markers are found
- Child chunks are used for precise retrieval (focused semantics); parent chunks for context return (complete information)
- Child chunk splitting also respects structure markers, ensuring each child chunk is a complete logical unit
- HTML tables (`<table>...</table>`) are protected as whole blocks, never split across chunks
- Recognizes VL model-specific markers (`[Non-Text]`, `[Image]`, etc.) as segmentation points

### Design Rationale

Traditional RAG splits by fixed character count, which often cuts a complete logical paragraph (e.g., "rebuttal regarding lost wages") in half. This causes the embedding vector to represent mixed semantics, reducing retrieval precision. Structure-aware splitting ensures each child chunk is an independent semantic unit, with embeddings precisely representing that topic, resulting in higher retrieval hit rates.

### No Recall Loss

- After hitting a child chunk, the system returns the full parent chunk content via parent-child mapping, giving the LLM sufficient context
- Complex cross-paragraph questions are handled by Agent mode — query rewriting generates multiple sub-queries, iterative retrieval hits multiple child chunks, and multiple parent chunks are merged and returned
- Structure splitting + parent chunk expansion + Agent iteration work together to improve both recall and precision

---

## Mixed Content Document Processing

For documents containing both text and images (e.g., PDFs with charts, Word docs with screenshots, PPTs with images), the system ensures no information is lost:

### Processing Flow

```
Loader extracts text + extracts embedded images (written to temp dir)
  │
  ├─ Text is empty (pure scanned document) → Full-file OCR
  │
  └─ Text is non-empty + has embedded images → Concurrent OCR on images
       │
       └─ Insert image OCR text after corresponding page text by position
```

### Production-Grade Optimizations

| Optimization | Implementation | Effect |
|-------------|----------------|--------|
| Memory control | Images written to temp dir, not held in memory as bytes | Large documents won't OOM |
| Concurrent OCR | `asyncio.Semaphore(4)` controls parallelism | 75% reduction in processing time for 30 images |
| Image dedup | MD5 hash dedup, same content OCR'd only once | Watermarks/logos not processed repeatedly |
| Count limit | Max 50 images extracted per document | Prevents abnormal files from overwhelming OCR service |
| Small image filter | Images < 50px or < 1KB data are skipped | Filters decorative icons |
| Position association | Image text inserted after corresponding page | Image content in same chunk as surrounding context during retrieval |
| Resource cleanup | `shutil.rmtree` in `finally` block cleans temp dir | No disk leaks |

### Format Support Details

| Document Format | Text Extraction | Image Extraction Method | Page-Level Positioning |
|----------------|-----------------|------------------------|----------------------|
| PDF | `pymupdf` get_text() | `page.get_images()` + `extract_image()` | ✅ Precise to page |
| Word | `python-docx` paragraphs | `doc.part.rels` image relationships | By image sequence |
| PPTX | `python-pptx` text_frame | `shape.image.blob` (PICTURE type shapes) | ✅ Precise to slide |
| Pure images | None (returns empty text) | Entire file treated as image | N/A |

---

## Retrieval Modes in Detail

### Three-Tier Retrieval Modes

| Mode | Flow | Use Case |
|------|------|----------|
| direct | Dense vector ANN retrieval | Simple queries, low latency |
| hybrid | Dense + sparse parallel → RRF fusion → Rerank → parent chunk expansion | General purpose |
| agent | Route determination → query rewriting → iterative retrieval + reflection (up to 3 rounds) | Complex multi-hop queries |

### Key Differences from Traditional RAG

| Capability | Traditional RAG | This System |
|-----------|----------------|-------------|
| Chunking | Fixed character count | Structure-aware, preserving logical integrity |
| Retrieval | Single vector search | Dense + sparse hybrid + RRF fusion + Rerank |
| Query understanding | Raw query direct retrieval | LLM routing + query rewriting (multi-perspective retrieval) |
| Iteration | None | Retrieve → reflect → supplement, up to 3 iterations |
| Context return | Returns matched small chunks | Child chunk hit expands to parent chunk for complete context |
| Fault tolerance | None | Multi-level degradation (Agent error → hybrid → pure retrieval) |
| Performance | None | Query dedup, score-based fast judgment reduces 60% LLM calls, batch Rerank eliminates lock contention |

---

## Agent Orchestration Flow (Agent Mode)

```
User Query
  │
  ├─ Router + Rewriter execute in parallel
  │    ├─ Router determines "simple" → cancel rewriting, go directly to hybrid fast path
  │    └─ Router determines "complex" → wait for rewriting result ↓
  │
  ├─ Executor parallel retrieval
  │    ├─ Query-level dedup (embedding cosine similarity > 0.92 → skip)
  │    ├─ Sub-queries skip rerank (pure vector + RRF, fully parallel, no locks)
  │    └─ Merge and dedup, then unified rerank + parent chunk expansion (single call, eliminates lock contention)
  │
  ├─ Reflector two-level evaluation
  │    ├─ Fast judgment (no LLM): top-3 avg score ≥ 0.7 → sufficient / top-5 avg score < 0.3 → insufficient
  │    ├─ LLM deep evaluation: middle score range, multi-dimensional scoring (relevance/coverage/consistency)
  │    ├─ Sufficient → return results
  │    ├─ Coverage improvement < 10% → early termination (further iteration is pointless)
  │    └─ Insufficient → generate supplementary queries, return to Executor (max 3 rounds)
  │
  └─ Exception → degrade to hybrid fast path
```

### Agent Node Model Configuration

Independent LLMs can be configured for each Agent node via the frontend "Model Management" page:

| Node | Recommended Model | Purpose |
|------|-------------------|---------|
| Router | Lightweight model | Determine simple/complex |
| Rewriter | Lightweight model | Query rewriting |
| Reflector | Lightweight model | Evaluate retrieval quality |
| Final Answer | Strong model (selected in chat) | Generate answer |

When not configured, all nodes use the model selected in the chat session.

### Graceful Degradation Mechanisms

- **Agent error degradation**: Any exception during orchestration automatically falls back to hybrid retrieval
- **LLM unavailable degradation**: When streaming generation fails, returns raw retrieved text directly
- **Reranker error degradation**: Skips reranking, returns RRF fusion results
- Response `metadata.degraded` field indicates whether degradation occurred; `metadata.llm_degraded` indicates LLM degradation

---

## OCR Service Management

The system supports configurable OCR services for processing scanned PDFs and other documents without text layers.

### Supported OCR Providers

| Provider Type | Description | Configuration Notes |
|--------------|-------------|-------------------|
| `paddleocr` | PaddleOCR local service | Requires PaddleOCR dependencies, configure `lang` and `use_gpu` via `extra_config` |
| `textin` | TextIn OCR (Hehe Information) | Response format `{code, message, data: [{page, content}]}`, provide API URL and key |
| `external_api` | Generic external API (compatibility mode) | Auto-detects common response formats, suitable for quick integration |

### Architecture Design

```
OCRProvider (Abstract Base Class)
├── PaddleOCRProvider          # Local PaddleOCR
├── BaseExternalAPIProvider    # External HTTP API abstract base (common upload logic)
│   ├── TextInProvider         # TextIn OCR adapter
│   └── ExternalAPIProvider    # Generic compatibility (auto-detect response format)
└── New Provider...            # Just inherit BaseExternalAPIProvider
```

### Adding a New OCR Service

1. Create `xxx_provider.py` under `backend/app/pipeline/ocr/`
2. Inherit `BaseExternalAPIProvider`, implement `_adapt_response` method to parse the service's response format
3. Register the new type in `_create_provider` factory method in `backend/app/pipeline/ocr/manager.py`
4. Add the new `provider_type` to validation logic in `backend/app/api/ocr_config.py`
5. Add the option to the Select component in frontend `OcrServices.tsx`

### Default Service and Fallback

- At most one default service and one fallback service at any time
- The same configuration cannot be both default and fallback
- Document processing uses the default service first; on failure, automatically switches to fallback for one retry
- When no OCR configuration exists in the database, the pipeline runs normally (skips OCR steps)

### API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/ocr-configs` | Get all configs (api_key masked) |
| POST | `/api/ocr-configs` | Create config |
| PUT | `/api/ocr-configs/{id}` | Update config (partial update) |
| DELETE | `/api/ocr-configs/{id}` | Delete config |
| POST | `/api/ocr-configs/test` | Connectivity test with temporary config |
| POST | `/api/ocr-configs/{id}/test` | Connectivity test for saved config |

---

## Embedding / Rerank Service Configuration

The system calls external Embedding and Rerank services via HTTP API, supporting any OpenAI-compatible interface.

### Remote Service URL Rules

| Interface Type | URL Format | Example |
|---------------|-----------|---------|
| OpenAI-compatible (TEI/Infinity/vLLM) | Fill to `/v1`, system auto-appends `/embeddings` or `/rerank` | `http://server:8080/v1` |
| Custom interface | Fill complete endpoint path | `http://server:8001/ranking_score` |

### Configuration Methods

- **Environment variables**: `EMBED_PROVIDER=remote` + `EMBED_BASE_URL` + `EMBED_MODEL` + `EMBED_API_KEY`
- **Frontend page**: After startup, dynamically add/switch on the **Embedding Config** page, takes effect immediately without restart
- Database configs with `is_active=True` take priority over environment variables

---

## Complete Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_PROVIDER` | ollama | LLM provider (ollama / vllm) |
| `LLM_BASE_URL` | http://localhost:11434 | LLM service URL |
| `LLM_MODEL` | qwen2.5:7b | LLM model name |
| `LLM_API_KEY` | - | API key |
| `EMBED_PROVIDER` | remote | Embedding backend (remote recommended) |
| `EMBED_BASE_URL` | - | Embedding service URL |
| `EMBED_MODEL` | BAAI/bge-m3 | Embedding model name |
| `EMBED_API_KEY` | - | Embedding service key |
| `RERANK_PROVIDER` | remote | Rerank backend (remote recommended) |
| `RERANK_BASE_URL` | - | Rerank service URL |
| `RERANK_MODEL` | BAAI/bge-reranker-v2-m3 | Rerank model |
| `RERANK_API_KEY` | - | Rerank service key |
| `DATABASE_URL` | postgresql+asyncpg://...localhost:5432/aladdin | PostgreSQL connection URL |
| `MILVUS_HOST` | localhost | Milvus address |
| `MILVUS_PORT` | 19530 | Milvus port |
| `REDIS_URL` | redis://localhost:6379/0 | Redis connection URL (task queue + cache) |
| `AGENT_MAX_ITERATIONS` | 3 | Agent max iteration count |
| `AGENT_TIMEOUT` | 30.0 | Agent timeout (seconds) |
| `PARENT_CHUNK_SIZE` | 1500 | Parent chunk size (characters) |
| `CHILD_CHUNK_SIZE` | 300 | Child chunk size (characters) |
| `CHUNK_OVERLAP` | 50 | Child chunk overlap (characters) |
| `PIPELINE_MAX_CONCURRENT` | 3 | Worker max concurrent document processing |
| `PIPELINE_MAX_RETRIES` | 3 | Document processing max retries |
| `PIPELINE_TASK_TIMEOUT_MINUTES` | 30 | Single document processing timeout (minutes) |

---

## Complete Feature List

- **Multi-format documents**: PDF, Word, Excel, PPT, TXT, Markdown
- **Mixed content processing**: Auto-extracts embedded images from PDF/Word/PPT, concurrent OCR with page-position text insertion
- **Smart image handling**: Content hash dedup, decorative small image filtering, 50-image-per-document limit
- **Hybrid retrieval**: Dense semantic + sparse keyword retrieval with RRF fusion
- **Intelligent routing**: Auto-determines query complexity; simple queries take fast path (router and rewriter run in parallel, zero wait)
- **Query rewriting**: Multi-strategy expansion (keyword extraction, HyDE hypothetical document generation, perspective shifting), generates 2-4 retrieval queries
- **Query dedup**: Cross-iteration dedup based on embedding cosine similarity to avoid redundant retrieval
- **Iterative reflection**: Two-level evaluation (score-based fast judgment + LLM deep evaluation), early termination when coverage improvement is insufficient
- **Structural fragment penalty**: Rerank phase applies score penalty to short texts with no substantive information (headings, TOC entries)
- **Multi-model management**: Database-persisted multiple LLM configs with create/edit/delete/set-default/connectivity-test, dynamic switching during chat
- **Configurable Embedding/Rerank**: Supports both local model and remote service modes, frontend dynamic switching without restart
- **OCR service management**: Visual management of multiple OCR services with default + fallback auto-switching
- **Markdown chunking optimization**: Smart splitting of VL model Markdown output (tables, headings), table block protection
- **Context window management**: Configurable max context tokens per model, intelligent truncation by chunk relevance
- **Streaming response**: SSE streaming output, OpenAI API format compatible, real-time Agent thinking progress events
- **Citation tracing**: Answers include citation sources (filename, child chunk content, parent chunk context, relevance score)
- **API Key authentication**: SHA256 hash storage, create/revoke/usage statistics, only `/v1/` paths require auth
- **Retrieval testing**: Dedicated retrieval test page for comparing different mode results

---

## Future Roadmap

- **Semantic chunking**: Split based on embedding similarity change points for better chunk quality
- **LLM Rerank**: Use large models for reranking instead of small model Rerankers
- **Chunk enrichment**: Enable Enricher to generate summaries and keywords for each chunk
- **Conversation memory**: Multi-turn conversation context management with coreference resolution
- **Knowledge graph**: Extract entity relationships from documents for graph-enhanced retrieval
- **Evaluation framework**: Integrate RAGAS and similar frameworks to quantify retrieval and generation quality
- **Distributed deployment**: Multi-worker horizontal scaling with document processing queues
- **Access control**: Knowledge-base-level permission management
- **Incremental updates**: Re-process only changed portions when documents are modified
```
