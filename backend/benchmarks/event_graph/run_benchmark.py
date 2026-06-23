"""事件中心图谱检索基准入口（需求 5 / design.md「Benchmark 设计」）。

本脚本是离线可复跑的检索基准框架。完整流程（design.md「评测方法」）：

    建临时 KB → 入库 corpus/ → 等待图谱抽取完成
        → 跑两种召回（baseline 实体桥接 / event-centric 事件中心）
        → 计算指标（Recall@k / MRR / latency / llm_calls）
        → 输出对比报告 report_<timestamp>.md

本文件（任务 14）实现「建临时 KB → 入库 → 等抽取完成」核心，并为后续任务预留清晰的
扩展点（函数桩 + docstring）：

- 任务 15：双模式评测与指标 —— 见 :func:`run_dual_mode_eval` / :func:`compute_metrics`。
- 任务 16：fallback 冒烟与报告产出 —— 见 ``--smoke`` 分支与 :func:`write_report`。

用法::

    # 真实评测（需配置远程 Embedding/LLM + Neo4j + Milvus + Redis）
    python -m benchmarks.event_graph.run_benchmark

    # CI 冒烟：确定性 fallback 跑通流程，不依赖远程模型（任务 16 完成）
    python -m benchmarks.event_graph.run_benchmark --smoke

    # 只校验数据集格式与脚本可导入（不连任何外部依赖）
    python -m benchmarks.event_graph.run_benchmark --validate-only
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import re
import sys
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

# 确保 backend 目录在 sys.path 上，使 ``import app...`` 可用（与 scripts/ 脚本一致）。
_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

logger = logging.getLogger("benchmark.event_graph")

# ── 数据集路径（随仓库附带，design.md「内置评测集」）──
_DATASET_DIR = Path(__file__).resolve().parent / "dataset"
_CORPUS_DIR = _DATASET_DIR / "corpus"
_QUESTIONS_FILE = _DATASET_DIR / "questions.jsonl"

# 等待图谱抽取完成的轮询参数（建库入库后，worker 异步抽取事件 + 实体）。
_EXTRACT_POLL_INTERVAL_S = 3.0
_EXTRACT_TIMEOUT_S = 1800.0  # 30 分钟兜底，避免无限等待


# ============================================================
# 数据集模型与加载/校验（任务 14）
# ============================================================


@dataclass
class BenchQuestion:
    """单条多跳 QA 评测样本（对齐 design.md 数据格式）。

    Attributes:
        id: 题目唯一标识。
        question: 中文多跳问题。
        answer: 参考答案（人读用，不参与召回打分）。
        gold_doc_ids: 相关文档标识列表（与 corpus 文件名对齐），Recall@k 命中判定用。
        gold_chunk_ids: 可选的相关 chunk 标识（更细粒度命中判定，留待真实评测填充）。
        hop_type: 多跳类型标注（跨段落桥接 / 实体共现 / 时间地点关联等），仅用于分组分析。
    """

    id: str
    question: str
    answer: str
    gold_doc_ids: list[str]
    gold_chunk_ids: list[str] = field(default_factory=list)
    hop_type: str = ""


def load_questions(path: Path = _QUESTIONS_FILE) -> list[BenchQuestion]:
    """从 ``questions.jsonl`` 逐行解析评测样本（每行一个 JSON 对象）。

    Args:
        path: questions.jsonl 路径。

    Returns:
        :class:`BenchQuestion` 列表。

    Raises:
        ValueError: 文件缺失、JSON 解析失败或缺必需字段（id/question/gold_doc_ids）。
    """
    if not path.exists():
        raise ValueError(f"评测集不存在: {path}")

    questions: list[BenchQuestion] = []
    seen_ids: set[str] = set()
    with path.open("r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"questions.jsonl 第 {lineno} 行 JSON 解析失败: {e}") from e

            for key in ("id", "question", "gold_doc_ids"):
                if key not in obj:
                    raise ValueError(f"questions.jsonl 第 {lineno} 行缺少必需字段 {key!r}")

            qid = str(obj["id"])
            if qid in seen_ids:
                raise ValueError(f"questions.jsonl 第 {lineno} 行 id 重复: {qid!r}")
            seen_ids.add(qid)

            gold = obj["gold_doc_ids"]
            if not isinstance(gold, list) or not gold:
                raise ValueError(
                    f"questions.jsonl 第 {lineno} 行 gold_doc_ids 必须是非空列表"
                )

            questions.append(
                BenchQuestion(
                    id=qid,
                    question=str(obj["question"]),
                    answer=str(obj.get("answer", "")),
                    gold_doc_ids=[str(d) for d in gold],
                    gold_chunk_ids=[str(c) for c in obj.get("gold_chunk_ids", [])],
                    hop_type=str(obj.get("hop_type", "")),
                )
            )

    if not questions:
        raise ValueError(f"评测集为空: {path}")
    return questions


def list_corpus_files(corpus_dir: Path = _CORPUS_DIR) -> list[Path]:
    """列出 corpus 目录下的所有语料文档（.md / .txt），按文件名排序。"""
    if not corpus_dir.exists():
        raise ValueError(f"语料目录不存在: {corpus_dir}")
    files = sorted(
        p for p in corpus_dir.iterdir()
        if p.is_file() and p.suffix.lower() in (".md", ".txt")
    )
    if not files:
        raise ValueError(f"语料目录为空（无 .md/.txt）: {corpus_dir}")
    return files


def validate_dataset() -> tuple[list[BenchQuestion], list[Path]]:
    """校验数据集自洽：questions.jsonl 合法、gold_doc_ids 全部指向真实 corpus 文档。

    Returns:
        (questions, corpus_files) 二元组。

    Raises:
        ValueError: 数据集不合法（解析失败、空集、gold_doc_ids 引用了不存在的语料文件）。
    """
    questions = load_questions()
    corpus_files = list_corpus_files()
    corpus_names = {p.name for p in corpus_files}

    dangling: dict[str, list[str]] = {}
    for q in questions:
        missing = [d for d in q.gold_doc_ids if d not in corpus_names]
        if missing:
            dangling[q.id] = missing
    if dangling:
        raise ValueError(
            "以下题目的 gold_doc_ids 指向不存在的语料文档（语料文件名应与 gold_doc_ids 对齐）："
            f"{dangling}；现有语料: {sorted(corpus_names)}"
        )

    logger.info(
        "数据集校验通过：%d 篇语料，%d 道多跳 QA",
        len(corpus_files), len(questions),
    )
    return questions, corpus_files


# ============================================================
# 临时 KB 生命周期：建库 → 入库 → 等抽取完成（任务 14 核心）
# ============================================================


@dataclass
class BenchHarness:
    """基准运行期句柄：承载临时 KB 标识与构造好的依赖组件。

    供后续任务（15 召回评测、16 报告）复用，避免重复组装依赖。
    """

    kb_id: str
    tenant_id: str | None
    doc_id_to_name: dict[str, str]  # doc_id → 语料文件名（命中判定按文件名对齐 gold_doc_ids）


async def create_temp_kb(*, enable_events: bool = True) -> str:
    """建立临时基准 KB，并开启图谱（含事件抽取开关）。

    直接写 ``KnowledgeBase`` 表（脚本无 HTTP 请求上下文），KB 名带时间戳便于辨识与清理。
    通过 ``config["graph"]`` 写入图谱配置：``enabled=True`` 触发抽取，``enable_events``
    控制事件抽取（baseline 模式可置 False 退回纯实体图，供任务 15 A/B 对比）。

    Args:
        enable_events: 是否开启事件抽取（True=事件中心；False=纯实体桥接 baseline）。

    Returns:
        新建 KB 的 id。
    """
    from app.pipeline.graph.config import GraphKBConfig, write_graph_config
    from app.schema.db import KnowledgeBase
    from app.storage.database import async_session

    kb_id = str(uuid.uuid4())
    graph_cfg = GraphKBConfig(enabled=True, enable_events=enable_events)
    config = write_graph_config({}, graph_cfg)

    async with async_session() as session:
        kb = KnowledgeBase(
            id=kb_id,
            name=f"event-graph-bench-{int(time.time())}",
            description="事件中心图谱检索基准临时知识库（可安全删除）",
            config=config,
            tenant_id=None,
        )
        session.add(kb)
        await session.commit()

    logger.info("已创建临时基准 KB: %s（enable_events=%s）", kb_id, enable_events)
    return kb_id


async def ingest_corpus(kb_id: str, corpus_files: list[Path]) -> dict[str, str]:
    """把 corpus 文档入库到临时 KB，复用现有 :class:`DocumentPipeline`。

    每篇语料：写 ``Document`` 行（filename=语料文件名，作为 gold_doc_ids 命中锚点）→
    调 ``pipeline.process`` 走 load→chunk→embed→index（同步执行，便于基准串行控制）→
    入库完成后触发图谱抽取（``maybe_trigger_graph_extract`` 入慢道队列）。

    Args:
        kb_id: 目标 KB id。
        corpus_files: 语料文件路径列表。

    Returns:
        doc_id → 语料文件名 的映射（命中判定与报告用）。
    """
    from app.pipeline.factory import create_pipeline
    from app.pipeline.graph.trigger import maybe_trigger_graph_extract
    from app.schema.db import Document, KnowledgeBase
    from app.storage.database import async_session
    from sqlalchemy import select

    pipeline = await create_pipeline()
    doc_id_to_name: dict[str, str] = {}

    async with async_session() as session:
        kb_tenant = await session.scalar(
            select(KnowledgeBase.tenant_id).where(KnowledgeBase.id == kb_id)
        )

    for path in corpus_files:
        doc_id = str(uuid.uuid4())
        async with async_session() as session:
            doc = Document(
                id=doc_id,
                kb_id=kb_id,
                filename=path.name,
                file_type=path.suffix.lstrip(".").lower(),
                file_size=path.stat().st_size,
                status="pending",
                tenant_id=kb_tenant,
            )
            session.add(doc)
            await session.commit()

        logger.info("入库语料 %s（doc_id=%s）", path.name, doc_id)
        await pipeline.process(str(path), doc_id, kb_id)
        doc_id_to_name[doc_id] = path.name

        # 入库完成后触发图谱抽取（fire-and-forget 语义，这里直接 await 以串行可控）。
        await maybe_trigger_graph_extract(
            kb_id=kb_id,
            doc_id=doc_id,
            tenant_id=kb_tenant,
            db_session_factory=async_session,
            graph_queue=pipeline.graph_queue,
        )

    return doc_id_to_name


async def wait_for_extraction(
    kb_id: str,
    *,
    timeout_s: float = _EXTRACT_TIMEOUT_S,
    poll_interval_s: float = _EXTRACT_POLL_INTERVAL_S,
) -> bool:
    """轮询等待该 KB 下所有文档的图谱抽取到达终态（completed/failed）。

    抽取由独立 graph worker 异步消费慢道队列完成；本函数按 ``Document.graph_status``
    轮询，直到全部文档不再处于 ``pending``/``processing``，或超时。

    注意：本函数只等待，不负责启动 worker。真实评测时需另起 graph worker 进程
    （``python -m app.worker_main``，GRAPH_ENABLE=true）。冒烟模式（任务 16）走确定性
    fallback，不依赖 worker。

    Args:
        kb_id: KB id。
        timeout_s: 最长等待秒数。
        poll_interval_s: 轮询间隔秒数。

    Returns:
        True 表示全部到达终态；False 表示超时仍有未完成文档。
    """
    from app.schema.db import Document
    from app.storage.database import async_session
    from sqlalchemy import func, select

    deadline = time.monotonic() + timeout_s
    while True:
        async with async_session() as session:
            total = await session.scalar(
                select(func.count(Document.id)).where(Document.kb_id == kb_id)
            ) or 0
            pending = await session.scalar(
                select(func.count(Document.id)).where(
                    Document.kb_id == kb_id,
                    Document.graph_status.in_(("pending", "processing", "none")),
                )
            ) or 0

        done = total - pending
        logger.info("图谱抽取进度：%d/%d 文档已完成", done, total)
        if pending == 0:
            return True
        if time.monotonic() >= deadline:
            logger.warning("等待图谱抽取超时（%.0fs），仍有 %d 个文档未完成", timeout_s, pending)
            return False
        await asyncio.sleep(poll_interval_s)


async def teardown_kb(kb_id: str) -> None:
    """清理临时 KB 及其所有衍生数据（SQLite / Milvus chunk+event / Neo4j 图）。

    基准是离线可复跑的——每次跑完应能干净回收，避免临时 KB 堆积。各步骤独立 try，
    单点失败不阻断其余清理（优雅降级）。
    """
    from app.schema.db import KnowledgeBase
    from app.storage.database import async_session
    from sqlalchemy import delete as sql_delete

    # Milvus chunk 向量集合
    try:
        from app.storage.milvus import get_milvus_client

        milvus = get_milvus_client()
        if await milvus.has_collection(kb_id):
            await milvus.drop_collection(kb_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("清理 Milvus chunk 集合失败（可忽略）: %s", e)

    # Milvus event 向量集合
    try:
        from app.storage.milvus_event_store import get_milvus_event_store

        event_store = get_milvus_event_store()
        if await event_store.has_collection(kb_id):
            await event_store.delete_by_kb(kb_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("清理 Milvus event 集合失败（可忽略）: %s", e)

    # Neo4j 图（实体 + 关系 + 事件）
    try:
        from app.storage.graph_store import get_graph_store

        store = await get_graph_store()
        if store is not None:
            await store.delete_by_kb(kb_id=kb_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("清理 Neo4j 图失败（可忽略）: %s", e)

    # SQLite：删 KB（ORM cascade 连带删 documents/chunks）
    try:
        async with async_session() as session:
            await session.execute(
                sql_delete(KnowledgeBase).where(KnowledgeBase.id == kb_id)
            )
            await session.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("清理 SQLite KB 记录失败（可忽略）: %s", e)

    logger.info("已清理临时基准 KB: %s", kb_id)


# ============================================================
# 双模式评测与指标（任务 15）
# ============================================================

# Recall@k / MRR 评测的 k 值（design.md「指标」：k ∈ {2,5,10}）。
_EVAL_KS: tuple[int, ...] = (2, 5, 10)

# 每问图谱召回请求的结果上限：需 >= max(_EVAL_KS) 才能算出 Recall@10。
_RECALL_TOP_K = max(_EVAL_KS)


class _CountingLLM:
    """LLM provider 计数代理：透传 ``generate`` 并累计调用次数。

    ``GraphRetriever`` 仅用 ``llm.generate`` 做 query→实体名抽取（入口B / 实体桥接），
    包一层即可统计「每问 LLM 调用次数」（入口A 事件向量召回不触达 LLM）。其余属性
    透传内层 provider，保持鸭子类型兼容。
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls = 0

    async def generate(self, messages: list[dict], **kwargs) -> str:
        self.calls += 1
        return await self._inner.generate(messages, **kwargs)

    def __getattr__(self, name):  # 透传非代理属性（如 stream 等）
        return getattr(self._inner, name)


async def _set_kb_enable_events(kb_id: str, enable_events: bool) -> None:
    """切换 KB 图谱配置的 ``enable_events``（写 ``config["graph"]``），其余字段不变。

    ``GraphRetriever.search`` 每次按 KB 配置读 ``enabled`` / ``enable_events`` 选择召回路径，
    故只需改库里的配置即可在同一套抽取产物上切换 baseline / event-centric 两态（A/B）。
    """
    from app.pipeline.graph.config import read_graph_config, write_graph_config
    from app.schema.db import KnowledgeBase
    from app.storage.database import async_session
    from sqlalchemy import select, update

    async with async_session() as session:
        cfg = await session.scalar(
            select(KnowledgeBase.config).where(KnowledgeBase.id == kb_id)
        )
        graph_cfg = read_graph_config(cfg)
        graph_cfg.enable_events = enable_events
        new_cfg = write_graph_config(cfg, graph_cfg)
        await session.execute(
            update(KnowledgeBase).where(KnowledgeBase.id == kb_id).values(config=new_cfg)
        )
        await session.commit()


async def _resolve_default_llm():
    """解析默认 LLM provider（实体名抽取用）；不可用时返回 None（回退分词，0 LLM 调用）。

    复用 ``chat._get_llm_for_request(None)`` 的默认解析（数据库默认配置 → 系统全局配置）。
    解析失败（未配置 / 导入异常）按降级处理，入口B 退化为分词抽取，不阻断基准。
    """
    try:
        from app.api.chat import _get_llm_for_request

        llm, _stream, _max_ctx = await _get_llm_for_request(None)
        return llm
    except Exception as e:  # noqa: BLE001 - LLM 解析软失败，回退分词
        logger.warning("默认 LLM 解析失败，实体名抽取回退分词（入口B 不调 LLM）: %s", e)
        return None


async def run_graph_recall(
    harness: BenchHarness,
    questions: list[BenchQuestion],
    *,
    enable_events: bool,
) -> dict:
    """对单一模式跑「图谱第四路」召回，隔离变量（不经 RRF/rerank，design.md「评测方法」）。

    构造一个 ``GraphRetriever``（注入 store / event_store / embedder / 计数 LLM），先把 KB
    配置的 ``enable_events`` 切到目标模式，再对每道题调用 ``retriever.search`` 记录明细：

    - ``enable_events=True``：事件中心召回（入口A 事件向量 + 入口B 实体桥接 + 多跳）。
    - ``enable_events=False``：baseline 纯实体桥接旧逻辑。

    每问明细含：``ranked_docs``（结果 doc_id 经 ``harness.doc_id_to_name`` 映射为语料文件名的
    有序列表，供 Recall@k / MRR）、``latency_ms``（单问召回延迟）、``llm_calls``（该问触达 LLM
    的次数，入口A 不计、入口B 实体名抽取计一次）。

    Returns:
        ``{"mode", "per_question": [...]}``，供 :func:`compute_metrics` 计算指标。
    """
    from app.models.manager import get_model_manager
    from app.retrieval.config import get_platform_config_store
    from app.retrieval.graph_retriever import GraphRetriever
    from app.storage.database import async_session
    from app.storage.graph_store import get_graph_store

    # 1) 切换 KB 模式（写 config["graph"].enable_events）。
    await _set_kb_enable_events(harness.kb_id, enable_events)

    # 2) 组装依赖（与 chat._maybe_build_graph_retriever 注入方式一致）。
    store = await get_graph_store()
    manager = get_model_manager()
    platform = await get_platform_config_store().get_effective()

    event_store = None
    try:
        from app.storage.milvus_event_store import get_milvus_event_store

        event_store = get_milvus_event_store()
    except Exception as e:  # noqa: BLE001 - 取单例失败则入口A 跳过，仅走入口B
        logger.warning("获取 MilvusEventStore 失败，事件向量召回入口A 降级跳过: %s", e)
        event_store = None

    inner_llm = await _resolve_default_llm()
    counting_llm = _CountingLLM(inner_llm) if inner_llm is not None else None

    retriever = GraphRetriever(
        store=store,
        db_session_factory=async_session,
        embedder=manager.embedder,
        llm_provider=counting_llm,
        hops=platform.graph_retriever_hops,
        max_chunks=platform.graph_retriever_max_chunks,
        event_store=event_store,
    )

    mode = "event_centric" if enable_events else "baseline"
    logger.info("开始跑 %s 模式图谱召回（%d 题）", mode, len(questions))

    per_question: list[dict] = []
    for q in questions:
        calls_before = counting_llm.calls if counting_llm is not None else 0
        t0 = time.perf_counter()
        results = await retriever.search(q.question, harness.kb_id, top_k=_RECALL_TOP_K)
        latency_ms = (time.perf_counter() - t0) * 1000.0
        calls = (counting_llm.calls - calls_before) if counting_llm is not None else 0

        # 结果 doc_id → 语料文件名（命中判定按文件名对齐 gold_doc_ids）。
        ranked_docs = [
            harness.doc_id_to_name.get(r.doc_id, r.doc_id) for r in results
        ]
        ranked_chunk_ids = [r.chunk_id for r in results]

        per_question.append(
            {
                "id": q.id,
                "ranked_docs": ranked_docs,
                "ranked_chunk_ids": ranked_chunk_ids,
                "latency_ms": latency_ms,
                "llm_calls": calls,
            }
        )

    return {"mode": mode, "per_question": per_question}


def compute_metrics(recall_details: dict, questions: list[BenchQuestion]) -> dict:
    """按 design.md「指标」计算评测指标。

    对每个 k ∈ {2,5,10}：

    - ``Recall@k``：top-k 结果中出现任一 gold 文档的题目比例。
    - ``MRR``：每题首个相关结果的倒数排名（``1/rank``）均值，无命中记 0。
    - ``hit_rate``：整个召回列表中命中任一相关文档的题目比例（需求 5.2「命中相关文档比例」）。
    - ``latency_ms``：平均单问召回延迟。
    - ``llm_calls_per_q``：平均每问 LLM 调用次数。

    Args:
        recall_details: :func:`run_graph_recall` 的返回（含 ``per_question`` 明细）。
        questions: 评测样本（提供 ``gold_doc_ids`` 用于命中判定）。

    Returns:
        指标字典，供 :func:`write_report` 渲染对比表格。
    """
    per_q = recall_details.get("per_question", [])
    gold_by_id = {q.id: set(q.gold_doc_ids) for q in questions}
    n = len(per_q)

    recall_at_k = {str(k): 0.0 for k in _EVAL_KS}
    base = {
        "mode": recall_details.get("mode", ""),
        "num_questions": n,
        "recall_at_k": recall_at_k,
        "mrr": 0.0,
        "hit_rate": 0.0,
        "latency_ms": 0.0,
        "llm_calls_per_q": 0.0,
    }
    if n == 0:
        return base

    recall_hits = {k: 0 for k in _EVAL_KS}
    mrr_total = 0.0
    hit_total = 0
    latency_total = 0.0
    llm_total = 0.0

    for rec in per_q:
        gold = gold_by_id.get(rec["id"], set())
        ranked = rec.get("ranked_docs", [])

        # Recall@k：top-k 内出现任一 gold 文档即命中。
        for k in _EVAL_KS:
            if gold and any(d in gold for d in ranked[:k]):
                recall_hits[k] += 1

        # MRR：首个相关结果的倒数排名（1-indexed）。
        rr = 0.0
        for idx, d in enumerate(ranked):
            if d in gold:
                rr = 1.0 / (idx + 1)
                break
        mrr_total += rr
        if rr > 0.0:
            hit_total += 1

        latency_total += float(rec.get("latency_ms", 0.0))
        llm_total += float(rec.get("llm_calls", 0))

    base["recall_at_k"] = {str(k): recall_hits[k] / n for k in _EVAL_KS}
    base["mrr"] = mrr_total / n
    base["hit_rate"] = hit_total / n
    base["latency_ms"] = latency_total / n
    base["llm_calls_per_q"] = llm_total / n
    return base


async def run_dual_mode_eval(harness: BenchHarness, questions: list[BenchQuestion]) -> dict:
    """同一 KB 上分别跑 baseline 与 event-centric 两模式并汇总指标。

    通过切换 KB 的 ``enable_events`` 配置（写 ``config["graph"]``）实现 A/B：对同一套抽取
    产物（事件 + 实体已在入库阶段抽好）分别评两路图谱召回，互不影响。

    Returns:
        ``{"baseline": metrics, "event_centric": metrics}``，供 :func:`write_report` 出对比表。
    """
    # baseline（纯实体桥接，enable_events=False）。
    baseline_details = await run_graph_recall(harness, questions, enable_events=False)
    baseline_metrics = compute_metrics(baseline_details, questions)

    # event-centric（事件中心，enable_events=True）。
    event_details = await run_graph_recall(harness, questions, enable_events=True)
    event_metrics = compute_metrics(event_details, questions)

    return {"baseline": baseline_metrics, "event_centric": event_metrics}


# ============================================================
# 任务 16 扩展点：fallback 冒烟与报告产出（占位桩，由任务 16 实现）
# ============================================================


# ============================================================
# 确定性 fallback 冒烟（任务 16）：哈希向量 + 规则抽实体
# ============================================================
#
# 冒烟目标（需求 5.3）：无远程 Embedding/LLM、无 Neo4j/Milvus/Redis 时，用确定性替身把
# 整条评测流程（建库 → 入库 → 抽取 → 双模式召回 → 指标 → 报告）跑通，验证「流程不崩」，
# 不校验真实质量数。为此用进程内的 in-memory 替身组装一套 GraphRetriever：
#
# - ``_HashEmbedder``     ：哈希向量，替代远程 Embedding（确定性、无网络）。
# - ``_RuleEntityLLM``    ：规则抽实体，替代远程 LLM（确定性、无网络）。
# - ``_FakeGraphStore``   ：内存事件/实体图，实现 GraphRetriever 调用的图查询子集。
# - ``_FakeEventStore``   ：内存事件向量集合，实现入口A 的 ANN ``search``。
# - in-memory SQLite      ：承载 Chunk / KnowledgeBase 表（回取原文 + 读 KB 图谱开关）。

_SMOKE_EMBED_DIM = 256
_SMOKE_KB_ID = "smoke-event-graph-kb"

# 规则抽实体：salient 多字实体的「类型后缀」白名单（命中即视为一个实体名）。
_SMOKE_ENTITY_SUFFIXES = (
    "科技", "公司", "相机", "物流", "智能", "大学", "研究所", "研究院",
    "学院", "奖学金", "协会", "系",
)
_SMOKE_ENTITY_RE = re.compile(
    r"[\u4e00-\u9fff]{2,10}(?:" + "|".join(_SMOKE_ENTITY_SUFFIXES) + ")"
)
# 「...」/『...』包裹的专名（产品/项目名，如「灵眸相机」「北斗物流」「远航奖学金」）。
_SMOKE_BRACKET_RE = re.compile(r"[「『]([^」』]{2,12})[」』]")


def _smoke_char_tokens(text: str) -> list[str]:
    """把文本切成确定性 token（CJK 单字 + bigram + ascii 词），供哈希向量累加。

    用字符 bigram 让共享子串（实体名、词）在 query 与 event 向量间产生重叠相似度，
    无需任何外部分词器，纯确定性、零依赖。
    """
    lowered = text.strip().lower()
    chars = [c for c in lowered if "\u4e00" <= c <= "\u9fff"]
    tokens: list[str] = list(chars)
    tokens.extend(chars[i] + chars[i + 1] for i in range(len(chars) - 1))
    tokens.extend(re.findall(r"[a-z0-9]+", lowered))
    return tokens


def _smoke_extract_entities(text: str) -> list[str]:
    """规则抽实体（确定性，需求 5.3「规则抽实体」）：

    - 类型后缀正则命中多字专名（组织/产品/院校/项目等）。
    - 「...」/『...』包裹的专名。
    - 词级 token（``textutil.tokenize``，jieba 或字符 bigram 回退）中的纯 CJK 词，
      兜住人名/地名等无固定后缀的实体。

    返回去重排序的实体名列表（无 LLM、无网络）。
    """
    names: set[str] = set()
    for m in _SMOKE_ENTITY_RE.finditer(text):
        names.add(m.group(0))
    for m in _SMOKE_BRACKET_RE.finditer(text):
        names.add(m.group(1))
    try:
        from app.retrieval.textutil import tokenize

        for tok in tokenize(text):
            if 2 <= len(tok) <= 6 and all("\u4e00" <= c <= "\u9fff" for c in tok):
                names.add(tok)
    except Exception:  # noqa: BLE001 - 分词不可用不阻断，前两条规则已足够跑通
        pass
    return sorted(names)


def _smoke_cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度（冒烟内部用，等长向量；零向量/空返回 0）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (na * nb)


class _HashEmbedder:
    """确定性哈希向量 Embedder（替代远程 Embedding，需求 5.3「哈希向量」）。

    把文本 token 哈希到固定维度桶并带符号累加后 L2 归一化。同一文本恒得同一向量，
    共享子串的文本相似度更高——足以让事件向量召回（入口A）与粗排在冒烟中产生有意义的
    确定性排序，且完全不触网。鸭子兼容 ``EmbedProvider``（``embed`` / ``embed_sparse``）。
    """

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    async def embed_sparse(self, texts: list[str]) -> list[dict[int, float]]:
        return [{} for _ in texts]

    @staticmethod
    def _vec(text: str) -> list[float]:
        vec = [0.0] * _SMOKE_EMBED_DIM
        for tok in _smoke_char_tokens(text):
            h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
            idx = h % _SMOKE_EMBED_DIM
            sign = 1.0 if (h >> 8) & 1 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0.0:
            vec = [v / norm for v in vec]
        return vec


class _RuleEntityLLM:
    """规则抽实体的 LLM 替身（替代远程 LLM，需求 5.3「规则抽实体」）。

    ``GraphRetriever`` 仅用 ``llm.generate`` 做 query→实体名抽取（入口B）。这里对最后一条
    user 消息（即 query）跑 :func:`_smoke_extract_entities`，按 GraphRetriever 期望的格式
    返回 JSON 字符串数组。确定性、无网络。
    """

    async def generate(self, messages: list[dict], **kwargs) -> str:
        query = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                query = msg.get("content", "") or ""
                break
        return json.dumps(_smoke_extract_entities(query), ensure_ascii=False)

    async def stream(self, messages: list[dict], **kwargs):  # pragma: no cover - 未用
        yield await self.generate(messages, **kwargs)


@dataclass
class _SmokeChunk:
    """冒烟入库的一个 chunk（in-memory，写入 SQLite Chunk 表）。"""

    id: str
    doc_id: str
    content: str
    chunk_index: int


class _FakeGraphStore:
    """内存事件/实体图，实现 ``GraphRetriever`` 调用的图查询子集（确定性，无 Neo4j）。

    覆盖事件中心路（``find_entities_by_names`` / ``events_by_entities`` / ``expand_events``）
    与降级实体桥接路（``neighbors`` / ``get_entity``）所需方法，以及社区摘要桩
    （``community_summaries`` 返回空）。事件↔实体的 MENTIONS 关系在构图时物化为
    双向索引，多跳扩展沿共享实体桥接。
    """

    def __init__(
        self,
        entities: dict[str, GraphEntityDTO],
        events: dict[str, GraphEventDTO],
    ) -> None:
        self._entities = entities
        self._events = events
        # entity_id → [event_id...]（MENTIONS 反向索引，事件桥接/多跳用）。
        self._entity_to_events: dict[str, list[str]] = {}
        for ev in events.values():
            for eid in ev.entity_ids:
                self._entity_to_events.setdefault(eid, []).append(ev.id)

    def _event_dto(self, event_id: str, *, score: float = 0.0) -> "GraphEventDTO":
        from app.storage.graph_store import GraphEventDTO

        ev = self._events[event_id]
        return GraphEventDTO(
            id=ev.id, title=ev.title, summary=ev.summary, content=ev.content,
            chunk_id=ev.chunk_id, doc_id=ev.doc_id,
            entity_ids=list(ev.entity_ids), entity_names=list(ev.entity_names),
            score=score,
        )

    async def find_entities_by_names(
        self, *, kb_id: str, names: list[str], limit: int,
    ) -> list[GraphEntityDTO]:
        matched: dict[str, GraphEntityDTO] = {}
        for qname in names:
            q = qname.strip()
            if not q:
                continue
            for ent in self._entities.values():
                if q in ent.name or ent.name in q:
                    matched[ent.id] = ent
        ranked = sorted(matched.values(), key=lambda e: (-e.degree, e.id))
        return ranked[:limit]

    async def events_by_entities(
        self, *, kb_id: str, entity_ids: list[str], limit: int,
    ) -> list[GraphEventDTO]:
        counts: dict[str, int] = {}
        for eid in entity_ids:
            for ev_id in self._entity_to_events.get(eid, []):
                counts[ev_id] = counts.get(ev_id, 0) + 1
        ranked = sorted(counts, key=lambda x: (-counts[x], x))[:limit]
        return [self._event_dto(ev_id, score=float(counts[ev_id])) for ev_id in ranked]

    async def expand_events(
        self, *, kb_id: str, event_ids: list[str], hops: int, max_events: int,
    ) -> list[GraphEventDTO]:
        seeds = set(event_ids)
        frontier = set(event_ids)
        found: set[str] = set()
        for _ in range(max(1, hops)):
            ent_ids: set[str] = set()
            for ev_id in frontier:
                ev = self._events.get(ev_id)
                if ev is not None:
                    ent_ids.update(ev.entity_ids)
            nxt: set[str] = set()
            for eid in ent_ids:
                for cand in self._entity_to_events.get(eid, []):
                    if cand not in seeds and cand not in found:
                        nxt.add(cand)
            found.update(nxt)
            frontier = nxt
            if not frontier:
                break
        ranked = sorted(found)[:max_events]
        return [self._event_dto(ev_id) for ev_id in ranked]

    async def neighbors(
        self, *, kb_id: str, entity_ids: list[str], hops: int, max_nodes: int,
        types: list[str] | None = None,
    ) -> "GraphSubsetDTO":
        from app.storage.graph_store import GraphNodeDTO, GraphSubsetDTO, GraphSubsetMeta

        # 种子实体在前，再沿共享事件桥接到共现实体（degree 降序），截断 max_nodes。
        ordered: "OrderedDict[str, GraphEntityDTO]" = OrderedDict()
        for eid in entity_ids:
            ent = self._entities.get(eid)
            if ent is not None:
                ordered[eid] = ent
        neighbor_ids: set[str] = set()
        for eid in list(ordered.keys()):
            for ev_id in self._entity_to_events.get(eid, []):
                ev = self._events.get(ev_id)
                if ev is not None:
                    neighbor_ids.update(ev.entity_ids)
        extras = sorted(
            (self._entities[nid] for nid in neighbor_ids if nid not in ordered),
            key=lambda e: (-e.degree, e.id),
        )
        for ent in extras:
            ordered[ent.id] = ent
        nodes = [
            GraphNodeDTO(id=e.id, name=e.name, type=e.type, degree=e.degree)
            for e in list(ordered.values())[:max_nodes]
        ]
        meta = GraphSubsetMeta(
            mode="ego", total=len(ordered), returned=len(nodes),
            truncated=len(nodes) < len(ordered),
        )
        return GraphSubsetDTO(nodes=nodes, edges=[], meta=meta)

    async def get_entity(self, *, kb_id: str, entity_id: str) -> GraphEntityDTO | None:
        return self._entities.get(entity_id)

    async def community_summaries(
        self, *, kb_id: str, limit: int | None = None,
    ) -> list:
        return []


class _FakeEventStore:
    """内存事件向量集合，实现入口A 的 ANN ``search``（确定性，无 Milvus）。

    构造时用 ``_HashEmbedder`` 预算每个事件 ``content`` 的向量；``search`` 对 query 向量
    做线性扫描余弦排序取 top_k，鸭子兼容 ``MilvusEventStore.search`` 的返回（dict 列表）。
    """

    def __init__(self, events: dict[str, GraphEventDTO], vectors: dict[str, list[float]]) -> None:
        self._events = events
        self._vectors = vectors

    async def search(
        self, kb_id: str, query_vector: list[float], top_k: int = 10, **kwargs,
    ) -> list[dict]:
        scored = [
            (_smoke_cosine(query_vector, self._vectors[ev_id]), ev)
            for ev_id, ev in self._events.items()
        ]
        scored.sort(key=lambda t: (-t[0], t[1].id))
        hits: list[dict] = []
        for score, ev in scored[:top_k]:
            hits.append({
                "event_id": ev.id, "kb_id": kb_id, "doc_id": ev.doc_id,
                "chunk_id": ev.chunk_id, "content": ev.content, "score": score,
            })
        return hits


def _smoke_build_corpus_graph(
    corpus_files: list[Path],
) -> tuple[list["_SmokeChunk"], dict[str, str], dict[str, "GraphEntityDTO"], dict[str, "GraphEventDTO"]]:
    """对 corpus 跑确定性规则抽取，构建 chunks + doc 映射 + 实体图 + 事件。

    每篇文档按非空非标题行切成 chunk；每个 chunk 抽一个事件（content=chunk 文本，
    关联实体=规则抽实体）；实体跨 chunk 按规范名合并（id 为 name 的稳定 uuid5）。

    Returns:
        ``(chunks, doc_id_to_name, entities, events)``。
    """
    from app.storage.graph_store import GraphEntityDTO, GraphEventDTO

    chunks: list[_SmokeChunk] = []
    doc_id_to_name: dict[str, str] = {}
    entities: dict[str, GraphEntityDTO] = {}
    events: dict[str, GraphEventDTO] = {}

    def _entity_id(name: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_OID, f"smoke-entity::{name}"))

    for path in corpus_files:
        doc_id = str(uuid.uuid5(uuid.NAMESPACE_OID, f"smoke-doc::{path.name}"))
        doc_id_to_name[doc_id] = path.name
        text = path.read_text(encoding="utf-8")
        # 切 chunk：非空、非 markdown 标题行各成一个 chunk（确定性、贴合段落）。
        paragraphs = [
            ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        ]
        for idx, para in enumerate(paragraphs):
            chunk_id = str(uuid.uuid5(uuid.NAMESPACE_OID, f"smoke-chunk::{path.name}::{idx}"))
            chunks.append(_SmokeChunk(id=chunk_id, doc_id=doc_id, content=para, chunk_index=idx))

            names = _smoke_extract_entities(para)
            entity_ids: list[str] = []
            for name in names:
                ent_id = _entity_id(name)
                entity_ids.append(ent_id)
                ent = entities.get(ent_id)
                if ent is None:
                    entities[ent_id] = GraphEntityDTO(
                        id=ent_id, name=name, type="概念",
                        chunk_ids=[chunk_id], doc_ids=[doc_id], degree=1,
                    )
                else:
                    if chunk_id not in ent.chunk_ids:
                        ent.chunk_ids.append(chunk_id)
                    if doc_id not in ent.doc_ids:
                        ent.doc_ids.append(doc_id)
                    ent.degree += 1

            event_id = str(uuid.uuid5(uuid.NAMESPACE_OID, f"smoke-event::{path.name}::{idx}"))
            events[event_id] = GraphEventDTO(
                id=event_id,
                title=para[:20],
                summary=para[:40],
                content=para,
                chunk_id=chunk_id,
                doc_id=doc_id,
                entity_ids=entity_ids,
                entity_names=list(names),
            )

    return chunks, doc_id_to_name, entities, events


async def _smoke_recall(
    retriever, harness: BenchHarness, questions: list[BenchQuestion],
    *, mode: str, counting_llm,
) -> dict:
    """对单一模式跑图谱召回（冒烟版，复用 :func:`compute_metrics` 的 per_question 结构）。

    与 :func:`run_graph_recall` 同构，但用注入好的内存 retriever（不连任何外部依赖）。
    """
    per_question: list[dict] = []
    for q in questions:
        calls_before = counting_llm.calls
        t0 = time.perf_counter()
        results = await retriever.search(q.question, harness.kb_id, top_k=_RECALL_TOP_K)
        latency_ms = (time.perf_counter() - t0) * 1000.0
        calls = counting_llm.calls - calls_before
        ranked_docs = [harness.doc_id_to_name.get(r.doc_id, r.doc_id) for r in results]
        per_question.append({
            "id": q.id,
            "ranked_docs": ranked_docs,
            "ranked_chunk_ids": [r.chunk_id for r in results],
            "latency_ms": latency_ms,
            "llm_calls": calls,
        })
    return {"mode": mode, "per_question": per_question}


async def run_smoke(questions: list[BenchQuestion], corpus_files: list[Path]) -> dict:
    """无远程模型时的确定性 fallback 冒烟（需求 5.3、5.4）。

    用哈希向量 + 规则抽实体替代远程 Embedding/LLM，用内存图/事件向量集合 + in-memory
    SQLite 替代 Neo4j/Milvus，把整条流程（建库 → 入库 → 抽取 → 双模式召回 → 指标 → 报告）
    跑通，验证「流程不崩」，不校验真实质量数。

    Returns:
        ``{"baseline": m, "event_centric": m, "report_path": str}``。
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    from app.pipeline.graph.config import (
        GraphKBConfig,
        read_graph_config,
        write_graph_config,
    )
    from app.retrieval.graph_retriever import GraphRetriever
    from app.schema.db import Base, Chunk, KnowledgeBase

    logger.info("开始确定性 fallback 冒烟（哈希向量 + 规则抽实体，无远程依赖）")

    # 1) 规则抽取构建语料图（chunks + 实体 + 事件）。
    chunks, doc_id_to_name, entities, events = _smoke_build_corpus_graph(corpus_files)
    logger.info(
        "冒烟规则抽取：%d chunk / %d 实体 / %d 事件",
        len(chunks), len(entities), len(events),
    )

    # 2) in-memory SQLite（共享连接），建表 + 写 KB / Chunk 行。
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    kb_config = write_graph_config({}, GraphKBConfig(enabled=True, enable_events=True))
    async with session_factory() as session:
        session.add(KnowledgeBase(
            id=_SMOKE_KB_ID, name="smoke-event-graph",
            description="确定性 fallback 冒烟临时 KB（内存）", config=kb_config,
            tenant_id=None,
        ))
        for ch in chunks:
            session.add(Chunk(
                id=ch.id, doc_id=ch.doc_id, kb_id=_SMOKE_KB_ID,
                content=ch.content, chunk_index=ch.chunk_index,
                chunk_metadata={"element_type": "text"}, tenant_id=None,
            ))
        await session.commit()

    # 3) 组装确定性替身组件。
    embedder = _HashEmbedder()
    event_vecs_list = await embedder.embed([ev.content for ev in events.values()])
    event_vectors = {ev.id: vec for ev, vec in zip(events.values(), event_vecs_list)}

    store = _FakeGraphStore(entities, events)
    event_store = _FakeEventStore(events, event_vectors)
    counting_llm = _CountingLLM(_RuleEntityLLM())

    harness = BenchHarness(
        kb_id=_SMOKE_KB_ID, tenant_id=None, doc_id_to_name=doc_id_to_name,
    )

    # 4) 双模式召回（切 KB 的 enable_events）→ 指标。
    metrics: dict = {}
    try:
        for enable_events in (False, True):
            async with session_factory() as session:
                kb = await session.get(KnowledgeBase, _SMOKE_KB_ID)
                cfg = read_graph_config(kb.config)
                cfg.enable_events = enable_events
                kb.config = write_graph_config(kb.config, cfg)
                await session.commit()

            retriever = GraphRetriever(
                store=store,
                db_session_factory=session_factory,
                embedder=embedder,
                llm_provider=counting_llm,
                hops=1,
                max_chunks=_RECALL_TOP_K,
                event_store=event_store,
            )
            mode = "event_centric" if enable_events else "baseline"
            details = await _smoke_recall(
                retriever, harness, questions, mode=mode, counting_llm=counting_llm
            )
            metrics[mode] = compute_metrics(details, questions)
            logger.info("冒烟 %s 模式完成：%s", mode, metrics[mode]["recall_at_k"])
    finally:
        await engine.dispose()

    # 5) 出报告（带 smoke 标记，明示非真实质量数）。
    report_payload = {**metrics, "smoke": True}
    report_path = write_report(report_payload)
    print(f"✅ 冒烟通过：流程跑通，报告已产出 → {report_path}")
    return {**metrics, "report_path": str(report_path)}


def write_report(metrics: dict, *, out_dir: Path | None = None) -> Path:
    """把双模式指标渲染为 ``report_<timestamp>.md`` 对比表格（design.md「报告样例」）。

    报告默认落到 ``backend/benchmarks/event_graph/`` 下（``out_dir`` 可覆盖），含
    Recall@{2,5,10} / MRR / hit_rate / latency_ms / llm_calls_per_q 的
    baseline（实体桥接）vs event-centric 对比，并附「提升（delta）」列。

    Args:
        metrics: ``{"baseline": m, "event_centric": m}``，m 为 :func:`compute_metrics` 的输出。
        out_dir: 报告输出目录；None 时落到本基准目录（``run_benchmark.py`` 同级）。

    Returns:
        生成的报告文件 :class:`Path`。
    """
    base = metrics.get("baseline", {}) or {}
    event = metrics.get("event_centric", {}) or {}

    base_recall = base.get("recall_at_k", {}) or {}
    event_recall = event.get("recall_at_k", {}) or {}

    # 表格行定义：(展示名, baseline 取值, event 取值, 数值格式, 提升小数位)。
    # 召回/比率类保留 4 位小数；延迟/调用次数保留 1 位。
    rows: list[tuple[str, float, float, str]] = []
    for k in _EVAL_KS:
        rows.append(
            (
                f"Recall@{k}",
                float(base_recall.get(str(k), 0.0)),
                float(event_recall.get(str(k), 0.0)),
                "ratio",
            )
        )
    rows.append(("MRR", float(base.get("mrr", 0.0)), float(event.get("mrr", 0.0)), "ratio"))
    rows.append(
        (
            "命中相关文档比例(hit_rate)",
            float(base.get("hit_rate", 0.0)),
            float(event.get("hit_rate", 0.0)),
            "ratio",
        )
    )
    rows.append(
        (
            "召回延迟(latency_ms)",
            float(base.get("latency_ms", 0.0)),
            float(event.get("latency_ms", 0.0)),
            "ms",
        )
    )
    rows.append(
        (
            "LLM 调用/问(llm_calls_per_q)",
            float(base.get("llm_calls_per_q", 0.0)),
            float(event.get("llm_calls_per_q", 0.0)),
            "calls",
        )
    )

    def _fmt(value: float, kind: str) -> str:
        if kind == "ratio":
            return f"{value:.4f}"
        return f"{value:.1f}"

    def _fmt_delta(delta: float, kind: str) -> str:
        sign = "+" if delta >= 0 else "-"
        mag = abs(delta)
        if kind == "ratio":
            return f"{sign}{mag:.4f}"
        return f"{sign}{mag:.1f}"

    lines: list[str] = []
    lines.append("# 事件中心图谱检索基准报告")
    lines.append("")
    lines.append(f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- Baseline 模式：{base.get('mode', 'baseline')}（实体桥接，enable_events=False）")
    lines.append(f"- Event-Centric 模式：{event.get('mode', 'event_centric')}（事件中心，enable_events=True）")
    lines.append(f"- 评测题数：{event.get('num_questions', base.get('num_questions', 0))}")
    if metrics.get("smoke"):
        lines.append("")
        lines.append(
            "> ⚠️ 本报告由 `--smoke` 确定性 fallback（哈希向量 + 规则抽实体）产出，"
            "仅验证流程跑通，**不代表真实检索质量**。真实质量数需配置远程 Embedding/LLM "
            "+ Neo4j + Milvus 后运行完整基准。"
        )
    lines.append("")
    lines.append("| 指标 | Baseline(实体桥接) | Event-Centric | 提升(delta) |")
    lines.append("|------|-------------------|---------------|-------------|")
    for name, bval, eval_, kind in rows:
        delta = eval_ - bval
        lines.append(
            f"| {name} | {_fmt(bval, kind)} | {_fmt(eval_, kind)} | {_fmt_delta(delta, kind)} |"
        )
    lines.append("")

    target_dir = out_dir if out_dir is not None else Path(__file__).resolve().parent
    target_dir.mkdir(parents=True, exist_ok=True)
    report_path = target_dir / f"report_{time.strftime('%Y%m%d_%H%M%S')}.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("基准报告已写出: %s", report_path)
    return report_path


# ============================================================
# 入口编排
# ============================================================


async def run_full_benchmark() -> None:
    """完整基准（真实评测）：建库 → 入库 → 等抽取 → 双模式评测 → 报告。

    双模式评测（任务 15）与报告产出（任务 16）尚未实现，故当前完整链路在抽取等待后
    会因 NotImplementedError 停在评测步骤。任务 14 已打通到「抽取完成」这一里程碑。
    """
    questions, corpus_files = validate_dataset()

    from app.storage.database import init_db

    await init_db()

    kb_id = await create_temp_kb(enable_events=True)
    try:
        doc_id_to_name = await ingest_corpus(kb_id, corpus_files)
        completed = await wait_for_extraction(kb_id)
        if not completed:
            logger.warning("部分文档图谱抽取未完成，评测结果可能不完整")

        harness = BenchHarness(
            kb_id=kb_id, tenant_id=None, doc_id_to_name=doc_id_to_name
        )
        # 任务 15/16 扩展点：跑双模式评测并出报告。
        metrics = await run_dual_mode_eval(harness, questions)
        report_path = write_report(metrics)
        logger.info("基准报告已产出: %s", report_path)
    finally:
        await teardown_kb(kb_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="事件中心图谱检索基准（建临时 KB → 入库 → 等抽取 → 双模式评测 → 报告）"
    )
    parser.add_argument(
        "--smoke", action="store_true",
        help="确定性 fallback 冒烟（CI 用，不依赖远程模型；任务 16 实现）",
    )
    parser.add_argument(
        "--validate-only", action="store_true",
        help="只校验数据集格式与脚本可导入，不连任何外部依赖",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="输出详细日志")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if args.validate_only:
        questions, corpus_files = validate_dataset()
        print(f"✅ 数据集校验通过：{len(corpus_files)} 篇语料，{len(questions)} 道多跳 QA")
        return 0

    if args.smoke:
        # 任务 16 实现确定性 fallback；此处先校验数据集再委托给冒烟流程。
        questions, corpus_files = validate_dataset()
        asyncio.run(run_smoke(questions, corpus_files))
        return 0

    asyncio.run(run_full_benchmark())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
