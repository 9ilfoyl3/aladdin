"""知识图谱删除清理链路与 housekeeping 巡检（design.md 4.6 / Error Handling）。

本模块汇集「文档删除 / 文档重解析 / KB 删除」三类场景的图谱清理入口，以及抽取任务
台账的超时巡检。核心原则：**所有图谱清理都必须优雅降级**——``get_graph_store()`` 返回
None（图谱未启用 / Neo4j 不可用 / 驱动未装）时静默跳过；任何 Neo4j 异常都被吞掉并记
warning，绝不冒泡影响主删除 / 重解析 / RAG 链路（Req 5.1 / 5.2 / 5.3 / 7.2，优雅降级）。

调用方均以 ``asyncio.create_task`` fire-and-forget 方式调用本模块函数，与既有 Milvus /
MinIO 后台清理对称，不阻塞 API 响应。

housekeeping（Req 4.4）：worker 硬崩溃可能导致 ``GraphExtractJob.pending_subtasks`` 永不
归零、job 永久停留在 pending/processing。:func:`sweep_stuck_graph_jobs` 周期性把超过
``graph_job_timeout_minutes`` 仍未到达终态的 job 置 ``failed`` 并零化计数器，使其最终到达
终态、不永久卡死。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.schema.db import GraphExtractJob

logger = logging.getLogger("pipeline.graph.cleanup")


async def cleanup_graph_for_doc(kb_id: str, doc_id: str) -> None:
    """删除某文档贡献的图数据（文档删除 / 重解析清旧图，Req 5.1 / 5.2）。

    优雅降级：``get_graph_store()`` 为 None（图谱未启用 / Neo4j 不可用）时静默跳过；
    Neo4j 异常被吞掉并记 warning，绝不影响主删除流程。应以 fire-and-forget 调用。

    事件中心图谱（event-centric-graph，Req 2.3 / 2.4）：Neo4j 的 ``delete_by_doc`` 已连带删除
    ``:Event`` 节点与 ``MENTIONS`` 边；此处再按 ``doc_id`` 删除 Milvus 事件向量，与 Neo4j 对称，
    保证重处理「先删后写」幂等、删库无孤儿。Milvus 删除独立 try/except 降级，不影响 Neo4j 清理。
    """
    try:
        from app.storage.graph_store import get_graph_store

        store = await get_graph_store()
        if store is None:
            return
        deleted = await store.delete_by_doc(kb_id=kb_id, doc_id=doc_id)
        logger.info(
            "[graph-cleanup] 文档图谱清理完成 kb=%s doc=%s（删除关系 %d）",
            kb_id, doc_id, deleted,
        )
    except Exception as e:  # noqa: BLE001 — 图谱清理失败不得影响主删除链路（优雅降级）
        logger.warning("[graph-cleanup] 删除文档图谱失败 kb=%s doc=%s: %s", kb_id, doc_id, e)

    # 删除该文档的 Milvus 事件向量（与 Neo4j 事件节点删除对称；独立降级）。
    await _delete_event_vectors_for_doc(kb_id, doc_id)


async def _delete_event_vectors_for_doc(kb_id: str, doc_id: str) -> None:
    """按 doc_id 删除 Milvus 事件向量（事件中心图谱重处理 / 删除一致性）。

    优雅降级：事件集合不存在 / Milvus 不可用时静默跳过；任何异常被吞掉记 warning，
    绝不影响主删除 / 重解析链路。
    """
    try:
        from app.storage.milvus_event_store import get_milvus_event_store

        event_store = get_milvus_event_store()
        await event_store.delete_by_doc(kb_id=kb_id, doc_id=doc_id)
        logger.info("[graph-cleanup] 文档事件向量清理完成 kb=%s doc=%s", kb_id, doc_id)
    except Exception as e:  # noqa: BLE001 — 事件向量清理失败不得影响主链路（优雅降级）
        logger.warning("[graph-cleanup] 删除文档事件向量失败 kb=%s doc=%s: %s", kb_id, doc_id, e)


async def cleanup_graph_for_docs(kb_id: str, doc_ids: list[str]) -> None:
    """批量删除多个文档贡献的图数据（批量删除 / 文件夹删除场景）。

    逐 doc 调 :func:`cleanup_graph_for_doc`，单个失败不影响其余。store 为 None 时整体跳过
    （只取一次 store，避免重复构造）。
    """
    if not doc_ids:
        return
    try:
        from app.storage.graph_store import get_graph_store

        store = await get_graph_store()
        if store is None:
            return
        for doc_id in doc_ids:
            try:
                await store.delete_by_doc(kb_id=kb_id, doc_id=doc_id)
            except Exception as e:  # noqa: BLE001 — 单 doc 失败不影响其余
                logger.warning(
                    "[graph-cleanup] 批量删除文档图谱失败 kb=%s doc=%s: %s", kb_id, doc_id, e
                )
            # 删除该文档事件向量（与 Neo4j 事件节点删除对称；单 doc 失败不影响其余）。
            await _delete_event_vectors_for_doc(kb_id, doc_id)
        logger.info("[graph-cleanup] 批量文档图谱清理完成 kb=%s（%d 个文档）", kb_id, len(doc_ids))
    except Exception as e:  # noqa: BLE001
        logger.warning("[graph-cleanup] 批量删除文档图谱失败 kb=%s: %s", kb_id, e)


async def cleanup_graph_for_kb(kb_id: str) -> None:
    """删除整个 KB 的图（KB 删除时调用，Req 5.3）。

    优雅降级同 :func:`cleanup_graph_for_doc`。应以 fire-and-forget 调用。

    事件中心图谱（Req 2.4）：删除 Neo4j 图后再删整个 Milvus 事件集合 ``kb_event_<kb_id>``，
    不留事件向量孤儿。两者独立降级。
    """
    try:
        from app.storage.graph_store import get_graph_store

        store = await get_graph_store()
        if store is None:
            return
        await store.delete_by_kb(kb_id=kb_id)
        logger.info("[graph-cleanup] KB 图谱整库清理完成 kb=%s", kb_id)
    except Exception as e:  # noqa: BLE001 — 图谱清理失败不得影响主删库链路（优雅降级）
        logger.warning("[graph-cleanup] 删除 KB 图谱失败 kb=%s: %s", kb_id, e)

    # 删除整个 KB 的事件向量集合（与 Neo4j 整库清理对称；独立降级）。
    try:
        from app.storage.milvus_event_store import get_milvus_event_store

        await get_milvus_event_store().delete_by_kb(kb_id=kb_id)
        logger.info("[graph-cleanup] KB 事件向量集合清理完成 kb=%s", kb_id)
    except Exception as e:  # noqa: BLE001 — 事件向量清理失败不得影响主删库链路（优雅降级）
        logger.warning("[graph-cleanup] 删除 KB 事件向量集合失败 kb=%s: %s", kb_id, e)


async def sweep_stuck_graph_jobs(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    timeout_minutes: int | None = None,
) -> int:
    """巡检并终结超时未完成的抽取 job（Req 4.4 / Error Handling「抽取任务卡死」）。

    把 ``status in (pending, processing)`` 且 ``updated_at`` 早于 ``now - timeout_minutes`` 的
    job 原子置 ``status='failed'``、``pending_subtasks=0``、补 ``error_message``。零化计数器使
    其到达终态，不再永久停留 pending（worker 的孤儿回收 / 毒消息兜底负责重投，本巡检负责
    兜底终结确实卡死的 job）。

    Args:
        db_session_factory: 异步会话工厂。
        timeout_minutes: 超时阈值（分钟）；None 时取 ``settings.graph_job_timeout_minutes``。

    Returns:
        本次置 failed 的 job 行数。查询 / 更新失败返回 0（仅记 warning，不抛出）。
    """
    if timeout_minutes is None:
        timeout_minutes = get_settings().graph_job_timeout_minutes
    cutoff = datetime.utcnow() - timedelta(minutes=timeout_minutes)

    try:
        async with db_session_factory() as session:
            result = await session.execute(
                update(GraphExtractJob)
                .where(
                    GraphExtractJob.status.in_(("pending", "processing")),
                    GraphExtractJob.updated_at < cutoff,
                )
                .values(
                    status="failed",
                    pending_subtasks=0,
                    error_message="housekeeping 巡检：抽取任务超时未完成，置 failed",
                )
                .returning(GraphExtractJob.id)
            )
            stuck_ids = [row[0] for row in result.all()]
            await session.commit()
        if stuck_ids:
            logger.warning(
                "[graph-housekeeping] 巡检置 failed %d 个超时 job（超时阈值 %d 分钟）: %s",
                len(stuck_ids), timeout_minutes, stuck_ids,
            )
        return len(stuck_ids)
    except Exception as e:  # noqa: BLE001 — 巡检失败不应影响进程其余职责
        logger.warning("[graph-housekeeping] 超时 job 巡检失败: %s", e)
        return 0


async def run_graph_housekeeping_loop(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    stop_event: asyncio.Event | None = None,
) -> None:
    """周期性运行 :func:`sweep_stuck_graph_jobs` 的常驻巡检循环（Req 4.4）。

    每 ``settings.graph_housekeeping_interval_seconds`` 巡检一次；``stop_event`` 置位或被
    cancel 时优雅退出。由 worker 进程在 ``graph_enable`` 开启时以独立后台任务启动。
    """
    settings = get_settings()
    interval = max(10, settings.graph_housekeeping_interval_seconds)
    logger.info("[graph-housekeeping] 巡检循环启动，间隔 %ds", interval)
    try:
        while stop_event is None or not stop_event.is_set():
            await sweep_stuck_graph_jobs(db_session_factory)
            try:
                if stop_event is not None:
                    await asyncio.wait_for(stop_event.wait(), timeout=interval)
                else:
                    await asyncio.sleep(interval)
            except asyncio.TimeoutError:
                continue  # 间隔到，进入下一轮巡检
    except asyncio.CancelledError:
        logger.info("[graph-housekeeping] 巡检循环收到取消信号，退出")
        raise
