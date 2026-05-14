# Technical Design Document

## 系统架构概览

```
┌─────────────────────────────────────────────────────────────┐
│                        接入层                                │
│   Chat API (OpenAI 兼容) │ Admin API (RESTful)              │
├─────────────────────────────────────────────────────────────┤
│                     Agent 编排层                             │
│   QueryRouter → QueryRewriter → Executor → Reflector        │
├─────────────────────────────────────────────────────────────┤
│                      检索工具层                              │
│   VectorRetriever │ SparseRetriever │ HybridRetriever       │
│                      Reranker                               │
├─────────────────────────────────────────────────────────────┤
│                    索引/存储层                               │
│   Milvus (稠密+稀疏向量) │ SQLite (元数据)                   │
├─────────────────────────────────────────────────────────────┤
│                    数据处理层                                │
│   Loader → Chunker → Enricher → Embedder → Indexer         │
├─────────────────────────────────────────────────────────────┤
│                    模型抽象层                                │
│   LLMProvider │ EmbedProvider │ RerankProvider              │
└─────────────────────────────────────────────────────────────┘
```

## 项目结构

```
agentic-rag/
├── backend/
│   ├── app/
│   │   ├── main.py                  # FastAPI 入口
│   │   ├── config.py                # 配置管理
│   │   ├── api/
│   │   │   ├── chat.py              # Chat API (OpenAI 兼容)
│   │   │   ├── knowledge_base.py    # 知识库 CRUD
│   │   │   ├── document.py          # 文档管理
│   │   │   ├── api_key.py           # API Key 管理
│   │   │   └── system.py            # 系统配置/健康检查
│   │   ├── models/
│   │   │   ├── provider.py          # 模型 Provider 抽象
│   │   │   ├── llm/
│   │   │   │   ├── base.py          # LLM 基类
│   │   │   │   ├── ollama.py        # Ollama 实现
│   │   │   │   └── vllm.py          # vLLM 实现
│   │   │   ├── embedding/
│   │   │   │   └── bge_m3.py        # bge-m3 本地 Embedding
│   │   │   └── rerank/
│   │   │       └── bge_reranker.py  # bge-reranker-v2-m3
│   │   ├── pipeline/
│   │   │   ├── loader.py            # 文档加载器
│   │   │   ├── chunker.py           # 切片器
│   │   │   ├── enricher.py          # 富化器
│   │   │   └── embedder.py          # 向量化器
│   │   ├── retrieval/
│   │   │   ├── base.py              # Retriever 基类
│   │   │   ├── vector.py            # 稠密向量检索
│   │   │   ├── sparse.py            # 稀疏向量检索
│   │   │   ├── hybrid.py            # 混合检索 + RRF
│   │   │   └── reranker.py          # Rerank 精排
│   │   ├── agent/
│   │   │   ├── orchestrator.py      # Agent 编排主控
│   │   │   ├── router.py            # 查询路由
│   │   │   ├── rewriter.py          # 查询改写
│   │   │   ├── executor.py          # 检索执行
│   │   │   └── reflector.py         # 结果反思
│   │   ├── storage/
│   │   │   ├── milvus.py            # Milvus 操作封装
│   │   │   └── database.py          # SQLite ORM
│   │   └── schema/
│   │       ├── db.py                # 数据库模型
│   │       └── api.py               # API 请求/响应模型
│   ├── Dockerfile
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── pages/
│   │   │   ├── KnowledgeBase.tsx    # 知识库管理
│   │   │   ├── Documents.tsx        # 文档管理
│   │   │   ├── Chat.tsx             # 对话界面
│   │   │   ├── Retrieval.tsx        # 检索测试
│   │   │   ├── Settings.tsx         # 系统配置
│   │   │   └── ApiKeys.tsx          # API Key 管理
│   │   ├── components/
│   │   ├── lib/
│   │   │   └── api.ts               # API 客户端
│   │   └── App.tsx
│   ├── Dockerfile
│   └── package.json
└── docker-compose.yml
```


## 数据模型设计

### SQLite 表结构

```sql
-- 知识库
CREATE TABLE knowledge_bases (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    retrieval_mode TEXT DEFAULT 'hybrid',  -- direct | hybrid | agent
    config JSON,                           -- 检索参数配置
    doc_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 文档
CREATE TABLE documents (
    id TEXT PRIMARY KEY,
    kb_id TEXT NOT NULL REFERENCES knowledge_bases(id),
    filename TEXT NOT NULL,
    file_type TEXT NOT NULL,
    file_size INTEGER,
    status TEXT DEFAULT 'pending',  -- pending | processing | completed | failed
    error_message TEXT,
    chunk_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Chunk 元数据（向量存 Milvus，元数据存 SQLite）
CREATE TABLE chunks (
    id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL REFERENCES documents(id),
    kb_id TEXT NOT NULL REFERENCES knowledge_bases(id),
    parent_id TEXT,                  -- 父 chunk ID，NULL 表示自身为父
    content TEXT NOT NULL,
    chunk_index INTEGER,
    metadata JSON,                   -- 标题层级、位置等
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- API Key
CREATE TABLE api_keys (
    id TEXT PRIMARY KEY,
    key_hash TEXT NOT NULL UNIQUE,   -- SHA256 哈希存储
    prefix TEXT NOT NULL,            -- 前 8 位用于展示 (sk-xxxx...)
    name TEXT,
    is_active BOOLEAN DEFAULT TRUE,
    call_count INTEGER DEFAULT 0,
    last_used_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### Milvus Collection Schema

每个知识库对应一个 Collection，命名规则：`kb_{knowledge_base_id}`

```python
# Collection 字段定义
fields = [
    FieldSchema(name="chunk_id", dtype=DataType.VARCHAR, is_primary=True, max_length=64),
    FieldSchema(name="doc_id", dtype=DataType.VARCHAR, max_length=64),
    FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=65535),
    FieldSchema(name="dense_vector", dtype=DataType.FLOAT_VECTOR, dim=1024),   # bge-m3 稠密向量
    FieldSchema(name="sparse_vector", dtype=DataType.SPARSE_FLOAT_VECTOR),      # BM25 稀疏向量
    FieldSchema(name="parent_id", dtype=DataType.VARCHAR, max_length=64),
    FieldSchema(name="chunk_index", dtype=DataType.INT64),
]

# 索引配置
dense_index = {"index_type": "HNSW", "metric_type": "COSINE", "params": {"M": 16, "efConstruction": 256}}
sparse_index = {"index_type": "SPARSE_INVERTED_INDEX", "metric_type": "IP"}
```


## 核心模块设计

### 模型抽象层

```python
# app/models/provider.py
from abc import ABC, abstractmethod
from typing import AsyncIterator

class LLMProvider(ABC):
    @abstractmethod
    async def generate(self, messages: list[dict], **kwargs) -> str: ...

    @abstractmethod
    async def stream(self, messages: list[dict], **kwargs) -> AsyncIterator[str]: ...

class EmbedProvider(ABC):
    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]: ...

    @abstractmethod
    async def embed_sparse(self, texts: list[str]) -> list[dict[int, float]]: ...

class RerankProvider(ABC):
    @abstractmethod
    async def rerank(self, query: str, documents: list[str], top_k: int = 10) -> list[tuple[int, float]]: ...

class ModelManager:
    """统一管理所有模型实例，按配置初始化"""
    def __init__(self, config: dict):
        self.llm: LLMProvider = self._init_llm(config)
        self.embedder: EmbedProvider = self._init_embedder(config)
        self.reranker: RerankProvider = self._init_reranker(config)
```

### 数据处理管道

```python
# app/pipeline/chunker.py
@dataclass
class ChunkResult:
    parent_chunks: list[str]       # 大块，用于上下文返回
    child_chunks: list[str]        # 小块，用于精准检索
    parent_child_map: dict[int, list[int]]  # 父→子映射

class HierarchicalChunker:
    """父子 chunk 切分策略"""
    def __init__(self, parent_size: int = 1500, child_size: int = 300, overlap: int = 50):
        self.parent_size = parent_size
        self.child_size = child_size
        self.overlap = overlap

    def chunk(self, text: str, metadata: dict = None) -> ChunkResult:
        """先按标题/语义边界切父块，再将父块细分为子块"""
        ...
```

### 检索工具层

```python
# app/retrieval/base.py
@dataclass
class RetrievalResult:
    chunk_id: str
    content: str              # Parent chunk 内容（上下文完整）
    score: float
    doc_id: str
    metadata: dict

class BaseRetriever(ABC):
    @abstractmethod
    async def search(self, query: str, top_k: int = 10, **kwargs) -> list[RetrievalResult]: ...

# app/retrieval/hybrid.py
class HybridRetriever(BaseRetriever):
    """混合检索：稠密 + 稀疏 + RRF 融合"""
    async def search(self, query: str, top_k: int = 10, **kwargs) -> list[RetrievalResult]:
        dense_results = await self.vector_retriever.search(query, top_k=top_k * 3)
        sparse_results = await self.sparse_retriever.search(query, top_k=top_k * 3)
        fused = self._rrf_fusion(dense_results, sparse_results)
        reranked = await self.reranker.rerank(query, fused, top_k=top_k)
        return self._expand_to_parent(reranked)  # 子块命中 → 返回父块
```

### Agent 编排层

```python
# app/agent/orchestrator.py
class AgentOrchestrator:
    """Plan-Execute-Reflect 编排"""
    def __init__(self, model_manager: ModelManager, retriever: HybridRetriever,
                 max_iterations: int = 3, timeout: float = 8.0):
        self.router = QueryRouter(model_manager.llm)
        self.rewriter = QueryRewriter(model_manager.llm)
        self.executor = RetrievalExecutor(retriever)
        self.reflector = Reflector(model_manager.llm)
        self.max_iterations = max_iterations
        self.timeout = timeout

    async def run(self, query: str, kb_id: str) -> AgentResult:
        try:
            async with asyncio.timeout(self.timeout):
                # 1. 路由判定
                route = await self.router.classify(query)
                if route == "simple":
                    return await self._fast_path(query, kb_id)

                # 2. 查询改写
                rewritten_queries = await self.rewriter.rewrite(query)

                # 3. 迭代检索+反思
                results = []
                for i in range(self.max_iterations):
                    new_results = await self.executor.execute(rewritten_queries, kb_id)
                    results.extend(new_results)
                    verdict = await self.reflector.evaluate(query, results)
                    if verdict.is_sufficient:
                        break
                    rewritten_queries = verdict.follow_up_queries

                return AgentResult(chunks=results, iterations=i+1)
        except (TimeoutError, Exception):
            # 降级到混合检索
            return await self._fast_path(query, kb_id)
```


## API 接口设计

### Chat API（OpenAI 兼容）

```
POST /v1/chat/completions
Headers: Authorization: Bearer sk-xxx

Request:
{
    "model": "rag",
    "messages": [{"role": "user", "content": "如何重置密码？"}],
    "stream": true,
    "knowledge_base_id": "kb_001",
    "retrieval_mode": "hybrid"  // direct | hybrid | agent（可选，覆盖知识库默认配置）
}

Response (stream=false):
{
    "id": "chatcmpl-xxx",
    "object": "chat.completion",
    "choices": [{
        "index": 0,
        "message": {"role": "assistant", "content": "..."},
        "finish_reason": "stop"
    }],
    "usage": {"prompt_tokens": 120, "completion_tokens": 85, "total_tokens": 205},
    "references": [
        {"doc_id": "doc_01", "chunk_id": "chk_01", "content": "...", "score": 0.92}
    ],
    "metadata": {"retrieval_mode": "hybrid", "degraded": false}
}

Response (stream=true): SSE 格式，与 OpenAI 一致
data: {"choices":[{"delta":{"content":"..."}}]}
data: [DONE]
```

### Admin API

```
# 知识库管理
GET    /api/knowledge-bases              # 列表
POST   /api/knowledge-bases              # 创建
GET    /api/knowledge-bases/:id          # 详情
PUT    /api/knowledge-bases/:id          # 更新
DELETE /api/knowledge-bases/:id          # 删除（级联清理）

# 文档管理
GET    /api/knowledge-bases/:kb_id/documents          # 文档列表
POST   /api/knowledge-bases/:kb_id/documents/upload   # 上传文档
GET    /api/documents/:id                             # 文档详情
DELETE /api/documents/:id                             # 删除文档
GET    /api/documents/:id/chunks                      # 查看切片

# 检索测试
POST   /api/retrieval/test
{
    "query": "...",
    "knowledge_base_id": "kb_001",
    "mode": "hybrid",
    "top_k": 10
}

# API Key 管理
GET    /api/api-keys                     # 列表
POST   /api/api-keys                     # 创建
DELETE /api/api-keys/:id                 # 撤销

# 系统
GET    /api/system/health                # 健康检查
GET    /api/system/config                # 获取配置
PUT    /api/system/config                # 更新配置
```


## 前端架构设计

### 技术栈

- React 18 + TypeScript
- shadcn/ui 组件库
- TanStack Query（数据请求）
- React Router（路由）
- Zustand（轻量状态管理，仅对话状态）

### 页面结构

```
App
├── Layout (侧边栏导航)
│   ├── /knowledge-bases          # 知识库列表
│   ├── /knowledge-bases/:id      # 知识库详情（含文档管理）
│   ├── /chat                     # 对话界面
│   ├── /retrieval                # 检索测试
│   ├── /api-keys                 # API Key 管理
│   └── /settings                 # 系统配置
```

### 关键交互

- 文档上传：拖拽区域 + 进度条 + 状态轮询
- 对话界面：SSE 流式渲染 + Markdown 渲染 + 引用来源折叠展示
- 检索测试：查询输入 → 结果列表（含分数、来源、高亮命中）

## 部署架构

### docker-compose 服务编排

```yaml
services:
  backend:
    build: ./backend
    ports: ["8000:8000"]
    environment:
      - MILVUS_HOST=milvus
      - DATABASE_URL=sqlite:///data/rag.db
      - LLM_PROVIDER=ollama
      - LLM_BASE_URL=http://ollama:11434
    volumes:
      - ./data:/app/data        # SQLite + 上传文件
    depends_on: [milvus, ollama]

  frontend:
    build: ./frontend
    ports: ["3000:80"]

  milvus:
    image: milvusdb/milvus:v2.4-latest
    ports: ["19530:19530"]
    volumes:
      - milvus_data:/var/lib/milvus

  etcd:
    image: quay.io/coreos/etcd:v3.5.5
    # Milvus 依赖

  minio:
    image: minio/minio:latest
    # Milvus 对象存储

  ollama:
    image: ollama/ollama:latest
    ports: ["11434:11434"]
    volumes:
      - ollama_data:/root/.ollama
    deploy:
      resources:
        reservations:
          devices:
            - capabilities: [gpu]  # 可选 GPU

volumes:
  milvus_data:
  ollama_data:
```

## 核心流程

### 文档处理流程

```
上传文件 → 存储原文件 → 创建 Document 记录(status=pending)
    → 异步任务启动(status=processing)
    → Loader 解析文件内容
    → Chunker 切分为 Parent/Child chunks
    → Enricher 生成摘要/关键词（可选）
    → Embedder 生成稠密向量 + 稀疏向量
    → Milvus 写入向量数据
    → SQLite 写入 chunk 元数据
    → 更新 Document(status=completed, chunk_count=N)
    → 异常时: status=failed, error_message=xxx
```

### 检索流程（三档模式）

```
用户查询 → 根据知识库配置选择模式:

[直检索模式]
  query → dense_vector_search(top_k) → 返回结果

[混合+Rerank 模式]
  query → parallel(dense_search, sparse_search)
       → RRF 融合(top_100)
       → Reranker 精排(top_10)
       → 子块→父块扩展
       → 返回结果

[全 Agent 模式]
  query → Router 判定复杂度
       → [简单] 走混合+Rerank 快路径
       → [复杂] Rewriter 改写/分解
            → Executor 并行检索
            → Reflector 评估质量
            → [不足] 生成追加查询，回到 Executor（max 3 轮）
            → [充分] 返回结果
       → 超时/异常 → 降级到混合+Rerank
```

### RRF 融合算法

```python
def rrf_fusion(results_lists: list[list], k: int = 60) -> list:
    """Reciprocal Rank Fusion"""
    scores = {}
    for results in results_lists:
        for rank, item in enumerate(results):
            scores[item.id] = scores.get(item.id, 0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)
```

## 配置管理

```python
# app/config.py
class Settings(BaseSettings):
    # 服务
    host: str = "0.0.0.0"
    port: int = 8000

    # 数据库
    database_url: str = "sqlite:///data/rag.db"

    # Milvus
    milvus_host: str = "localhost"
    milvus_port: int = 19530

    # LLM
    llm_provider: str = "ollama"          # ollama | vllm | openai_compat
    llm_base_url: str = "http://localhost:11434"
    llm_model: str = "qwen2.5:7b"

    # Embedding
    embed_model: str = "BAAI/bge-m3"
    embed_device: str = "cuda"            # cuda | cpu

    # Rerank
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_device: str = "cuda"

    # Agent
    agent_max_iterations: int = 3
    agent_timeout: float = 8.0

    # Chunking
    parent_chunk_size: int = 1500
    child_chunk_size: int = 300
    chunk_overlap: int = 50

    class Config:
        env_file = ".env"
```
