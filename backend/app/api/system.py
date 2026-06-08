"""系统配置与健康检查接口"""

import asyncio
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db_session, require_platform, require_tenant_admin
from app.auth.audit import add_audit
from app.auth.constants import AuditActionEnum
from app.auth.identity import IdentityContext
from app.config import get_settings
from app.pipeline.queue import QueueStats
from app.retrieval.config import (
    PLATFORM_RETRIEVAL_KEY,
    RETRIEVAL_FIELD_SPECS,
    RetrievalConfig,
    get_platform_config_store,
    get_retrieval_config_store,
    validate_patch,
    validate_platform_patch,
)
from app.session_upload.memory import recommend_kb_chunk_cap
from app.storage.invalidation import get_invalidation_bus
from app.storage.milvus import MilvusClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/system", tags=["System"])


# ============================================================
# 响应模型
# ============================================================


class HealthResponse(BaseModel):
    """健康检查响应"""
    status: str = "ok"
    version: str = "0.1.0"
    services: dict = Field(default_factory=dict)


class ConfigChange(BaseModel):
    """单字段变更明细（审计 detail 与响应回显共用）。

    仅承载字段名与新旧值这类元数据，不含业务内容正文（对齐审计边界）。
    """

    field: str
    old: object | None = None
    new: object | None = None


class RetrievalConfigSection(BaseModel):
    """检索参数分区（嵌套进 SystemConfigResponse，按五档组织）。

    字段与 ``RetrievalConfig`` 同名同型，可直接 ``RetrievalConfigSection(**config.model_dump())``
    从有效配置构造。返回当前检索参数的有效值（Req 6.1/6.3/6.4）。
    """

    # 分块档 Chunk_Tier（本期纳入租户级配置）
    parent_chunk_size: int
    child_chunk_size: int
    chunk_overlap: int
    # 召回档 Recall_Tier
    recall_k: int
    rerank_candidate_k: int
    # 融合档 Fusion_Tier
    rrf_k: int
    composite_rerank_weight: float
    composite_base_weight: float
    composite_source_weight: float
    # 精排档 Rerank_Tier
    rerank_threshold: float
    rerank_top_k: int
    threshold_degradation_enabled: bool
    # 去重档 Dedup_Tier
    mmr_lambda: float
    mmr_threshold: float
    # 索引档 Index_Tier
    hnsw_ef: int
    hnsw_ef_construction: int
    hnsw_m: int
    # 上传限制档（租户级）
    upload_max_file_mb: int

    @classmethod
    def from_config(cls, config: RetrievalConfig) -> "RetrievalConfigSection":
        """从有效 ``RetrievalConfig`` 构造分区（字段同名同型，直接透传）。"""
        return cls(**config.model_dump())


class RetrievalConfigUpdate(BaseModel):
    """检索参数更新请求（五档，全部 Optional，仅更新传入字段）。

    各字段默认 None，未提交字段不参与更新（Req 6.2）。范围校验由
    ``app.retrieval.config.validate_patch`` 在写库前统一执行。
    """

    # 分块档 Chunk_Tier（本期纳入租户级配置）
    parent_chunk_size: int | None = None
    child_chunk_size: int | None = None
    chunk_overlap: int | None = None
    # 召回档 Recall_Tier
    recall_k: int | None = None
    rerank_candidate_k: int | None = None
    # 融合档 Fusion_Tier
    rrf_k: int | None = None
    composite_rerank_weight: float | None = None
    composite_base_weight: float | None = None
    composite_source_weight: float | None = None
    # 精排档 Rerank_Tier
    rerank_threshold: float | None = None
    rerank_top_k: int | None = None
    threshold_degradation_enabled: bool | None = None
    # 去重档 Dedup_Tier
    mmr_lambda: float | None = None
    mmr_threshold: float | None = None
    # 索引档 Index_Tier
    hnsw_ef: int | None = None
    hnsw_ef_construction: int | None = None
    hnsw_m: int | None = None
    # 上传限制档（租户级）
    upload_max_file_mb: int | None = None


class SystemConfigResponse(BaseModel):
    """系统配置响应"""
    llm_provider: str
    llm_base_url: str
    llm_model: str
    llm_api_key: str
    embed_model: str
    embed_base_url: str
    rerank_model: str
    rerank_base_url: str
    parent_chunk_size: int
    child_chunk_size: int
    chunk_overlap: int
    milvus_host: str
    milvus_port: int
    # OCR 配置
    ocr_enabled: bool
    ocr_provider: str
    ocr_fallback_provider: str
    ocr_external_api_url: str
    ocr_external_api_key: str  # 脱敏显示
    ocr_external_api_timeout: float
    # 检索参数（五档：召回/融合/精排/去重/索引），与分块参数并列（Req 6.1/6.3/6.4）
    retrieval: RetrievalConfigSection
    # 本次实际变更明细（仅 PUT 填充；GET/reset 无变更时为空列表，供前端确认/提示用）
    changes: list[ConfigChange] = Field(default_factory=list)


class SystemConfigUpdate(BaseModel):
    """系统配置更新请求（仅允许更新部分字段）"""
    llm_provider: str | None = None
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = None
    # OCR 配置
    ocr_enabled: bool | None = None
    ocr_provider: str | None = None
    ocr_fallback_provider: str | None = None
    ocr_external_api_url: str | None = None
    ocr_external_api_key: str | None = None
    ocr_external_api_timeout: float | None = None
    # 检索参数（嵌套，各字段 Optional；None 表示不更新检索参数）（Req 6.2）
    retrieval: RetrievalConfigUpdate | None = None


# ============================================================
# 健康检查辅助函数
# ============================================================


async def _check_milvus(settings) -> str:
    """检测 Milvus 连接状态"""
    try:
        client = MilvusClient(host=settings.milvus_host, port=settings.milvus_port)
        # 尝试连接并列出 collections
        await asyncio.to_thread(client._connect)
        return "ok"
    except Exception as e:
        logger.warning("Milvus 健康检查失败: %s", e)
        return f"unavailable: {e}"


async def _check_llm(settings) -> str:
    """检测 LLM 服务连接状态"""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            if settings.llm_provider == "ollama":
                resp = await client.get(f"{settings.llm_base_url}/api/tags")
            else:
                # vLLM / OpenAI 兼容接口
                resp = await client.get(f"{settings.llm_base_url}/v1/models")
            if resp.status_code == 200:
                return "ok"
            return f"unhealthy (status={resp.status_code})"
    except Exception as e:
        logger.warning("LLM 健康检查失败: %s", e)
        return f"unavailable: {e}"


# ============================================================
# 接口实现
# ============================================================


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """健康检查 - 实际检测各依赖服务连接状态"""
    settings = get_settings()

    # 并行检测各服务
    milvus_status, llm_status = await asyncio.gather(
        _check_milvus(settings),
        _check_llm(settings),
    )

    services = {
        "database": "ok",
        "milvus": milvus_status,
        "llm": llm_status,
    }

    # 整体状态：任一服务不可用则标记为 degraded
    overall = "ok"
    if any(v != "ok" for v in services.values()):
        overall = "degraded"

    return HealthResponse(status=overall, services=services)


def _mask_ocr_api_key(key: str) -> str:
    """OCR API Key 脱敏：显示 **** + 最后4位"""
    if not key:
        return ""
    if len(key) <= 4:
        return "****"
    return "****" + key[-4:]


def _resolve_target_tenant(identity: IdentityContext, request: Request) -> str:
    """解析分块/检索配置读写的目标键。

    capability-config-to-platform：检索/分块参数已上收为平台底座，全平台一份
    （仅超级管理员维护）。不再按租户分行，故统一返回平台 sentinel 键
    ``PLATFORM_RETRIEVAL_KEY``，不再读取 ``X-Tenant-ID`` 或 ``identity.tenant_id``。
    保留函数签名以最小化端点改动。
    """
    return PLATFORM_RETRIEVAL_KEY


def _diff_changes(before: dict, after_patch: dict) -> list[dict]:
    """计算逐字段变更明细：仅返回 after_patch 中与 before 不同的字段。

    返回 ``[{"field": k, "old": before.get(k), "new": v}, ...]``（按字段名排序，稳定输出）。
    仅比较 ``after_patch`` 涉及的字段；与现状相同的字段不计入（无变更不留痕）。
    """
    changes = []
    for k in sorted(after_patch.keys()):
        old = before.get(k)
        new = after_patch[k]
        if old != new:
            changes.append({"field": k, "old": old, "new": new})
    return changes


def _build_config_response(
    settings,
    retrieval_section: RetrievalConfigSection,
    changes: list[dict] | None = None,
) -> SystemConfigResponse:
    """构造系统配置响应（GET/PUT/reset 复用）。

    LLM/OCR/Milvus 字段来自当前内存 ``Settings``（保留现有脱敏逻辑：llm_api_key、
    ocr_external_api_key）。检索参数分区由调用方从 ``RetrievalConfigStore`` 读取后传入。

    分块档（parent_chunk_size/child_chunk_size/chunk_overlap）本期迁入租户级
    ``RetrievalConfig``，不再来自 ``Settings``；顶层仍平铺这三个字段（值取自该租户
    ``retrieval`` 有效值）以兼容现有前端字段路径，前端统一后再迁入 retrieval 分档。
    """
    # LLM API Key 脱敏：只显示前8位 + ***
    api_key_display = ""
    if settings.llm_api_key:
        api_key_display = (
            settings.llm_api_key[:8] + "***" if len(settings.llm_api_key) > 8 else "***"
        )
    return SystemConfigResponse(
        llm_provider=settings.llm_provider,
        llm_base_url=settings.llm_base_url,
        llm_model=settings.llm_model,
        llm_api_key=api_key_display,
        embed_model=settings.embed_model,
        embed_base_url=settings.embed_base_url,
        rerank_model=settings.rerank_model,
        rerank_base_url=settings.rerank_base_url,
        # 分块档顶层兼容平铺：取自该租户 retrieval 有效值（不再来自 settings）
        parent_chunk_size=retrieval_section.parent_chunk_size,
        child_chunk_size=retrieval_section.child_chunk_size,
        chunk_overlap=retrieval_section.chunk_overlap,
        milvus_host=settings.milvus_host,
        milvus_port=settings.milvus_port,
        ocr_enabled=settings.ocr_enabled,
        ocr_provider=settings.ocr_provider,
        ocr_fallback_provider=settings.ocr_fallback_provider,
        ocr_external_api_url=settings.ocr_external_api_url,
        ocr_external_api_key=_mask_ocr_api_key(settings.ocr_external_api_key),
        ocr_external_api_timeout=settings.ocr_external_api_timeout,
        retrieval=retrieval_section,
        changes=[ConfigChange(**c) for c in (changes or [])],
    )


@router.get("/config", response_model=SystemConfigResponse)
async def get_config(
    request: Request,
    _identity: IdentityContext = Depends(require_platform()),
):
    """获取系统配置（密钥脱敏；能力配置属平台底座，仅超级管理员，禁 api_key 通道）。

    分块与检索参数为平台级配置（全平台一份，capability-config-to-platform），
    不再按租户区分。
    """
    settings = get_settings()
    tenant_id = _resolve_target_tenant(_identity, request)
    # 分块/检索参数走 RetrievalConfigStore（DB，按租户），与 OCR/LLM 同响应返回（Req 6.1/6.4）
    store = get_retrieval_config_store()
    eff = await store.get_effective(tenant_id)
    return _build_config_response(settings, RetrievalConfigSection.from_config(eff))


@router.put("/config", response_model=SystemConfigResponse)
async def update_config(
    body: SystemConfigUpdate,
    request: Request,
    _identity: IdentityContext = Depends(require_platform()),
    db: AsyncSession = Depends(get_db_session),
):
    """更新系统配置

    注意：LLM/OCR 为运行时修改，重启后会恢复 .env 中的值；如需持久化请改 .env。
    分块与检索参数走 RetrievalConfigStore（DB，全平台一份），即时热生效且持久化（Req 5.2）。
    能力配置属平台底座，仅超级管理员可改（capability-config-to-platform）。

    检索/分块字段若产生实际变更，写一条 ``system.config_update`` 审计（detail 仅含
    字段级 changes 元数据）；提交值与现状全相同则视为幂等，不写库也不留痕。
    """
    settings = get_settings()
    tenant_id = _resolve_target_tenant(_identity, request)

    # 1) LLM/OCR：沿用现有 object.__setattr__ 内存 Settings 逻辑（不动分块/检索参数）。
    #    分块参数本期已迁入 retrieval 分档，不再出现在 SystemConfigUpdate 顶层。
    #    LLM/OCR 为进程内存配置、非持久化，本期不强制审计，维持原行为。
    update_data = body.model_dump(exclude_unset=True)
    update_data.pop("retrieval", None)  # 检索/分块参数单独走 store，不写入 Settings
    for field, value in update_data.items():
        if hasattr(settings, field):
            # 如果 llm_api_key 传入的是脱敏值（含***），跳过不更新
            if field == "llm_api_key" and "***" in (value or ""):
                continue
            # 如果 ocr_external_api_key 传入的是脱敏值（含****），跳过不更新
            if field == "ocr_external_api_key" and "****" in (value or ""):
                continue
            object.__setattr__(settings, field, value)

    # 2) 分块/检索参数：按租户经 store 读写（含分块档），与 LLM/OCR 并存互不影响（Req 6.1/6.4）
    store = get_retrieval_config_store()
    changes: list[dict] = []
    if body.retrieval is not None:
        # 更新前有效值作为 before（用于字段级 diff 与审计 detail）
        eff_before = await store.get_effective(tenant_id)
        before = eff_before.model_dump()
        # 仅取用户实际提交的字段（未提交字段默认 None，不参与更新）
        patch = body.retrieval.model_dump(exclude_unset=True, exclude_none=True)
        # 范围校验：越界 → 422 且不写库（Req 3.2/3.3/3.4）
        errors = validate_patch(patch)
        if errors:
            raise HTTPException(
                status_code=422,
                detail=[e.to_dict() for e in errors],
            )
        # 仅 patch 涉及字段中与现状不同的才算变更
        changes = _diff_changes(before, patch)
        if changes:
            # 有实际变更才写库 + 写审计；无变更短路（幂等，不留痕）
            await store.update(tenant_id, patch)
            add_audit(
                db,
                actor=_identity,
                action=AuditActionEnum.SYSTEM_CONFIG_UPDATE,
                target_type="system_config",
                target_id=tenant_id,
                target_name="检索/分块配置",
                detail={"tenant_id": tenant_id, "changes": changes},
                request=request,
            )
            await db.commit()
            # 广播失效信号：通知其他进程失效该租户配置缓存（M1 多进程热生效）
            # 本进程已由 store.update 内联失效，广播只负责通知其他进程
            bus = get_invalidation_bus()
            if bus:
                await bus.publish("tenant_config", tenant_id)

    # 3) 响应回读该租户最新有效检索参数（确保返回更新后的值，Req 6.1），并回显本次变更明细
    eff = await store.get_effective(tenant_id)
    return _build_config_response(
        settings, RetrievalConfigSection.from_config(eff), changes
    )


@router.post("/config/retrieval/reset", response_model=SystemConfigResponse)
async def reset_retrieval_config(
    request: Request,
    _identity: IdentityContext = Depends(require_platform()),
    db: AsyncSession = Depends(get_db_session),
):
    """恢复检索参数默认值（Req 4.1/4.2/6.9）。

    将平台级（全平台一份）的全部分块与检索参数重置为各自 Safe_Default，并返回包含
    settings 当前值与重置后检索分区的完整系统配置响应。能力配置属平台底座，仅超级
    管理员可重置（capability-config-to-platform，禁 api_key 通道）。

    重置若产生实际变更（现状与默认值不同的字段），写一条 ``system.config_reset`` 审计；
    本就是全默认（无变更）则不写审计。
    """
    settings = get_settings()
    tenant_id = _resolve_target_tenant(_identity, request)
    store = get_retrieval_config_store()
    # 重置前有效值作为 before，与默认值 diff 出实际变更字段
    eff_before = await store.get_effective(tenant_id)
    before = eff_before.model_dump()
    defaults = {name: spec.default for name, spec in RETRIEVAL_FIELD_SPECS.items()}
    changes = _diff_changes(before, defaults)

    eff = await store.reset_defaults(tenant_id)

    if changes:
        add_audit(
            db,
            actor=_identity,
            action=AuditActionEnum.SYSTEM_CONFIG_RESET,
            target_type="system_config",
            target_id=tenant_id,
            target_name="检索/分块配置",
            detail={"tenant_id": tenant_id, "changes": changes},
            request=request,
        )
        await db.commit()
        # 广播失效信号：通知其他进程失效该租户配置缓存（M1 多进程热生效）
        # 本进程已由 store.reset_defaults 内联失效，广播只负责通知其他进程
        bus = get_invalidation_bus()
        if bus:
            await bus.publish("tenant_config", tenant_id)

    return _build_config_response(
        settings, RetrievalConfigSection.from_config(eff), changes
    )


# ============================================================
# Platform_Config_API（超管平台级配置：Load_Cache_TTL）
# ============================================================


class MemoryRecommendationVO(BaseModel):
    """单库 chunk 上限（KB_Chunk_Cap）的内存推荐值（信息性，不自动写入，Req 5.1/5.3/5.6）。

    字段与 ``app.session_upload.memory.recommend_kb_chunk_cap`` 返回的 dict 同名同型，
    可直接 ``MemoryRecommendationVO(**recommend_kb_chunk_cap())`` 构造。
    """

    detected_memory_gb: float
    recommended_kb_chunk_cap: int
    safety_factor: float
    active_kbs_assumption: int
    assumption: str


class PlatformConfigResponse(BaseModel):
    """平台级配置响应（超管专属）。"""

    load_cache_ttl: int
    # 上传限制平台级（单库/单会话 chunk 硬上限）
    kb_chunk_cap: int
    # 单库 chunk 上限的内存推荐值（仅 GET 填充，信息性建议）
    memory_recommendation: MemoryRecommendationVO | None = None
    # 本次实际变更明细（仅 PUT 填充；GET 无变更时为空列表）
    changes: list[ConfigChange] = Field(default_factory=list)


class PlatformConfigUpdate(BaseModel):
    """平台级配置更新请求（字段 Optional，仅更新提交字段）。"""

    load_cache_ttl: int | None = None
    # 上传限制平台级（单库/单会话 chunk 硬上限）
    kb_chunk_cap: int | None = None


@router.get("/platform-config", response_model=PlatformConfigResponse)
async def get_platform_config(
    _identity: IdentityContext = Depends(require_platform()),
):
    """读取平台级配置（仅超级管理员/JWT）。

    本期承载 Load_Cache_TTL（控制 collection 加载缓存有效期）与单库/单会话 chunk 硬上限
    （kb_chunk_cap）。额外返回基于运行内存的 KB_Chunk_Cap 推荐值（信息性，不自动写入）。
    """
    eff = await get_platform_config_store().get_effective()
    return PlatformConfigResponse(
        **eff.model_dump(),
        memory_recommendation=MemoryRecommendationVO(**recommend_kb_chunk_cap()),
    )


@router.put("/platform-config", response_model=PlatformConfigResponse)
async def update_platform_config(
    body: PlatformConfigUpdate,
    request: Request,
    _identity: IdentityContext = Depends(require_platform()),
    db: AsyncSession = Depends(get_db_session),
):
    """更新平台级配置（仅超级管理员/JWT，禁 api_key 通道，Req 18.1/18.2）。

    经 ``validate_platform_patch`` 范围校验：越界 → 422 且不写库（Req 17.4）；合法则
    ``store.update`` 即时热生效（Req 17.3）。产生实际变更才写 ``platform.config_update``
    审计；提交值与现状相同（无变更）则不写库也不留痕。
    """
    store = get_platform_config_store()
    patch = body.model_dump(exclude_unset=True, exclude_none=True)
    errors = validate_platform_patch(patch)
    if errors:
        raise HTTPException(
            status_code=422,
            detail=[e.to_dict() for e in errors],
        )
    # 更新前有效值作为 before，与 patch diff 出实际变更字段
    before = (await store.get_effective()).model_dump()
    changes = _diff_changes(before, patch)
    if changes:
        await store.update(patch)
        add_audit(
            db,
            actor=_identity,
            action=AuditActionEnum.PLATFORM_CONFIG_UPDATE,
            target_type="platform_config",
            target_id="global",
            target_name="平台级配置",
            detail={"changes": changes},
            request=request,
        )
        await db.commit()
    eff = await store.get_effective()
    return PlatformConfigResponse(
        load_cache_ttl=eff.load_cache_ttl,
        kb_chunk_cap=eff.kb_chunk_cap,
        changes=[ConfigChange(**c) for c in changes],
    )


@router.get("/queue-stats", response_model=QueueStats)
async def get_queue_stats(
    request: Request,
    _identity: IdentityContext = Depends(require_tenant_admin()),
):
    """获取任务队列统计信息

    返回当前队列深度、pending 任务数、活跃 Worker 数、DLQ 任务数。
    Redis 不可用时返回全零值。
    """
    task_queue = getattr(request.app.state, "task_queue", None)
    if task_queue is None:
        return QueueStats()
    stats = await task_queue.get_stats()

    # 叠加慢道统计（大文件队列），让前端看到完整的队列深度
    slow_queue = getattr(request.app.state, "slow_task_queue", None)
    if slow_queue is not None:
        slow = await slow_queue.get_stats()
        stats = QueueStats(
            stream_length=stats.stream_length + slow.stream_length,
            pending_count=stats.pending_count + slow.pending_count,
            active_workers=max(stats.active_workers, slow.active_workers),
            dlq_length=stats.dlq_length,  # 快慢道共用同一 DLQ，避免重复计数
        )
    return stats


class FrontendConfigResponse(BaseModel):
    """前端运行时配置（无需认证，前端启动时拉取）"""
    upload_max_concurrent: int = 3
    upload_max_file_size_mb: int = 500


@router.get("/frontend-config", response_model=FrontendConfigResponse)
async def get_frontend_config():
    """获取前端配置（公开接口，前端启动时拉取）"""
    settings = get_settings()
    return FrontendConfigResponse(
        upload_max_concurrent=settings.upload_max_concurrent,
        upload_max_file_size_mb=settings.upload_max_file_size_mb,
    )
