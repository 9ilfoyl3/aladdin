# Pipeline TODO 清单

## 已完成 ✓

- [x] CSV 文件上传支持
- [x] 表格数据智能切分（kv 模式 + Markdown 表格保护）
- [x] Embedding 并发调用（Semaphore + 连接池复用）
- [x] Milvus 分批写入（每批 1000 条，避免 gRPC 消息超限）
- [x] Embedder 超长文本截断防御
- [x] 多 Chunker 策略路由（ChunkerRouter + NaiveChunker/TableChunker/LawsChunker/PaperChunker/QAChunker）
- [x] 处理进度实时追踪（ProgressTracker + 各阶段加权进度）
- [x] 结构化日志（PipelineLogger + trace_id 链路追踪 + 慢阶段检测）
- [x] 文件处理并发控制（asyncio.Semaphore 限制同时处理文件数）
- [x] 持久化任务队列（Redis Stream + Consumer Group，降级 asyncio.create_task）
- [x] 检索结果缓存（Redis 缓存 + TTL 过期 + 知识库变更主动清除）
- [x] HyDE 检索（QueryRewriter 多策略改写：关键词扩展 + 假设文档 + 视角转换）
- [x] Agent 迭代检索反思（路由→改写→并行检索→Reflector 评估→追加查询）

---

## 一、性能优化

### 1. CSV/XLSX 大文件流式读取
- **文件**: `backend/app/pipeline/loaders/csv_loader.py`
- **现状**: 一次性读取所有行到内存，200MB 文件占用大量内存
- **目标**: 改为流式读取 + 分批处理，支持 500MB+ 文件
- **优先级**: 低
- **预估工作量**: 1-2 天

### 2. Embedding 服务端 batch 优化
- **文件**: `backend/app/pipeline/embedder.py`
- **现状**: batch_size=128，并发=8，远程服务可能是瓶颈
- **目标**: 支持配置化 batch_size/concurrency，根据服务端能力动态调整
- **优先级**: 低
- **预估工作量**: 0.5 天

---

## 二、工程化

### 3. 数据库迁移管理
- **文件**: 需引入 Alembic
- **现状**: `create_all` + 手动 ALTER，无版本管理
- **目标**: 引入 Alembic 管理 schema 变更，支持升级/回滚
- **优先级**: 中（团队协作必需）
- **预估工作量**: 1 天初始化 + 持续维护

---

## 三、准度优化

### 4. Chunk 元数据增强
- **文件**: `backend/app/pipeline/pipeline.py`、`backend/app/schema/db.py`、`backend/app/storage/milvus.py`
- **现状**: chunk 只记录 chunk_index 和 parent_id，Milvus schema 无 file_type 等 scalar 字段
- **设计原则**: 用户只管传文件，系统自动提取一切可用元数据，无需用户手动标注
- **目标**（分两步）:
  - **Step 1: 结构化解析自动提取（当前实施）**
    - Milvus schema 扩展 `file_type` (VARCHAR) 和 `doc_id` (VARCHAR) 为 scalar 字段（支持 pre-filter）
    - chunk 入库时自动写入 `chunk_metadata` JSON：文件名、file_type、chunker_type、chunk_index
    - PDF: 利用 pymupdf 提取页码（page_num）
    - 结构化文档: chunker 切分时记录所属章节标题路径（如 `["第三章", "第二节", "合同条款"]`）
    - 前端引用展示：显示来源文件名 + 页码/章节（提升用户信任感）
  - **Step 2: 版面分析模型增强（后续，依赖 DLR 模型接入）**
    - 接入版面分析模型（如 DocLayout-YOLO / LayoutLMv3）识别页面元素类型
    - 自动提取：标题层级、表格/图片位置、段落类型（正文/摘要/脚注）
    - 元数据字段扩展：`element_type`（text/table/figure/title）、`bbox` 坐标
    - 为 #7 文档预处理提供更精准的 header/footer 识别能力
- **参考**: RAGFlow 的 Auto-metadata 用 LLM 提取自定义字段（成本高），Dify 的 chunk 继承文档级 metadata。Aladdin 走"自动提取 + 零用户配置"路线，符合 ima 式体验
- **优先级**: 高（元数据过滤和 embedding 上下文增强的前提）
- **预估工作量**: Step 1: 2-3 天，Step 2: 3-5 天

### 5. 元数据过滤检索
- **文件**: `backend/app/retrieval/hybrid.py`、`backend/app/storage/milvus.py`、`backend/app/api/chat.py`
- **现状**: Milvus search 无 filter 表达式，全库搜索无法按条件过滤
- **设计原则**: 过滤对用户透明——系统智能决定过滤策略，用户无需手动配置过滤条件
- **目标**:
  - Milvus search 支持传入 `expr` 参数做 pre-filter（Milvus 原生能力，性能最优）
  - 最常用过滤：`doc_id in [...]`（只在指定文件中搜索）、`file_type in [...]`
  - 对表格类 chunk 在 RRF 融合时施加类型权重（降权 0.8），避免大量表格行淹没文本结果
  - 后续可选：前端对话时支持"仅在这些文件中搜索"的交互（拖入文件 → 自动加 doc_id filter）
- **依赖**: #4 Chunk 元数据增强 Step 1
- **参考**: Milvus pre-filter 是主流最优解（Qdrant/Pinecone 也是同样思路），不需要引入 Elasticsearch
- **优先级**: 高
- **预估工作量**: 2-3 天

### 6. 多知识库联合检索
- **文件**: `backend/app/api/chat.py`、`backend/app/schema/api.py`、前端 Chat.tsx
- **现状**: 对话时只能选单个知识库
- **设计原则**: 对话绑定知识库（默认只搜当前库，效果最优），多库联合作为高级能力
- **目标**:
  - 默认行为不变：对话在当前知识库内检索（保证效果和速度）
  - 高级能力：对话设置中可关联额外知识库（"同时参考 XX 库"）
  - API 层：`kb_ids: list[str]`，并行检索各库（asyncio.gather），合并后统一 Rerank
  - 知识库优先级权重：主库 boost 高，辅助库 boost 低，确保主库结果优先（参考 RAGFlow Page Rank）
  - 要求所有关联知识库使用相同 embedding 模型（向量空间一致性）
- **方案路径**:
  1. 先做：对话设置支持关联多个知识库 + 并行检索 + 优先级权重
  2. 后续：知识库数量 > 10 时，可加 LLM 路由自动选库
- **参考**: RAGFlow 用户手动选择关联 dataset + Page Rank boost；Dify 在 workflow 中显式配置
- **优先级**: 中（当前单库已能满足大部分场景，多库是锦上添花）
- **预估工作量**: 2-3 天

### 7. 文档预处理（去噪）暂定，可能要用到DLR模型
- **文件**: 修改 `backend/app/pipeline/loaders/pdf_loader.py`，新建 `backend/app/pipeline/cleaner.py`，修改 `pipeline.py`
- **现状**: PDF 提取后直接进入切分，页眉页脚、重复水印文字作为噪音混入 chunk
- **目标**（分两步实施）:
  - **Step 1: 坐标过滤 + 统计去噪（当前实施）**
    - 改造 `pdf_loader.py`：用 pymupdf 的 `get_text("dict")` 获取 bbox 坐标，过滤页面顶部/底部 5% 区域的短文本块
    - 新建 `cleaner.py` (TextCleaner)：跨页重复短文本检测（出现频率 > 50% 的页首/页尾短文本判定为页眉页脚）
    - 正则兜底：去除纯页码行（`- 3 -`、`第 3 页`、`Page 3 of 10`）
    - 在 pipeline 的 load 之后、chunk 之前插入 TextCleaner
  - **Step 2: OCR 增强模式（后续，依赖高能力 OCR 服务）**
    - OCRConfig 表新增 `output_format` 字段（`markdown` | `plain_text`）
    - KnowledgeBase config 新增 `pdf_parse_mode` 选项（`native` | `ocr_enhanced`）
    - `ocr_enhanced` 模式：所有 PDF 统一走 OCR 预处理，适合 DeepSeek OCR 等输出结构化 Markdown 的服务
    - Pipeline 根据 OCR 的 `output_format` 决定是否跳过 TextCleaner（markdown 输出已自带去噪）
- **优先级**: 中（Step 1 先做，Step 2 等有高能力 OCR 需求时再做）
- **预估工作量**: Step 1: 1-2 天，Step 2: 1-2 天

### 8. Embedding 上下文增强
- **文件**: `backend/app/pipeline/embedder.py`、`backend/app/pipeline/pipeline.py`
- **现状**: child chunk 直接 embedding 原文，脱离上下文后语义模糊的 chunk 检索效果差
- **目标**（零 LLM 成本）:
  - 标题路径前缀：embedding 时在 child chunk 前拼接 `[文件名 | 章节标题]`
  - 父块上下文：拼接 parent chunk 的前 150 字符作为语境补充
  - 最终 embedding 文本 = `[metadata prefix]\n{parent[:150]}\n{child_chunk}`
  - 不额外存储拼接文本，只影响 embedding 阶段的输入构造
- **依赖**: #4 Chunk 元数据增强（需要章节标题等 metadata）
- **优先级**: 中
- **预估工作量**: 0.5-1 天
- **后续方向（暂定）**:
  - RAPTOR（递归摘要树）：对 chunk 聚类后生成摘要节点，形成树状索引，适合多跳推理场景。需调 LLM 但只在聚类级别调用，非每个 chunk
  - Late Chunking（Jina AI）：用长上下文 embedding 模型对整文档编码，再按 chunk 边界切出向量，每个 chunk 向量天然包含文档级上下文。需要支持 8192+ token 的 embedding 模型

### 9. 超长记录拆分 embedding
- **文件**: `backend/app/pipeline/embedder.py`
- **现状**: 超过 8000 字符的记录截断后 embedding，尾部信息无法被检索命中
- **目标**: 超长记录拆成多个子 chunk 分别 embedding，都指向同一个父 chunk
- **参考**: Dify 的 late chunking 策略
- **优先级**: 低
- **预估工作量**: 1 天

### 10. RAG 评估体系
- **文件**: 需新建 `backend/tests/eval/`
- **现状**: 无自动化评测，准度靠人工判断
- **目标**: 引入 RAGAS 或自建评测框架，自动评估 Faithfulness/Relevancy/Recall
- **优先级**: 中（迭代优化必需）
- **预估工作量**: 3-5 天

---

## 四、大文件上传方案研究

### 12. 业内主流 RAG 系统文件上传限制与方案

#### 各系统默认限制

| 系统 | 默认最大文件 | 可调上限 | 上传方式 |
|------|-------------|---------|---------|
| RAGFlow | 10MB（Web UI） | 通过 Nginx `client_max_body_size` 调整，无官方硬上限 | 单次上传 + S3 数据源连接器（v0.22+） |
| Dify | 15MB（SaaS） | 自部署通过 `UPLOAD_FILE_SIZE_LIMIT` 环境变量调整 | 单次上传，无分片 |
| Open WebUI | 无硬限制（默认受 Nginx/反代限制） | `RAG_FILE_MAX_SIZE` 环境变量 | 单次上传，大文件易崩 ChromaDB |
| AnythingLLM | 云版受限（大 PDF 易 502） | 自部署无硬限制 | 单次上传 |
| ChatGPT Enterprise | 512MB/文件 | 不可调 | OpenAI 托管 |

#### 主流框架的大文件处理策略

**RAGFlow 的方案（最成熟）**:
- Web UI 上传有限制，但核心大文件策略是 **S3 数据源连接器**
- v0.22 引入 S3 Bucket 监听：自动发现新文件 → 增量同步 → 异步解析
- v0.25 实现 ETag 增量同步，避免重复传输
- 本质思路：**大文件不走 HTTP 上传，走对象存储 + 后台同步**

**Dify 的方案**:
- 严格限制单文件大小（SaaS 15MB），自部署可调但官方不推荐过大
- ETL 管道：Unstructured/Dify 内置解析 → 分块 → 向量化，全异步
- 对大型结构化数据（CSV/Excel）：推荐用 External Knowledge API 对接外部数据源

**业界共识做法**:
1. **分片上传（Chunked Upload）**: tus 协议或自研，前端切片 5-10MB/片，支持断点续传
2. **对象存储中转**: 文件先传 MinIO/S3，后台 Worker 异步拉取解析
3. **数据源连接器**: 不走上传，直接连接 S3/数据库/API，定时同步（RAGFlow、LlamaIndex 均支持）
4. **流式解析**: 对 CSV 等结构化文件，边读边处理，内存恒定（pandas `chunksize` 模式）

#### 结构化大文件（CSV/Excel 500MB+）的正确处理方式

业界共识：**大型结构化数据不适合直接做 RAG 语义检索**，应走 Text-to-SQL 路线：
- 向量检索对表格数据效果差（行与行之间语义相似度高，噪声大）
- 正确路径：CSV → 导入数据库 → Text-to-SQL Agent 生成查询 → 返回精确结果
- RAGFlow 对表格的处理：按行切分为 chunk，但官方也承认大表格效果有限
- Dify 的建议：大型 CSV 用 External Knowledge API 或 Code 节点做预处理

#### 对 Aladdin 的建议方案

**短期（当前优先级低）**:
- 维持现有上传限制（建议设为 100MB），覆盖 99% 的文档类文件
- CSV 大文件流式读取已在 TODO #1 中规划，解决内存问题即可

**中期（如有明确需求）**:
- 引入分片上传：前端 `tus-js-client` + 后端分片接收合并
- 文件落盘后异步处理（当前已有 Redis Stream 任务队列，可复用）

**长期（如需支持企业级数据量）**:
- S3/MinIO 数据源连接器（参考 RAGFlow v0.22 方案）
- 大型 CSV/Excel → Text-to-SQL Agent（比 RAG 更适合结构化数据查询）
- 数据库连接器：直接对接 MySQL/PostgreSQL，LLM 生成 SQL 查询

**结论**: 500MB+ 文件上传本身不是核心需求，核心是"如何让大量结构化数据可被 AI 查询"。答案是 Text-to-SQL，不是更大的上传限制。

---

## 五、Worker 架构演进

### 14. 任务队列框架评估与演进（Redis Streams → Celery/Dramatiq）
- **现状**: gf-deployment 已实现 Worker 独立进程 + Redis Streams 消费，支持熔断/超时/健康检查
- **当前方案优势**: 轻量、无额外依赖、对单一任务类型（文档处理）足够
- **局限性**:
  - 多任务类型路由需自行实现（如文档处理、Agent 工作流、定时清理分不同队列）
  - 定时任务（Beat）无内置支持
  - 任务编排（链式/并行/chord）需手写
  - 监控面板需自建
  - 水平扩展时 consumer 注册/注销需手动管理
- **业界参考**:
  - Dify: Celery + Redis Broker，api/worker/worker_beat 三服务分离，多队列路由（dataset/workflow/mail）
  - RAGFlow: 自研 Task Executor，独立进程，通过 Redis 通信
  - Haystack: 推荐 Celery 做 indexing pipeline 异步化
- **演进触发条件**（满足任一即考虑迁移）:
  - 需要定时任务调度（缓存清理、统计报表、数据同步）
  - 需要多种任务类型优先级路由（文档处理 vs Agent 执行 vs 通知）
  - Worker 实例 > 3 个，需要自动负载均衡和监控
- **候选方案对比**:
  | 方案 | 优势 | 劣势 |
  |------|------|------|
  | Celery | 生态最成熟、Beat/Flower/Canvas 全套、社区大 | 重、配置复杂、偶发内存泄漏 |
  | Dramatiq | 轻量、API 更现代、性能好 | 生态小、无官方 Beat |
  | arq | 纯 asyncio、极轻量、Redis 原生 | 功能少、无任务编排 |
  | 维持 Redis Streams | 零依赖、完全可控 | 功能需自建、维护成本随复杂度增长 |
- **建议路径**: 短期维持 Redis Streams（当前够用），中期如需 Beat/多队列则引入 arq 或 Dramatiq（asyncio 友好），长期如任务类型 > 5 种考虑 Celery
- **优先级**: 低（当前架构满足需求，作为技术储备跟踪）
- **预估工作量**: 评估 1 天，迁移 3-5 天

---

## 六、文件类型扩展

### 13. 音频文件上传解析（mp3/m4a/wav）
- **文件**: 新建 `backend/app/pipeline/loaders/audio_loader.py`、修改 `loader.py`、`document.py`、前端 `Documents.tsx`
- **现状**: 仅支持文本/文档/图片类文件，不支持音频
- **目标**:
  - 支持 mp3、m4a、wav 等音频文件上传
  - 通过 ASR（语音转文字）将音频转为文本后进入 pipeline
  - 候选方案：faster-whisper（轻量本地）、FunASR（中文优）、OpenAI Whisper API
- **优先级**: 中
- **预估工作量**: 2-3 天

---

## 七、对标 WeKnora 的新方向（参考腾讯开源 WeKnora v0.6）

> WeKnora（维娜拉）是腾讯开源的 LLM 知识管理框架，围绕 RAG 快速问答、ReAct Agent 智能推理、Wiki 自动生成三大核心能力构建。
> 以下方向基于对 WeKnora 架构和特性的分析，结合 Aladdin 现状提炼出可借鉴的改进点。

### 15. Rerank 分数阈值 + 兜底回复机制
- **文件**: `backend/app/retrieval/hybrid.py`、`backend/app/api/chat.py`
- **现状**: rerank 后无分数阈值过滤，即使所有结果与 query 完全不相关，仍返回 top_k 条结果给 LLM，容易产生幻觉
- **WeKnora 做法**:
  - 检索阈值可在对话策略中配置（知识库级别），低于阈值的结果直接丢弃
  - 当有效结果为空时，返回预设的"兜底回复"（如"知识库中未找到相关信息，请尝试换个问法"）
  - 兜底回复支持自定义模板，可按知识库配置不同的兜底话术
  - Rerank 前还会做"段落清洗"——去除候选文本中的页眉页脚残留、连续空行、页码标记等噪音，提升 rerank 模型的判断准确率
- **目标**:
  - `_rerank` 方法增加 `min_score` 参数（默认 0.15），低于阈值的结果丢弃
  - 知识库 config 中增加 `retrieval_threshold` 和 `fallback_reply` 字段
  - chat API 检测到有效结果为空时，在 system prompt 中注入"无相关信息"提示，引导 LLM 诚实回答
  - rerank 前对候选 content 做轻量清洗（strip 连续空行、去除纯页码行）
- **优先级**: P0（改动最小，效果最直接，防止幻觉）
- **预估工作量**: 0.5-1 天

### 16. BM25 全文检索（第三路召回）
- **文件**: `backend/app/storage/milvus.py`、`backend/app/retrieval/`、`backend/app/pipeline/pipeline.py`
- **现状**: 检索只有 Dense + Sparse（BGE-M3 sparse vector）两路。Sparse vector 基于 subword tokenizer，对中文精确关键词匹配（人名、案号、合同编号）效果不如传统 BM25
- **WeKnora 做法**:
  - 三路检索架构：BM25 稀疏召回 + Dense 稠密召回 + GraphRAG 图谱增强
  - BM25 通过 Elasticsearch 实现，使用 jieba 中文分词器，对精确关键词匹配效果显著优于 subword 级别的 sparse vector
  - 三路结果通过可配置权重的 RRF 融合，不同知识库可以调整各路权重（如法律文档加大 BM25 权重，技术文档加大 Dense 权重）
  - v0.3.0 引入增强索引技术，BM25 检索延迟 120ms，吞吐 850 queries/s
- **目标**:
  - 利用 Milvus 2.4+ 原生 BM25 全文检索能力（`TextMatch` / `FullTextSearch`），无需引入 Elasticsearch
  - Collection schema 增加 `content` 字段的全文索引（`enable_analyzer=True`）
  - 新建 `BM25Retriever`，检索时生成 BM25 查询
  - `HybridRetriever` 扩展为三路融合：Dense + Sparse + BM25，RRF 参数可配置
  - 知识库 config 中增加 `retrieval_weights` 字段，允许按场景调整各路权重
- **优先级**: P1（对精确匹配场景提升大，Milvus 原生支持无额外运维成本）
- **预估工作量**: 2-3 天

### 17. 端到端检索评测体系
- **文件**: 新建 `backend/app/evaluation/`、修改 `backend/app/api/retrieval.py`
- **现状**: 只有 `/api/retrieval/test` 单条测试接口，无批量评测、无量化指标
- **WeKnora 做法**:
  - 内置端到端测试模块，支持检索+生成全链路可视化评估
  - 评估指标：召回命中率（Recall@K）、BLEU、ROUGE 生成质量指标
  - 支持上传 QA 测试集（问题 + 期望命中的文档/chunk），自动计算各项指标
  - 提供 A/B 测试能力：对比不同检索策略（如调整 chunk size、RRF k 值、rerank 阈值）的效果差异
  - v0.3.0 报告 MAP 0.82、F1 0.79（多轮对话场景）
  - 评测结果可视化面板，展示每条 query 的命中/未命中详情
- **目标**:
  - 新建评测 API：`POST /api/evaluation/run`，接受 `[{query, expected_doc_ids, expected_content_keywords}]` 格式的测试集
  - 自动计算 Recall@5/10/20、MRR（Mean Reciprocal Rank）、命中率
  - 支持对比模式：同一测试集在不同参数配置下运行，输出对比报告
  - 前端增加"评测"页面，展示历史评测结果和趋势图
  - 后续可接入 RAGAS 框架做 Faithfulness/Answer Relevancy 评估
- **优先级**: P1（量化优化效果的基础设施，没有评测就是盲调）
- **预估工作量**: 3-4 天

### 18. 分块调试面板（可视化）
- **文件**: 前端新建 `frontend/src/pages/ChunkDebug.tsx`、后端增强 `/api/documents/{doc_id}/chunks`
- **现状**: 有 ChunkViewer 展示切片列表，但无法直观看到分块策略效果、元数据分布、embedding 质量
- **WeKnora 做法**:
  - v0.5.2 引入"自适应三层分块 + 实时调试面板"
  - 调试面板功能：上传文档后实时预览分块结果，每个 chunk 展示元数据（章节路径、页码、元素类型）
  - 支持调整分块参数（chunk size、overlap）后重新预览，无需重新入库
  - 展示 chunk 间的父子关系树状图
  - 展示 embedding 相似度热力图（chunk 之间的语义距离）
  - 支持选中某个 chunk 后模拟检索，查看该 chunk 在不同 query 下的排名
- **目标**:
  - 后端 `/api/documents/{doc_id}/chunks` 增加返回 `chunk_metadata`（章节路径、页码、元素类型、字符数）
  - 前端 ChunkViewer 增强：展示父子块树状结构、元数据标签、chunk 长度分布柱状图
  - 新增"模拟检索"功能：输入 query，高亮命中的 chunk，展示各 chunk 的相似度分数
  - 后续可加：分块参数调整预览（dry-run 模式，不实际入库）
- **优先级**: P2（调试工具，帮助理解系统行为和调优参数）
- **预估工作量**: 2-3 天

### 19. Langfuse 全链路可观测性集成
- **文件**: 新建 `backend/app/observability/`、修改 `backend/app/agent/orchestrator.py`、`backend/app/retrieval/hybrid.py`
- **现状**: 有 PipelineLogger 结构化日志 + trace_id，但缺少 Token 消耗追踪、Agent 决策链路可视化、检索延迟分布统计
- **WeKnora 做法**:
  - 无缝集成 Langfuse（开源可观测性平台），通过 Docker Profile 一键启动
  - 追踪维度：ReAct 循环每一步（路由/改写/检索/反思）、每次 LLM 调用的 input/output tokens、工具调用耗时、任务流水线各阶段
  - 提供 Trace 视图：完整展示一次对话从 query 到 answer 的全链路，包括中间的检索结果、LLM prompt、Agent 决策
  - 支持按 trace_id 关联 pipeline 入库和检索两个阶段的日志
  - Token 消耗统计：按模型、按知识库、按时间维度聚合，用于成本控制
  - 异常检测：自动标记延迟异常的 trace（如某次检索耗时 > P99）
- **目标**:
  - 引入 Langfuse Python SDK（`langfuse`），在 Agent 编排和检索链路中埋点
  - 每次 LLM 调用记录：model、input_tokens、output_tokens、latency、prompt_template
  - 每次检索记录：query、kb_id、mode、result_count、latency、top_score
  - Agent 编排记录：完整的 routing→rewriting→retrieval→reflection 链路
  - docker-compose 增加 langfuse profile，一键启动可观测性服务
  - 后续可加：成本报表页面、异常告警
- **优先级**: P2（生产环境必备，但不影响功能正确性）
- **预估工作量**: 2-3 天

### 20. 轻量级 GraphRAG（知识图谱增强检索）
- **文件**: 新建 `backend/app/pipeline/graph_extractor.py`、`backend/app/retrieval/graph.py`、修改 `backend/app/schema/db.py`
- **现状**: 纯向量检索，无法回答跨文档关联问题（如"A 公司和 B 公司的关系"）和全局性问题（如"这批合同涉及哪些方？"）
- **WeKnora 做法**:
  - 完整的 GraphRAG 实现，基于 Neo4j 知识图谱
  - 入库阶段：用 LLM 从文档中提取实体（人名、机构、金额、日期等）和关系（属于、签署、涉及等），构建知识图谱
  - 检索阶段：三路之一，通过图遍历找到与 query 相关的实体及其关联节点，补充向量检索可能遗漏的跨文档信息
  - Wiki 模式：Agent 从原始文档自动生成结构化、相互链接的 Markdown Wiki 页面及可视化知识图谱
  - v0.5.0 正式发布 Wiki 模式，支持知识图谱可视化浏览
  - 图谱数据支持增量更新（新文档入库时只提取新实体/关系，不重建全图）
- **目标**（分阶段）:
  - **Phase 1: 轻量实体提取（无 Neo4j）**
    - 入库时用 LLM 从每个 parent chunk 中提取关键实体（人名、机构、金额、日期）
    - 实体存入 PostgreSQL 的 `entities` 表（entity_name, entity_type, doc_id, chunk_id）
    - 检索时：先从 query 中提取实体关键词，在 entities 表中查找关联的 chunk_id，作为补充候选加入 RRF 融合
  - **Phase 2: 关系图谱（引入图数据库）**
    - 引入 Neo4j 或 PostgreSQL 的 AGE 扩展
    - 提取实体间关系，构建知识图谱
    - 检索时支持图遍历（1-2 跳），找到间接关联的信息
  - **Phase 3: Wiki 自动生成**
    - Agent 驱动，从知识图谱自动生成结构化 Wiki 页面
    - 支持知识图谱可视化浏览
- **优先级**: P3（长期方向，Phase 1 可作为中期目标）
- **预估工作量**: Phase 1: 3-4 天，Phase 2: 5-7 天，Phase 3: 7-10 天

### 22. ReAct Agent 模式（完整 Tool Calling 架构，对标 WeKnora v0.6）
- **文件**: 新建 `backend/app/agent/react_engine.py`、修改 `backend/app/agent/orchestrator.py`、`backend/app/api/chat.py`
- **现状**: Agent 模式使用固定管道（Planner→Executor→Reflector），虽然 v2 已支持意图拆分和分组检索，但检索策略仍由管道预设，LLM 无法动态决定搜索次数和角度
- **WeKnora 做法**:
  - 完整的 ReAct（Reasoning + Acting）循环：LLM 作为 Agent 自主决定调用什么工具
  - `knowledge_search` 是一个 Tool，Agent 可以传入 1-5 个语义查询，自己决定搜什么
  - Agent 可以在一次对话中多次调用 `knowledge_search`，每次用不同角度
  - 支持 `grep_chunks`（正则精确匹配）+ `knowledge_search`（语义检索）+ `list_knowledge_chunks`（深度阅读）三级工具链
  - MMR（Maximal Marginal Relevance）去冗余，确保结果多样性
  - Agent 自己做"反思"：通过 `thinking` tool 或内部推理判断信息是否充分
  - Progressive RAG 系统提示词：引导 Agent 按"侦察→规划→执行→反思"循环工作
  - 支持 `final_answer` 工具强制终止循环，避免无限迭代
  - 全链路 Langfuse 可观测性追踪
- **目标**（分阶段）:
  - **Phase 1: Function Calling 基础设施**
    - 实现 LLM Provider 的 function calling / tool use 接口（Ollama 和 vLLM 均支持）
    - 定义 `knowledge_search`、`list_chunks`、`final_answer` 三个核心 Tool Schema
    - 实现 ReAct 循环引擎：解析 LLM 的 tool_call → 执行 → 将结果注入上下文 → 继续生成
  - **Phase 2: Progressive RAG Agent**
    - 移植 WeKnora 的 Progressive RAG 系统提示词（适配中文法律场景）
    - 实现"侦察→规划→执行→反思"的 Agent 工作流
    - 支持 Agent 动态决定检索策略（语义 vs 关键词 vs 混合）
    - 增加 MMR 多样性控制
  - **Phase 3: 高级能力**
    - 支持 MCP 工具集成（外部工具调用）
    - 支持 Web Search 作为知识库检索的补充
    - Agent Skills 机制（可配置的专业技能模板）
    - 人机审批（高风险工具调用前确认）
- **优先级**: P2（当前 v2 Planner 方案已解决多意图问题，ReAct 是更彻底的长期方案）
- **预估工作量**: Phase 1: 3-4 天，Phase 2: 4-5 天，Phase 3: 5-7 天
- **前置依赖**: LLM 需支持 function calling（Ollama qwen2.5 已支持，vLLM 原生支持）

### 23. 多轮对话 Query Rewrite（指代消解 + 意图分类，参考 WeKnora）
- **文件**: 新建 `backend/app/agent/query_rewrite.py`、修改 `backend/app/api/chat.py`
- **现状**: 多轮对话中用户说"它的第25条是什么"，系统无法理解"它"指代的是上一轮提到的法律名称，导致检索失败
- **WeKnora 做法**:
  - 在对话入口（chat API 层）增加 Query Rewrite 步骤，在消息进入 Agent 之前先做指代消解
  - 结合对话历史，将代词（它、这个、那个）替换为明确的实体
  - 同时做意图分类（greeting / kb_search / follow_up / chitchat 等），非检索意图直接跳过 Agent
  - 输出结构化 JSON：`{rewrite_query, intent}`
  - 改写后的查询保留核心实体和关键词，不生成元指令（如"请搜索..."）
- **目标**:
  - 在 chat API 层增加 rewrite 步骤，对多轮对话做指代消解
  - 意图分类：greeting/chitchat 直接回复，kb_search 进入检索流程
  - 改写后的 query 传给 Agent/检索层，替代原始 user message
- **优先级**: P1（多轮对话体验的基础）
- **预估工作量**: 1-2 天

### 24. Agent 编排优化：Router + Rewriter 并行（替代 Planner 单次调用）
- **文件**: 修改 `backend/app/agent/orchestrator.py`、`backend/app/agent/planner.py`
- **现状**: Planner 合并了路由判断和查询改写为一次 LLM 调用，对 simple 查询浪费（改写部分用不上），且用户等待时只看到"正在分析问题类型..."
- **目标**:
  - 拆分 Planner 为 Router（轻量，只判断 simple/complex）+ IntentRewriter（重量，意图拆分+查询生成）
  - Router 和 IntentRewriter 并行启动，Router 先返回就先推送进度
  - simple 查询：Router 完成后立即取消 IntentRewriter，直接走快路径检索
  - complex 查询：等 IntentRewriter 完成，继续分组检索
  - 前端进度体验优化：simple 查询 1-2 秒内开始检索，不再等改写
- **优先级**: P1（用户体验优化）
- **预估工作量**: 1 天

### 25. MMR 多样性去重（Maximal Marginal Relevance）
- **文件**: 修改 `backend/app/retrieval/hybrid.py`
- **现状**: Rerank 后直接按分数取 top-K，当知识库有大量相似 chunk（如同一段话被切成多个重叠 chunk）时，返回结果高度重复
- **WeKnora 做法**:
  - 在 Rerank 之后、返回最终结果之前，应用 MMR 算法
  - lambda=0.7（平衡相关性和多样性）
  - 基于 Jaccard 相似度计算 chunk 间的冗余度
  - 迭代选择：每次选 MMR 分数最高的候选，直到达到 top_k
- **目标**:
  - 在 HybridRetriever 的 `_rerank` 之后增加 MMR 步骤
  - 对 Rerank 输出的 top-50 应用 MMR，选出最终 top-10
  - 确保返回结果既相关又多样，避免同一文档的相邻 chunk 占满结果
- **优先级**: P2（当前法条场景重复问题不严重，文档型知识库更需要）
- **预估工作量**: 0.5 天

### 28. RRF 自适应权重融合（Post-hoc Adaptive Fusion）
- **文件**: 修改 `backend/app/retrieval/hybrid.py`
- **现状**: 三路检索（Dense + Sparse + BM25）使用 uniform RRF 融合，各路权重相同。对于精确关键词查询（案号、人名），BM25 应该权重更高；对于语义理解查询（争议焦点），Dense 应该权重更高。当前 uniform 权重会让各路互相稀释
- **设计思路**:
  - 不预判查询类型（规则无法穷举），而是让三路检索结果的**分数分布**自动决定权重
  - 先用 uniform RRF 拿到三路各自的结果，然后根据各路 top-K 分数分布特征动态调整权重
  - 信号：某路 top-1 分数远高于 top-10（分数衰减陡峭）→ 该路有高置信命中 → 权重拉高
  - 信号：某路 top-10 分数都很平（无明显头部）→ 该路没找到强相关的 → 权重降低
  - 三路分数需先做 min-max 归一化到同一尺度后再计算 confidence
- **计算方式**:
  ```python
  def compute_adaptive_weight(scores: list[float]) -> float:
      top1 = scores[0]
      top10 = scores[min(9, len(scores) - 1)]
      spread = top1 - top10  # 分数跨度作为置信度信号
      confidence = top1 * 0.6 + spread * 0.4
      return max(confidence, 0.01)  # 最低保底，不完全压制任何一路
  # 三路权重 = 各自 confidence 归一化
  ```
- **优势**:
  - 零延迟（纯数值计算），不需要额外 LLM 调用
  - 不需要规则维护，对任何查询类型自动适应
  - 对任何领域、任何语言都适用
- **优先级**: P1（对"又要语义又要关键词"的混合场景提升最直接）
- **预估工作量**: 1 天

### 26. Reranker 模型升级 + 远程 Rerank API 支持
- **文件**: 修改 `backend/app/models/rerank/`、`backend/app/config.py`
- **现状**: 使用 BGE-reranker-v2-m3（本地部署），对于语义高度相似的候选（如"第三十条"vs"第三十一条"）区分度有限
- **可选升级路径**:
  - `bge-reranker-v2-gemma`：更强但更慢（需要 GPU），适合对精度要求极高的场景
  - `jina-reranker-v2-base-multilingual`：多语言支持好，中文效果优秀
  - 远程 Rerank API（Cohere rerank / Jina rerank API）：无需本地 GPU，按调用计费
- **目标**:
  - 支持远程 Rerank API（类似现有的远程 Embedding 支持）
  - 配置化选择 rerank 模型（本地 vs 远程）
  - 评测不同 reranker 在法条场景的精度差异
- **优先级**: P2（当前 BGE-reranker-v2-m3 + 文件名前缀已基本够用）
- **预估工作量**: 1-2 天

### 27. Embedding 上下文增强（Contextual Embedding）
- **文件**: 修改 `backend/app/pipeline/pipeline.py`、`backend/app/pipeline/embedder.py`
- **现状**: Dense embedding 用原始 chunk content 生成向量，不包含文档级上下文信息。当用户查询包含文档名但 chunk 内容不含文档名时，语义检索无法匹配
- **RAGFlow / LlamaIndex 做法**:
  - 生成 embedding 时，输入文本为 `{章节路径}: {chunk content}`（如"电影产业促进法 > 第三章 > 第二十五条: 依照本法规定..."）
  - 存储的 content 保持原文不变（展示给用户的不受影响）
  - 只影响 Dense 向量的生成，BM25 和 Rerank 不受影响
- **目标**:
  - 在 embedding 输入时拼接章节路径/文档标题（当前已有 ContextualEmbedder 模块）
  - 评估对检索精度的影响（可能对某些场景有帮助，某些场景引入噪音）
  - 可配置开关，按知识库决定是否启用
- **优先级**: P3（当前 BM25 content 前缀已解决文档归属问题，Dense embedding 增强收益有限）
- **预估工作量**: 0.5 天

### 21. 数据源连接器（飞书/Notion/语雀自动同步）
- **文件**: 新建 `backend/app/connectors/`
- **现状**: 只支持手动上传文件，不支持从外部平台自动同步知识
- **WeKnora 做法**:
  - 支持飞书、Notion、语雀等外部平台的知识库自动同步
  - 支持增量同步和全量同步两种模式
  - 连接器凭据使用 AES-256-GCM 加密存储
  - 同步任务通过 MQ 异步执行，支持定时触发
  - v0.4.0 引入 Notion 连接器，v0.5.2 引入语雀连接器
  - 同步状态可视化：展示最近同步时间、同步文档数、失败记录
- **目标**:
  - 设计统一的 Connector 接口（`list_documents`、`fetch_content`、`check_updates`）
  - 优先实现飞书文档连接器（企业内网场景最常用）
  - 支持定时同步（cron 表达式）和手动触发
  - 同步记录持久化，支持增量更新（只同步变更的文档）
- **优先级**: P3（企业级需求，当前手动上传已满足基本场景）
- **预估工作量**: 每个连接器 3-5 天

---

## 优先级排序建议

**P0（检索质量基础）**:
1. #15 Rerank 分数阈值 + 兜底回复（改动最小，防幻觉）
2. #4 Chunk 元数据增强（Step 1）
3. #5 元数据过滤检索
4. #8 Embedding 上下文增强

**P1（准度提升）**:
5. #16 BM25 全文检索（第三路召回，精确匹配提升）
6. #17 端到端检索评测体系（量化优化的基础）
7. #7 文档预处理（去噪 Step 1）
8. #6 多知识库联合检索
9. #10 RAG 评估体系

**P2（工程完善 + 可观测性）**:
10. #18 分块调试面板
11. #19 Langfuse 全链路可观测性
12. #22 ReAct Agent 模式（Phase 1: Function Calling 基础设施）
13. #3 数据库迁移管理
14. #1 CSV 大文件流式读取
15. #13 音频文件支持

**P3（长期方向）**:
16. #20 轻量级 GraphRAG（Phase 1 实体提取）
17. #22 ReAct Agent 模式（Phase 2-3: 完整 Agent 能力）
18. #21 数据源连接器（飞书/Notion）
19. #9 超长记录拆分
20. #2 Embedding 配置化
21. #12 大文件上传方案
22. #14 任务队列框架演进
