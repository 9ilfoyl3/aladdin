"""知识图谱可视化 API（design.md 5.1）。

提供图谱可视化所需的只读查询接口：

- ``GET /api/kb/{kb_id}/graph``        总览 / ego 邻居子图（mode=overview|ego）
- ``GET /api/kb/{kb_id}/graph/stats``  图谱统计（实体/关系数、类型分布、孤立数、状态）
- ``GET /api/kb/{kb_id}/graph/entity/{entity_id}``  实体详情（属性/别名/邻居/原文）

设计要点：

- **鉴权**（Requirements 8.2）：所有端点经现有 ``authorize_requested_kbs(READ)`` 校验
  调用者对 ``kb_id`` 的读权限；跨租户 / 不可读统一 404（存在性非泄露），与 retrieval
  接口同一范式。所有返回节点的 ``kb_id`` 由 ``GraphStore`` 强制等于请求 ``kb_id``
  （Property 1），API 仅透传 ``kb_id``。
- **降级**（Requirements 7.x / 9.3，Property 8）：``get_graph_store()`` 返回 None（全局
  未启用 / Neo4j 不可用 / 驱动未安装）时返回 ``503 {detail: "知识图谱未启用或不可用"}``，
  明确不可用而非静默空。
- **有界查询**（Requirements 6.1/6.2/9.1，Property 9）：API 边界对 ``depth`` / ``limit``
  按平台配置硬上限做 clamp（``GraphStore`` 内部亦再 clamp，防御纵深）。

本文件当前仅实现「可视化 API」。管理 API（PUT /graph/config、GET /graph/jobs、
POST /graph/rebuild）见 task 5.2，将追加到本文件末尾的「管理 API」标记段。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import require_authenticated
from app.api.errors import NotFoundError, PermissionDeniedError
from app.auth.identity import IdentityContext
from app.auth.kb_authz import KbAccessEnum
from app.auth.kb_scope import authorize_requested_kbs
from app.config import get_settings
from app.pipeline.graph.config import GraphKBConfig, read_graph_config, write_graph_config
from app.retrieval.config import get_platform_config_store
from app.schema.db import Document, GraphExtractJob, KnowledgeBase
from app.storage.database import async_session
from app.storage.graph_store import (
    GraphEdgeDTO,
    GraphEntityDTO,
    GraphNodeDTO,
    GraphStatsDTO,
    GraphStore,
    GraphSubsetDTO,
    GraphSubsetMeta,
    get_graph_store,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/kb", tags=["Graph"])

# 图存储不可用时的对外文案（design.md 5.1 降级，与 Error Handling 表一致）。
_STORE_UNAVAILABLE_DETAIL = "知识图谱未启用或不可用"


# ---------------------------------------------------------------------------
# 鉴权 / store 获取辅助
# ---------------------------------------------------------------------------


async def _authorize_and_boundary(identity: IdentityContext, kb_id: str) -> None:
    """查询前置：内容边界（超管默认不可读正文）+ KB 读授权。

    与 retrieval 接口同一范式：触达图存储前先校验 KB 读权限（跨租户 / 不可读 404）。
    """
    if identity.is_super_admin and not get_settings().content_view_boundary_open:
        raise PermissionDeniedError("超级管理员默认不可查看业务内容正文")
    async with async_session() as session:
        await authorize_requested_kbs(session, identity, [kb_id], KbAccessEnum.READ)


async def _authorize_write(identity: IdentityContext, kb_id: str) -> None:
    """管理写操作前置：内容边界 + KB **写**授权（design.md 5.2 鉴权 KB 写权限）。

    与 :func:`_authorize_and_boundary` 同一范式，但 ``access=WRITE``：跨租户 / 不可读
    统一 404（存在性非泄露），可读但无写权 403（由 ``authorize_requested_kbs`` 透传）。
    用于 PUT /graph/config 与 POST /graph/rebuild。
    """
    if identity.is_super_admin and not get_settings().content_view_boundary_open:
        raise PermissionDeniedError("超级管理员默认不可查看业务内容正文")
    async with async_session() as session:
        await authorize_requested_kbs(session, identity, [kb_id], KbAccessEnum.WRITE)


async def _require_store() -> GraphStore:
    """获取图存储单例；不可用（None）时抛 503（design.md 5.1 降级，明确不可用）。"""
    store = await get_graph_store()
    if store is None:
        raise HTTPException(status_code=503, detail=_STORE_UNAVAILABLE_DETAIL)
    return store


async def _query_or_503(coro):
    """执行图查询协程；运行期异常统一降级为 503（Error Handling 表「读-可视化」）。

    design.md Error Handling 表规定「Neo4j 不可用（运行期，读-可视化）：Graph API 查询
    异常 / store 为 None → 503」。``_require_store`` 已覆盖 store 为 None；本辅助覆盖另一半：
    图查询执行期间 Neo4j 断连/超时等异常**不得**以 500 崩溃外泄，而应返回明确的 503，
    前端据此显示「服务暂不可用」态而非报红崩溃（Property 8：主链路零影响、图谱故障可见可控）。

    ``HTTPException`` 原样透传（如 ego 缺 center 的 400），不被吞成 503。
    """
    try:
        return await coro
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 — 运行期图查询故障统一降级为明确的 503
        logger.warning("[graph-viz] 图查询失败，降级 503: %s", e)
        raise HTTPException(status_code=503, detail=_STORE_UNAVAILABLE_DETAIL) from e


def _parse_types(types: str | None) -> list[str] | None:
    """把逗号分隔的类型过滤串解析为去空白后的非空列表；无有效项返回 None（不过滤）。"""
    if not types:
        return None
    parsed = [t.strip() for t in types.split(",") if t and t.strip()]
    return parsed or None


# ---------------------------------------------------------------------------
# 响应序列化（DTO -> 朴素 dict，贴合现有接口风格）
# ---------------------------------------------------------------------------


def _subset_to_dict(subset: GraphSubsetDTO) -> dict:
    """子图 DTO -> 可视化响应 dict（nodes/edges/meta，对齐 design.md 5.1）。"""
    meta = {
        "mode": subset.meta.mode,
        "total": subset.meta.total,
        "returned": subset.meta.returned,
        "truncated": subset.meta.truncated,
    }
    # center/depth 仅 ego 模式有意义，overview 模式省略（None 不下发）。
    if subset.meta.center is not None:
        meta["center"] = subset.meta.center
    if subset.meta.depth is not None:
        meta["depth"] = subset.meta.depth
    return {
        "nodes": [
            {
                "id": n.id,
                "name": n.name,
                "type": n.type,
                "degree": n.degree,
                "node_type": n.node_type,
            }
            for n in subset.nodes
        ],
        "edges": [
            {"source": e.source, "target": e.target, "type": e.type, "weight": e.weight}
            for e in subset.edges
        ],
        "meta": meta,
    }


async def _augment_subset_with_events(
    store: GraphStore, kb_id: str, subset: GraphSubsetDTO, limit: int
) -> None:
    """把事件作为一类节点并入子图（``include_events=true`` 时，Requirements 4.3）。

    复用实体桥接查询 ``events_by_entities``：取当前子图中**可见实体**所提及的事件，
    追加为 ``node_type='event'`` 的节点，并对每条 ``(:Event)-[:MENTIONS]->(:Entity)``
    生成一条边（仅当实体端点在当前节点集合内，无悬挂边）。事件节点与实体节点用
    ``node_type`` 字段区分，供前端探索。

    就地修改 ``subset.nodes`` / ``subset.edges``；任何异常由调用方 ``_query_or_503`` 兜底。
    事件数据缺失或图未含事件时为 no-op（不影响原实体子图）。
    """
    entity_ids = [n.id for n in subset.nodes if n.node_type == "entity"]
    if not entity_ids or limit <= 0:
        return
    events = await store.events_by_entities(kb_id=kb_id, entity_ids=entity_ids, limit=limit)
    if not events:
        return

    entity_id_set = {n.id for n in subset.nodes if n.node_type == "entity"}
    existing_ids = {n.id for n in subset.nodes}
    for ev in events:
        if ev.id in existing_ids:
            continue
        # 事件节点：name 取标题（缺失回退 summary），type/node_type 标记为 event。
        display_name = ev.title or ev.summary or ev.content[:30] or ev.id
        subset.nodes.append(
            GraphNodeDTO(
                id=ev.id,
                name=display_name,
                type="event",
                degree=len(ev.entity_ids),
                node_type="event",
            )
        )
        existing_ids.add(ev.id)
        # MENTIONS 边：仅连到当前子图内的实体（无悬挂边）。
        for ent_id in ev.entity_ids:
            if ent_id in entity_id_set:
                subset.edges.append(
                    GraphEdgeDTO(source=ev.id, target=ent_id, type="MENTIONS", weight=1)
                )


def _stats_to_dict(stats: GraphStatsDTO) -> dict:
    """统计 DTO -> 响应 dict（对齐 design.md 5.1）。"""
    return {
        "entity_count": stats.entity_count,
        "relation_count": stats.relation_count,
        "types": stats.types,
        "orphan_count": stats.orphan_count,
        "status": stats.status,
    }


def _entity_to_dict(entity: GraphEntityDTO) -> dict:
    """实体详情 DTO -> 响应 dict（对齐 design.md 5.1）。"""
    return {
        "id": entity.id,
        "name": entity.name,
        "type": entity.type,
        "aliases": entity.aliases,
        "attributes": entity.attributes,
        "degree": entity.degree,
        "neighbors": [
            {"id": n.id, "name": n.name, "type": n.type, "rel_type": n.rel_type}
            for n in entity.neighbors
        ],
        "chunks": [
            {
                "chunk_id": c.chunk_id,
                "doc_id": c.doc_id,
                "content_preview": c.content_preview,
            }
            for c in entity.chunks
        ],
    }


# ---------------------------------------------------------------------------
# center 解析（ego 模式）
# ---------------------------------------------------------------------------


async def _resolve_center_id(store: GraphStore, kb_id: str, center: str) -> str | None:
    """把 ego 的 center（entity_id 或 name）解析为实体 id。

    先按 entity_id 直查（``get_entity``）；命中即用。未命中再按名称模糊匹配
    （``find_entities_by_names``），取最佳（degree 最高，已在 store 内排序）一个的 id。
    都解析不到返回 None（调用方据此返回空 ego 子图）。
    """
    # 1) 先当作 entity_id 直查
    entity = await store.get_entity(kb_id=kb_id, entity_id=center)
    if entity is not None:
        return entity.id
    # 2) 回退按名称模糊匹配，取 degree 最高者
    hits = await store.find_entities_by_names(kb_id=kb_id, names=[center], limit=1)
    if hits:
        return hits[0].id
    return None


# ---------------------------------------------------------------------------
# 可视化 API
# ---------------------------------------------------------------------------


@router.get("/{kb_id}/graph")
async def get_graph(
    kb_id: str,
    mode: str = Query(default="overview", description="overview | ego"),
    center: str | None = Query(default=None, description="ego 中心节点（entity_id 或 name），ego 必填"),
    depth: int = Query(default=1, description="ego BFS 跳数（被 clamp 到平台硬上限）"),
    types: str | None = Query(default=None, description="逗号分隔的类型过滤"),
    limit: int = Query(default=0, description="节点数上限（0=用平台默认上限，被 clamp）"),
    include_events: bool = Query(
        default=False,
        description="是否把事件作为一类节点并入返回（node_type 区分，默认 false 不改变现有行为）",
    ),
    identity: IdentityContext = Depends(require_authenticated()),
) -> dict:
    """图谱总览 / ego 邻居子图（design.md 5.1）。

    - ``mode=overview``：返回度数最高的 top-N 节点及其内部边（Requirements 6.1）。
    - ``mode=ego``：以 ``center``（entity_id 或 name）为中心做 BFS 邻居子图，
      ``depth`` / ``limit`` 被 clamp 到平台硬上限（Requirements 6.2 / 9.1）。
    - ``include_events=true``（可选，opt-in）：把当前子图中可见实体所提及的事件作为
      ``node_type='event'`` 节点并入返回，实体节点 ``node_type='entity'``，供前端探索
      事件中心图谱（Requirements 4.3）。默认 false，不改变现有可视化行为。

    所有返回节点的 ``kb_id`` 由 store 强制等于请求 ``kb_id``（Property 1 / Req 8.2）。
    """
    await _authorize_and_boundary(identity, kb_id)
    store = await _require_store()

    type_filter = _parse_types(types)
    caps = await get_platform_config_store().get_effective()

    norm_mode = (mode or "overview").strip().lower()

    if norm_mode == "ego":
        if not center or not center.strip():
            raise HTTPException(status_code=400, detail="ego 模式必须提供 center 参数")
        # 边界 clamp：depth -> [1, graph_ego_max_depth]，limit -> [1, graph_ego_max_nodes]
        eff_depth = _clamp(depth, 1, caps.graph_ego_max_depth)
        eff_limit = _clamp(
            limit if limit > 0 else caps.graph_ego_max_nodes,
            1,
            caps.graph_ego_max_nodes,
        )
        center_id = await _query_or_503(_resolve_center_id(store, kb_id, center.strip()))
        if center_id is None:
            # 中心解析不到：返回空 ego 子图（meta 仍带 center/depth），非报错。
            empty = GraphSubsetDTO(
                nodes=[],
                edges=[],
                meta=GraphSubsetMeta(
                    mode="ego", total=0, returned=0, truncated=False,
                    center=center.strip(), depth=eff_depth,
                ),
            )
            return _subset_to_dict(empty)
        subset = await _query_or_503(
            store.neighbors(
                kb_id=kb_id,
                entity_ids=[center_id],
                hops=eff_depth,
                max_nodes=eff_limit,
                types=type_filter,
            )
        )
        if include_events:
            await _query_or_503(
                _augment_subset_with_events(store, kb_id, subset, eff_limit)
            )
        return _subset_to_dict(subset)

    # 默认 overview（含未知 mode 兜底为 overview）
    eff_limit = _clamp(
        limit if limit > 0 else caps.graph_overview_max_nodes,
        1,
        caps.graph_overview_max_nodes,
    )
    subset = await _query_or_503(
        store.overview(kb_id=kb_id, limit=eff_limit, types=type_filter)
    )
    if include_events:
        await _query_or_503(
            _augment_subset_with_events(store, kb_id, subset, eff_limit)
        )
    return _subset_to_dict(subset)


@router.get("/{kb_id}/graph/stats")
async def get_graph_stats(
    kb_id: str,
    identity: IdentityContext = Depends(require_authenticated()),
) -> dict:
    """图谱统计（design.md 5.1）：实体数 / 关系数 / 类型分布 / 孤立数 / 状态。"""
    await _authorize_and_boundary(identity, kb_id)
    store = await _require_store()
    stats = await _query_or_503(store.stats(kb_id=kb_id))
    return _stats_to_dict(stats)


@router.get("/{kb_id}/graph/entity/{entity_id}")
async def get_graph_entity(
    kb_id: str,
    entity_id: str,
    identity: IdentityContext = Depends(require_authenticated()),
) -> dict:
    """实体详情（design.md 5.1）：属性 / 别名 / 邻居 / 关联原文 chunk 预览。

    实体不存在（或不属于该 kb）时 404（Requirements 6.3）。
    """
    await _authorize_and_boundary(identity, kb_id)
    store = await _require_store()
    entity = await _query_or_503(store.get_entity(kb_id=kb_id, entity_id=entity_id))
    if entity is None:
        raise NotFoundError("实体不存在")
    return _entity_to_dict(entity)


# ---------------------------------------------------------------------------
# clamp 工具（API 边界服务端硬 clamp，Property 9 / Requirements 9.1）
# ---------------------------------------------------------------------------


def _clamp(value: int, lo: int, hi: int) -> int:
    """把 value 夹到闭区间 ``[lo, hi]``；``lo > hi`` 时以 lo 为准。"""
    if hi < lo:
        return lo
    return max(lo, min(int(value), hi))


# === 管理 API（task 5.2）===
# PUT  /api/kb/{kb_id}/graph/config   更新 config.graph（鉴权 KB 写权限）
# GET  /api/kb/{kb_id}/graph/jobs     抽取任务台账（进度 / 失败可观测）
# POST /api/kb/{kb_id}/graph/rebuild  全库重建图谱（清空后按文档重新 seed）
# 由 task 5.2 在此追加，复用上方同一 router 与鉴权辅助。


# 单次 jobs 查询返回的台账行上限（最近优先），避免大库一次拉爆。
_JOBS_DEFAULT_LIMIT = 50
_JOBS_MAX_LIMIT = 200


# ---------------------------------------------------------------------------
# 请求 / 响应模型
# ---------------------------------------------------------------------------


class GraphConfigUpdate(BaseModel):
    """``PUT /graph/config`` 请求体（design.md 3.3 / 5.2）。

    全部字段可选：仅提供的字段被合并进现有 ``config.graph``，未提供的保留原值
    （部分更新）。字段语义与取值约束由 ``read_graph_config`` 逐字段兜底保证，本处
    仅做类型层校验，非法值在合并后由 ``read_graph_config`` 回退安全默认。
    """

    enabled: bool | None = Field(default=None, description="KB 级图谱总开关")
    entity_types: list[str] | None = Field(default=None, description="实体类型白名单")
    relation_types: list[str] | None = Field(default=None, description="关系类型白名单")
    extract_granularity: str | None = Field(default=None, description="抽取粒度 parent|child")
    extract_model_id: str | None = Field(default=None, description="指定抽取 LLM；空表示用 KB 默认")
    enable_alias_dedup: bool | None = Field(default=None, description="是否启用向量别名消歧")
    alias_sim_threshold: float | None = Field(default=None, description="别名合并相似度阈值 [0,1]")


def _graph_config_to_dict(cfg: GraphKBConfig) -> dict:
    """``GraphKBConfig`` -> 响应 dict（即 ``config["graph"]`` 序列化形态）。"""
    return cfg.to_dict()


# ---------------------------------------------------------------------------
# PUT /graph/config —— 更新 KB 图谱配置
# ---------------------------------------------------------------------------


@router.put("/{kb_id}/graph/config")
async def update_graph_config(
    kb_id: str,
    body: GraphConfigUpdate,
    identity: IdentityContext = Depends(require_authenticated()),
) -> dict:
    """更新 KB 级图谱配置 ``config.graph``（design.md 5.2，Requirements 8.2）。

    - **鉴权**：需 KB **写**权限（``_authorize_write``）；跨租户 / 不可读 404，可读无写 403。
    - **合并语义**：读出现有有效配置（``read_graph_config`` 逐字段兜底）→ 用请求体中
      显式提供的字段覆盖 → 写回 ``config["graph"]``（``write_graph_config`` 不原地改）→
      持久化到 ``KnowledgeBase.config``。未提供的字段保留原值（部分更新）。
    - **缓存失效**（可选增强）：配置变更后广播 ``kb_data`` 失效信号，让其他进程清除
      该 KB 的检索/加载缓存（与现有 config 更新失效范式一致）。

    Returns:
        生效后的图谱配置 dict（``config["graph"]`` 形态）。
    """
    await _authorize_write(identity, kb_id)

    async with async_session() as session:
        kb = await session.get(KnowledgeBase, kb_id)
        if kb is None:
            # 鉴权已校验存在性；并发删除兜底为 404（存在性非泄露）。
            raise NotFoundError("知识库不存在")

        # 1) 读出现有有效配置（缺失/非法字段已兜底为安全默认）。
        current = read_graph_config(kb.config)

        # 2) 用请求体显式提供的字段覆盖（仅 set 的字段，未提供保留原值）。
        provided = body.model_dump(exclude_unset=True)
        merged = GraphKBConfig(
            enabled=provided.get("enabled", current.enabled),
            entity_types=provided.get("entity_types", current.entity_types),
            relation_types=provided.get("relation_types", current.relation_types),
            extract_granularity=provided.get("extract_granularity", current.extract_granularity),
            extract_model_id=provided.get("extract_model_id", current.extract_model_id),
            enable_alias_dedup=provided.get("enable_alias_dedup", current.enable_alias_dedup),
            alias_sim_threshold=provided.get("alias_sim_threshold", current.alias_sim_threshold),
        )

        # 3) 写回 config["graph"]（先序列化进 config dict，再经 read 回读保证恒合法）。
        new_config = write_graph_config(kb.config, merged)
        kb.config = new_config
        # JSON 列原地改 key 不一定触发脏标记，显式标记确保持久化。
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(kb, "config")
        await session.commit()

        # 回读最终有效配置（经逐字段兜底，确保返回值恒合法）。
        effective = read_graph_config(new_config)

    # 4) 可选：广播 kb_data 失效，让其他进程清缓存（失败不影响主流程）。
    try:
        from app.storage.invalidation import get_invalidation_bus

        bus = get_invalidation_bus()
        if bus is not None:
            await bus.publish("kb_data", kb_id)
    except Exception as e:  # noqa: BLE001 — 缓存失效广播失败不影响配置更新结果
        logger.warning("[graph-config] kb_id=%s 失效广播失败（非致命）: %s", kb_id, e)

    return _graph_config_to_dict(effective)


# ---------------------------------------------------------------------------
# GET /graph/jobs —— 抽取任务台账
# ---------------------------------------------------------------------------


def _job_to_dict(job: GraphExtractJob) -> dict:
    """``GraphExtractJob`` -> 响应 dict（进度 / 失败可观测，design.md 3.2 / 5.2）。"""
    return {
        "id": job.id,
        "doc_id": job.doc_id,
        "attempt": job.attempt,
        "status": job.status,
        "pending_subtasks": job.pending_subtasks,
        "total_subtasks": job.total_subtasks,
        "entities_count": job.entities_count,
        "relations_count": job.relations_count,
        "events_count": job.events_count,
        "error_message": job.error_message,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }


@router.get("/{kb_id}/graph/jobs")
async def list_graph_jobs(
    kb_id: str,
    limit: int = Query(default=_JOBS_DEFAULT_LIMIT, description="返回行数上限（被 clamp）"),
    identity: IdentityContext = Depends(require_authenticated()),
) -> dict:
    """图谱抽取任务台账（design.md 5.2，Requirements 4.1）。

    返回该 KB 最近的 ``GraphExtractJob`` 行（按 ``created_at`` 倒序），用于观测抽取
    进度（pending/total）与失败（error_message）。需 KB **读**权限。租户隔离由
    ``TenantScopedMixin`` + 仓储兜底自动注入（``GraphExtractJob`` 带 tenant_id）。

    Returns:
        ``{"jobs": [...], "total": N}``，其中 N 为本次返回的行数。
    """
    await _authorize_and_boundary(identity, kb_id)

    eff_limit = _clamp(limit, 1, _JOBS_MAX_LIMIT)
    async with async_session() as session:
        rows = (
            await session.execute(
                select(GraphExtractJob)
                .where(GraphExtractJob.kb_id == kb_id)
                .order_by(GraphExtractJob.created_at.desc())
                .limit(eff_limit)
            )
        ).scalars().all()

    jobs = [_job_to_dict(j) for j in rows]
    return {"jobs": jobs, "total": len(jobs)}


# ---------------------------------------------------------------------------
# POST /graph/rebuild —— 全库重建
# ---------------------------------------------------------------------------


@router.post("/{kb_id}/graph/rebuild")
async def rebuild_graph(
    kb_id: str,
    identity: IdentityContext = Depends(require_authenticated()),
) -> dict:
    """全库重建图谱（design.md 5.2，Requirements 5.2）：清空 KB 图 → 按文档重新 seed。

    - **鉴权**：需 KB **写**权限。
    - **门控**：store 不可用（None）返回 503；KB 未启用图谱（``config.graph.enabled``
      为 False）返回 400（重建无意义，避免误触发重负载）。
    - **流程**：
      1. ``store.delete_by_kb(kb_id)`` 清空该 KB 现有图；
      2. 创建 ``pipeline:graph`` 慢道队列（Redis 不可用返回 503）；
      3. 遍历该 KB 所有 ``completed`` 文档，逐个 ``maybe_trigger_graph_extract`` 重新
         seed 抽取（自增 attempt + 建 job + 入队），复用既有触发链路。

    本操作为潜在重负载，仅在图谱启用时允许；抽取本身异步离线，不阻塞响应。

    Returns:
        ``{"detail": ..., "documents_queued": N}``，N 为触发重建的文档数。
    """
    await _authorize_write(identity, kb_id)
    store = await _require_store()

    # KB 级开关门控：未启用图谱时不重建（避免对未启用 KB 误触发重负载）。
    async with async_session() as session:
        kb = await session.get(KnowledgeBase, kb_id)
        if kb is None:
            raise NotFoundError("知识库不存在")
        tenant_id = kb.tenant_id
        cfg = read_graph_config(kb.config)
    if not cfg.enabled:
        raise HTTPException(status_code=400, detail="该知识库未启用图谱，无法重建")

    # 1) 清空该 KB 现有图（store 已确保非 None）。
    await store.delete_by_kb(kb_id=kb_id)

    # 2) 创建慢道队列；Redis 不可用 → 503（无法 seed 抽取）。
    from app.pipeline.graph.trigger import create_graph_queue, maybe_trigger_graph_extract

    graph_queue = await create_graph_queue(get_settings().redis_url)
    if graph_queue is None:
        raise HTTPException(status_code=503, detail=_STORE_UNAVAILABLE_DETAIL)

    # 3) 遍历 completed 文档，逐个重新触发抽取（复用既有触发链路）。
    async with async_session() as session:
        doc_ids = (
            await session.execute(
                select(Document.id).where(
                    Document.kb_id == kb_id,
                    Document.status == "completed",
                )
            )
        ).scalars().all()

    queued = 0
    for doc_id in doc_ids:
        try:
            await maybe_trigger_graph_extract(
                kb_id=kb_id,
                doc_id=doc_id,
                tenant_id=tenant_id,
                db_session_factory=async_session,
                graph_queue=graph_queue,
            )
            queued += 1
        except Exception as e:  # noqa: BLE001 — 单文档触发失败不阻断其余文档重建
            logger.warning(
                "[graph-rebuild] kb_id=%s doc_id=%s 触发抽取失败: %s", kb_id, doc_id, e
            )

    logger.info("[graph-rebuild] kb_id=%s 已触发 %d 个文档重建图谱", kb_id, queued)
    return {"detail": "图谱重建已触发", "documents_queued": queued}
