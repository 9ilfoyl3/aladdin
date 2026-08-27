"""全量重建向量索引脚本（Milvus 拓扑切换 / 换 embedding 模型后使用）。

为什么需要它
------------
Milvus 的 Partition Key 字段、``num_partitions``、``num_shards`` 以及向量维度都是
**建表时固定**的，无法事后修改。因此以下变更都必须「重建表 + 重新灌向量」：

- 从旧拓扑（每个知识库一个 collection）切到共享 collection + Partition Key。
- 调整 ``MILVUS_NUM_PARTITIONS`` / ``MILVUS_SHARDS_NUM``。
- 更换 embedding 模型导致维度变化（这种情况下**旧维度表会被保留**，可先灌新维度、
  验证无误后再用 ``init_milvus.py --prune-dims`` 清旧表，实现近似零停机切换）。

向量是**派生数据**：权威源文件在 MinIO、文档/切片元数据在 PostgreSQL，所以重建是安全的
——把文档重新投喂给既有的处理管线即可，不需要备份 Milvus。

做什么
------
1. 体检：统计各状态文档数、检查源文件是否还在 MinIO、打印当前 Milvus 拓扑。
2. （可选 ``--reset-milvus``）重建 Milvus 拓扑：删掉受管表 + 可选清理旧 ``kb_*`` 表，
   再按当前配置建表。
3. 清理 PostgreSQL 中的 ``chunks``（切片元数据会由管线重新生成；不清会残留孤儿行）。
4. 把目标文档状态重置为 ``pending``、清空 ``chunk_count`` / 错误信息 / 图谱状态。
5. 按文件大小分快 / 慢道批量入队 Redis Stream，由既有 Worker 进程消费重建索引。
6. （可选 ``--watch``）轮询打印进度直到全部完成或失败。

**本脚本只负责入队，实际重建由 Worker 完成**，因此执行期间 Worker 必须在运行。

用法::

    # 1) 只体检，不改任何东西（强烈建议先跑这个）
    python -m scripts.reindex_all --dry-run

    # 2) 拓扑切换（首次上新拓扑）：重建表 + 清旧 kb_* + 全量重灌 + 看进度
    python -m scripts.reindex_all --reset-milvus --drop-legacy --watch

    # 3) 换 embedding 模型（保留旧维度表，先灌新维度验证）
    python -m scripts.reindex_all --watch

    # 4) 只重建某个知识库（灰度验证）
    python -m scripts.reindex_all --kb-id <kb_uuid> --watch

    # 5) 断点续跑：只补还没成功的（pending/failed/processing），已 completed 的不动
    python -m scripts.reindex_all --only-unfinished --watch

安全说明
--------
- ``--reset-milvus`` 是破坏性动作，会清空全部向量；不带该参数时只重灌不清表
  （靠管线内的「写入前按 doc_id 先删旧」保证幂等，不会产生重复向量）。
- 破坏性动作默认需要交互确认，自动化场景用 ``--yes`` 跳过。
- 脚本不设置租户 contextvar，因此默认可跨全部租户读写（与启动期任务同一语义）。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# 确保 backend 目录在 sys.path 中（脚本以 `python -m scripts.reindex_all` 运行）
_backend_dir = Path(__file__).resolve().parent.parent
if str(_backend_dir) not in sys.path:
    sys.path.insert(0, str(_backend_dir))

from sqlalchemy import delete as sql_delete, func, select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.pipeline.queue import TaskMessage, TaskQueue  # noqa: E402
from app.schema.db import Chunk, Document, KnowledgeBase  # noqa: E402
from app.storage.database import async_session  # noqa: E402
from app.storage.milvus import MilvusClient, parse_dim  # noqa: E402
from app.storage.object_store import document_object_key, get_object_store  # noqa: E402

logger = logging.getLogger("reindex_all")

# 旧拓扑遗留的 per-KB collection 前缀；kb_event_* 是事件向量（另一套 per-KB 拓扑，
# 不在本次范围）必须排除。
_LEGACY_PREFIX = "kb_"
_LEGACY_EXCLUDE_PREFIX = "kb_event_"

# 入队节流：每批入队后短暂让出，避免瞬间灌满 Redis Stream 与 Worker 队列。
_ENQUEUE_BATCH = 200
_ENQUEUE_PAUSE_SECONDS = 0.2

# 未完成状态集合（可被重建的目标状态）
_UNFINISHED = ("pending", "processing", "failed")


@dataclass
class DocTask:
    """一条待重建的文档任务（脱离 ORM session 的纯值对象）。"""

    doc_id: str
    kb_id: str
    tenant_id: str | None
    file_type: str
    file_size: int | None
    filename: str

    @property
    def object_key(self) -> str:
        return document_object_key(self.doc_id, self.file_type)


def _build_client() -> MilvusClient:
    """按当前配置构造 Milvus 客户端（与运行时 ``get_milvus_client`` 同参）。"""
    s = get_settings()
    return MilvusClient(
        host=s.milvus_host,
        port=s.milvus_port,
        collection=s.milvus_collection,
        session_collection=s.milvus_session_collection,
        num_partitions=s.milvus_num_partitions,
        session_num_partitions=s.milvus_session_num_partitions,
        dim=s.embed_dim,
        shards_num=s.milvus_shards_num,
        replica_number=s.milvus_replica_number,
    )


# ------------------------------------------------------------------
# 体检
# ------------------------------------------------------------------


async def _collect_tasks(kb_id: str | None, only_unfinished: bool) -> list[DocTask]:
    """读取待重建文档（跨全部租户；脚本不设置租户 contextvar 故默认不注入过滤）。"""
    async with async_session() as session:
        stmt = select(
            Document.id, Document.kb_id, Document.tenant_id,
            Document.file_type, Document.file_size, Document.filename,
        )
        if kb_id:
            stmt = stmt.where(Document.kb_id == kb_id)
        if only_unfinished:
            stmt = stmt.where(Document.status.in_(_UNFINISHED))
        stmt = stmt.order_by(Document.created_at)
        rows = (await session.execute(stmt)).all()
    return [
        DocTask(
            doc_id=r.id, kb_id=r.kb_id, tenant_id=r.tenant_id,
            file_type=r.file_type or "", file_size=r.file_size,
            filename=r.filename or "",
        )
        for r in rows
    ]


async def _status_summary(kb_id: str | None) -> dict[str, int]:
    """按状态统计文档数。"""
    async with async_session() as session:
        stmt = select(Document.status, func.count(Document.id)).group_by(Document.status)
        if kb_id:
            stmt = stmt.where(Document.kb_id == kb_id)
        return {status: count for status, count in (await session.execute(stmt)).all()}


async def _kb_count() -> int:
    async with async_session() as session:
        return await session.scalar(select(func.count(KnowledgeBase.id))) or 0


async def _check_source_files(tasks: list[DocTask]) -> tuple[list[DocTask], list[DocTask]]:
    """把任务分成「源文件存在」与「源文件缺失」两组。

    源文件缺失的文档无法重建（向量是派生数据，但正文来自 MinIO 原件），
    脚本会跳过它们并标记 failed，避免无谓入队后由 Worker 逐个失败。
    """
    store = get_object_store()
    if store is None:
        logger.warning("对象存储不可用，跳过源文件存在性检查")
        return tasks, []
    ok, missing = [], []
    for t in tasks:
        if await store.exists(t.object_key):
            ok.append(t)
        else:
            missing.append(t)
    return ok, missing


def _print_topology(topology: dict) -> None:
    for base, info in topology.items():
        cols = info["collections"]
        if not cols:
            logger.info("  %s: 暂无维度表（配置 dim=%d）", base, info["configured_dim"])
            continue
        for c in cols:
            logger.info(
                "  %s: dim=%s 分区=%s 分片=%s",
                c["name"], c["dim"], c["num_partitions"], c["num_shards"],
            )


async def _print_legacy(client: MilvusClient) -> list[str]:
    """列出旧拓扑遗留的 per-KB collection。"""
    from pymilvus import utility

    client._connect()
    names = utility.list_collections(using=client._alias)
    managed = {client._collection, client._session_collection}
    legacy = sorted(
        n for n in names
        if n.startswith(_LEGACY_PREFIX)
        and not n.startswith(_LEGACY_EXCLUDE_PREFIX)
        and not any(parse_dim(n, b) is not None for b in managed)
    )
    if legacy:
        logger.info("发现 %d 个旧拓扑遗留 collection（可用 --drop-legacy 清理）", len(legacy))
    return legacy


# ------------------------------------------------------------------
# 执行
# ------------------------------------------------------------------


async def _reset_milvus(client: MilvusClient, drop_legacy: bool) -> None:
    """重建 Milvus 拓扑：删受管表（+ 可选旧 kb_* 表）后按当前配置建表。"""
    from pymilvus import utility

    dropped = await client.drop_all_collections()
    logger.info("已删除受管表: %s", ", ".join(dropped) if dropped else "（无）")

    if drop_legacy:
        legacy = await _print_legacy(client)
        for name in legacy:
            utility.drop_collection(name, using=client._alias)
            logger.info("已删除旧拓扑 collection: %s", name)

    await client.ensure_collections()
    logger.info("已按当前配置重建 Milvus 拓扑")


async def _reset_documents(tasks: list[DocTask], missing: list[DocTask]) -> None:
    """清理 chunks 并把目标文档重置为 pending（分批提交，避免长事务）。

    ``chunks`` 表存的是切片元数据（正文 + parent 关系），会由管线重新生成；不清会残留
    与新向量对不上的孤儿行，导致父块扩展取到旧内容。
    """
    doc_ids = [t.doc_id for t in tasks]
    missing_ids = {t.doc_id for t in missing}
    batch = 500

    async with async_session() as session:
        # 清 chunks（按 doc_id 分批删，含源文件缺失的文档——它们的旧切片同样已失效）
        all_ids = doc_ids + list(missing_ids)
        for i in range(0, len(all_ids), batch):
            chunk_ids = all_ids[i:i + batch]
            await session.execute(sql_delete(Chunk).where(Chunk.doc_id.in_(chunk_ids)))
            await session.commit()
        logger.info("已清理 %d 个文档的 chunks 元数据", len(all_ids))

        # 重置可重建文档为 pending
        for i in range(0, len(doc_ids), batch):
            ids = doc_ids[i:i + batch]
            rows = (await session.execute(
                select(Document).where(Document.id.in_(ids))
            )).scalars().all()
            for doc in rows:
                doc.status = "pending"
                doc.error_message = None
                doc.chunk_count = 0
                doc.progress = 0
                doc.progress_message = None
                # 图谱侧同样需要重抽：attempt 自增使在途旧子任务失效
                doc.graph_status = "none"
                doc.graph_attempt = (doc.graph_attempt or 0) + 1
            await session.commit()
        logger.info("已把 %d 个文档重置为 pending", len(doc_ids))

        # 源文件缺失的直接标 failed，附明确原因
        if missing_ids:
            rows = (await session.execute(
                select(Document).where(Document.id.in_(list(missing_ids)))
            )).scalars().all()
            for doc in rows:
                doc.status = "failed"
                doc.error_message = "重建索引失败：源文件在对象存储中已丢失"
                doc.chunk_count = 0
                doc.progress = 0
            await session.commit()
            logger.warning("%d 个文档因源文件丢失被标记 failed", len(missing_ids))


async def _enqueue(tasks: list[DocTask]) -> int:
    """按文件大小分快 / 慢道批量入队 Redis Stream。

    与 API 侧 ``_select_queue`` 同一分道口径：≥ ``PIPELINE_SLOW_LANE_MIN_MB`` 走慢道，
    避免大文件占满快道让小文件排队。
    """
    s = get_settings()
    fast = await TaskQueue.create(s.redis_url)
    if fast is None:
        logger.error("Redis 不可用，无法入队。请先确认 Redis 正常再重跑本脚本。")
        return 0
    slow = await TaskQueue.create(
        s.redis_url,
        stream_key="pipeline:tasks:slow",
        dlq_key="pipeline:dlq:slow",
        group_name="pipeline-workers",
    )
    threshold = s.pipeline_slow_lane_min_mb * 1024 * 1024

    enqueued = 0
    for idx, t in enumerate(tasks, start=1):
        queue = fast
        if slow is not None and t.file_size is not None and t.file_size >= threshold:
            queue = slow
        try:
            await queue.enqueue(TaskMessage(
                doc_id=t.doc_id, kb_id=t.kb_id, file_path=t.object_key,
                tenant_id=t.tenant_id, object_key=t.object_key,
            ))
            enqueued += 1
        except Exception as e:
            logger.warning("文档 %s (%s) 入队失败: %s", t.doc_id, t.filename, e)
        if idx % _ENQUEUE_BATCH == 0:
            logger.info("已入队 %d/%d", idx, len(tasks))
            # 节流：给 Worker 消费与 Redis 一点呼吸空间
            await asyncio.sleep(_ENQUEUE_PAUSE_SECONDS)
    logger.info("入队完成：成功 %d / 目标 %d", enqueued, len(tasks))
    return enqueued


async def _watch(kb_id: str | None, total: int, poll_seconds: int) -> None:
    """轮询进度直到没有 pending/processing 文档。"""
    logger.info("开始监控重建进度（每 %ds 刷新，Ctrl-C 可安全退出，不影响后台重建）", poll_seconds)
    start = time.monotonic()
    last = None
    while True:
        summary = await _status_summary(kb_id)
        inflight = summary.get("pending", 0) + summary.get("processing", 0)
        line = (
            f"completed={summary.get('completed', 0)} "
            f"processing={summary.get('processing', 0)} "
            f"pending={summary.get('pending', 0)} "
            f"failed={summary.get('failed', 0)}"
        )
        if line != last:
            elapsed = int(time.monotonic() - start)
            logger.info("[%4ds] %s（目标 %d）", elapsed, line, total)
            last = line
        if inflight == 0:
            logger.info("重建完成：%s", line)
            if summary.get("failed"):
                logger.warning(
                    "有 %d 个文档失败，可在前端文档列表查看错误原因并单独重试",
                    summary["failed"],
                )
            return
        await asyncio.sleep(poll_seconds)


# ------------------------------------------------------------------
# main
# ------------------------------------------------------------------


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="全量重建向量索引（Milvus 拓扑切换 / 换 embedding 模型后使用）",
    )
    parser.add_argument("--kb-id", default=None, help="只重建指定知识库（灰度验证用）")
    parser.add_argument(
        "--only-unfinished", action="store_true",
        help="只重建未完成（pending/processing/failed）的文档，用于断点续跑",
    )
    parser.add_argument(
        "--reset-milvus", action="store_true",
        help="重建前先删除并重建 Milvus 受管表（破坏性：清空全部向量）",
    )
    parser.add_argument(
        "--drop-legacy", action="store_true",
        help="配合 --reset-milvus，同时清理旧拓扑遗留的 kb_* collection",
    )
    parser.add_argument(
        "--watch", action="store_true", help="入队后轮询打印进度直到全部完成",
    )
    parser.add_argument(
        "--poll-seconds", type=int, default=10, help="--watch 的刷新间隔（默认 10s）",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="只体检并打印计划，不做任何修改",
    )
    parser.add_argument(
        "--yes", action="store_true", help="跳过破坏性动作的交互确认（自动化用）",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
    )

    s = get_settings()
    client = _build_client()

    # ---------- 1. 体检 ----------
    logger.info("=" * 72)
    logger.info("Milvus: %s:%s", s.milvus_host, s.milvus_port)
    logger.info(
        "目标拓扑: %s_%d (kb_id, %d 分区) | %s_%d (session_id, %d 分区) "
        "| num_shards=%s replica=%s",
        s.milvus_collection, s.embed_dim, s.milvus_num_partitions,
        s.milvus_session_collection, s.embed_dim, s.milvus_session_num_partitions,
        s.milvus_shards_num or "default", s.milvus_replica_number or "default",
    )
    logger.info("当前 Milvus 实际拓扑:")
    _print_topology(await client.describe())
    await _print_legacy(client)

    # get_object_store() 内部懒初始化，返回 None 表示不可用（此时跳过源文件检查）
    if get_object_store() is None:
        logger.warning("对象存储不可用，将跳过源文件存在性检查")

    kb_total = await _kb_count()
    summary = await _status_summary(args.kb_id)
    tasks = await _collect_tasks(args.kb_id, args.only_unfinished)
    ok_tasks, missing = await _check_source_files(tasks)

    logger.info("-" * 72)
    logger.info("知识库总数: %d", kb_total)
    logger.info("文档状态分布: %s", summary or "（无文档）")
    logger.info(
        "本次目标文档: %d（源文件就绪 %d，源文件丢失 %d）",
        len(tasks), len(ok_tasks), len(missing),
    )
    if missing:
        for t in missing[:10]:
            logger.warning("  源文件丢失: %s (%s)", t.filename, t.doc_id)
        if len(missing) > 10:
            logger.warning("  ...另有 %d 个源文件丢失的文档", len(missing) - 10)
    logger.info("=" * 72)

    if not tasks:
        logger.info("没有需要重建的文档，退出")
        return 0

    if args.dry_run:
        logger.info("[dry-run] 将执行的操作：")
        if args.reset_milvus:
            logger.info("  1. 删除并重建 Milvus 受管表%s",
                        "（含清理旧 kb_* 表）" if args.drop_legacy else "")
        logger.info("  2. 清理 %d 个文档的 chunks 元数据", len(tasks))
        logger.info("  3. 把 %d 个文档重置为 pending", len(ok_tasks))
        if missing:
            logger.info("  4. 把 %d 个源文件丢失的文档标记 failed", len(missing))
        logger.info("  5. 批量入队 %d 个重建任务（Worker 消费）", len(ok_tasks))
        logger.info("[dry-run] 未做任何修改")
        return 0

    # ---------- 2. 确认 ----------
    if not args.yes:
        logger.warning("即将执行：")
        if args.reset_milvus:
            logger.warning("  - 【破坏性】删除并重建 Milvus 受管表，清空全部向量")
            if args.drop_legacy:
                logger.warning("  - 【破坏性】删除旧拓扑遗留的 kb_* collection")
        logger.warning("  - 清理 %d 个文档的 chunks 元数据并重置状态", len(tasks))
        logger.warning("  - 入队 %d 个重建任务（需要 Worker 在运行）", len(ok_tasks))
        answer = input("确认继续？输入 yes 继续，其它任意输入取消: ").strip()
        if answer != "yes":
            logger.info("已取消，未做任何修改")
            return 1

    # ---------- 3. 执行 ----------
    if args.reset_milvus:
        await _reset_milvus(client, args.drop_legacy)
    else:
        # 不重置也要保证目标维度表存在（换模型场景：新维度表首次出现）
        await client.ensure_collections()
        logger.info("已确保目标维度表存在（未清空既有向量）")

    await _reset_documents(ok_tasks, missing)
    enqueued = await _enqueue(ok_tasks)

    if enqueued == 0:
        logger.error("没有任何任务入队成功，请检查 Redis 与 Worker 状态")
        return 2

    logger.info(
        "已交给 Worker 重建。若 Worker 未运行，任务会留在 Redis Stream 中等待消费。",
    )

    # ---------- 4. 监控 ----------
    if args.watch:
        try:
            await _watch(args.kb_id, enqueued, args.poll_seconds)
        except KeyboardInterrupt:
            logger.info("已退出监控（后台重建继续进行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
