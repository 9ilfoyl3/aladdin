# 事件中心图谱检索基准

量化「事件中心召回」相对「实体桥接（旧）」的多跳 QA 召回提升与成本变化（需求 5 /
design.md「Benchmark 设计」）。

## 目录结构

```
backend/benchmarks/event_graph/
  dataset/
    corpus/             # 中文语料文档（.md），围绕「青舟科技」等实体构建多跳关联
    questions.jsonl     # 中文多跳 QA，每行 {id, question, answer, gold_doc_ids, ...}
  run_benchmark.py      # 入口：建临时 KB → 入库 → 等抽取完成 →（评测 → 报告）
  report_<timestamp>.md # 对比报告（任务 16 产出）
```

## 数据集格式

`questions.jsonl` 每行一个 JSON 对象：

| 字段 | 必需 | 说明 |
|---|---|---|
| `id` | 是 | 题目唯一标识 |
| `question` | 是 | 中文多跳问题 |
| `answer` | 否 | 参考答案（人读用，不参与召回打分） |
| `gold_doc_ids` | 是 | 相关文档列表，与 `corpus/` 文件名对齐，Recall@k 命中判定用 |
| `gold_chunk_ids` | 否 | 更细粒度的相关 chunk 标识（真实评测时可填） |
| `hop_type` | 否 | 多跳类型标注（跨段落桥接 / 实体共现 / 时间地点关联等），仅用于分组分析 |

语料围绕「青舟科技」生态构建：公司、创始人林远、CTO 苏晴、产品灵眸相机、北斗物流项目、
合作方云栖智能。题目覆盖跨段落桥接、实体共现、跨文档多跳与时间地点关联，确保需要
事件/实体桥接而非单纯向量相似才能召回。

## 用法

```bash
# 在 backend 目录下运行

# 只校验数据集格式与脚本可导入（不连任何外部依赖）
python -m benchmarks.event_graph.run_benchmark --validate-only

# CI 冒烟：确定性 fallback 跑通流程，不依赖远程模型（任务 16 完成）
python -m benchmarks.event_graph.run_benchmark --smoke

# 真实评测（需 Neo4j + Milvus + Redis + 远程 Embedding/LLM，并另起 graph worker）
python -m benchmarks.event_graph.run_benchmark
```

真实评测时图谱抽取由独立 graph worker 异步完成，需在另一进程启动
`GRAPH_ENABLE=true python -m app.worker_main`；`run_benchmark.py` 会轮询
`Document.graph_status` 等待抽取到达终态。

## 实施进度

- 任务 14（本框架）：建临时 KB → 入库 corpus → 等抽取完成 + 数据集与校验。
- 任务 15：双模式评测（baseline `enable_events=False` vs event-centric `enable_events=True`）与
  Recall@{2,5,10} / MRR / latency / llm_calls 指标。
- 任务 16：无远程模型时的确定性 fallback 冒烟（`--smoke`）与 `report_<timestamp>.md` 报告产出。
