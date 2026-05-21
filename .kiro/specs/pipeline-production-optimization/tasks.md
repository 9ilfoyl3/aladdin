# Implementation Plan: Pipeline 生产级优化

## Overview

将 Aladdin RAG 文档处理管道从 `asyncio.create_task` 临时方案升级为基于 Redis Stream 的生产级任务处理系统。实施按模块递进：配置扩展 → 数据模型 → 核心基础设施（队列/日志/进度） → Chunker 策略 → Worker 编排 → API 集成 → Pipeline 改造。

## Tasks

- [x] 1. 配置扩展与数据模型准备
  - [x] 1.1 扩展 Settings 配置类，新增 Redis 和 Pipeline Worker 相关配置项
    - 在 `backend/app/config.py` 的 `Settings` 类中新增：`redis_url`、`pipeline_max_concurrent`、`pipeline_max_retries`、`pipeline_slow_threshold_ms` 字段
    - 在 `backend/.env.example` 中添加对应环境变量示例
    - _Requirements: 1.1, 4.1, 5.6_

  - [x] 1.2 扩展 Document 数据模型，新增 progress 和 progress_message 字段
    - 在 `backend/app/schema/db.py` 的 `Document` 类中新增 `progress: Mapped[int]`（默认 0）和 `progress_message: Mapped[Optional[str]]` 字段
    - 创建 Alembic migration 脚本
    - _Requirements: 2.1_

  - [x] 1.3 定义 ChunkResult 数据结构和 QueueStats 响应模型
    - 在 `backend/app/pipeline/chunker_router.py` 中定义 `ChunkResult` dataclass（复用现有 chunker.py 中的定义）
    - 在 `backend/app/pipeline/queue.py` 中定义 `TaskMessage` dataclass 和 `QueueStats` Pydantic model
    - _Requirements: 1.1, 1.5, 3.4_

- [x] 2. 结构化日志模块
  - [x] 2.1 实现 PipelineLogger 结构化日志器
    - 创建 `backend/app/pipeline/logging.py`
    - 实现 `JSONFormatter` 格式化器，配置 `pipeline.*` 命名空间
    - 实现 `PipelineLogger` 类：`stage_complete()`、`summary()` 方法
    - 每个阶段日志包含：trace_id、doc_id、stage、duration_ms、input_size、output_size、status
    - embed 阶段额外包含：batch_count、total_chunks、avg_batch_duration_ms
    - 慢阶段检测：duration_ms > slow_threshold_ms 时级别提升为 WARNING，添加 `"slow": true`
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6_

  - [x] 2.2 编写 PipelineLogger 属性测试
    - **Property 12: 阶段日志包含所有必需字段**
    - **Property 13: 汇总日志各阶段耗时之和等于总耗时**
    - **Property 14: 慢阶段检测阈值正确**
    - **Validates: Requirements 5.2, 5.4, 5.6**

  - [x] 2.3 编写 PipelineLogger 单元测试
    - 测试 JSON 格式输出正确性
    - 测试 trace_id 一致性（Property 11）
    - 测试 summary 各阶段耗时汇总
    - _Requirements: 5.1, 5.2, 5.4_

- [x] 3. 进度追踪模块
  - [x] 3.1 实现 ProgressTracker 进度追踪器
    - 创建 `backend/app/pipeline/progress.py`
    - 定义 `PipelineStage` 枚举和 `STAGE_WEIGHTS` 权重映射
    - 实现 `ProgressTracker` 类：`start_stage()`、`complete_stage()`、`skip_stage()`、`update_sub_progress()`、`fail()`、`complete()` 方法
    - 实现 `interpolate()` 静态方法：阶段内线性插值计算
    - 进度更新失败时仅记录 WARNING，不影响主流程
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6_

  - [x] 3.2 编写 ProgressTracker 属性测试
    - **Property 5: Embed 阶段进度线性插值正确**
    - **Property 6: 失败时进度值不变**
    - **Validates: Requirements 2.3, 2.5**

  - [x] 3.3 编写 ProgressTracker 单元测试
    - 测试各阶段权重区间正确
    - 测试 skip_stage 直接累加权重
    - 测试 complete 设置 progress=100
    - _Requirements: 2.1, 2.4, 2.6_

- [x] 4. Checkpoint - 确保基础模块测试通过
  - 确保所有测试通过，ask the user if questions arise.

- [x] 5. 持久化任务队列模块
  - [x] 5.1 实现 TaskQueue Redis Stream 任务队列
    - 创建 `backend/app/pipeline/queue.py`（扩展 1.3 中的定义）
    - 实现 `TaskQueue` 类：`enqueue()`、`consume()`、`ack()`、`move_to_dlq()`、`claim_pending()`、`get_stats()` 方法
    - 实现 `create()` 工厂方法：Redis 不可用时返回 None
    - 使用 `redis.asyncio` 客户端，stream key 为 `pipeline:tasks`，DLQ key 为 `pipeline:dlq`，group 为 `pipeline-workers`
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6_

  - [x] 5.2 编写 TaskQueue 属性测试
    - **Property 1: 任务入队保留所有元数据**
    - **Property 2: 重试计数与退避时间正确**
    - **Property 3: 队列统计准确反映实际状态**
    - 使用 fakeredis 模拟 Redis
    - **Validates: Requirements 1.1, 1.3, 1.5**

  - [x] 5.3 编写 TaskQueue 单元测试
    - 测试 enqueue/consume/ack 基本流程
    - 测试 DLQ 转移逻辑
    - 测试 Redis 不可用时 create() 返回 None
    - _Requirements: 1.1, 1.4, 1.6_

- [x] 6. 多 Chunker 策略路由模块
  - [x] 6.1 实现 BaseChunker 抽象基类和 ChunkerFactory 工厂
    - 创建 `backend/app/pipeline/chunker_router.py`
    - 定义 `BaseChunker` ABC：抽象方法 `chunk(text, metadata) -> ChunkResult`
    - 实现 `ChunkerFactory`：`REGISTRY` dict、`register()`、`create()` 方法
    - 实现 `ChunkerRouter`：`select(file_type, content)` 方法，按优先级匹配规则
    - _Requirements: 3.1, 3.2, 3.4, 3.5_

  - [x] 6.2 实现 NaiveChunker（包装现有 HierarchicalChunker）
    - 创建 `backend/app/pipeline/chunkers/__init__.py`
    - 创建 `backend/app/pipeline/chunkers/naive.py`
    - 继承 `BaseChunker`，内部委托给现有 `HierarchicalChunker`
    - 在 `ChunkerFactory.REGISTRY` 中注册 `"naive"` 类型
    - _Requirements: 3.1_

  - [x] 6.3 实现 TableChunker
    - 创建 `backend/app/pipeline/chunkers/table.py`
    - 继承 `BaseChunker`，复用现有 loader pre_chunked 逻辑
    - 在 `ChunkerFactory.REGISTRY` 中注册 `"table"` 类型
    - _Requirements: 3.1_

  - [x] 6.4 实现 LawsChunker
    - 创建 `backend/app/pipeline/chunkers/laws.py`
    - 继承 `BaseChunker`，按条款编号（第X条）和判决结构切分为父块，条款内容切分为子块
    - 在 `ChunkerFactory.REGISTRY` 中注册 `"laws"` 类型
    - _Requirements: 3.1_

  - [x] 6.5 实现 PaperChunker
    - 创建 `backend/app/pipeline/chunkers/paper.py`
    - 继承 `BaseChunker`，按 Abstract/Introduction/Methods/Results/Conclusion/References 章节切分
    - 在 `ChunkerFactory.REGISTRY` 中注册 `"paper"` 类型
    - _Requirements: 3.1_

  - [x] 6.6 实现 QAChunker
    - 创建 `backend/app/pipeline/chunkers/qa.py`
    - 继承 `BaseChunker`，每个 Q+A 配对作为父块，Q 和 A 分别作为子块
    - 在 `ChunkerFactory.REGISTRY` 中注册 `"qa"` 类型
    - _Requirements: 3.1_

  - [x] 6.7 编写 ChunkerRouter 和 Chunker 属性测试
    - **Property 7: 所有 Chunker 返回结构有效的 ChunkResult**
    - **Property 8: Chunker 路由优先级正确**
    - **Property 9: 手动指定 chunker_type 覆盖自动路由**
    - **Validates: Requirements 3.1, 3.2, 3.3, 3.4**

  - [x] 6.8 编写各 Chunker 单元测试
    - 测试 LawsChunker 条款识别（法律文书样本）
    - 测试 PaperChunker 章节识别（论文样本）
    - 测试 QAChunker 配对识别
    - 测试 ChunkerRouter 各规则边界条件（关键词恰好 3 次）
    - _Requirements: 3.1, 3.2_

- [x] 7. Checkpoint - 确保队列和 Chunker 模块测试通过
  - 确保所有测试通过，ask the user if questions arise.

- [x] 8. PipelineWorker 工作进程
  - [x] 8.1 实现 PipelineWorker 任务消费与并发控制
    - 创建 `backend/app/pipeline/worker.py`
    - 实现 `PipelineWorker` 类：`start()`、`stop()`、`_process_task()`、`_handle_failure()` 方法
    - 启动时先 `claim_pending()` 恢复中断任务，再循环 `consume()` 新任务
    - 使用 `asyncio.Semaphore(max_concurrent)` 控制并发
    - 处理前检查文档状态，已 completed 则跳过（幂等）
    - 失败重试：retry_count < max_retries 时指数退避重新入队，否则移入 DLQ
    - 不可重试错误（FileNotFoundError、ValueError、PermissionError）直接进 DLQ
    - 启动日志：`"Pipeline worker started, max_concurrent={N}"`
    - _Requirements: 1.2, 1.3, 1.4, 1.7, 4.1, 4.2, 4.3, 4.4_

  - [x] 8.2 编写 PipelineWorker 属性测试
    - **Property 4: 已完成文档被幂等跳过**
    - **Property 10: 并发数不超过 Semaphore 上限**
    - **Validates: Requirements 1.7, 4.1, 4.2, 4.3**

  - [x] 8.3 编写 PipelineWorker 单元测试
    - 测试启动恢复 pending 任务
    - 测试重试逻辑和指数退避
    - 测试 DLQ 转移
    - 测试 Redis 断连后等待重连
    - _Requirements: 1.2, 1.3, 1.4_

- [x] 9. Pipeline 改造与 API 集成
  - [x] 9.1 改造 DocumentPipeline，集成 ProgressTracker 和 StructuredLogger
    - 修改 `backend/app/pipeline/pipeline.py`
    - `process()` 方法接受可选 `trace_id` 参数
    - 各阶段调用 `ProgressTracker` 更新进度
    - 各阶段完成后通过 `PipelineLogger` 输出结构化日志
    - embed 阶段按 batch 更新子进度
    - 跳过 OCR 时调用 `skip_stage()`
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 5.1, 5.2, 5.3, 5.4_

  - [x] 9.2 改造 DocumentPipeline，集成 ChunkerRouter
    - 修改 `backend/app/pipeline/pipeline.py`
    - 使用 `ChunkerRouter.select()` 自动选择 Chunker
    - 支持知识库 config 中 `chunker_type` 手动覆盖
    - 替换原有固定 `HierarchicalChunker` 调用
    - _Requirements: 3.2, 3.3_

  - [x] 9.3 改造文档上传 API，集成 TaskQueue 入队逻辑
    - 修改 `backend/app/api/document.py`
    - 上传时尝试将任务写入 Redis Stream（通过 TaskQueue.enqueue）
    - Redis 不可用时降级为 `asyncio.create_task` 并输出 WARNING 日志
    - _Requirements: 1.1, 1.6_

  - [x] 9.4 新增队列状态 API 端点
    - 修改 `backend/app/api/system.py`
    - 新增 `GET /api/system/queue-stats` 端点，返回 QueueStats
    - _Requirements: 1.5_

  - [x] 9.5 集成 Worker 启动到应用生命周期
    - 修改 `backend/app/main.py`
    - 在 FastAPI `lifespan` 中初始化 TaskQueue 和 PipelineWorker
    - Redis 不可用时跳过 Worker 启动，仅使用降级模式
    - _Requirements: 1.2, 1.6_

- [x] 10. Checkpoint - 确保集成测试通过
  - 确保所有测试通过，ask the user if questions arise.

- [x] 11. 端到端集成验证
  - [x] 11.1 编写端到端集成测试
    - 测试完整流程：上传 → 入队 → 消费 → 处理 → 完成
    - 测试 Redis 降级模式：Redis 不可用时 fallback 到 asyncio.create_task
    - 测试并发上传：同时提交多个文件，验证 max_concurrent 限制
    - 使用 fakeredis + unittest.mock 模拟外部依赖
    - _Requirements: 1.1, 1.6, 4.1_

  - [x] 11.2 编写 trace_id 链路追踪属性测试
    - **Property 11: Trace ID 在所有阶段日志中一致**
    - **Validates: Requirements 5.1**

- [x] 12. Final checkpoint - 确保所有测试通过
  - 确保所有测试通过，ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- 每个任务引用了具体的 Requirements 编号以确保可追溯性
- Checkpoints 确保增量验证
- 属性测试验证 Correctness Properties 中定义的通用正确性保证
- 单元测试验证具体示例和边界条件
- 使用 fakeredis 进行 Redis 相关测试，避免依赖真实 Redis 实例
- 测试框架：pytest + pytest-asyncio + hypothesis

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2", "1.3"] },
    { "id": 1, "tasks": ["2.1", "3.1", "5.1", "6.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "3.2", "3.3", "5.2", "5.3", "6.2", "6.3", "6.4", "6.5", "6.6"] },
    { "id": 3, "tasks": ["6.7", "6.8", "8.1"] },
    { "id": 4, "tasks": ["8.2", "8.3", "9.1", "9.2"] },
    { "id": 5, "tasks": ["9.3", "9.4", "9.5"] },
    { "id": 6, "tasks": ["11.1", "11.2"] }
  ]
}
```
