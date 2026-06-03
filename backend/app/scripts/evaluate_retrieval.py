"""检索评测工具（B5 Evaluation_Harness）—— 调参安全网，非断言式测试。

设计依据：design.md Components C7、requirements Req 12。

本脚本**复用** ``app/retrieval/hybrid.py::HybridRetriever.search_with_trace``（已存在，
返回 ``(results, trace)``），不重写任何检索逻辑。它对一组评测 query 逐条跑检索、采集
召回/命中/漏斗等指标，并支持对两组检索参数（改动前 / 改动后）并排对比，用于量化
B1 调参与 B2 阈值改动对召回与准确度的实际影响，避免"优化变劣化而不自知"。

放在 ``app/scripts/`` 而非 ``tests/``：它是调参工具，输出供人观察，不做断言。

子模块组成：
- 数据结构：``EvalQuery`` / ``EvalSet``（评测集格式，见 C7）。
- 指标结构：``QueryMetrics`` / ``EvalReport``。
- ``load_eval_set``：从 ``app/scripts/eval_sets/*.json`` 解析评测集。
- ``run_eval``：逐条复用 ``search_with_trace`` 采集并汇总指标。
- ``compare``：在 before / after 两组参数下各跑一遍，并排对比，**跑完恢复原配置**。
- ``build_default_hybrid`` / ``main``：CLI 入口，组装真实检索器并执行。

用法示例（在 ``aladdin/backend`` 目录，aladdin conda 环境）::

    conda run -n aladdin python -m app.scripts.evaluate_retrieval \\
        --eval-set app/scripts/eval_sets/large-kb-legal-sample.json --top-k 10

    conda run -n aladdin python -m app.scripts.evaluate_retrieval \\
        --eval-set app/scripts/eval_sets/large-kb-legal-sample.json \\
        --compare-before before.json --compare-after after.json --save-json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ============================================================
# 12.1 评测集数据结构与解析
# ============================================================


@dataclass
class EvalQuery:
    """单条评测 query。

    Attributes:
        query: 查询文本。
        expected_doc_ids: 期望命中的标准答案文档 id 集合（可选）。有标注时用于
            计算 ``recall@k`` 与基于 doc_id 的 ``hit@k``。
        expected_keywords: 期望关键词（可选）。当无 ``expected_doc_ids`` 标注时，
            用关键词是否出现在返回 content 中近似判定 ``hit@k``。
        note: 备注说明（可选），仅用于人工阅读，不参与指标计算。
    """

    query: str
    expected_doc_ids: list[str] = field(default_factory=list)
    expected_keywords: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class EvalSet:
    """评测集（一个知识库 + 一组评测 query）。

    Attributes:
        name: 评测集名称（用于报告标识）。
        kb_id: 目标知识库 id（所有 query 在此库内检索）。
        queries: 评测 query 列表。
    """

    name: str
    kb_id: str
    queries: list[EvalQuery] = field(default_factory=list)


def load_eval_set(path: str) -> EvalSet:
    """从 JSON 文件加载评测集（格式见 design C7）。

    Args:
        path: 评测集 JSON 路径，如 ``app/scripts/eval_sets/large-kb-legal-sample.json``。

    Returns:
        解析后的 ``EvalSet``。

    Raises:
        FileNotFoundError: 路径不存在。
        KeyError: 缺少必填字段 ``name`` / ``kb_id`` / ``queries``。
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))

    queries: list[EvalQuery] = []
    for item in raw["queries"]:
        queries.append(
            EvalQuery(
                query=item["query"],
                expected_doc_ids=list(item.get("expected_doc_ids", [])),
                expected_keywords=list(item.get("expected_keywords", [])),
                note=item.get("note", ""),
            )
        )

    return EvalSet(name=raw["name"], kb_id=raw["kb_id"], queries=queries)


# ============================================================
# 12.2 指标结构与采集
# ============================================================


@dataclass
class QueryMetrics:
    """单条 query 的检索指标。

    Attributes:
        query: 查询文本。
        recall_at_k: ``recall@k``。有 ``expected_doc_ids`` 标注时 = 命中 expected
            的 doc_id 数 / len(expected_doc_ids)；无标注时为 None。
        hit_at_k: ``hit@k``，top-k 是否至少命中一个 expected（0 / 1）。
        returned_count: 最终返回结果数（观察阈值/兜底是否过度收缩）。
        funnel: 各阶段计数，直接取自 ``trace["funnel"]``。
        top1_rerank_score: top-1 结果的 rerank 原始分（取自 ``trace["per_result"]``），
            观察阈值合理性；无结果或缺失时为 None。
    """

    query: str
    recall_at_k: float | None
    hit_at_k: int
    returned_count: int
    funnel: list[dict] = field(default_factory=list)
    top1_rerank_score: float | None = None


@dataclass
class EvalReport:
    """一组参数下跑完整个评测集的汇总报告。

    Attributes:
        config_label: 参数组标签（如 "before" / "after" / "default"）。
        eval_set_name: 评测集名称。
        per_query: 每条 query 的指标。
        avg_recall_at_k: 对有标注的 query 求 ``recall@k`` 均值；无任何标注时为 None。
        hit_rate: ``hit@k`` 均值（命中率）；无 query 时为 0.0。
    """

    config_label: str
    eval_set_name: str
    per_query: list[QueryMetrics] = field(default_factory=list)
    avg_recall_at_k: float | None = None
    hit_rate: float = 0.0


def _compute_recall_at_k(returned_doc_ids: list[str], expected_doc_ids: list[str]) -> float | None:
    """计算 ``recall@k``：命中 expected 的 doc_id 数 / len(expected)。

    无 ``expected_doc_ids`` 标注时返回 None（该 query 不计入召回均值）。
    """
    if not expected_doc_ids:
        return None
    expected = set(expected_doc_ids)
    hit = expected & set(returned_doc_ids)
    return len(hit) / len(expected)


def _compute_hit_at_k(
    results: list[Any],
    expected_doc_ids: list[str],
    expected_keywords: list[str],
) -> int:
    """计算 ``hit@k``（0 / 1）：top-k 是否至少命中一个 expected。

    判定优先级：
    - 有 ``expected_doc_ids`` 标注：任一返回结果的 doc_id ∈ expected_doc_ids 即命中。
    - 无 doc_id 标注但有 ``expected_keywords``：任一 keyword 出现在任一返回 content 中即命中。
    - 两者皆无：无法判定，返回 0。
    """
    if expected_doc_ids:
        expected = set(expected_doc_ids)
        return 1 if any(r.doc_id in expected for r in results) else 0

    if expected_keywords:
        for r in results:
            content = r.content or ""
            if any(kw in content for kw in expected_keywords):
                return 1
        return 0

    return 0


def _extract_top1_rerank_score(results: list[Any], trace: dict) -> float | None:
    """取 top-1 结果在 ``trace["per_result"]`` 中的 rerank_score（可能为 None）。"""
    if not results:
        return None
    per_result = trace.get("per_result") or {}
    entry = per_result.get(results[0].chunk_id) or {}
    return entry.get("rerank_score")


async def run_eval(hybrid, eval_set: EvalSet, config_label: str, top_k: int = 10) -> EvalReport:
    """逐条复用 ``search_with_trace`` 采集指标并汇总（Req 12.2 / 12.3）。

    Args:
        hybrid: 具备 ``search_with_trace(query, kb_id, top_k=...)`` 的检索器（生产为
            ``HybridRetriever``）。作为参数注入，便于测试用 mock 替换，不在内部硬构造。
        eval_set: 评测集。
        config_label: 本组参数标签，写入报告。
        top_k: 每条 query 返回结果数。

    Returns:
        ``EvalReport``，含逐条指标与聚合（avg_recall_at_k / hit_rate）。
    """
    per_query: list[QueryMetrics] = []

    for q in eval_set.queries:
        results, trace = await hybrid.search_with_trace(q.query, eval_set.kb_id, top_k=top_k)

        returned_doc_ids = [r.doc_id for r in results]
        metrics = QueryMetrics(
            query=q.query,
            recall_at_k=_compute_recall_at_k(returned_doc_ids, q.expected_doc_ids),
            hit_at_k=_compute_hit_at_k(results, q.expected_doc_ids, q.expected_keywords),
            returned_count=len(results),
            funnel=trace.get("funnel", []),
            top1_rerank_score=_extract_top1_rerank_score(results, trace),
        )
        per_query.append(metrics)

    # 聚合：avg_recall_at_k 仅对有标注（非 None）的 query 求均值；hit_rate 为 hit@k 均值。
    recalls = [m.recall_at_k for m in per_query if m.recall_at_k is not None]
    avg_recall = sum(recalls) / len(recalls) if recalls else None
    hit_rate = sum(m.hit_at_k for m in per_query) / len(per_query) if per_query else 0.0

    return EvalReport(
        config_label=config_label,
        eval_set_name=eval_set.name,
        per_query=per_query,
        avg_recall_at_k=avg_recall,
        hit_rate=hit_rate,
    )


# ============================================================
# 12.3 compare：两组参数并排对比（跑完恢复原配置）
# ============================================================


async def compare(
    hybrid,
    eval_set: EvalSet,
    before: dict,
    after: dict,
    top_k: int = 10,
    store=None,
) -> dict:
    """在 before / after 两组参数下各跑一遍评测集，并排对比（Req 12.4）。

    切换配置通过 ``RetrievalConfigStore.update(patch)`` 临时生效，**跑完务必恢复原配置**：
    先存原配置快照，用 try/finally 保证无论是否异常都把全局检索配置还原，避免污染。

    Args:
        hybrid: 注入的检索器（同 ``run_eval``）。
        eval_set: 评测集。
        before: 改动前配置 patch（dict）。
        after: 改动后配置 patch（dict）。
        top_k: 每条 query 返回结果数。
        store: 配置存储（``RetrievalConfigStore``）。默认取进程内单例；测试可注入 mock。

    Returns:
        并排对比结构::

            {
              "eval_set": <name>,
              "before": <EvalReport-as-dict>,
              "after": <EvalReport-as-dict>,
              "diff": [
                {"query": ..., "recall_before": ..., "recall_after": ...,
                 "recall_delta": ..., "hit_before": ..., "hit_after": ...,
                 "returned_before": ..., "returned_after": ...}, ...
              ],
              "summary": {"avg_recall_before": ..., "avg_recall_after": ...,
                          "hit_rate_before": ..., "hit_rate_after": ...}
            }
    """
    if store is None:
        from app.retrieval.config import get_retrieval_config_store

        store = get_retrieval_config_store()

    # 存原配置快照（用于 finally 还原），避免对比跑完污染全局检索配置。
    orig = await store.get_effective()
    try:
        await store.update(before)
        report_before = await run_eval(hybrid, eval_set, "before", top_k=top_k)

        await store.update(after)
        report_after = await run_eval(hybrid, eval_set, "after", top_k=top_k)
    finally:
        # 无论成功或异常，都把全局检索配置还原到对比开始前的快照。
        await store.update(orig.model_dump())

    diff = _build_diff(report_before, report_after)

    return {
        "eval_set": eval_set.name,
        "before": asdict(report_before),
        "after": asdict(report_after),
        "diff": diff,
        "summary": {
            "avg_recall_before": report_before.avg_recall_at_k,
            "avg_recall_after": report_after.avg_recall_at_k,
            "hit_rate_before": report_before.hit_rate,
            "hit_rate_after": report_after.hit_rate,
        },
    }


def _build_diff(before: EvalReport, after: EvalReport) -> list[dict]:
    """按 query 文本对齐 before / after 的逐条指标差异。"""
    after_by_query = {m.query: m for m in after.per_query}
    diff: list[dict] = []
    for b in before.per_query:
        a = after_by_query.get(b.query)
        recall_delta = None
        if a is not None and b.recall_at_k is not None and a.recall_at_k is not None:
            recall_delta = a.recall_at_k - b.recall_at_k
        diff.append(
            {
                "query": b.query,
                "recall_before": b.recall_at_k,
                "recall_after": a.recall_at_k if a else None,
                "recall_delta": recall_delta,
                "hit_before": b.hit_at_k,
                "hit_after": a.hit_at_k if a else None,
                "returned_before": b.returned_count,
                "returned_after": a.returned_count if a else None,
            }
        )
    return diff


# ============================================================
# 终端输出（纯 print 对齐，项目未引入 tabulate 依赖）
# ============================================================


def _fmt(value: Any, width: int) -> str:
    """格式化单元格：None → "-"，float 保留 3 位，左对齐补宽。"""
    if value is None:
        text = "-"
    elif isinstance(value, float):
        text = f"{value:.3f}"
    else:
        text = str(value)
    return text.ljust(width)


def _truncate(text: str, width: int) -> str:
    """截断过长查询文本以对齐表格。"""
    if len(text) <= width:
        return text.ljust(width)
    return text[: width - 1] + "…"


def print_report(report: EvalReport) -> None:
    """终端打印单组参数的评测报告。"""
    print(f"\n=== 评测报告 [{report.config_label}] eval_set={report.eval_set_name} ===")
    header = (
        f"{_truncate('query', 30)}  "
        f"{'recall@k'.ljust(10)}{'hit@k'.ljust(7)}{'returned'.ljust(10)}{'top1_rerank'.ljust(12)}"
    )
    print(header)
    print("-" * len(header))
    for m in report.per_query:
        print(
            f"{_truncate(m.query, 30)}  "
            f"{_fmt(m.recall_at_k, 10)}{_fmt(m.hit_at_k, 7)}"
            f"{_fmt(m.returned_count, 10)}{_fmt(m.top1_rerank_score, 12)}"
        )
    print("-" * len(header))
    print(
        f"avg_recall@k={_fmt(report.avg_recall_at_k, 8).strip()}  "
        f"hit_rate={report.hit_rate:.3f}  queries={len(report.per_query)}"
    )


def print_compare(compare_result: dict) -> None:
    """终端打印 before / after 并排对比。"""
    print(f"\n=== 并排对比 eval_set={compare_result['eval_set']} ===")
    header = (
        f"{_truncate('query', 28)}  "
        f"{'recall(b→a)'.ljust(16)}{'Δrecall'.ljust(10)}"
        f"{'hit(b→a)'.ljust(10)}{'returned(b→a)'.ljust(14)}"
    )
    print(header)
    print("-" * len(header))
    for d in compare_result["diff"]:
        recall_ba = f"{_fmt(d['recall_before'], 5).strip()}→{_fmt(d['recall_after'], 5).strip()}"
        hit_ba = f"{d['hit_before']}→{d['hit_after']}"
        returned_ba = f"{d['returned_before']}→{d['returned_after']}"
        print(
            f"{_truncate(d['query'], 28)}  "
            f"{recall_ba.ljust(16)}{_fmt(d['recall_delta'], 10)}"
            f"{hit_ba.ljust(10)}{returned_ba.ljust(14)}"
        )
    print("-" * len(header))
    s = compare_result["summary"]
    print(
        f"avg_recall: {_fmt(s['avg_recall_before'], 5).strip()} → "
        f"{_fmt(s['avg_recall_after'], 5).strip()}    "
        f"hit_rate: {s['hit_rate_before']:.3f} → {s['hit_rate_after']:.3f}"
    )


def save_json(payload: dict, eval_set_name: str) -> str:
    """把报告/对比结果落盘到 ``app/scripts/eval_reports/``（目录写时创建）。

    Returns:
        写入的文件路径。
    """
    reports_dir = Path(__file__).parent / "eval_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = reports_dir / f"{eval_set_name}_{timestamp}.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(out_path)


# ============================================================
# CLI 入口：组装真实检索器并执行
# ============================================================


def build_default_hybrid():
    """组装生产 ``HybridRetriever``（真实 Milvus + 模型）。供 ``main`` 用。

    与 ``app/api/retrieval.py`` 的构造方式一致：三路子检索器 + reranker + DB 会话工厂，
    config_store 用默认进程内单例。

    Returns:
        ``HybridRetriever`` 实例。
    """
    from app.models.manager import get_model_manager
    from app.retrieval.bm25 import BM25Retriever
    from app.retrieval.hybrid import HybridRetriever
    from app.retrieval.sparse import SparseRetriever
    from app.retrieval.vector import VectorRetriever
    from app.storage.database import async_session
    from app.storage.milvus import get_milvus_client

    manager = get_model_manager()
    milvus = get_milvus_client()

    return HybridRetriever(
        vector_retriever=VectorRetriever(manager.embedder, milvus),
        sparse_retriever=SparseRetriever(manager.embedder, milvus),
        rerank_provider=manager.reranker,
        db_session_factory=async_session,
        bm25_retriever=BM25Retriever(milvus),
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="检索评测工具（B5）：复用 search_with_trace 采集召回/命中指标，支持两组参数对比。"
    )
    parser.add_argument("--eval-set", required=True, help="评测集 JSON 路径")
    parser.add_argument("--top-k", type=int, default=10, help="每条 query 返回结果数（默认 10）")
    parser.add_argument(
        "--compare-before",
        default=None,
        help="对比模式：改动前配置 patch 的 JSON 路径（需与 --compare-after 同时给出）",
    )
    parser.add_argument(
        "--compare-after",
        default=None,
        help="对比模式：改动后配置 patch 的 JSON 路径",
    )
    parser.add_argument("--save-json", action="store_true", help="把结果落盘到 app/scripts/eval_reports/")
    return parser.parse_args(argv)


async def _async_main(args: argparse.Namespace) -> None:
    eval_set = load_eval_set(args.eval_set)
    hybrid = build_default_hybrid()

    if args.compare_before and args.compare_after:
        before = json.loads(Path(args.compare_before).read_text(encoding="utf-8"))
        after = json.loads(Path(args.compare_after).read_text(encoding="utf-8"))
        result = await compare(hybrid, eval_set, before, after, top_k=args.top_k)
        print_compare(result)
        if args.save_json:
            print(f"\n已落盘: {save_json(result, eval_set.name)}")
    else:
        report = await run_eval(hybrid, eval_set, "default", top_k=args.top_k)
        print_report(report)
        if args.save_json:
            print(f"\n已落盘: {save_json(asdict(report), eval_set.name)}")


def main(argv: list[str] | None = None) -> None:
    """CLI 入口。"""
    logging.basicConfig(level=logging.INFO)
    args = _parse_args(argv)
    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
