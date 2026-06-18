"""GraphExtractWorker —— 知识图谱抽取慢道工作进程（design.md 4.4）。

**独立 worker**，复用 :class:`app.pipeline.worker.PipelineWorker` 的容错骨架思路
（DLQ / XAUTOCLAIM 孤儿回收 / 毒消息兜底 / 熔断 / 指数退避重试），但把单消息处理逻辑
替换为图谱抽取，且使用**独立并发信号量** ``settings.graph_extract_concurrency``，与文档
入库 worker **物理隔离**（独立队列 + 独立信号量），绝不挤占主入库链路（Req 1.2）。

与 ``PipelineWorker`` 的关系：``PipelineWorker`` 的骨架与文档入库强耦合（DocumentPipeline /
embedding 健康探活 / MinIO 文件下载 / 按 ``TaskMessage`` 处理），无法直接继承，故这里**镜像
其容错循环**（``consume`` → 处理 → ack / 退避重投 / DLQ；周期性 ``claim_pending`` 回收孤儿 +
毒消息进 DLQ；连续失败熔断），消费 :class:`GraphTaskMessage`（``pipeline:graph`` 慢道）。

单任务处理流程（含一致性守卫，design.md 4.4）：

1. **陈旧守卫**（Property 6 / Req 4.3）：``msg.attempt < Document.graph_attempt`` → ack 跳过，
   **不递减** 新 attempt 计数器、**不写** 图数据。
2. **取消守卫**（Req 4.4）：Document 已删除 / 取消 → ack 跳过。
3. 抽取：载入 chunk（可能已被新 attempt 删除 → 跳过并终态递减）→ 读 KB 图谱配置 → 取
   LLM（``extract_model_id`` 或 KB 默认）→ ``GraphExtractor.extract`` → ``EntityResolver.resolve``
   → 给实体 / 关系打 chunk 来源 → ``GraphStore.upsert_graph`` → 累加 job 计数。
4. **终态递减**（Property 5 / Req 4.1、4.2）：成功或终败（重试耗尽进 DLQ）都恰好递减一次
   ``pending_subtasks``；归零 → job 与 Document 置 ``completed``。
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from typing import TYPE_CHECKING

from sqlalchemy import select, update

from app.config import get_settings
from app.pipeline.graph.config import GraphKBConfig, read_graph_config
from app.pipeline.graph.extractor import GraphExtractor
from app.pipeline.graph.messages import GraphTaskMessage
from app.pipeline.graph.resolver import EntityResolver
from app.schema.db import Chunk, Document, GraphExtractJob, KnowledgeBase

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.pipeline.queue import TaskQueue
    from app.storage.graph_store import GraphStore

logger = logging.getLogger("pipeline.graph.worker")


class GraphExtractWorker:
    """知识图谱抽取慢道工作进程。

    通过独立信号量 ``max_concurrent``（= ``settings.graph_extract_concurrency``）限制在途抽取
    并发，与文档入库 worker 物理隔离。``store`` 为 None（图谱未启用 / Neo4j 不可用）时
    ``start()`` 直接 no-op 返回，零成本（Req 9.3）。
    """

    def __init__(
        self,
        queue: "TaskQueue",
        store: "GraphStore | None",
        db_session_factory: "async_sessionmaker[AsyncSession]",
        *,
        max_concurrent: int = 2,
        max_retries: int = 3,
    ) -> None:
        """构造。

        Args:
            queue: ``pipeline:graph`` 慢道队列（``GraphTaskQueue``，消费产出 ``GraphTaskMessage``）。
            store: 图存储；None 表示图谱未启用 / 不可用 → worker 不消费。
            db_session_factory: 异步会话工厂。
            max_concurrent: 独立并发信号量上限（抽取慢道，物理隔离于主链路）。
            max_retries: 单子任务应用层最大重试次数，超过进 DLQ。
        """
        self._queue = queue
        self._store = store
        self._db_session_factory = db_session_factory
        self._max_concurrent = max(1, max_concurrent)
        self._max_retries = max_retries
        # 独立并发信号量：与文档入库 worker 的 semaphore 互不共享（Req 1.2 物理隔离）。
        self.semaphore = asyncio.Semaphore(self._max_concurrent)
        self._running = False
        self._consumer_name = f"graph-worker-{socket.gethostname()}"
        self._tasks: set[asyncio.Task] = set()

        settings = get_settings()
        # 熔断：连续失败 N 次暂停消费一段时间再恢复（复用主链路阈值与轮询间隔）。
        self._circuit_breaker_threshold = settings.pipeline_circuit_breaker_threshold
        self._health_check_interval = settings.pipeline_health_check_interval
        self._consecutive_failures = 0
        self._circuit_open = False

        # PEL 孤儿任务周期性回收（崩溃恢复）。idle 阈值取主链路同名配置，毒消息投递
        # 次数上限复用 max_retries。
        self._claim_interval = settings.pipeline_claim_interval_seconds
        self._claim_min_idle_ms = settings.pipeline_claim_min_idle_minutes * 60 * 1000
        self._claim_max_delivery = self._max_retries
        self._last_claim_at = 0.0

    async def start(self) -> None:
        """启动 worker 消费循环；``store`` 为 None 时直接返回（图谱未启用）。"""
        if self._store is None:
            logger.info("[graph-worker] 图谱未启用或存储不可用，GraphExtractWorker 不启动")
            return

        self._running = True
        logger.info(
            "[graph-worker] 启动，max_concurrent=%d, max_retries=%d",
            self._max_concurrent, self._max_retries,
        )
        print(f"[GraphWorker] 知识图谱抽取 worker 启动，并发上限={self._max_concurrent}")

        # 启动时回收上一个实例遗留在 PEL 的孤儿任务。
        await self._reclaim_orphan_tasks()

        while self._running:
            # 熔断：连续失败过多 → 暂停消费、退避后恢复（不依赖外部健康探活，避免误判）。
            if self._circuit_open:
                logger.warning("[graph-worker] 熔断中，%ds 后恢复消费", self._health_check_interval)
                print(f"[GraphWorker] ⚡ 熔断中，{self._health_check_interval}s 后恢复...")
                await asyncio.sleep(self._health_check_interval)
                self._circuit_open = False
                self._consecutive_failures = 0

            # 周期性回收 PEL 孤儿任务（崩溃恢复）。
            if time.monotonic() - self._last_claim_at >= self._claim_interval:
                await self._reclaim_orphan_tasks()

            try:
                messages = await self._queue.consume(
                    self._consumer_name, count=1, block_ms=5000
                )
                for message_id, msg in messages:
                    task = asyncio.create_task(self._process_task(message_id, msg))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001 - 消费异常不退出循环，退避后重试
                logger.error("[graph-worker] 消费任务出错：%s，5s 后重试", e)
                await asyncio.sleep(5)

    async def stop(self) -> None:
        """停止 worker，等待在途任务完成。"""
        self._running = False
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _reclaim_orphan_tasks(self) -> None:
        """认领 PEL 中 idle 超时的孤儿消息重新处理；毒消息由队列层移入 DLQ。"""
        self._last_claim_at = time.monotonic()
        try:
            pending = await self._queue.claim_pending(
                self._consumer_name,
                min_idle_ms=self._claim_min_idle_ms,
                max_delivery_count=self._claim_max_delivery,
                on_poison_pill=self._on_poison_pill,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[graph-worker] 回收孤儿任务失败：%s", e)
            return

        if not pending:
            return
        logger.info("[graph-worker] ♻️ 回收 %d 个 PEL 孤儿图谱任务", len(pending))
        for message_id, msg in pending:
            task = asyncio.create_task(self._process_task(message_id, msg))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _on_poison_pill(self, msg: GraphTaskMessage, reason: str) -> None:
        """毒消息进 DLQ 后回调：终态递减该子任务计数器，避免 job 永久 pending。

        毒消息（硬崩溃反复重投超 delivery_count）从未走过正常终态递减，需在此补一次
        递减以推进 ``pending_subtasks`` 归零（Property 5：终败也恰好递减一次）。
        """
        logger.error(
            "[graph-worker] 💀 图谱子任务判定为毒消息进入 DLQ: job=%s chunk=%s 原因=%s",
            msg.job_id, msg.chunk_id, reason,
        )
        await self._finalize_subtask(msg.job_id, msg.doc_id, msg.attempt)

    async def _process_task(self, message_id: str, msg: GraphTaskMessage) -> None:
        """在独立信号量内处理单条图谱抽取子任务（含守卫、抽取、终态递减、重试 / DLQ）。"""
        async with self.semaphore:
            # ─── 守卫 1：陈旧任务（重解析已发生）→ ack 跳过，不递减、不写图（Property 6） ───
            if await self._is_superseded(msg.doc_id, msg.attempt):
                logger.info(
                    "[graph-worker] 跳过陈旧子任务 doc=%s msg.attempt=%d（已重解析）",
                    msg.doc_id, msg.attempt,
                )
                await self._queue.ack(message_id)
                return

            # ─── 守卫 2：文档已删除 / 取消 → ack 跳过（Req 4.4） ───
            if await self._is_doc_gone_or_cancelled(msg.doc_id):
                logger.info("[graph-worker] 跳过：文档 %s 已删除 / 取消", msg.doc_id)
                await self._queue.ack(message_id)
                return

            try:
                await self._process_graph_task(msg)
            except Exception as e:  # noqa: BLE001 - 失败交由重试 / DLQ 决策
                logger.error(
                    "[graph-worker] ❌ 抽取失败 doc=%s chunk=%s: %s: %s",
                    msg.doc_id, msg.chunk_id, type(e).__name__, e,
                )
                self._record_failure()
                await self._handle_failure(message_id, msg, e)
                return

            # 成功：ack、终态递减、重置熔断计数。
            await self._queue.ack(message_id)
            await self._finalize_subtask(msg.job_id, msg.doc_id, msg.attempt)
            self._consecutive_failures = 0

    async def _process_graph_task(self, msg: GraphTaskMessage) -> None:
        """单 chunk 抽取核心：载入 chunk → 配置 → LLM 抽取 → 消歧 → 打标 → upsert → 计数。

        抽取 / 消歧 / 写库任一步异常向上抛出，由 ``_process_task`` 走重试 / DLQ。chunk 已被
        新 attempt 删除（载入为空）时按正常终态处理（不抛错，由调用方 ack + 递减）。
        """
        chunk = await self._load_chunk(msg.chunk_id, msg.kb_id)
        if chunk is None:
            # chunk 已被新 attempt 删除（重解析）或不存在：无内容可抽，视为正常终态。
            logger.info("[graph-worker] chunk %s 不存在（可能已被新 attempt 删除），跳过抽取", msg.chunk_id)
            return

        content, cfg = chunk
        llm = await self._get_extract_llm(cfg)

        graph = await GraphExtractor(llm).extract(
            text=content,
            entity_types=cfg.entity_types,
            relation_types=cfg.relation_types,
        )

        embedder = self._get_embedder()
        resolver = EntityResolver(
            embedder,
            self._store,  # type: ignore[arg-type]  # 已在 start() 保证非 None
            enable_alias_dedup=cfg.enable_alias_dedup,
            sim_threshold=cfg.alias_sim_threshold,
        )
        resolved = await resolver.resolve(kb_id=msg.kb_id, graph=graph)

        # 给实体 / 关系打 chunk 来源（upsert_graph 以 getattr 读取 chunk_ids / chunk_id）。
        for ent in resolved.entities:
            ent.chunk_ids = [msg.chunk_id]
        for rel in resolved.relations:
            rel.chunk_id = msg.chunk_id  # type: ignore[attr-defined]

        ne, nr = await self._store.upsert_graph(  # type: ignore[union-attr]
            kb_id=msg.kb_id,
            tenant_id=msg.tenant_id,
            doc_id=msg.doc_id,
            entities=resolved.entities,
            relations=resolved.relations,
        )
        await self._accumulate_job_counts(msg.job_id, msg.attempt, ne, nr)
        logger.info(
            "[graph-worker] ✅ chunk %s 抽取完成：实体 %d、关系 %d",
            msg.chunk_id, ne, nr,
        )

    # ------------------------------------------------------------------
    # 一致性守卫与数据载入
    # ------------------------------------------------------------------

    async def _is_superseded(self, doc_id: str, attempt: int) -> bool:
        """子任务 attempt 是否已被 Document 当前 graph_attempt 超越（重解析发生，Property 6）。

        查询失败按「未陈旧」处理（让抽取继续尝试，避免误跳过）。
        """
        try:
            async with self._db_session_factory() as session:
                current = await session.scalar(
                    select(Document.graph_attempt).where(Document.id == doc_id)
                )
            if current is None:
                return False  # 文档不存在交由取消守卫处理
            return attempt < current
        except Exception as e:  # noqa: BLE001
            logger.warning("[graph-worker] 陈旧守卫查询失败 doc=%s: %s", doc_id, e)
            return False

    async def _is_doc_gone_or_cancelled(self, doc_id: str) -> bool:
        """文档是否已删除或被取消（Req 4.4）。查询失败按「未删除」处理。"""
        try:
            async with self._db_session_factory() as session:
                status = await session.scalar(
                    select(Document.status).where(Document.id == doc_id)
                )
            if status is None:
                return True  # 文档已删除
            return status == "cancelled"
        except Exception as e:  # noqa: BLE001
            logger.warning("[graph-worker] 取消守卫查询失败 doc=%s: %s", doc_id, e)
            return False

    async def _load_chunk(
        self, chunk_id: str, kb_id: str
    ) -> tuple[str, GraphKBConfig] | None:
        """载入 chunk 内容与所属 KB 的图谱配置；chunk 不存在返回 None。

        一次会话内取 chunk.content 与 KnowledgeBase.config（避免多次开会话）。
        """
        async with self._db_session_factory() as session:
            content = await session.scalar(
                select(Chunk.content).where(
                    Chunk.id == chunk_id, Chunk.kb_id == kb_id
                )
            )
            if content is None:
                return None
            kb_config = await session.scalar(
                select(KnowledgeBase.config).where(KnowledgeBase.id == kb_id)
            )
        return content, read_graph_config(kb_config)

    async def _get_extract_llm(self, cfg: GraphKBConfig):
        """取抽取用 LLM：指定 ``extract_model_id`` 则用之，否则用 KB / 系统默认。

        复用 ``app.api.chat._get_llm_for_request`` 的模型选择逻辑（懒导入避免循环依赖）。
        """
        from app.api.chat import _get_llm_for_request

        llm, _stream, _max_ctx = await _get_llm_for_request(cfg.extract_model_id)
        return llm

    def _get_embedder(self):
        """取 embedder（实体别名消歧用）；不可用时由 resolver 自行降级。"""
        try:
            from app.models.manager import get_model_manager

            return get_model_manager().embedder
        except Exception as e:  # noqa: BLE001 - embedder 取不到 → 传 None，resolver 降级
            logger.warning("[graph-worker] 获取 embedder 失败，别名消歧将降级：%s", e)
            return None

    # ------------------------------------------------------------------
    # 终态递减与计数（原子 SQL，带 attempt 条件）
    # ------------------------------------------------------------------

    async def _finalize_subtask(self, job_id: str, doc_id: str, attempt: int) -> None:
        """终态原子递减 ``pending_subtasks`` 并判零（Property 5 / Req 4.1、4.2）。

        单条原子 ``UPDATE ... WHERE id=:job AND attempt=:attempt AND pending_subtasks>0
        RETURNING pending_subtasks``：

        - ``attempt`` 条件确保陈旧 attempt 的递减不命中新行（Property 5 / 6），并发安全；
        - ``pending_subtasks > 0`` 条件确保不会递减到负（重复调用幂等，最多减一次到 0）；
        - 命中后剩余为 0 → 紧随置 job.status='completed'、Document.graph_status='completed'
          （Document 仅在其 graph_attempt 仍等于本 attempt 时回写，避免覆盖更新的重解析状态）。

        递减失败仅记 warning，不抛出（避免影响已成功的抽取结果与消息 ack）。
        """
        try:
            async with self._db_session_factory() as session:
                result = await session.execute(
                    update(GraphExtractJob)
                    .where(
                        GraphExtractJob.id == job_id,
                        GraphExtractJob.attempt == attempt,
                        GraphExtractJob.pending_subtasks > 0,
                    )
                    .values(pending_subtasks=GraphExtractJob.pending_subtasks - 1)
                    .returning(GraphExtractJob.pending_subtasks)
                )
                row = result.first()
                remaining = row[0] if row else None

                if remaining == 0:
                    # 归零：置 job 与 Document 完成态。
                    job = await session.scalar(
                        select(GraphExtractJob).where(GraphExtractJob.id == job_id)
                    )
                    if job is not None and job.status not in ("completed", "failed", "cancelled"):
                        job.status = "completed"
                    doc = await session.scalar(
                        select(Document).where(Document.id == doc_id)
                    )
                    if doc is not None and doc.graph_attempt == attempt:
                        doc.graph_status = "completed"

                await session.commit()

            if remaining == 0:
                logger.info("[graph-worker] 🎉 job=%s 全部子任务完成，doc=%s 图谱抽取完成", job_id, doc_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("[graph-worker] 终态递减失败 job=%s: %s", job_id, e)

    async def _accumulate_job_counts(
        self, job_id: str, attempt: int, entities: int, relations: int
    ) -> None:
        """原子累加 job 的实体 / 关系计数（带 attempt 条件，陈旧任务不累加）。"""
        if entities <= 0 and relations <= 0:
            return
        try:
            async with self._db_session_factory() as session:
                await session.execute(
                    update(GraphExtractJob)
                    .where(
                        GraphExtractJob.id == job_id,
                        GraphExtractJob.attempt == attempt,
                    )
                    .values(
                        entities_count=GraphExtractJob.entities_count + entities,
                        relations_count=GraphExtractJob.relations_count + relations,
                    )
                )
                await session.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning("[graph-worker] 累加 job 计数失败 job=%s: %s", job_id, e)

    # ------------------------------------------------------------------
    # 熔断与失败处理（重试 / DLQ）
    # ------------------------------------------------------------------

    def _record_failure(self) -> None:
        """记录连续失败次数，达到阈值触发熔断。"""
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._circuit_breaker_threshold:
            self._circuit_open = True
            logger.error(
                "[graph-worker] ⚡ 熔断触发：连续 %d 次失败（阈值 %d）",
                self._consecutive_failures, self._circuit_breaker_threshold,
            )

    async def _handle_failure(
        self, message_id: str, msg: GraphTaskMessage, error: Exception
    ) -> None:
        """失败处理：``retry_count < max_retries`` 指数退避重投，否则进 DLQ 并终态递减。

        重投时 **不递减** 计数器（子任务尚未到达终态）；进 DLQ 是终败，递减一次（Property 5）。
        """
        error_str = f"{type(error).__name__}: {error}"
        next_retry = msg.retry_count + 1

        if next_retry <= self._max_retries:
            delay = 2 ** (next_retry - 1)  # 1s, 2s, 4s
            logger.warning(
                "[graph-worker] 🔄 chunk=%s 第 %d/%d 次重试，%ds 后重投：%s",
                msg.chunk_id, next_retry, self._max_retries, delay, error_str,
            )
            await self._queue.ack(message_id)
            await asyncio.sleep(delay)
            retry_msg = GraphTaskMessage(
                job_id=msg.job_id,
                kb_id=msg.kb_id,
                doc_id=msg.doc_id,
                chunk_id=msg.chunk_id,
                chunk_index=msg.chunk_index,
                attempt=msg.attempt,
                tenant_id=msg.tenant_id,
                retry_count=next_retry,
                created_at=msg.created_at,
                trace_id=msg.trace_id,
            )
            try:
                await self._queue.enqueue(retry_msg)
            except Exception as e:  # noqa: BLE001 - 重投入队失败：作为终败，终态递减释放 slot
                logger.error("[graph-worker] 重投入队失败 chunk=%s，按终败递减：%s", msg.chunk_id, e)
                await self._finalize_subtask(msg.job_id, msg.doc_id, msg.attempt)
        else:
            logger.error(
                "[graph-worker] 💀 chunk=%s 重试 %d 次后失败，进入 DLQ：%s",
                msg.chunk_id, self._max_retries, error_str,
            )
            await self._queue.move_to_dlq(message_id, msg, error_str)
            # 终败：递减一次推进 pending_subtasks 归零（Property 5）。
            await self._finalize_subtask(msg.job_id, msg.doc_id, msg.attempt)
