# Requirements Document

## Introduction

本文档定义 RAG 检索质量优化功能集合的需求规格，涵盖五个核心模块：Chunk 元数据增强、元数据过滤检索、Embedding 上下文增强、文档预处理去噪、多知识库联合检索。按 P0（#4、#5、#8）和 P1（#7、#6）两个优先级阶段实施。

## Glossary

- **Chunk**: 文档切分后的文本片段，分为 parent chunk（大块，用于上下文返回）和 child chunk（小块，用于精准检索）
- **RRF**: Reciprocal Rank Fusion，多路检索结果融合算法
- **Pre-filter**: Milvus 在向量检索前基于 scalar 字段的过滤机制
- **element_type**: chunk 的元素类型，包括 text（正文）、table（表格）、title（标题）
- **section_path**: chunk 所属的章节标题层级路径

## Requirements

### Requirement 1: Chunk 元数据增强

**User Story:** 作为系统，我需要在文档入库时自动提取并存储结构化元数据，以便后续支持元数据过滤检索和上下文增强 embedding。

#### Acceptance Criteria

- 1.1 When 创建新的 Milvus collection 时, then schema 必须包含 `file_type` (VARCHAR(20)) 和 `element_type` (VARCHAR(20)) 两个 scalar 字段，并为这两个字段创建 scalar 索引。
- 1.2 When 文档经过 pipeline 入库时, then 每个 chunk 的 SQLite 记录中 `chunk_metadata` JSON 字段必须包含: filename、file_type、chunker_type、chunk_index，且 filename 和 file_type 非空。
- 1.3 When PDF 文档入库时, then 每个 child chunk 的 chunk_metadata 中应包含 `page_num` 字段，值为该 chunk 内容所在的 PDF 页码（从1开始），无法定位时为 null。
- 1.4 When 结构化文档（含标题标记的 PDF/Markdown/DOCX）入库时, then chunk_metadata 中应包含 `section_path` 字段，记录该 chunk 所属的章节标题层级路径（如 `["第三章", "第二节"]`）。
- 1.5 When chunk 入库时, then 系统应自动识别 chunk 的元素类型（text/table/title），并写入 chunk_metadata 的 `element_type` 字段和 Milvus 的 `element_type` scalar 字段。

### Requirement 2: 元数据过滤检索

**User Story:** 作为系统，我需要在向量检索时支持基于元数据的 pre-filter，以便缩小搜索范围、提升检索精度，并对表格类 chunk 进行降权处理。

#### Acceptance Criteria

- 2.1 When HybridRetriever 执行检索时, then search_dense 和 search_sparse 方法必须支持传入 `expr` 参数，将其作为 Milvus 的 pre-filter 表达式传递给搜索请求。
- 2.2 When 用户指定 filter_doc_ids 参数时, then 检索结果必须仅包含 doc_id 在指定列表中的 chunk，不返回其他文档的结果。
- 2.3 When 用户指定 file_type 过滤条件时, then 检索结果必须仅包含 file_type 匹配指定类型的 chunk。
- 2.4 When RRF 融合排序时, then element_type 为 "table" 的 chunk 的 RRF 分数应乘以 0.8 的降权系数，确保大量表格行不会淹没文本检索结果。
- 2.5 When 构造过滤条件时, then RetrievalFilter 对象能正确生成合法的 Milvus expr 字符串，支持 doc_id 和 file_type 的组合过滤（AND 连接）。

### Requirement 3: Embedding 上下文增强

**User Story:** 作为系统，我需要在 embedding 阶段为 child chunk 拼接标题路径和父块上下文，以便提升脱离上下文后语义模糊的 chunk 的检索召回率。

#### Acceptance Criteria

- 3.1 When 对 child chunk 执行 embedding 时, then embedding 输入文本必须以 `[文件名 | 章节路径]` 格式的前缀开头，其中章节路径来自 chunk_metadata 的 section_path。
- 3.2 When child chunk 有对应的 parent chunk 时, then embedding 输入文本应在前缀和 child chunk 之间插入 parent chunk 的前 150 字符作为上下文补充。
- 3.3 When 写入 Milvus 和 SQLite 时, then content 字段存储的是原始 child chunk 文本，不包含上下文增强前缀，增强仅影响 embedding 向量的生成。
- 3.4 When parent chunk 的前 150 字符与 child chunk 的前 150 字符相同时, then 不拼接父块上下文（避免重复信息降低 embedding 质量）。

### Requirement 4: 文档预处理去噪

**User Story:** 作为系统，我需要在文档切分前去除页眉页脚、页码等噪音文本，以便减少无意义 chunk 对检索结果的干扰。

#### Acceptance Criteria

- 4.1 When 处理 PDF 文档时, then 使用 pymupdf 的 `get_text("dict")` 获取文本块 bbox 坐标，过滤页面顶部 5% 和底部 5% 区域内的短文本块（<100字符），长文本即使在边缘区域也保留。
- 4.2 When 文档包含 2 页以上时, then 检测每页首尾各 3 行中出现频率超过 50% 的短文本（2-50字符），将其判定为页眉页脚并从文本中去除。
- 4.3 When 文本中包含纯页码行时, then 使用正则匹配去除以下格式的独立行: `- N -`、`第 N 页`、`Page N of M`、`N/M`、纯 1-4 位数字。
- 4.4 When 文档经过 pipeline 处理时, then TextCleaner 在 Loader 加载之后、Chunker 切分之前执行，确保噪音文本不进入 chunk。
- 4.5 When 知识库 config 中设置 `enable_cleaner: false` 时, then pipeline 跳过 TextCleaner 阶段，直接将 Loader 输出传给 Chunker。

### Requirement 5: 多知识库联合检索

**User Story:** 作为用户，我需要在对话时同时检索多个知识库的内容，以便获得跨知识库的综合回答。

#### Acceptance Criteria

- 5.1 When 用户发起对话请求时, then ChatCompletionRequest 支持 `kb_ids: list[str]` 参数，指定多个知识库进行联合检索，向后兼容现有 `knowledge_base_id` 单库参数。
- 5.2 When 指定多个知识库时, then 系统使用 asyncio.gather 并行检索所有知识库（每个库 skip_rerank=True），总延迟等于最慢库的延迟而非累加。
- 5.3 When 多库结果合并时, then 主库（列表第一个）的 priority 为 1.0，辅助库默认 priority 为 0.8，RRF 分数乘以对应 priority 后再合并排序。
- 5.4 When 多库结果合并完成后, then 对合并后的候选集执行一次统一 Rerank + 父块扩展，而非每个库单独 Rerank。
- 5.5 When 某个辅助知识库检索失败（collection 不存在或超时）时, then 该库返回空结果，其他库结果正常合并返回，响应 metadata 中标注 degraded=True。
- 5.6 When 用户在对话设置中关联额外知识库时, then 前端将选中的知识库 ID 列表通过 kb_ids 参数传递给后端，主库为当前对话绑定的知识库。
