"""知识图谱抽取触发器（design.md 4.4）。

文档入库置 ``completed`` 之后，由 :func:`maybe_trigger_graph_extract` **非阻塞**地触发
该文档的图谱抽取：双开关门控 → 自增权威 attempt → 选抽取粒度的 chunk → 建
``GraphExtractJob`` 台账 → 逐 chunk enqueue ``GraphTaskMessage`` 到 ``pipeline:graph``
慢道；任一 chunk enqueue 失败立即递减计数器（释放 slot，与 WeKnora "未实际入队需释放
slot" 一致）。

抽取走独立慢道队列 + 独立并发信号量，与文档入库 worker 物理隔离，绝不挤占主链路
（Req 1.1 / 1.2）。任何异常都不得影响主入库结果——调用方以 fire-and-forget 方式调用并
吞掉异常（优雅降级，Req 1.1）。
"""

from __future__ import annotations

import logging
import time
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.pipeline.graph.config import read_graph_config
from app.pipeline.graph.messages import GraphTaskMessage
from app.pipeline.queue import TaskQueue
from app.schema.db import Chunk, Document, GraphExtractJob, KnowledgeBase

logger = logging.getLogger(__name__)

# 慢道队列标识（design.md 4.4）：独立 stream / DLQ / consumer group，复用 TaskQueue 全部能力。
GRAPH_STREAM_KEY = "pipeline:graph"
GRAPH_DLQ_KEY = "pipeline:graph:dlq"
GRAPH_GROUP_NAME = "graph-workers"


class GraphTaskQueue(TaskQueue):
    """``pipeline:graph`` 慢道队列：复用 :class:`TaskQueue` 全部能力（DLQ / XAUTOCLAIM
    孤儿回收 / 毒消息 / 统计），仅覆写 :meth:`_parse_message` 把 Redis Stream 消息还原为
    :class:`GraphTaskMessage`（而非主链路的 ``TaskMessage``，二者字段不同）。

    入队侧（trigger）用 ``asdict`` 序列化任意 dataclass，故基类 ``enqueue`` / ``move_to_dlq``
    对 ``GraphTaskMessage`` 同样适用，无需覆写。消费侧（worker，task 4.2）的 ``consume`` /
    ``claim_pending`` 经本覆写产出 ``GraphTaskMessage``。
    """

    def _parse_message(self, msg_id, fields):  # type: ignore[override]
        """把 Redis Stream 消息解析为 ``(message_id, GraphTaskMessage)``。

        缺少必需字段（job_id/kb_id/doc_id/chunk_id）或反序列化失败时返回 None，
        由基类 ``consume`` / ``claim_pending`` 据此 ACK 丢弃脏消息（避免阻塞队列）。
        """
        import json

        try:
            mid = msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
            raw_data = fields.get(b"data") or fields.get("data")
            if raw_data is None:
                return None
            if isinstance(raw_data, bytes):
                raw_data = raw_data.decode()
            data = json.loads(raw_data)
            return (mid, GraphTaskMessage.from_dict(data))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.error("[graph-queue] 解析图谱任务消息失败: %s", e)
            return None


async def create_graph_queue(redis_url: str) -> TaskQueue | None:
    """创建 ``pipeline:graph`` 慢道 :class:`GraphTaskQueue`（design.md 4.4）。

    复用 :meth:`TaskQueue.create` 的优雅降级范式：Redis 不可用时返回 None，调用方据此
    跳过图谱触发（主链路零影响）。仅在全局 ``graph_enable`` 开启时才需创建——关闭时不
    创建以免增加未启用成本（Req 9.3）。

    Args:
        redis_url: Redis 连接串。

    Returns:
        慢道 :class:`GraphTaskQueue` 实例；Redis 不可用时 None。
    """
    return await GraphTaskQueue.create(
        redis_url,
        stream_key=GRAPH_STREAM_KEY,
        dlq_key=GRAPH_DLQ_KEY,
        group_name=GRAPH_GROUP_NAME,
    )


async def maybe_trigger_graph_extract(
    *,
    kb_id: str,
    doc_id: str,
    tenant_id: str | None,
    db_session_factory: async_sessionmaker[AsyncSession],
    graph_queue: TaskQueue | None,
) -> None:
    """文档入库完成后触发图谱抽取（design.md 4.4）。

    仅当 **全局开关 ``GRAPH_ENABLE`` + KB 级 ``config.graph.enabled`` 双开关均开启** 且
    ``graph_queue`` 可用时才执行；任一门控关闭则立即 no-op 返回（零成本，Req 9.3）。

    流程：
      1. 双开关 + 队列门控；
      2. ``Document.graph_attempt += 1``（权威 attempt），``graph_status='pending'``；
      3. 按抽取粒度选 chunk（默认 parent：``parent_id IS NULL``）；无 chunk 直接置完成；
      4. 创建 ``GraphExtractJob(pending_subtasks=N, total_subtasks=N, attempt=当前)``；
      5. 逐 chunk enqueue ``GraphTaskMessage`` 到 ``pipeline:graph``；
      6. 某 chunk enqueue 失败 → 立即递减计数器释放 slot；全部失败 → job 置 failed。

    本函数应由调用方以 fire-and-forget 方式调用（``asyncio.create_task`` + try/except），
    内部异常不向上抛出以免影响主入库结果（Req 1.1，优雅降级）。

    Args:
        kb_id: 知识库 id。
        doc_id: 已完成入库的文档 id。
        tenant_id: 文档所属租户 id（写图时盖章，可为 None）。
        db_session_factory: 异步会话工厂。
        graph_queue: ``pipeline:graph`` 慢道队列；None 表示队列不可用 → no-op。
    """
    # ─── 门控 1：全局开关 + 队列可用性 ───
    settings = get_settings()
    if not settings.graph_enable:
        return
    if graph_queue is None:
        logger.debug("[graph-trigger] doc_id=%s 跳过：graph_queue 不可用", doc_id)
        return

    # ─── 门控 2：KB 级开关（读 KnowledgeBase.config.graph.enabled） ───
    async with db_session_factory() as session:
        kb_config = await session.scalar(
            select(KnowledgeBase.config).where(KnowledgeBase.id == kb_id)
        )
        cfg = read_graph_config(kb_config)
        if not cfg.enabled:
            logger.debug("[graph-trigger] doc_id=%s 跳过：KB %s 未启用图谱", doc_id, kb_id)
            return

        # ─── 2) 自增权威 attempt + 置 pending ───
        doc = await session.scalar(select(Document).where(Document.id == doc_id))
        if doc is None:
            logger.debug("[graph-trigger] doc_id=%s 跳过：文档不存在（已删除）", doc_id)
            return
        doc.graph_attempt = (doc.graph_attempt or 0) + 1
        doc.graph_status = "pending"
        current_attempt = doc.graph_attempt

        # ─── 3) 选抽取粒度的 chunk ───
        # 默认 parent 粒度：父块（parent_id IS NULL）。child 粒度：取子块（parent_id 非空）。
        stmt = select(Chunk.id, Chunk.chunk_index).where(
            Chunk.doc_id == doc_id, Chunk.kb_id == kb_id
        )
        if cfg.extract_granularity == "child":
            stmt = stmt.where(Chunk.parent_id.is_not(None))
        else:
            stmt = stmt.where(Chunk.parent_id.is_(None))
        stmt = stmt.order_by(Chunk.chunk_index)
        chunk_rows = (await session.execute(stmt)).all()

        if not chunk_rows:
            # 无可抽取 chunk：直接把 Document 标 completed（无子任务，编排语义为已完成）。
            doc.graph_status = "completed"
            await session.commit()
            logger.info("[graph-trigger] doc_id=%s 无 %s chunk，图谱抽取直接置完成",
                        doc_id, cfg.extract_granularity)
            return

        total = len(chunk_rows)

        # ─── 4) 建 GraphExtractJob 台账 ───
        job_id = str(uuid.uuid4())
        job = GraphExtractJob(
            id=job_id,
            kb_id=kb_id,
            doc_id=doc_id,
            attempt=current_attempt,
            status="processing",
            pending_subtasks=total,
            total_subtasks=total,
            tenant_id=tenant_id,
        )
        session.add(job)
        # 先提交：job 台账与 Document attempt/status 落库后再入队，保证 worker 端
        # 陈旧守卫与 _finalize_subtask 能查到权威 attempt 与 job 行。
        await session.commit()

    # ─── 5) 逐 chunk enqueue（队列 IO 在 DB 事务之外，避免长事务） ───
    enqueued = 0
    failed = 0
    now = time.time()
    for chunk_id, chunk_index in chunk_rows:
        msg = GraphTaskMessage(
            job_id=job_id,
            kb_id=kb_id,
            doc_id=doc_id,
            chunk_id=chunk_id,
            chunk_index=int(chunk_index or 0),
            attempt=current_attempt,
            tenant_id=tenant_id,
            created_at=now,
        )
        try:
            await graph_queue.enqueue(msg)
            enqueued += 1
        except Exception as e:  # noqa: BLE001 — 单条入队失败需释放该子任务 slot，不影响其余
            failed += 1
            logger.warning(
                "[graph-trigger] doc_id=%s chunk_id=%s 入队失败，释放计数器: %s",
                doc_id, chunk_id, e,
            )
            await _release_subtask(db_session_factory, job_id, current_attempt)

    if failed:
        logger.warning(
            "[graph-trigger] doc_id=%s 入队完成：成功 %d / 失败 %d（共 %d）",
            doc_id, enqueued, failed, total,
        )
    else:
        logger.info(
            "[graph-trigger] doc_id=%s 已入队 %d 个图谱抽取子任务（attempt=%d, job=%s）",
            doc_id, enqueued, current_attempt, job_id,
        )

    # ─── 6) 全部入队失败：无子任务会到达终态递减计数器，此处直接把 job/doc 标 failed ───
    if enqueued == 0:
        await _fail_job_all_enqueue_failed(db_session_factory, job_id, doc_id, current_attempt)


async def _release_subtask(
    db_session_factory: async_sessionmaker[AsyncSession],
    job_id: str,
    attempt: int,
) -> None:
    """单条 enqueue 失败时原子递减 ``pending_subtasks`` 释放该子任务 slot。

    与 worker 终态递减语义一致（仅在 ``pending_subtasks > 0`` 且 attempt 匹配时递减），
    避免因 enqueue 失败导致计数器永不归零、job 永久 processing。用 SQLAlchemy core
    ``update`` 表达原子递减（``onupdate`` 自动刷新 updated_at），跨方言可移植。
    """
    from sqlalchemy import update

    try:
        async with db_session_factory() as session:
            await session.execute(
                update(GraphExtractJob)
                .where(
                    GraphExtractJob.id == job_id,
                    GraphExtractJob.attempt == attempt,
                    GraphExtractJob.pending_subtasks > 0,
                )
                .values(pending_subtasks=GraphExtractJob.pending_subtasks - 1)
            )
            await session.commit()
    except Exception as e:  # noqa: BLE001 — 计数器释放失败不应影响主流程
        logger.warning("[graph-trigger] 释放子任务计数器失败 job=%s: %s", job_id, e)


async def _fail_job_all_enqueue_failed(
    db_session_factory: async_sessionmaker[AsyncSession],
    job_id: str,
    doc_id: str,
    attempt: int,
) -> None:
    """所有 chunk 入队均失败时，把 job 置 failed、Document 图谱状态置 failed。

    仅当 Document 当前 attempt 仍等于本次 attempt 时才回写 graph_status（避免覆盖
    重解析产生的新 attempt 状态）。
    """
    try:
        async with db_session_factory() as session:
            job = await session.scalar(
                select(GraphExtractJob).where(GraphExtractJob.id == job_id)
            )
            if job is not None and job.status not in ("completed", "failed", "cancelled"):
                job.status = "failed"
                job.error_message = "所有抽取子任务入队失败"
            doc = await session.scalar(select(Document).where(Document.id == doc_id))
            if doc is not None and doc.graph_attempt == attempt:
                doc.graph_status = "failed"
            await session.commit()
        logger.warning("[graph-trigger] doc_id=%s 全部子任务入队失败，job/doc 置 failed", doc_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("[graph-trigger] 置 job failed 失败 job=%s: %s", job_id, e)
