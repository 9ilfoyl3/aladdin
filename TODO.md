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

## 四、文件类型扩展

### 11. 音频文件上传解析（mp3/m4a/wav）
- **文件**: 新建 `backend/app/pipeline/loaders/audio_loader.py`、修改 `loader.py`、`document.py`、前端 `Documents.tsx`
- **现状**: 仅支持文本/文档/图片类文件，不支持音频
- **目标**:
  - 支持 mp3、m4a、wav 等音频文件上传
  - 通过 ASR（语音转文字）将音频转为文本后进入 pipeline
  - 候选方案：faster-whisper（轻量本地）、FunASR（中文优）、OpenAI Whisper API
- **优先级**: 中
- **预估工作量**: 2-3 天

---

## 优先级排序建议

**P0（检索质量基础）**:
1. #4 Chunk 元数据增强（Step 1）
2. #5 元数据过滤检索
3. #8 Embedding 上下文增强

**P1（准度提升）**:
4. #7 文档预处理（去噪 Step 1）
5. #6 多知识库联合检索
6. #10 RAG 评估体系

**P2（工程完善）**:
7. #3 数据库迁移管理
8. #1 CSV 大文件流式读取
9. #11 音频文件支持

**P3（锦上添花）**:
10. #9 超长记录拆分
11. #2 Embedding 配置化
