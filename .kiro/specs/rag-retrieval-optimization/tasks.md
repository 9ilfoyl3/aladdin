# Implementation Plan: RAG 检索质量优化

## Overview

本实施计划按优先级分为 P0（检索质量基础）和 P1（准度提升）两个阶段，共 6 个任务组、30+ 个子任务。P0 包含元数据增强、过滤检索和 Embedding 上下文增强；P1 包含文档去噪和多知识库联合检索。

## Task Dependency Graph

```json
{
  "waves": [
    {
      "name": "Wave 1 - P0 元数据增强",
      "tasks": [1, 2, 3, 4, 5, 6, 7, 8]
    },
    {
      "name": "Wave 2 - P0 过滤检索 + 上下文增强",
      "tasks": [9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19],
      "dependsOn": ["Wave 1 - P0 元数据增强"]
    },
    {
      "name": "Wave 3 - P1 文档去噪",
      "tasks": [20, 21, 22, 23, 24, 25, 26, 27]
    },
    {
      "name": "Wave 4 - P1 多知识库联合检索",
      "tasks": [28, 29, 30, 31, 32, 33, 34, 35],
      "dependsOn": ["Wave 2 - P0 过滤检索 + 上下文增强"]
    },
    {
      "name": "Wave 5 - Schema 迁移",
      "tasks": [36, 37, 38],
      "dependsOn": ["Wave 1 - P0 元数据增强"]
    }
  ]
}
```

## Tasks

- [x] 1. 创建 `backend/app/pipeline/metadata.py`，实现 `ChunkMetadata` dataclass 和 `MetadataExtractor` 类框架
- [x] 2. 实现 `MetadataExtractor._detect_page_num()` 方法：根据 chunk 前50字符在 page_texts 中定位页码
- [x] 3. 实现 `MetadataExtractor._extract_section_path()` 方法：基于标题正则匹配提取章节路径
- [x] 4. 实现 `MetadataExtractor._detect_element_type()` 方法：识别 text/table/title 类型
- [x] 5. 修改 `backend/app/storage/milvus.py`：在 `_FIELDS` 中添加 `file_type` 和 `element_type` scalar 字段，更新 `_create_collection_sync` 创建 scalar 索引
- [x] 6. 修改 `backend/app/pipeline/pipeline.py`：在 Index 阶段调用 MetadataExtractor，将 metadata 写入 SQLite chunk_metadata 和 Milvus scalar 字段
- [x] 7. 修改 `backend/app/storage/milvus.py`：`_parse_search_results` 输出包含 file_type 和 element_type
- [x] 8. 编写 `backend/tests/test_metadata_extractor.py`：测试各文件类型的元数据提取正确性
- [x] 9. 创建 `backend/app/retrieval/filter.py`，实现 `RetrievalFilter` dataclass 和 `to_milvus_expr()` 方法
- [x] 10. 修改 `backend/app/storage/milvus.py`：`search_dense` 和 `search_sparse` 方法添加 `expr` 参数，传递给 `collection.search()`
- [x] 11. 修改 `backend/app/retrieval/vector.py` 和 `backend/app/retrieval/sparse.py`：search 方法透传 `expr` 参数给 MilvusClient
- [x] 12. 修改 `backend/app/retrieval/hybrid.py`：`search` 方法接受 `expr` 参数并传递给子检索器
- [x] 13. 修改 `backend/app/retrieval/hybrid.py`：`_rrf_fusion` 方法增加 `type_weights` 参数，对 table 类型施加 0.8 降权
- [x] 14. 修改 `backend/app/api/chat.py`：`ChatCompletionRequest` 添加 `filter_doc_ids` 字段，构造 RetrievalFilter 传入检索
- [x] 15. 编写 `backend/tests/test_retrieval_filter.py`：测试 filter 表达式生成和组合过滤逻辑
- [x] 16. 创建 `backend/app/pipeline/context_embedder.py`，实现 `ContextualEmbedder` 类和 `build_embed_text()` 方法
- [x] 17. 修改 `backend/app/pipeline/pipeline.py`：在 Embed 阶段使用 ContextualEmbedder 构造增强文本，传入 embedder.embed()
- [x] 18. 确保 Milvus 和 SQLite 的 content 字段存储原始文本（非增强文本）
- [x] 19. 编写 `backend/tests/test_contextual_embedder.py`：测试上下文拼接格式、父块去重逻辑
- [x] 20. 创建 `backend/app/pipeline/cleaner.py`，实现 `TextCleaner` 类框架
- [x] 21. 修改 `backend/app/pipeline/loaders/pdf_loader.py`：`load()` 方法使用 `get_text("dict")` 获取带 bbox 的文本块，在 LoadResult 中新增 `page_blocks` 字段
- [x] 22. 实现 `TextCleaner._filter_by_bbox()`：过滤页面顶部/底部 5% 区域的短文本块
- [x] 23. 实现 `TextCleaner._detect_repeated_headers()`：跨页重复短文本检测（频率>50%）
- [x] 24. 实现 `TextCleaner._remove_page_numbers()`：正则去除纯页码行
- [x] 25. 实现 `TextCleaner.clean()` 主方法：编排 bbox过滤 → 重复检测 → 正则清理流程
- [x] 26. 修改 `backend/app/pipeline/pipeline.py`：在 Load 之后、Chunk 之前插入 TextCleaner 调用，支持 `enable_cleaner` 开关
- [x] 27. 编写 `backend/tests/test_text_cleaner.py`：测试去噪不误删正文、正确识别页眉页脚和页码
- [x] 28. 创建 `backend/app/retrieval/multi_kb.py`，实现 `KBRetrievalConfig` dataclass 和 `MultiKBRetriever` 类
- [x] 29. 实现 `MultiKBRetriever.search()`：asyncio.gather 并行检索多库（skip_rerank=True）
- [x] 30. 实现 `MultiKBRetriever._weighted_merge()`：按知识库 priority 加权合并 RRF 分数
- [x] 31. 实现部分失败容错：单库异常时返回空结果，不影响其他库
- [x] 32. 修改 `backend/app/schema/api.py`：`ChatCompletionRequest` 添加 `kb_ids: list[str] | None` 字段
- [x] 33. 修改 `backend/app/api/chat.py`：当 `kb_ids` 非空时使用 MultiKBRetriever 替代单库检索
- [x] 34. 修改前端 `frontend/src/pages/Chat.tsx`：对话设置中添加"关联知识库"多选组件，传递 kb_ids 参数
- [x] 35. 编写 `backend/tests/test_multi_kb_retriever.py`：测试并行检索、加权合并、部分失败容错
- [x] 36. 实现 Milvus collection 版本检测：检查已有 collection 是否包含新字段
- [x] 37. 编写迁移脚本：创建新 collection → 重新 embedding 已有文档 → 切换
- [x] 38. 在 pipeline 入库时兼容新旧 schema：旧 collection 不写入 file_type/element_type 字段

## Notes

- Task 1-8 为 P0 元数据增强，是后续过滤检索和上下文增强的前提
- Task 9-15 为 P0 元数据过滤检索，依赖 Task 5（Milvus schema 扩展）
- Task 16-19 为 P0 Embedding 上下文增强，依赖 Task 1（MetadataExtractor）
- Task 20-27 为 P1 文档去噪，可独立实施，建议在元数据增强之前完成以提升元数据质量
- Task 28-35 为 P1 多知识库联合检索，依赖 Task 12（HybridRetriever expr 支持）
- Task 36-38 为 Schema 迁移兼容，需在部署时执行
- 所有新 collection 自动使用新 schema，旧 collection 通过迁移脚本升级
