"""知识内容全量清除脚本（切换 Milvus 拓扑时的「不向前兼容」路径）。

为什么需要它
------------
Milvus 的 Partition Key 字段、``num_partitions``、``num_shards`` 与向量维度都是
**建表时固定**的，无法事后修改。从旧拓扑（每个知识库一个 collection）切到新拓扑
（共享 collection + Partition Key + 按维度分表）必须重建物理表。

如果不打算保留历史数据（例如源文件已不在对象存储、无法重新解析），最干净的做法就是
把「知识内容」整体清空、让系统以新拓扑从零开始，而不是维护一套复杂的兼容/回填逻辑。

清除范围（默认 ``--scope content``）
------------------------------------
**会被清空**：

- Milvus：受管的全部维度表 + 旧拓扑遗留 ``kb_*`` + 事件向量 ``kb_event_*``，之后按当前
  配置重建受管表。
- PostgreSQL：``chunks`` / ``documents`` / ``folders`` / ``session_chunks`` /
  ``session_files`` / ``graph_extract_jobs`` / ``graph_communities``；并把
  ``knowledge_bases.doc_count`` 归零。
- 对象存储（MinIO）：bucket 内全部对象（源文件、缩略图、会话附件）。
- Neo4j 知识图谱：全部实体 / 事件 / 关系（仅在 ``GRAPH_ENABLE=true`` 且可连通时）。
- Redis：文档入库快 / 慢道 Stream 与 DLQ、会话上传 Stream 与 DLQ、图谱抽取队列、
  检索结果缓存。清掉在途任务，避免它们携带已删除的 doc_id 反复失败。

**默认保留**（系统保持可用，用户登录后可直接重新上传）：

- 租户 / 用户 / 外部用户 / API Key
- 知识库本体及其授权与分享链接（库名、权限、共享关系都在）
- 模型配置（LLM / Embedding / Rerank / OCR / ASR / MCP）、检索参数、平台配置、
  智能体预设、自定义技能、审计日志、邀请码

可选扩大范围：

- ``--include-kbs``：连知识库本体 + 授权 + 分享链接一起删（用户需重建知识库并重新共享）。
- ``--include-chats``：连对话会话与消息记录一起删。

用法::

    # 1) 先体检：打印将被清除的数量，不做任何修改（强烈建议先跑）
    python -m scripts.purge_data --dry-run

    # 2) 执行清除（交互确认；需输入 PURGE 二次确认）
    python -m scripts.purge_data

    # 3) 自动化执行（跳过交互确认）
    python -m scripts.purge_data --yes

    # 4) 连知识库与对话一起清空（等于内容层全新开始）
    python -m scripts.purge_data --include-kbs --include-chats --yes

    # 5) 只清 Milvus 与 Redis，保留 PG 与对象存储（谨慎：会造成 DB 有文档但无向量）
    python -m scripts.purge_data --scope vectors

执行前请停掉 backend / worker 进程，避免清除过程中有新数据写入。
``deploy/reset-knowledge-data.sh`` 已经把「停服务 → 清除 → 起服务」串好了。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# 确保 backend 目录在 sys.path 中（脚本以 `python -m scripts.purge_data` 运行）
_backend_dir = Path(__file__).resolve().parent.parent
if str(_backend_dir) not in sys.path:
    sys.path.insert(0, str(_backend_dir))

from sqlalchemy import delete as sql_delete, func, select, update as sql_update  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.schema.db import (  # noqa: E402
    ChatMessageRecord,
    ChatSession,
    Chunk,
    Document,
    Folder,
    GraphCommunity,
    GraphExtractJob,
    KnowledgeBase,
    KnowledgeBaseGrant,
    KbShareLink,
    SessionChunk,
    SessionFile,
)
from app.storage.database import async_session  # noqa: E402
from app.storage.milvus import MilvusClient, parse_dim  # noqa: E402

logger = logging.getLogger("purge_data")

# 旧拓扑遗留的 per-KB collection 前缀。``kb_event_*`` 是事件向量（同样 per-KB 拓扑），
# 本脚本会一并清除——它与 chunk 向量同源，留着就是孤儿。
_LEGACY_PREFIX = "kb_"

# 需要清空的 Redis key（Stream / DLQ）。在途任务携带已删除的 doc_id，留着只会反复失败。
_REDIS_STREAM_KEYS = [
    "pipeline:tasks",
    "pipeline:tasks:slow",
    "pipeline:dlq",
    "pipeline:dlq:slow",
    "pipeline:graph",
    "pipeline:graph:dlq",
    "session_upload:tasks",
    "session_upload:dlq",
]
# 检索结果缓存前缀（见 app/retrieval/cache.py 的 KEY_PREFIX）
_REDIS_CACHE_PATTERN = "rag:cache:*"

SCOPE_CONTENT = "content"
SCOPE_VECTORS = "vectors"


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


async def _pg_counts() -> dict[str, int]:
    """统计各业务表行数。"""
    models = {
        "knowledge_bases": KnowledgeBase,
        "folders": Folder,
        "documents": Document,
        "chunks": Chunk,
        "session_files": SessionFile,
        "session_chunks": SessionChunk,
        "graph_extract_jobs": GraphExtractJob,
        "graph_communities": GraphCommunity,
        "knowledge_base_grants": KnowledgeBaseGrant,
        "kb_share_links": KbShareLink,
        "chat_sessions": ChatSession,
        "chat_messages": ChatMessageRecord,
    }
    out: dict[str, int] = {}
    async with async_session() as session:
        for name, model in models.items():
            try:
                out[name] = await session.scalar(select(func.count()).select_from(model)) or 0
            except Exception as e:  # 表不存在等情况不应中断体检
                logger.warning("统计表 %s 失败: %s", name, e)
                out[name] = -1
    return out


def _milvus_inventory(client: MilvusClient) -> tuple[list[str], list[str]]:
    """返回 ``(受管表, 旧拓扑遗留表)``。"""
    from pymilvus import utility

    client._connect()
    names = utility.list_collections(using=client._alias)
    bases = (client._collection, client._session_collection)
    managed = sorted(n for n in names if any(parse_dim(n, b) is not None for b in bases))
    legacy = sorted(
        n for n in names
        if n.startswith(_LEGACY_PREFIX) and n not in managed
    )
    return managed, legacy


async def _minio_object_count() -> int:
    """统计对象存储中的对象数（不可用时返回 -1）。"""
    from app.storage.object_store import get_object_store

    store = get_object_store()
    if store is None:
        return -1
    try:
        return await asyncio.to_thread(
            lambda: sum(1 for _ in store._client.list_objects(store._bucket, recursive=True))
        )
    except Exception as e:
        logger.warning("统计对象存储失败: %s", e)
        return -1


# ------------------------------------------------------------------
# 执行
# ------------------------------------------------------------------


async def _purge_milvus(client: MilvusClient, managed: list[str], legacy: list[str]) -> None:
    """删除受管表 + 旧拓扑表，再按当前配置重建受管表。"""
    from pymilvus import utility

    dropped = await client.drop_all_collections()
    logger.info("Milvus 受管表已删除: %s", ", ".join(dropped) if dropped else "（无）")

    for name in legacy:
        try:
            utility.drop_collection(name, using=client._alias)
            logger.info("Milvus 旧拓扑表已删除: %s", name)
        except Exception as e:
            logger.warning("删除 %s 失败: %s", name, e)

    await client.ensure_collections()
    logger.info("Milvus 已按当前配置重建受管表")


async def _purge_neo4j() -> None:
    """清空知识图谱（逐 KB 调 delete_by_kb；未启用 / 不可用则跳过）。"""
    settings = get_settings()
    if not settings.graph_enable:
        logger.info("GRAPH_ENABLE 未开启，跳过知识图谱清理")
        return
    try:
        from app.storage.graph_store import get_graph_store

        store = await get_graph_store()
    except Exception as e:
        logger.warning("图存储不可用，跳过知识图谱清理: %s", e)
        return
    if store is None:
        logger.info("图存储不可用（未启用或 Neo4j 不通），跳过知识图谱清理")
        return

    async with async_session() as session:
        kb_ids = list((await session.execute(select(KnowledgeBase.id))).scalars().all())
    for kb_id in kb_ids:
        try:
            await store.delete_by_kb(kb_id=kb_id)
        except Exception as e:
            logger.warning("清理知识库 %s 的图谱失败: %s", kb_id, e)
    logger.info("知识图谱已清理（覆盖 %d 个知识库）", len(kb_ids))


async def _purge_minio() -> None:
    """清空对象存储 bucket 内全部对象。"""
    from app.storage.object_store import get_object_store

    store = get_object_store()
    if store is None:
        logger.warning("对象存储不可用，跳过原件清理")
        return

    def _remove_all() -> int:
        from minio.deleteobjects import DeleteObject

        client, bucket = store._client, store._bucket
        removed = 0
        batch: list[DeleteObject] = []
        for obj in client.list_objects(bucket, recursive=True):
            batch.append(DeleteObject(obj.object_name))
            if len(batch) >= 1000:
                # 必须消费返回的迭代器，否则 minio SDK 不会真正发起删除
                for err in client.remove_objects(bucket, batch):
                    logger.warning("删除对象失败: %s", err)
                removed += len(batch)
                batch = []
        if batch:
            for err in client.remove_objects(bucket, batch):
                logger.warning("删除对象失败: %s", err)
            removed += len(batch)
        return removed

    removed = await asyncio.to_thread(_remove_all)
    logger.info("对象存储已清空，删除 %d 个对象", removed)


async def _purge_redis() -> None:
    """删除在途任务 Stream / DLQ 与检索缓存。"""
    settings = get_settings()
    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(settings.redis_url, decode_responses=True)
        await client.ping()
    except Exception as e:
        logger.warning("Redis 不可用，跳过队列/缓存清理: %s", e)
        return

    try:
        deleted = 0
        for key in _REDIS_STREAM_KEYS:
            deleted += await client.delete(key)
        logger.info("Redis 队列已清理，删除 %d 个 key", deleted)

        cache_deleted = 0
        async for key in client.scan_iter(match=_REDIS_CACHE_PATTERN, count=500):
            cache_deleted += await client.delete(key)
        logger.info("检索缓存已清理，删除 %d 个 key", cache_deleted)
    finally:
        await client.aclose()


async def _purge_postgres(include_kbs: bool, include_chats: bool) -> None:
    """按外键依赖顺序删除业务表数据。"""
    # 顺序关键：先删子表再删父表，避免外键冲突。
    plan: list[tuple[str, object]] = [
        ("chunks", Chunk),
        ("session_chunks", SessionChunk),
        ("session_files", SessionFile),
        ("graph_extract_jobs", GraphExtractJob),
        ("graph_communities", GraphCommunity),
        ("documents", Document),
        ("folders", Folder),
    ]
    if include_chats:
        plan = [("chat_messages", ChatMessageRecord), ("chat_sessions", ChatSession)] + plan
    if include_kbs:
        plan += [
            ("kb_share_links", KbShareLink),
            ("knowledge_base_grants", KnowledgeBaseGrant),
            ("knowledge_bases", KnowledgeBase),
        ]

    async with async_session() as session:
        for name, model in plan:
            try:
                result = await session.execute(sql_delete(model))
                await session.commit()
                logger.info("已清空表 %s（%s 行）", name, result.rowcount)
            except Exception as e:
                await session.rollback()
                logger.error("清空表 %s 失败: %s", name, e)
                raise

        # 保留知识库时把文档计数归零，避免前端显示残留数字
        if not include_kbs:
            await session.execute(sql_update(KnowledgeBase).values(doc_count=0))
            await session.commit()
            logger.info("已把 knowledge_bases.doc_count 归零")


# ------------------------------------------------------------------
# main
# ------------------------------------------------------------------


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="知识内容全量清除（切换 Milvus 拓扑的不向前兼容路径）",
    )
    parser.add_argument(
        "--scope", choices=[SCOPE_CONTENT, SCOPE_VECTORS], default=SCOPE_CONTENT,
        help="content（默认）= 清 Milvus + PG 内容 + 对象存储 + 图谱 + Redis；"
             "vectors = 只清 Milvus 与 Redis（谨慎：会造成 DB 有文档但无向量）",
    )
    parser.add_argument(
        "--include-kbs", action="store_true",
        help="连知识库本体 + 授权 + 分享链接一起删（默认保留，用户可直接重新上传）",
    )
    parser.add_argument(
        "--include-chats", action="store_true",
        help="连对话会话与消息记录一起删（默认保留）",
    )
    parser.add_argument(
        "--keep-objects", action="store_true",
        help="保留对象存储中的源文件（默认清空；保留则可后续用 reindex_all.py 重建索引）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只体检并打印计划，不做任何修改")
    parser.add_argument("--yes", action="store_true", help="跳过交互确认（自动化用）")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
    )

    s = get_settings()
    client = _build_client()

    # ---------- 体检 ----------
    logger.info("=" * 72)
    logger.info("Milvus: %s:%s | PG: %s", s.milvus_host, s.milvus_port,
                s.database_url.rsplit("@", 1)[-1])
    logger.info(
        "目标新拓扑: %s_%d (kb_id, %d 分区) | %s_%d (session_id, %d 分区)",
        s.milvus_collection, s.embed_dim, s.milvus_num_partitions,
        s.milvus_session_collection, s.embed_dim, s.milvus_session_num_partitions,
    )
    managed, legacy = _milvus_inventory(client)
    logger.info("Milvus 受管表: %s", ", ".join(managed) if managed else "（无）")
    logger.info("Milvus 旧拓扑表: %d 个%s", len(legacy),
                f"（如 {legacy[0]} ...）" if legacy else "")

    counts = await _pg_counts()
    obj_count = -1 if args.scope == SCOPE_VECTORS else await _minio_object_count()

    logger.info("-" * 72)
    logger.info("PostgreSQL 现有数据：")
    for name, n in counts.items():
        logger.info("    %-24s %s", name, "统计失败" if n < 0 else n)
    if obj_count >= 0:
        logger.info("对象存储对象数: %d", obj_count)
    logger.info("=" * 72)

    # ---------- 计划 ----------
    will_purge_pg = args.scope == SCOPE_CONTENT
    will_purge_objects = args.scope == SCOPE_CONTENT and not args.keep_objects

    logger.info("将执行的清除动作：")
    logger.info("  1. Milvus：删除受管表 + %d 个旧拓扑表，并按新配置重建受管表", len(legacy))
    logger.info("  2. Redis：清空入库/图谱/会话上传队列与 DLQ + 检索结果缓存")
    if will_purge_pg:
        tables = ["chunks", "session_chunks", "session_files",
                  "graph_extract_jobs", "graph_communities", "documents", "folders"]
        if args.include_chats:
            tables = ["chat_messages", "chat_sessions"] + tables
        if args.include_kbs:
            tables += ["kb_share_links", "knowledge_base_grants", "knowledge_bases"]
        logger.info("  3. PostgreSQL：清空 %s", ", ".join(tables))
        logger.info("  4. Neo4j：清空全部实体 / 事件 / 关系（未启用则跳过）")
    if will_purge_objects:
        logger.info("  5. 对象存储：删除 bucket 内全部对象")
    logger.info("保留：租户 / 用户 / API Key / 模型与检索配置 / 智能体预设 / 审计日志%s%s",
                "" if args.include_kbs else " / 知识库及其授权",
                "" if args.include_chats else " / 对话记录")

    if args.dry_run:
        logger.info("[dry-run] 未做任何修改")
        return 0

    # ---------- 确认 ----------
    if not args.yes:
        logger.warning("!" * 72)
        logger.warning("这是不可逆的破坏性操作，且无法从 Milvus 恢复。")
        logger.warning("请确认已停止 backend / worker 进程，且已按需备份 PostgreSQL 与对象存储。")
        logger.warning("!" * 72)
        answer = input('确认清除？请输入大写 PURGE 继续，其它任意输入取消: ').strip()
        if answer != "PURGE":
            logger.info("已取消，未做任何修改")
            return 1

    # ---------- 执行 ----------
    # 顺序：先断在途任务（Redis），再清存储，最后清 PG 元数据。
    # 这样即使中途失败，也不会出现「PG 已删但 worker 还在按旧 doc_id 写向量」的竞态。
    await _purge_redis()
    await _purge_milvus(client, managed, legacy)
    if will_purge_pg:
        await _purge_neo4j()
    if will_purge_objects:
        await _purge_minio()
    if will_purge_pg:
        await _purge_postgres(args.include_kbs, args.include_chats)

    # ---------- 复核 ----------
    logger.info("-" * 72)
    logger.info("清除后状态：")
    _, legacy_after = _milvus_inventory(client)
    managed_after, _ = _milvus_inventory(client)
    logger.info("Milvus 受管表: %s", ", ".join(managed_after) if managed_after else "（无）")
    logger.info("Milvus 旧拓扑表残留: %d", len(legacy_after))
    for name, n in (await _pg_counts()).items():
        logger.info("    %-24s %s", name, "统计失败" if n < 0 else n)
    logger.info("=" * 72)
    logger.info("清除完成。启动 backend / worker 后即可以新拓扑重新上传文档。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
