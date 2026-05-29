# Requirements Document

## Introduction

本 spec 覆盖 Artoo RAG 系统文档处理管道的 P0/P1 优化项，目标是将当前基于 `asyncio.create_task` 的临时方案升级为生产级可用的任务处理系统。参考 RAGFlow 的设计理念，重点解决：任务持久化、进度可观测、多策略切分、并发控制、链路追踪。

## Glossary

| 术语 | 定义 |
|------|------|
| Pipeline | 文档处理管道，包含 load → OCR → chunk → embed → index 全流程 |
| Task Queue | 持久化任务队列，确保任务不因进程重启而丢失 |
| Worker | 消费任务队列的后台处理进程 |
| Chunker | 文档切分器，将文本按策略切分为父子 chunk |
| Progress | 文档处理进度（0-100%），实时更新到数据库 |
| Semaphore | 信号量，用于限制并发处理的文件数 |
| Structured Log | 结构化日志，JSON 格式，包含 trace_id、耗时、阶段等字段 |
| DLQ | Dead Letter Queue，死信队列，存放多次重试仍失败的任务 |
| Redis Stream | Redis 5.0+ 提供的持久化消息流，支持 consumer group |
| trace_id | 链路追踪 ID，贯穿单个文档处理全流程的唯一标识 |

## Requirements

### Requirement 1: 持久化任务队列

**User Story:** 作为系统运维人员，我希望文档处理任务在服务重启后能自动恢复，以避免用户上传的文件丢失处理。

#### Acceptance Criteria

1. 当用户上传文档时，系统将处理任务写入 Redis Stream（stream key: `pipeline:tasks`），包含 doc_id、kb_id、file_path 等元数据，而非直接 `asyncio.create_task`
2. 当后端服务启动时，Worker 通过 consumer group 从 Redis Stream 中消费未 ACK 的任务（pending entries），自动恢复中断的处理
3. 当任务执行失败时，系统自动重试最多 3 次，每次间隔指数退避（1s, 2s, 4s），重试次数记录在消息的 retry_count 字段中
4. 当所有重试均失败时，任务转移到死信 stream（`pipeline:dlq`），文档状态标记为 failed 并记录最终错误信息
5. 当系统运行时，可通过 `GET /api/system/queue-stats` 查询当前队列深度、活跃 Worker 数、pending 任务数、DLQ 任务数
6. 当 Redis 不可用时（连接失败或超时），系统降级为原有的 `asyncio.create_task` 模式，并输出 WARNING 级别日志 `"Redis unavailable, falling back to in-process task"`
7. 当任务开始处理前，Worker 先检查文档当前状态，若已为 completed 则跳过（防止重复处理）

### Requirement 2: 处理进度实时追踪

**User Story:** 作为文档上传用户，我希望看到文档处理的实时进度百分比，以了解还需等待多长时间。

#### Acceptance Criteria

1. 当文档进入处理流程时，Document 表新增 `progress` 字段（Integer, 0-100）和 `progress_message` 字段（String），各阶段权重为：load(10%) → OCR(20%) → chunk(20%) → embed(40%) → index(10%)
2. 当前端轮询 `GET /api/documents/{id}` 时，响应 JSON 中包含 `progress`（整数 0-100）和 `progress_message`（如 "正在生成向量 (3/10 批)"）
3. 当 embed 阶段处理大量 chunk 时，进度按已完成 batch 数 / 总 batch 数在 embed 权重区间（30%-70%）内线性插值更新
4. 当文档处理完成时，progress 设为 100，progress_message 设为 "处理完成"，status 为 completed
5. 当文档处理失败时，progress 停留在失败时的值不变，progress_message 记录失败阶段，status 为 failed
6. 当文档跳过 OCR 阶段（文本非空且无图片）时，OCR 权重的 20% 直接累加到当前进度，不产生中间更新

### Requirement 3: 多 Chunker 策略路由

**User Story:** 作为知识库管理员，我希望系统能根据文件类型和内容特征自动选择最优的切分策略，以提升不同类型文档的检索准确率。

#### Acceptance Criteria

1. 系统提供以下 Chunker 实现，均继承 `BaseChunker` 抽象类：
   - `NaiveChunker`：通用文本，即当前的 HierarchicalChunker（结构感知 + 父子切分）
   - `TableChunker`：CSV/XLSX 表格数据（复用现有 loader pre_chunked 逻辑）
   - `LawsChunker`：法律文书，按条款编号（第X条）、判决结构（本院认为/判决如下）切分为父块，条款内容切分为子块
   - `PaperChunker`：学术论文，按 Abstract/Introduction/Methods/Results/Conclusion/References 章节切分为父块
   - `QAChunker`：问答对格式，每个 Q+A 配对作为一个父块，Q 和 A 分别作为子块
2. 当文件上传时，`ChunkerRouter.select(file_type, content)` 按以下优先级自动选择 Chunker：
   - 文件扩展名为 csv/xlsx → TableChunker
   - 内容中法律关键词（本院认为|判决如下|第[一-十\d]+条）出现 3 次以上 → LawsChunker
   - 内容中出现 "Abstract" 且出现 "References" 或 "Bibliography" → PaperChunker
   - 内容中 Q:/A: 或 问:/答: 配对出现 5 次以上 → QAChunker
   - 其他 → NaiveChunker
3. 当知识库 config JSON 中设置了 `chunker_type` 字段时，Pipeline 使用指定的 Chunker，忽略自动路由结果
4. 所有 Chunker 实现的 `chunk(text, metadata)` 方法均返回 `ChunkResult(parent_chunks, child_chunks, parent_child_map)` 结构
5. 新增 Chunker 只需：继承 `BaseChunker`、实现 `chunk()` 方法、在 `ChunkerFactory.REGISTRY` dict 中注册类型名 → 类的映射

### Requirement 4: 文件处理并发控制

**User Story:** 作为系统运维人员，我希望同时处理的文件数有上限控制，以防止多文件并发上传时内存溢出导致服务崩溃。

#### Acceptance Criteria

1. Worker 通过 `asyncio.Semaphore(N)` 限制同时处理的文件数，N 由环境变量 `PIPELINE_MAX_CONCURRENT` 控制，默认值为 3
2. 当并发处理文件数达到上限时，新消费的任务在 semaphore.acquire() 处等待，不会被丢弃也不会 NACK 回队列
3. 当某个文件处理完成（completed）或失败（failed）时，semaphore 立即释放，下一个等待的任务开始处理
4. 当系统启动时，日志输出 `"Pipeline worker started, max_concurrent={N}"`

### Requirement 5: 可观测性（结构化日志 + 链路追踪）

**User Story:** 作为系统运维人员，我希望每个文档处理过程有完整的链路追踪和耗时统计，以便快速定位性能瓶颈和故障原因。

#### Acceptance Criteria

1. 每个文档处理任务分配唯一 `trace_id`（UUID4），该 trace_id 作为参数传递给 Pipeline 各阶段方法，并出现在所有相关日志中
2. 每个阶段（load/ocr/chunk/embed/index）完成后输出一条 JSON 结构化日志到 stdout，包含字段：`{"trace_id": "...", "doc_id": "...", "stage": "embed", "duration_ms": 1234, "input_size": 50, "output_size": 50, "status": "success"}`
3. embed 阶段的日志额外包含：`"batch_count": 10, "total_chunks": 1280, "avg_batch_duration_ms": 123`
4. 当文档处理完成时，输出一条汇总日志：`{"trace_id": "...", "doc_id": "...", "stage": "summary", "total_duration_ms": 5678, "stages": {"load": 100, "ocr": 0, "chunk": 200, "embed": 5000, "index": 378}}`
5. 日志通过 Python `logging` 模块输出，配置 `JSONFormatter`，所有 pipeline 相关 logger 使用 `pipeline.*` 命名空间
6. 当任何阶段耗时超过环境变量 `PIPELINE_SLOW_THRESHOLD_MS`（默认 30000ms）时，该阶段日志级别从 INFO 提升为 WARNING，并额外添加 `"slow": true` 字段
