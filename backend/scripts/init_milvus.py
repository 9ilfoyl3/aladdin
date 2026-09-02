"""Milvus 拓扑初始化 / 重置 / 巡检脚本（共享 collection + Partition Key + 按维度分表）。

本项目的向量拓扑（物理表名 = ``<base>_<dim>``）：

- ``settings.milvus_collection``（默认 ``artoo_chunks``）—— 全部正式知识库共用，
  Partition Key = ``kb_id``。
- ``settings.milvus_session_collection``（默认 ``artoo_session_chunks``）—— 会话附件，
  Partition Key = ``session_id``。

三个不可变属性（**建表时固定，Milvus 不支持事后修改**）：Partition Key 字段、
``num_partitions``、``num_shards``。调整这三项必须重建表（``--reset``，会丢失向量、
需重新灌库；重新灌库请用 ``scripts/reindex_all.py``）。
``replica_number`` 是 load 时参数，改配置重启即生效，无需重建。

注意 ``num_partitions`` **不是知识库数量上限**：无上限个 ``kb_id`` 会 hash 进这 N 个
分桶，它只决定一次检索最少扫描的数据比例（≈ 1/N）。

用法::

    # 幂等建表（与服务启动时的 init_milvus_collections 等价）
    python -m scripts.init_milvus

    # 查看当前拓扑（各维度表、分区数、分片数、字段）
    python -m scripts.init_milvus --describe

    # 破坏性重置：删掉本客户端管理的全部维度表后按当前配置重建
    python -m scripts.init_milvus --reset

    # 顺带清理旧拓扑遗留的 per-KB collection（kb_* / kb_session_files）
    python -m scripts.init_milvus --reset --drop-legacy

    # 换过 embedding 模型、确认旧维度数据无用后清理非当前维度的表
    python -m scripts.init_milvus --prune-dims

    # 只看会做什么，不实际执行
    python -m scripts.init_milvus --reset --drop-legacy --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# 确保 backend 目录在 sys.path 中（脚本以 `python -m scripts.init_milvus` 运行）
_backend_dir = Path(__file__).resolve().parent.parent
if str(_backend_dir) not in sys.path:
    sys.path.insert(0, str(_backend_dir))

from pymilvus import utility  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.storage.milvus import MilvusClient, parse_dim  # noqa: E402

logger = logging.getLogger("init_milvus")

# 旧拓扑（每个知识库一个 collection）遗留的物理集合名前缀。
# 事件向量集合 ``kb_event_*`` 仍是 per-KB 拓扑（不在本次改造范围），必须排除在清理之外。
_LEGACY_PREFIX = "kb_"
_LEGACY_EXCLUDE_PREFIX = "kb_event_"


def _build_client() -> MilvusClient:
    """按当前配置构造客户端（与运行时 ``get_milvus_client`` 同参）。"""
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


def _managed(client: MilvusClient, name: str) -> bool:
    """该 collection 是否属于本客户端管理（两套 base 的任一维度表）。"""
    return any(
        parse_dim(name, base) is not None
        for base in (client._collection, client._session_collection)
    )


def _find_legacy_collections(client: MilvusClient) -> list[str]:
    """列出旧拓扑遗留的 per-KB collection（``kb_*``，排除 ``kb_event_*`` 与本客户端管理的表）。"""
    client._connect()
    names = utility.list_collections(using=client._alias)
    return sorted(
        n for n in names
        if n.startswith(_LEGACY_PREFIX)
        and not n.startswith(_LEGACY_EXCLUDE_PREFIX)
        and not _managed(client, n)
    )


def _drop_legacy(client: MilvusClient, names: list[str], dry_run: bool) -> None:
    """删除旧拓扑遗留 collection。"""
    for name in names:
        if dry_run:
            logger.info("[dry-run] 将删除旧拓扑 collection: %s", name)
            continue
        utility.drop_collection(name, using=client._alias)
        logger.info("已删除旧拓扑 collection: %s", name)


def _print_describe(topology: dict) -> None:
    """打印拓扑描述。"""
    for base, info in topology.items():
        logger.info(
            "base=%s | Partition Key=%s | 配置: num_partitions=%d, dim=%d",
            base, info["partition_key"],
            info["configured_num_partitions"], info["configured_dim"],
        )
        if not info["collections"]:
            logger.info("    （当前无任何维度表）")
            continue
        for c in info["collections"]:
            logger.info(
                "    %s | dim=%s 实际分区=%s 分片=%s 字段数=%d",
                c["name"], c["dim"], c["num_partitions"],
                c["num_shards"], len(c["field_names"]),
            )


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Milvus 拓扑初始化 / 重置（共享 collection + Partition Key + 按维度分表）",
    )
    parser.add_argument(
        "--reset", action="store_true",
        help="破坏性重置：删除并重建本客户端管理的全部维度表（清空全部向量，需重新灌库）",
    )
    parser.add_argument(
        "--drop-legacy", action="store_true",
        help="同时清理旧拓扑遗留的 per-KB collection（kb_*，不含 kb_event_*）",
    )
    parser.add_argument(
        "--prune-dims", action="store_true",
        help="删除非当前 EMBED_DIM 的历史维度表（换模型后确认旧数据无用时用）",
    )
    parser.add_argument(
        "--describe", action="store_true",
        help="只打印当前拓扑，不做任何修改",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只打印将要执行的动作，不实际执行",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="跳过破坏性动作的交互确认（供 CI / 自动化使用）",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
    )

    s = get_settings()
    client = _build_client()
    logger.info(
        "目标 Milvus: %s:%s | 主表=%s_%d (kb_id, %d 分区) | 会话表=%s_%d (session_id, %d 分区) "
        "| num_shards=%s replica=%s",
        s.milvus_host, s.milvus_port,
        s.milvus_collection, s.embed_dim, s.milvus_num_partitions,
        s.milvus_session_collection, s.embed_dim, s.milvus_session_num_partitions,
        s.milvus_shards_num or "default", s.milvus_replica_number or "default",
    )

    if args.describe:
        _print_describe(await client.describe())
        return 0

    legacy = _find_legacy_collections(client) if args.drop_legacy else []

    # 破坏性动作前的确认闸门
    destructive = args.reset or args.prune_dims or bool(legacy)
    if destructive and not args.dry_run and not args.yes:
        logger.warning("即将执行破坏性操作：")
        if args.reset:
            logger.warning(
                "  - 删除并重建 %s / %s 的全部维度表（清空全部向量）",
                s.milvus_collection, s.milvus_session_collection,
            )
        if args.prune_dims:
            logger.warning("  - 删除非当前维度（%d）的历史维度表", s.embed_dim)
        if legacy:
            logger.warning(
                "  - 删除 %d 个旧拓扑 collection: %s", len(legacy), ", ".join(legacy),
            )
        answer = input("确认继续？输入 yes 继续，其它任意输入取消: ").strip()
        if answer != "yes":
            logger.info("已取消，未做任何修改")
            return 1

    if args.reset:
        if args.dry_run:
            logger.info(
                "[dry-run] 将删除并重建 %s / %s 的全部维度表",
                s.milvus_collection, s.milvus_session_collection,
            )
        else:
            dropped = await client.drop_all_collections()
            logger.info("已删除: %s", ", ".join(dropped) if dropped else "（无）")

    if legacy:
        _drop_legacy(client, legacy, args.dry_run)
    elif args.drop_legacy:
        logger.info("未发现旧拓扑遗留 collection")

    if args.dry_run:
        logger.info("[dry-run] 将幂等建表（含 dense/sparse/bm25/标量索引）")
        if args.prune_dims:
            logger.info("[dry-run] 将清理非 %d 维的历史维度表", s.embed_dim)
        return 0

    await client.ensure_collections()

    if args.prune_dims:
        pruned = await client.drop_other_dims()
        logger.info("已清理历史维度表: %s", ", ".join(pruned) if pruned else "（无）")

    _print_describe(await client.describe())
    logger.info("完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
