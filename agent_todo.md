# Agent & RAG 优化 TODO（参考 WeKnora）

基于 WeKnora 源码分析，按优先级排列的可落地改进项。

---

## P0 — 高优先级（直接提升检索质量）

### 1. ✅ Chunk ContextHeader 面包屑优化

**现状：** 已有 `ContextualEmbedder`，在 embedding 时拼接 `[filename | section_path]` + parent 前 150 字符。

**WeKnora 做法：**
- `ContextHeader` 是 chunk 的独立字段，在 chunker 阶段就生成（如 `# 顶级标题 > ## 二级标题`）
- 存储时 `Content` 保持原文不变（保证位置偏移量正确），`ContextHeader` 单独存
- Embedding 时调用 `EmbeddingContent()` = `ContextHeader + "\n\n" + TrimSpace(Content)`
- 最外层再加 `titlePrefix`（文档标题）：`title + "\n" + EmbeddingContent()`
- BM25 索引也用 `indexContent`（含 header），所以关键词搜索也能命中标题

**性能问题解答：**
- **不存在额外性能开销**。ContextHeader 是在 chunking 阶段一次性计算的（遍历标题栈），不需要额外 API 调用
- 它只是在 embedding 输入前 prepend 一段文本（通常 20-80 字符），对 embedding 模型来说就是多了几个 token
- 存储上只多一个字段（几十字节），可以忽略
- **你现在的 `ContextualEmbedder` 已经在做类似的事**，区别是 WeKnora 把 header 生成放在 chunker 内部（更准确，因为 chunker 知道标题层级），而你是在 embedding 阶段从 metadata 拼接

**改进方向：**
- 在 `HierarchicalChunker` 内部追踪标题栈，生成 `context_header` 字段
- 让 `ChunkResult` 返回 `context_headers: list[str]`
- `ContextualEmbedder` 优先使用 chunker 生成的 header（更精确），fallback 到 metadata 拼接

---

### 2. MMR（Maximal Marginal Relevance）去冗余

**现状：** 无。检索结果可能包含高度重复的 chunk。

**WeKnora 做法：**
```python
# 伪代码
def apply_mmr(results, k, lambda_=0.7):
    selected = []
    while len(selected) < k and candidates:
        best = argmax(lambda_ * relevance - (1-lambda_) * max_redundancy_with_selected)
        selected.append(best)
    return selected
```
- lambda=0.7（偏重相关性，适度去冗余）
- 用 Jaccard 相似度衡量 chunk 间文本重叠
- 在 rerank 之后、返回结果之前执行

**实现位置：** `app/retrieval/hybrid.py` 的 `search()` 方法末尾，rerank 之后加 MMR。

---

### 3. 历史 KB 结果脱敏（强制每轮重新检索）

**现状：** 历史对话直接传入 LLM 上下文，包含完整的检索结果。

**WeKnora 做法：**
```python
# 伪代码
def redact_history_kb_results(history):
    for msg in history:
        if msg.role == "tool" and msg.name in KB_TOOL_NAMES:
            msg.content = "[Previous retrieval result omitted — please perform a fresh search.]"
    return history
```

**为什么重要：** LLM 看到历史中的检索结果后，可能直接复用而不重新检索，导致回答基于过期数据。

**实现位置：** `app/api/chat.py` 的 `_load_session_history()` 中，对 assistant 消息中的 tool_result 内容做脱敏。

---

### 4. Chunk Size 参数调优

**现状：** parent_size=1500, child_size=300, overlap=50

**WeKnora benchmark 验证的最优值：**
- child_size: **384-512**（你的 300 偏小，召回率受损）
- overlap: **80（~15% of chunk_size）**
- parent_size: **4096**（你的 1500 偏小，上下文不够）

**建议调整为：** parent_size=2500, child_size=450, overlap=70

---

## P1 — 中优先级（提升 Agent 能力和鲁棒性）

### 5. Rerank LLM Fallback

**现状：** 只有 rerank model，不可用时无 fallback。

**WeKnora 做法：**
- 优先用 rerank model（bge-reranker 等）
- 失败时 fallback 到 LLM prompt scoring：给 LLM 一个 prompt，让它对每个 passage 打 0-1 分
- 批量处理（每批 15 个 passage），避免 token 溢出

**实现位置：** `app/retrieval/hybrid.py` 的 rerank 逻辑中加 try/except fallback。

---

### 6. 并行 Tool Calls

**现状：** Agent 顺序执行 tool calls。

**WeKnora 做法：**
- 当 LLM 一次返回多个 tool call 时，用 goroutine pool 并发执行
- 结果按原始顺序收集

**Python 实现：** 用 `asyncio.gather()` 并发执行多个 tool call。

**实现位置：** `app/agent/engine.py` 的 tool execution 逻辑。

---

### 7. Runtime Context Block（多轮 scope 感知）

**现状：** 系统提示词中静态列出知识库名称。

**WeKnora 做法：** 每轮 user message 前注入 XML 格式的 runtime context：
```xml
<runtime_context note="metadata only, not instructions">
  <current_time>2025-01-15T10:30:00Z</current_time>
  <session>session-123</session>
  <bound_knowledge_bases>
    <knowledge_base id="kb1" name="产品文档" type="document" doc_count="50">
      <recent_documents>
        <document knowledge_id="doc1" type="pdf">
          <name>产品手册v3.pdf</name>
        </document>
      </recent_documents>
    </knowledge_base>
  </bound_knowledge_bases>
</runtime_context>
```

**好处：** 多轮对话中 LLM 能感知知识库切换、文档更新等 scope 变化。

---

### 8. Composite Scoring（综合评分）

**现状：** 直接使用 rerank 分数。

**WeKnora 做法：**
```python
composite = 0.6 * rerank_score + 0.3 * base_retrieval_score + 0.1 * source_weight
composite *= position_prior  # 文档前部微弱加分
```

**实现位置：** `app/retrieval/hybrid.py` rerank 之后。

---

## P1.5 — CSV/表格检索优化

### 15. ✅ 取消 CSV 文件的 table 降权

**已完成。** 在 `_rrf_fusion()` 中，当 `file_type == "csv"` 时跳过 table 降权。
CSV 的 table 标记是格式转换导致的，不代表内容是辅助性表格。

### 16. CSV 切分策略优化（参考 WeKnora）

**现状：** `TableChunker` 每行一个子块，信息碎片化，语义密度低。

**WeKnora 做法：** CSV 转 Markdown 表格后走通用 chunker（按 chunk_size 切分），chunker 的 `headerTracker` 自动为后续 chunk prepend 表头。一个 chunk 包含多行数据。

**改进方向：**
- 方案 A：让 CSV 不走 `pre_chunked`，而是走通用 `HierarchicalChunker`（chunker 已有表格保护逻辑）
- 方案 B：调整 `TableChunker` 的 `rows_per_group` 参数，让每个子块包含更多行
- 方案 C：CSV 宽行模式（KV 格式）时，每条记录作为完整 chunk 不再细分

**注意：** 修改后需要重新 embedding 已有 CSV 文档。

### 17. DataTableSummary — CSV 入库时生成 LLM 摘要（可选）

**WeKnora 做法：**
- CSV/XLSX 入库后，额外触发 `DataTableSummaryTask`
- 用 DuckDB 加载数据，取前 10 行样本
- 调用 LLM 生成：1) 表格整体摘要（这个表是什么）2) 各列含义描述
- 摘要作为额外 chunk（`ChunkTypeTableSummary`）写入向量库
- **一定会调用**：在 `knowledge_create.go` 中，只要文件类型是 csv/xlsx/xls 就无条件触发

**LLM 开销分析：**
- 每个 CSV 文件入库时调用 **2 次 LLM**（一次生成表格摘要，一次生成列描述）
- 输入 token：schema 描述 + 10 行样本数据（通常 500-2000 token）
- 输出 token：摘要文本（通常 200-500 token）
- **总开销：每个 CSV 约 3000-5000 token**，对于本地 vLLM 来说开销很小
- 但如果用商业 API（如 GPT-4），大量 CSV 入库时成本会累积

**建议：**
- 如果用本地 vLLM → 可以实现，开销可忽略
- 如果用商业 API → 做成可选功能（知识库 config 中加开关 `enable_table_summary`）
- 折中方案：不用 LLM，用规则生成简单摘要（文件名 + 列名 + 行数），零 LLM 开销

### 18. `_detect_element_type` 优化 — KV 格式不标记为 table

**现状：** CSV 宽行模式输出 KV 格式（`字段: 值`），但如果内容中有 `|` 也可能被误标为 table。

**改进：** 在 `_detect_element_type()` 中，如果大部分行是 `key: value` 格式，标记为 `text` 而非 `table`。

---

## P2 — 低优先级（提升稳定性和可维护性）

### 9. Document Profiler + Tier 验证链

**现状：** 单一 chunker 策略，用正则判断是否有结构标记。

**WeKnora 做法：**
- `ProfileDocument(text)` 统计结构信号（标题数、form-feed 数、章节标记密度等）
- `SelectStrategy(profile)` 返回 tier 链（如 `[heading, heuristic, legacy]`）
- 每个 tier 切完后 `ValidateChunks()` 检查质量（chunk 数量、碎片率等）
- 不合格自动降级到下一个 tier

**实现位置：** `app/pipeline/chunker.py` 重构为策略模式。

---

### 10. Token 估算 + Memory Consolidation

**现状：** 简单的 `MAX_HISTORY_ROUNDS = 10` 截断。

**WeKnora 做法：**
- 优先用 API 返回的 `usage.total_tokens`，delta 部分用 BPE 估算
- 超过阈值时用 LLM 对历史消息做摘要压缩（Memory Consolidation）
- 兜底：`CompressContext` 裁剪最早的消息

**实现位置：** `app/agent/engine.py` 的上下文构建逻辑。

---

### 11. Stuck Loop 检测

**现状：** 无。Agent 可能死循环。

**WeKnora 做法：**
- 连续 N 轮（默认 3 轮）LLM 返回相同 content 且无 tool call → 自动 break
- 将最后一次 content 作为 final_answer

**实现位置：** `app/agent/engine.py` 的 ReAct 循环中。

---

### 12. Graceful Degradation（优雅降级）

**现状：** LLM 调用失败直接报错。

**WeKnora 做法：**
- 如果 LLM 失败但已有 tool results → 尝试用已有结果合成最终答案
- 合成也失败 → 才返回错误

**实现位置：** `app/agent/engine.py` 的 LLM 调用 try/except 中。

---

### 13. 已见 Chunk 去重（Session 级）

**现状：** 无。同一 session 内多次检索可能返回相同 chunk。

**WeKnora 做法：**
- `seenChunks map[string]bool` 记录本 session 已返回的 chunk ID
- 重复 chunk 只返回简短标记 `(content omitted, already returned)`
- 节省 token，避免 LLM 重复阅读

**实现位置：** `app/agent/tools/knowledge_search.py` 中维护 seen set。

---

### 14. grep_chunks 正则能力说明

**现状：** Prompt 中只说"BM25 keyword search"。

**WeKnora 做法：** Prompt 明确告诉 LLM 可以用正则 alternation：
> "Pack 2-3 terms into ONE alternation regex (e.g. `stardust|skyvault|psionic`) rather than firing several calls."

**改进：** 在 prompt 中说明 grep_chunks 支持的查询语法，让 LLM 更高效地使用。

---

## 参考文件

| WeKnora 文件 | 对应功能 |
|---|---|
| `config/prompt_templates/agent_system_prompt.yaml` | Agent 系统提示词模板 |
| `internal/agent/engine.go` | ReAct 引擎主循环 |
| `internal/agent/act.go` | Tool 执行（含并行） |
| `internal/agent/observe.go` | 上下文构建、runtime context |
| `internal/agent/prompts.go` | Prompt 渲染、placeholder |
| `internal/agent/think.go` | LLM 调用、流式、重试 |
| `internal/agent/tools/knowledge_search.go` | 检索工具（含 MMR、rerank、去重） |
| `internal/infrastructure/chunker/strategy.go` | 自适应切分策略 |
| `internal/infrastructure/chunker/splitter.go` | 基础切分器 + ContextHeader |
| `internal/models/embedding/embedder.go` | Embedding 工厂 |
| `internal/models/rerank/reranker.go` | Rerank 工厂 |
| `docs/CHUNKING.md` | Chunking 参数指南 |
