# Pipeline TODO 清单

## 已完成 ✓

- [x] CSV 文件上传支持
- [x] 表格数据智能切分（kv 模式 + Markdown 表格保护）
- [x] Embedding 并发调用（Semaphore + 连接池复用）
- [x] Milvus 分批写入（每批 1000 条，避免 gRPC 消息超限）
- [x] Embedder 超长文本截断防御

---

## 一、性能优化

### 1. CSV/XLSX 大文件流式读取
- **文件**: `backend/app/pipeline/loaders/csv_loader.py`
- **现状**: 一次性读取所有行到内存，200MB 文件占用大量内存
- **目标**: 改为流式读取 + 分批处理，支持 500MB+ 文件
- **优先级**: 中
- **预估工作量**: 1-2 天

### 2. 处理进度持久化 + 前端进度条
- **文件**: 需新建 `backend/app/api/task.py`、前端进度组件
- **现状**: 后台 asyncio task，前端只能看到 pending/processing/completed 状态
- **目标**: 处理过程中实时更新 Document 的 progress 字段（0-100%），前端轮询展示进度条
- **参考**: RAGFlow 的 `set_progress` 机制
- **优先级**: 高
- **预估工作量**: 2-3 天

### 3. Embedding 服务端 batch 优化
- **文件**: `backend/app/pipeline/embedder.py`
- **现状**: batch_size=128，并发=8，远程服务可能是瓶颈
- **目标**: 支持配置化 batch_size/concurrency，根据服务端能力动态调整
- **优先级**: 低
- **预估工作量**: 0.5 天

---

## 二、工程化

### 4. 持久化任务队列（替代 asyncio.create_task）
- **文件**: `backend/app/api/document.py`、需新建任务调度模块
- **现状**: `asyncio.create_task` 触发后台处理，重启丢失进行中任务
- **目标**: 引入 Redis 队列（或 Celery），任务持久化，重启后自动恢复
- **主流做法**: RAGFlow 用 Redis Stream + consumer group，Dify 用 Celery
- **优先级**: 高（生产必需）
- **预估工作量**: 3-5 天

### 5. 文件处理并发控制
- **文件**: `backend/app/api/document.py`
- **现状**: 多文件同时上传时无并发限制，可能 OOM
- **目标**: 用 `asyncio.Semaphore` 限制同时处理的文件数（如最多 3 个）
- **主流做法**: RAGFlow 的 `MAX_CONCURRENT_TASKS=5`
- **优先级**: 中
- **预估工作量**: 0.5 天

### 6. 可观测性（链路追踪 + 结构化日志）
- **文件**: 全局日志配置、pipeline 各节点
- **现状**: print 日志，无耗时统计、无 token 消耗记录
- **目标**: 
  - 每步记录耗时（load/chunk/embed/index）
  - 记录 token 消耗量
  - 结构化日志（JSON 格式），便于日志平台采集
- **主流做法**: RAGFlow 每步都有 `timer()` 计时 + progress 回调
- **优先级**: 中
- **预估工作量**: 1-2 天

### 7. 数据库迁移管理
- **文件**: 需引入 Alembic
- **现状**: `create_all` + 手动 ALTER，无版本管理
- **目标**: 引入 Alembic 管理 schema 变更，支持升级/回滚
- **优先级**: 中（团队协作必需）
- **预估工作量**: 1 天初始化 + 持续维护

---

## 三、准度优化

### 8. 检索结果文档类型均衡 + 元数据过滤
- **文件**: `backend/app/retrieval/hybrid.py`、`backend/app/storage/milvus.py`、`backend/app/api/retrieval.py`
- **现状**: 大量表格 chunk 可能在检索时"淹没"其他文档结果；全库搜索无法按条件过滤
- **目标**: 
  - 检索时支持 doc_id/file_type/date_range 过滤
  - 按文档类型加权、调整 top_k 策略
  - Milvus 查询加 filter 表达式
- **优先级**: 中
- **预估工作量**: 2-3 天

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

## 四、架构优化

### 11. 多 Chunker 策略路由 + Chunk 元数据增强
- **文件**: `backend/app/pipeline/chunker.py`、`backend/app/schema/db.py`、`backend/app/storage/milvus.py`
- **现状**: 通用 HierarchicalChunker + CSV/XLSX loader 预切分；chunk 只记录 chunk_index 和 parent_id
- **目标**: 
  - 实现多 chunker 策略模式，根据文件类型 + 内容特征自动选择：
    - `TableChunker`: CSV/XLSX（已通过 pre_chunked 实现）
    - `LawsChunker`: 法律文书（按条款、判决结构切分）
    - `PaperChunker`: 学术论文（按 Abstract/Section/References 切分）
    - `QAChunker`: 问答对格式
    - `NaiveChunker`: 通用文本（当前实现）
  - chunk 记录标题路径、页码、所属章节、文件来源等 metadata
- **参考**: RAGFlow 的 FACTORY 模式
- **优先级**: 高
- **预估工作量**: 5-7 天

---

## 优先级排序建议

**P0（生产阻塞）**:
1. #4 持久化任务队列
2. #2 进度条

**P1（体验提升）**:
3. #11 多 Chunker 策略路由
4. #5 并发控制
5. #6 可观测性

**P2（质量提升）**:
6. #8 检索均衡 + 元数据过滤
7. #10 评估体系
8. #12 检索结果缓存
9. #15 多知识库联合检索
10. #1 流式读取
11. #7 数据库迁移

**P3（锦上添花）**:
12. #9 超长记录拆分
13. #3 Embedding 配置化


---

## 五、检索增强（gf-deployment 分支）

### 12. 检索结果缓存
- **文件**: 新建 `backend/app/retrieval/cache.py`
- **现状**: 相同查询每次都重新检索+Rerank
- **目标**: 相同 query + kb_id 命中缓存，设置 TTL 过期
- **冲突风险**: ✅ 无（新建文件，不改现有代码）
- **优先级**: 中
- **预估工作量**: 0.5-1 天

### 15. 多知识库联合检索
- **文件**: `backend/app/api/chat.py`、`backend/app/schema/api.py`、前端 Chat.tsx
- **现状**: 对话时只能选单个知识库
- **目标**: 支持选多个知识库并行检索，合并结果统一 Rerank
- **冲突风险**: ⚠️ chat.py 被 P0 任务（#4 任务队列）可能涉及，但改动位置不同
- **优先级**: 高
- **预估工作量**: 2-3 天
