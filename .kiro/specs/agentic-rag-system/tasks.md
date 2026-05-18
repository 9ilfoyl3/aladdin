# Implementation Tasks

## Phase 1: 基础设施与模型抽象层

- [x] 1. 项目脚手架搭建
  - [x] 1.1 创建 backend/ 目录结构，初始化 FastAPI 项目，编写 requirements.txt
  - [x] 1.2 创建 frontend/ 目录结构，初始化 React + TypeScript + shadcn/ui 项目
  - [x] 1.3 编写 docker-compose.yml（backend、frontend、milvus、etcd、minio、ollama）
  - [x] 1.4 编写 backend/Dockerfile 和 frontend/Dockerfile
  - [x] 1.5 实现 app/config.py 配置管理（Settings + .env 加载）

- [x] 2. 模型抽象层实现
  - [x] 2.1 定义 LLMProvider / EmbedProvider / RerankProvider 抽象基类（app/models/provider.py）
  - [x] 2.2 实现 OllamaLLM（app/models/llm/ollama.py）：generate + stream
  - [x] 2.3 实现 VllmLLM（app/models/llm/vllm.py）：generate + stream
  - [x] 2.4 实现 BgeM3Embedder（app/models/embedding/bge_m3.py）：embed + embed_sparse
  - [x] 2.5 实现 BgeReranker（app/models/rerank/bge_reranker.py）：rerank
  - [x] 2.6 实现 ModelManager 统一管理类，按配置初始化各 Provider

- [x] 3. 存储层实现
  - [x] 3.1 实现 SQLite 数据库初始化与 ORM 模型（app/storage/database.py + app/schema/db.py）
  - [x] 3.2 实现 Milvus 操作封装（app/storage/milvus.py）：create_collection / insert / search_dense / search_sparse / delete

## Phase 2: 数据处理管道

- [x] 4. 文档加载器
  - [x] 4.1 实现 Loader 基类和工厂方法（app/pipeline/loader.py）
  - [x] 4.2 实现 Markdown / TXT 加载器
  - [x] 4.3 实现 PDF 加载器（基于 pymupdf 或 pdfplumber）
  - [x] 4.4 实现 Word（docx）加载器
  - [x] 4.5 实现 Excel（xlsx）加载器
  - [x] 4.6 实现 PPT（pptx）加载器

- [x] 5. 切片与向量化
  - [x] 5.1 实现 HierarchicalChunker（app/pipeline/chunker.py）：父子 chunk 切分逻辑
  - [x] 5.2 实现 Enricher（app/pipeline/enricher.py）：摘要/关键词生成（可选，初期可跳过）
  - [x] 5.3 实现 Embedder 管道节点（app/pipeline/embedder.py）：调用 EmbedProvider 生成稠密+稀疏向量
  - [x] 5.4 实现完整 Pipeline 编排：load → chunk → enrich → embed → index（写入 Milvus + SQLite）

## Phase 3: 检索工具层

- [x] 6. 检索器实现
  - [x] 6.1 定义 BaseRetriever 和 RetrievalResult 数据结构（app/retrieval/base.py）
  - [x] 6.2 实现 VectorRetriever：稠密向量检索（app/retrieval/vector.py）
  - [x] 6.3 实现 SparseRetriever：稀疏向量检索（app/retrieval/sparse.py）
  - [x] 6.4 实现 HybridRetriever：RRF 融合 + Rerank + 父块扩展（app/retrieval/hybrid.py）
  - [x] 6.5 实现 Reranker 封装（app/retrieval/reranker.py）

## Phase 4: Agent 编排层

- [x] 7. Agent 组件实现
  - [x] 7.1 实现 QueryRouter（app/agent/router.py）：简单/复杂查询分类
  - [x] 7.2 实现 QueryRewriter（app/agent/rewriter.py）：HyDE + 子问题分解
  - [x] 7.3 实现 RetrievalExecutor（app/agent/executor.py）：并行调用检索器
  - [x] 7.4 实现 Reflector（app/agent/reflector.py）：结果质量评估 + 追加查询生成
  - [x] 7.5 实现 AgentOrchestrator（app/agent/orchestrator.py）：完整编排流程 + 超时 + 降级

## Phase 5: API 接入层

- [x] 8. Chat API
  - [x] 8.1 实现 POST /v1/chat/completions 端点（app/api/chat.py）：兼容 OpenAI 协议
  - [x] 8.2 实现流式 SSE 响应
  - [x] 8.3 实现非流式同步响应
  - [x] 8.4 集成三档检索模式调度（direct / hybrid / agent）
  - [x] 8.5 实现 token 使用量统计和引用来源返回

- [ ] 9. Admin API
  - [x] 9.1 实现知识库 CRUD 接口（app/api/knowledge_base.py）
  - [x] 9.2 实现文档上传与管理接口（app/api/document.py）：上传 + 状态查询 + 删除 + 切片查看
  - [x] 9.3 实现检索测试接口 POST /api/retrieval/test
  - [x] 9.4 实现系统配置接口（app/api/system.py）：健康检查 + 配置读写

- [ ] 10. API Key 认证
  - [x] 10.1 实现 API Key 生成/存储/验证逻辑（SHA256 哈希存储）
  - [x] 10.2 实现 API Key CRUD 接口（app/api/api_key.py）
  - [x] 10.3 实现 FastAPI 中间件：请求拦截 + Key 验证 + 调用计数

## Phase 6: 管理后台前端

- [ ] 11. 前端基础框架
  - [x] 11.1 配置路由（React Router）+ 布局组件（侧边栏导航）
  - [x] 11.2 配置 API 客户端（lib/api.ts）+ TanStack Query
  - [x] 11.3 配置 Zustand store（对话状态管理）

- [ ] 12. 前端页面实现
  - [x] 12.1 知识库管理页面（列表 + 创建/编辑/删除）
  - [x] 12.2 文档管理页面（上传 + 状态展示 + 切片查看）
  - [x] 12.3 对话界面（流式渲染 + Markdown + 引用来源）
  - [x] 12.4 检索测试页面（查询输入 + 结果列表 + 分数展示）
  - [x] 12.5 API Key 管理页面（创建 + 列表 + 撤销）
  - [x] 12.6 系统配置页面（模型参数 + 检索参数调整）

## Phase 7: 防御性设计与部署

- [ ] 13. 容错与降级
  - [x] 13.1 实现 Agent 超时降级逻辑（超时返回当前最佳结果）
  - [x] 13.2 实现模型服务不可用时的降级（跳过 LLM 生成，返回纯检索结果）
  - [x] 13.3 实现 Reranker 异常时跳过重排序
  - [x] 13.4 响应中标注降级模式字段

- [ ] 14. 部署与运维
  - [x] 14.1 完善 docker-compose.yml（网络、卷、健康检查、重启策略）
  - [x] 14.2 实现 /api/system/health 健康检查（检测 Milvus、模型服务连接状态）
  - [x] 14.3 编写 README.md（部署说明、环境变量、快速启动）
