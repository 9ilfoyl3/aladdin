"""检索参数配置数据模型与纯函数（B1）

本模块是检索/分块参数（六档：分块 / 召回 / 融合 / 精排 / 去重 / 索引）与平台级配置
（Load_Cache_TTL）的**单一事实源**：

- ``RETRIEVAL_FIELD_SPECS`` / ``PLATFORM_FIELD_SPECS``：字段名 → ``FieldSpec(default, lo, hi, kind)``，
  校验、读时兜底、恢复默认共用此表，禁止把范围/默认值散落成魔法值。
- ``RetrievalConfig`` / ``PlatformConfig``：承载各档参数的 Pydantic 模型，默认值取自规格表。
- ``effective_from_raw``：从 DB 原始 dict 构造「有效配置」，逐字段独立兜底
  （缺失 / None / 类型错误 / 越界 → 回退该字段 Safe_Default），结果恒落在 Valid_Range 内。
- ``validate_patch`` / ``validate_platform_patch``：写库前的范围校验，返回越界字段错误列表。
- ``RetrievalConfigStore``（按 tenant_id 分键） / ``PlatformConfigStore``（全局单行）：
  绕过 ``get_settings()`` 的 ``@lru_cache``，短 TTL 内存缓存 + 写后失效，支持即时热生效。

设计依据：design.md Components C1 / C2 / C3。
"""

import logging
import time
from dataclasses import dataclass

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)


# ============================================================
# 字段类型常量（避免魔法字符串）
# ============================================================

KIND_INT = "int"
KIND_FLOAT = "float"
KIND_BOOL = "bool"


@dataclass(frozen=True)
class FieldSpec:
    """单个检索参数的规格定义。

    Attributes:
        default: Safe_Default（安全默认值）。
        lo: Valid_Range 下界（含）。bool 字段为 None（不参与范围校验）。
        hi: Valid_Range 上界（含）。bool 字段为 None（不参与范围校验）。
        kind: 字段类型，取值 ``KIND_INT`` / ``KIND_FLOAT`` / ``KIND_BOOL``。
    """

    default: int | float | bool
    lo: int | float | None
    hi: int | float | None
    kind: str


# ============================================================
# 单一事实源：检索参数字段规格表（对照 design C1 字段表）
# ============================================================

RETRIEVAL_FIELD_SPECS: dict[str, FieldSpec] = {
    # 分块档 Chunk_Tier（本期纳入，默认对齐 Settings：2500/450/70）
    "parent_chunk_size": FieldSpec(default=2500, lo=100, hi=8000, kind=KIND_INT),
    "child_chunk_size": FieldSpec(default=450, lo=50, hi=4000, kind=KIND_INT),
    "chunk_overlap": FieldSpec(default=70, lo=0, hi=1000, kind=KIND_INT),
    # 召回档 Recall_Tier
    "recall_k": FieldSpec(default=128, lo=1, hi=1000, kind=KIND_INT),
    "rerank_candidate_k": FieldSpec(default=50, lo=1, hi=200, kind=KIND_INT),
    # 融合档 Fusion_Tier
    "rrf_k": FieldSpec(default=60, lo=1, hi=1000, kind=KIND_INT),
    "composite_rerank_weight": FieldSpec(default=0.6, lo=0.0, hi=1.0, kind=KIND_FLOAT),
    "composite_base_weight": FieldSpec(default=0.3, lo=0.0, hi=1.0, kind=KIND_FLOAT),
    "composite_source_weight": FieldSpec(default=0.1, lo=0.0, hi=1.0, kind=KIND_FLOAT),
    # 精排档 Rerank_Tier
    "rerank_threshold": FieldSpec(default=0.2, lo=0.0, hi=1.0, kind=KIND_FLOAT),
    "rerank_top_k": FieldSpec(default=10, lo=1, hi=100, kind=KIND_INT),
    "threshold_degradation_enabled": FieldSpec(default=True, lo=None, hi=None, kind=KIND_BOOL),
    # 去重档 Dedup_Tier
    "mmr_lambda": FieldSpec(default=0.7, lo=0.0, hi=1.0, kind=KIND_FLOAT),
    "mmr_threshold": FieldSpec(default=0.7, lo=0.0, hi=1.0, kind=KIND_FLOAT),
    # 索引档 Index_Tier
    "hnsw_ef": FieldSpec(default=128, lo=1, hi=2048, kind=KIND_INT),
    # efConstruction 默认 200（对齐 app/storage/milvus.py 的 _DEFAULT_EF_CONSTRUCTION）：
    # 128→200 召回提升明显而建索引耗时仅增约五成；200→400 召回几乎不再提升但耗时翻倍，
    # 200 是生产环境的性价比拐点。M=16 为通用最优（内存与召回平衡）。
    "hnsw_ef_construction": FieldSpec(default=200, lo=8, hi=512, kind=KIND_INT),
    "hnsw_m": FieldSpec(default=16, lo=4, hi=64, kind=KIND_INT),
    # 上传限制档 Upload_Tier（租户级）
    # 单文件大小上限（MB），会话上传与知识库上传共用
    "upload_max_file_mb": FieldSpec(default=10, lo=1, hi=100, kind=KIND_INT),
}


def _format_range(spec: FieldSpec) -> str:
    """格式化字段允许范围，用于错误信息与日志。

    数值字段返回 ``"[lo, hi]"``；bool 字段返回 ``"{true, false}"``。
    """
    if spec.kind == KIND_BOOL:
        return "{true, false}"
    return f"[{spec.lo}, {spec.hi}]"


def _is_valid_type(value: object, kind: str) -> bool:
    """判断 value 的类型是否与字段 kind 匹配。

    - int 字段：必须是 int 且不能是 bool（Python 中 bool 是 int 的子类）。
    - float 字段：接受 int 或 float（int 可作为 float），但不能是 bool。
    - bool 字段：必须是 bool。
    """
    if kind == KIND_BOOL:
        return isinstance(value, bool)
    if kind == KIND_INT:
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == KIND_FLOAT:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return False


def _in_range(value: int | float, spec: FieldSpec) -> bool:
    """判断数值是否落在 ``[lo, hi]`` 闭区间内（bool 字段不调用此函数）。"""
    return spec.lo <= value <= spec.hi


@dataclass
class FieldError:
    """范围校验的单个违规字段。

    Attributes:
        field: 违规字段名。
        value: 提交的非法取值（原样保留）。
        allowed_range: 该字段允许范围的可读表示（如 ``"[1, 1000]"``）。
    """

    field: str
    value: object
    allowed_range: str

    def to_dict(self) -> dict:
        """转为可序列化 dict，供 System_Config_API 返回 422 body 使用。"""
        return {"field": self.field, "value": self.value, "allowed_range": self.allowed_range}


class RetrievalConfig(BaseModel):
    """检索配置（五档）。

    字段类型与默认值取自 ``RETRIEVAL_FIELD_SPECS``（单一事实源），不重复书写魔法值。
    """

    # 分块档
    parent_chunk_size: int = RETRIEVAL_FIELD_SPECS["parent_chunk_size"].default
    child_chunk_size: int = RETRIEVAL_FIELD_SPECS["child_chunk_size"].default
    chunk_overlap: int = RETRIEVAL_FIELD_SPECS["chunk_overlap"].default
    # 召回档
    recall_k: int = RETRIEVAL_FIELD_SPECS["recall_k"].default
    rerank_candidate_k: int = RETRIEVAL_FIELD_SPECS["rerank_candidate_k"].default
    # 融合档
    rrf_k: int = RETRIEVAL_FIELD_SPECS["rrf_k"].default
    composite_rerank_weight: float = RETRIEVAL_FIELD_SPECS["composite_rerank_weight"].default
    composite_base_weight: float = RETRIEVAL_FIELD_SPECS["composite_base_weight"].default
    composite_source_weight: float = RETRIEVAL_FIELD_SPECS["composite_source_weight"].default
    # 精排档
    rerank_threshold: float = RETRIEVAL_FIELD_SPECS["rerank_threshold"].default
    rerank_top_k: int = RETRIEVAL_FIELD_SPECS["rerank_top_k"].default
    threshold_degradation_enabled: bool = RETRIEVAL_FIELD_SPECS["threshold_degradation_enabled"].default
    # 去重档
    mmr_lambda: float = RETRIEVAL_FIELD_SPECS["mmr_lambda"].default
    mmr_threshold: float = RETRIEVAL_FIELD_SPECS["mmr_threshold"].default
    # 索引档
    hnsw_ef: int = RETRIEVAL_FIELD_SPECS["hnsw_ef"].default
    hnsw_ef_construction: int = RETRIEVAL_FIELD_SPECS["hnsw_ef_construction"].default
    hnsw_m: int = RETRIEVAL_FIELD_SPECS["hnsw_m"].default
    # 上传限制档（租户级）
    upload_max_file_mb: int = RETRIEVAL_FIELD_SPECS["upload_max_file_mb"].default

    @classmethod
    def effective_from_raw(cls, raw: dict | None) -> "RetrievalConfig":
        """从 DB 原始 dict 构造「有效配置」，逐字段独立兜底。

        逐字段规则（Req 2.2 / 2.3 / 2.5）：

        - 缺失（键不存在）或为 None → 回退该字段 Safe_Default。
        - 类型错误（如 int 字段填了字符串/float/bool）→ 回退 Safe_Default。
        - 数值越界（超出 Valid_Range）→ 回退 Safe_Default。
        - bool 字段（threshold_degradation_enabled）只判缺失/类型，不判范围。
        - 区间内的合法值原样保留；单字段兜底不影响其余字段。

        每次回退记一条 WARNING 日志，包含 field、原值、回退值（Req 2.4）。

        返回的实例所有字段恒落在各自 Valid_Range 内。

        Args:
            raw: DB 单行的原始 dict（可为 None，表示无持久化行）。

        Returns:
            所有字段均为合法有效值的 ``RetrievalConfig`` 实例。
        """
        return cls(**_effective_values_from_specs(RETRIEVAL_FIELD_SPECS, raw))


def _effective_values_from_specs(
    specs: dict[str, FieldSpec], raw: dict | None
) -> dict[str, int | float | bool]:
    """按规格表逐字段兜底，返回有效值 dict（供各配置模型 ``effective_from_raw`` 复用）。

    逐字段规则：缺失 / None / 类型错误 / 数值越界 → 回退该字段 Safe_Default；bool 字段
    只判缺失/类型不判范围；区间内合法值原样保留。每次回退记一条含 field/原值/回退值的
    WARNING（Req 2.4）。返回 dict 的每个值恒落在各自 Valid_Range 内。

    raw 为 None 表示「尚无持久化行」（正常未配置态）：全量用默认且不逐字段刷日志；
    raw 为 dict 时，其中缺失/为空的字段属于真实兜底回退，按 Req 2.4 记日志。
    """
    row_provided = raw is not None
    source = raw or {}
    effective: dict[str, int | float | bool] = {}

    for name, spec in specs.items():
        # 缺失或 None → 回退
        if name not in source or source[name] is None:
            if row_provided:
                original = source.get(name)  # 缺失记 None，显式 None 亦记 None
                _log_fallback(name, original, spec.default, "缺失/为空")
            effective[name] = spec.default
            continue

        value = source[name]

        # 类型错误 → 回退
        if not _is_valid_type(value, spec.kind):
            _log_fallback(name, value, spec.default, "类型错误")
            effective[name] = spec.default
            continue

        # bool 字段不做范围校验，类型正确即保留
        if spec.kind == KIND_BOOL:
            effective[name] = value
            continue

        # 数值越界 → 回退
        if not _in_range(value, spec):
            _log_fallback(name, value, spec.default, "越界")
            effective[name] = spec.default
            continue

        # 合法值原样保留
        effective[name] = value

    return effective


def _log_fallback(field: str, original: object, fallback: object, reason: str) -> None:
    """记录一条字段兜底回退的 WARNING 日志（含 field、原值、回退值）。"""
    logger.warning(
        "配置字段 %s 触发兜底回退（%s）：原值=%r，回退值=%r",
        field,
        reason,
        original,
        fallback,
    )


def _validate_against_specs(specs: dict[str, FieldSpec], patch: dict) -> list[FieldError]:
    """按规格表做范围校验，返回越界/类型错误字段的错误列表（供各 ``validate_*`` 复用）。

    - 只校验 patch 中出现且属于 ``specs`` 的字段（未知字段忽略）。
    - 数值字段：类型错误或超出 Valid_Range 入错误列表。
    - bool 字段：只校验类型，不校验范围。
    - 全部合法时返回空列表。
    """
    errors: list[FieldError] = []

    for name, value in patch.items():
        spec = specs.get(name)
        if spec is None:
            continue  # 非本规格表字段，不在职责内

        if not _is_valid_type(value, spec.kind):
            errors.append(FieldError(field=name, value=value, allowed_range=_format_range(spec)))
            continue

        # bool 字段类型正确即合法（不校验范围）
        if spec.kind == KIND_BOOL:
            continue

        if not _in_range(value, spec):
            errors.append(FieldError(field=name, value=value, allowed_range=_format_range(spec)))

    return errors


def validate_patch(patch: dict) -> list[FieldError]:
    """范围校验：返回 patch 中越界/类型错误字段的错误列表（Req 3.2 / 3.3）。

    - 只校验 patch 中出现且属于 ``RETRIEVAL_FIELD_SPECS`` 的字段（未知字段忽略）。
    - 数值字段：类型错误或超出 Valid_Range 入错误列表。
    - bool 字段：只校验类型，不校验范围。
    - 全部合法时返回空列表。

    供 System_Config_API 在写库前拦截，错误项含 ``field`` / ``value`` / ``allowed_range``。
    """
    return _validate_against_specs(RETRIEVAL_FIELD_SPECS, patch)


# ============================================================
# C2. RetrievalConfigStore：读取/写入层（按租户分键，即时热生效）
# ============================================================

# 单一事实源里全部字段名，行 ↔ dict 转换只取这些列（忽略 tenant_id / updated_at）。
_CONFIG_FIELD_NAMES: tuple[str, ...] = tuple(RETRIEVAL_FIELD_SPECS.keys())

# 全平台一份固定主键（capability-config-to-platform）：检索/切片参数为平台底座，
# 不再按租户分行，统一存于该 sentinel 主键的单行，仅超级管理员维护、对全平台生效。
# Store 入口把任意传入 tenant_id（含 None / kb.tenant_id / contextvar 租户）规范化为
# 该键，保证「写哪行 = 读哪行」恒一致，运行时调用点（hybrid/pipeline/kb/limits）无需改动。
PLATFORM_RETRIEVAL_KEY = "__platform__"


class RetrievalConfigStore:
    """检索配置读取/写入层（**全平台一份**，capability-config-to-platform），绕过
    ``get_settings()`` 的 ``@lru_cache``，支持即时热生效。

    历史上按 ``tenant_id`` 分键；现检索/切片参数已上收为平台底座（全平台共用一份，
    仅超管维护）。为零改动运行时调用点，方法仍保留 ``tenant_id`` 形参，但内部一律
    规范化为 ``PLATFORM_RETRIEVAL_KEY``：无论调用方传 None / kb.tenant_id / contextvar
    租户，都落到同一平台单行。

    每进程一个实例（API / Worker 各自持有）。内存缓存为单键，短 TTL + 写后失效：

    - ``get_effective(tenant_id=None)``：缓存命中（未过期）直接返回；否则读 DB 平台单行
      （主键 = ``PLATFORM_RETRIEVAL_KEY``）→ ``RetrievalConfig.effective_from_raw`` → 写缓存。
      DB 读失败时**不抛错**，降级返回全 Safe_Default 并记 WARNING（检索可用性优先；不缓存降级结果）。
    - ``update(tenant_id, patch)``：UPSERT 平台单行 → 失效缓存 → 返回新的有效配置。
      调用前应已通过 ``validate_patch``。
    - ``reset_defaults(tenant_id)``：将平台单行所有字段写为各自 Safe_Default → 失效缓存 → 返回全默认。
    - ``invalidate(tenant_id=None)``：失效平台单键缓存。

    设计依据：design.md Components C2；Architecture（即时热生效数据流）。
    """

    # 短 TTL：未命中失效时最多 5s 收敛；同进程写后立即失效（不等 TTL）。
    _CACHE_TTL_SECONDS = 5

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        """初始化。

        Args:
            session_factory: 异步会话工厂（``async_sessionmaker``），独立于
                ``get_settings()`` 的 ``@lru_cache``，专门承载即时热生效读取层。
        """
        self._session_factory = session_factory
        # 按 tenant_id 分键的内存缓存：tenant_id -> (有效配置, 写入时间戳)。
        self._cache: dict[str, tuple[RetrievalConfig, float]] = {}

    def invalidate(self, tenant_id: str | None = None) -> None:
        """失效内存缓存（全平台一份：忽略传入 tenant_id，统一失效平台单键）。"""
        self._cache.pop(PLATFORM_RETRIEVAL_KEY, None)

    def _cache_get(self, tenant_id: str) -> RetrievalConfig | None:
        """读某租户缓存，命中且未过期则返回，否则 None。"""
        entry = self._cache.get(tenant_id)
        if entry is None:
            return None
        config, cached_at = entry
        if (time.monotonic() - cached_at) < self._CACHE_TTL_SECONDS:
            return config
        return None

    def _cache_put(self, tenant_id: str, config: RetrievalConfig) -> None:
        """写某租户缓存并记录时间戳。"""
        self._cache[tenant_id] = (config, time.monotonic())

    @staticmethod
    def _row_to_raw(row) -> dict:
        """把 ORM 行转为 raw dict，只取 ``RETRIEVAL_FIELD_SPECS`` 中的字段（忽略 tenant_id / updated_at）。"""
        return {name: getattr(row, name) for name in _CONFIG_FIELD_NAMES}

    async def get_effective(self, tenant_id: str | None = None) -> RetrievalConfig:
        """读平台有效检索配置（全平台一份）。

        历史形参 ``tenant_id`` 保留以兼容运行时调用点（hybrid/pipeline/kb/limits），
        但一律规范化为 ``PLATFORM_RETRIEVAL_KEY``：无论传 None / kb.tenant_id / contextvar
        租户，都读同一平台单行。

        - 缓存命中（未过期）直接返回。
        - 否则读 DB 平台单行（主键 = ``PLATFORM_RETRIEVAL_KEY``）→ ``effective_from_raw``
          → 写缓存。行缺失（首次未配）→ 全 Safe_Default。
        - DB 读失败降级返回全 Safe_Default（不抛错、不缓存），并记一条 WARNING。
        """
        key = PLATFORM_RETRIEVAL_KEY

        cached = self._cache_get(key)
        if cached is not None:
            return cached

        try:
            from app.schema.db import RetrievalConfigRow

            async with self._session_factory() as session:
                row = await session.get(RetrievalConfigRow, key)
                raw = self._row_to_raw(row) if row is not None else None
        except Exception as e:
            # 检索可用性优先：DB 读失败降级为全 Safe_Default，不抛错、不缓存。
            logger.warning("读取平台检索配置失败（降级为全默认值）: %s", e)
            return RetrievalConfig.effective_from_raw(None)

        config = RetrievalConfig.effective_from_raw(raw)
        self._cache_put(key, config)
        return config

    async def update(self, tenant_id: str | None, patch: dict) -> RetrievalConfig:
        """UPSERT 平台单行（仅写 patch 中的检索字段）→ 失效缓存 → 返回新的有效配置。

        全平台一份：忽略传入 ``tenant_id``，统一写 ``PLATFORM_RETRIEVAL_KEY`` 单行。
        调用前应已通过 ``validate_patch``。只接受 ``RETRIEVAL_FIELD_SPECS`` 中的字段，
        其余键忽略，避免把未知字段写入。
        """
        from app.schema.db import RetrievalConfigRow

        key = PLATFORM_RETRIEVAL_KEY
        clean_patch = {k: v for k, v in patch.items() if k in RETRIEVAL_FIELD_SPECS}

        async with self._session_factory() as session:
            row = await session.get(RetrievalConfigRow, key)
            if row is None:
                row = RetrievalConfigRow(tenant_id=key, **clean_patch)
                session.add(row)
            else:
                for name, value in clean_patch.items():
                    setattr(row, name, value)
            await session.commit()

        self.invalidate()
        return await self.get_effective()

    async def reset_defaults(self, tenant_id: str | None = None) -> RetrievalConfig:
        """将平台单行所有字段写为各自 Safe_Default → 失效缓存 → 返回全默认有效配置（Req 4.1）。"""
        defaults = {name: spec.default for name, spec in RETRIEVAL_FIELD_SPECS.items()}
        return await self.update(None, defaults)


# ============================================================
# C3. PlatformConfig：平台级全局配置（本期承载 Load_Cache_TTL）
# ============================================================

# 平台级全局配置固定主键（单行，跨租户共享）。
_PLATFORM_ROW_ID = "global"

# 平台配置字段规格（单一事实源）。
PLATFORM_FIELD_SPECS: dict[str, FieldSpec] = {
    # 加载缓存有效期（秒），超管平台级配置。
    "load_cache_ttl": FieldSpec(default=30, lo=0, hi=3600, kind=KIND_INT),
    # 上传限制平台级（超管可配）
    # 单库 child chunk 硬上限（约束 Milvus 常驻内存），默认 100 万，范围 1 万–1000 万
    "kb_chunk_cap": FieldSpec(default=1000000, lo=10000, hi=10000000, kind=KIND_INT),
    # ============================================================
    # 知识图谱抗压参数（knowledge-graph，design.md 3.4）：可热调，服务端 clamp 硬上限
    # ============================================================
    # overview 默认返回节点上限
    "graph_overview_max_nodes": FieldSpec(default=500, lo=50, hi=2000, kind=KIND_INT),
    # ego 模式节点上限
    "graph_ego_max_nodes": FieldSpec(default=300, lo=10, hi=1000, kind=KIND_INT),
    # ego BFS 最大跳数（硬上限）
    "graph_ego_max_depth": FieldSpec(default=2, lo=1, hi=3, kind=KIND_INT),
    # 检索融合时邻居跳数
    "graph_retriever_hops": FieldSpec(default=2, lo=1, hi=2, kind=KIND_INT),
    # 图谱召回每查询最大 chunk 数
    "graph_retriever_max_chunks": FieldSpec(default=20, lo=1, hi=100, kind=KIND_INT),
}

# 平台配置全部字段名，行 ↔ dict 转换只取这些列（忽略 id / updated_at）。
_PLATFORM_FIELD_NAMES: tuple[str, ...] = tuple(PLATFORM_FIELD_SPECS.keys())


class PlatformConfig(BaseModel):
    """平台级全局配置（本期仅 Load_Cache_TTL）。

    字段类型与默认值取自 ``PLATFORM_FIELD_SPECS``（单一事实源），不重复书写魔法值。
    """

    load_cache_ttl: int = PLATFORM_FIELD_SPECS["load_cache_ttl"].default
    # 上传限制平台级（超管可配）
    kb_chunk_cap: int = PLATFORM_FIELD_SPECS["kb_chunk_cap"].default
    # 知识图谱抗压参数（design.md 3.4）
    graph_overview_max_nodes: int = PLATFORM_FIELD_SPECS["graph_overview_max_nodes"].default
    graph_ego_max_nodes: int = PLATFORM_FIELD_SPECS["graph_ego_max_nodes"].default
    graph_ego_max_depth: int = PLATFORM_FIELD_SPECS["graph_ego_max_depth"].default
    graph_retriever_hops: int = PLATFORM_FIELD_SPECS["graph_retriever_hops"].default
    graph_retriever_max_chunks: int = PLATFORM_FIELD_SPECS["graph_retriever_max_chunks"].default

    @classmethod
    def effective_from_raw(cls, raw: dict | None) -> "PlatformConfig":
        """从 DB 原始 dict 构造「有效平台配置」，逐字段独立兜底（同 ``RetrievalConfig`` 风格）。

        load_cache_ttl 缺失 / None / 类型错 / 越界 → 回退 Safe_Default（30）；区间内原样保留。
        每次回退记一条含 field/原值/回退值的 WARNING（Req 17.5）。
        """
        return cls(**_effective_values_from_specs(PLATFORM_FIELD_SPECS, raw))


def validate_platform_patch(patch: dict) -> list[FieldError]:
    """平台配置范围校验：返回 patch 中越界/类型错误字段的错误列表（Req 17.4）。

    复用与 ``validate_patch`` 同款逻辑，但针对 ``PLATFORM_FIELD_SPECS``。
    供 Platform_Config_API 在写库前拦截，错误项含 ``field`` / ``value`` / ``allowed_range``。
    """
    return _validate_against_specs(PLATFORM_FIELD_SPECS, patch)


class PlatformConfigStore:
    """平台级配置读取/写入层（**全局单行** ``id='global'``），绕过 ``get_settings()`` 的
    ``@lru_cache``，支持即时热生效。

    每进程一个实例。内存缓存 + 短 TTL + 写后失效，风格同 ``RetrievalConfigStore``：

    - ``get_effective()``：缓存命中（未过期）直接返回；否则读 DB 单行 → ``effective_from_raw``
      → 缓存。DB 读失败降级返回全 Safe_Default（不抛错、不缓存），并记 WARNING（Req 17.6）。
    - ``get_load_cache_ttl()``：便捷读取有效 TTL（DB 失败兜底 30）。
    - ``update(patch)``：UPSERT 单行 → 失效缓存 → 返回新的有效配置。调用前应已 ``validate_platform_patch``。
    - ``invalidate()``：显式失效本进程内存缓存。

    设计依据：design.md Components C3。
    """

    _CACHE_TTL_SECONDS = 5

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        """初始化。

        Args:
            session_factory: 异步会话工厂（``async_sessionmaker``），独立于
                ``get_settings()`` 的 ``@lru_cache``。
        """
        self._session_factory = session_factory
        self._cached: PlatformConfig | None = None
        self._cached_at: float = 0.0

    def invalidate(self) -> None:
        """显式失效本进程内存缓存（写后立即生效的关键）。"""
        self._cached = None
        self._cached_at = 0.0

    def _cache_valid(self) -> bool:
        """缓存是否命中且未过期。"""
        if self._cached is None:
            return False
        return (time.monotonic() - self._cached_at) < self._CACHE_TTL_SECONDS

    def _store_cache(self, config: PlatformConfig) -> None:
        """写入本进程内存缓存并记录时间戳。"""
        self._cached = config
        self._cached_at = time.monotonic()

    @staticmethod
    def _row_to_raw(row) -> dict:
        """把 ORM 行转为 raw dict，只取 ``PLATFORM_FIELD_SPECS`` 中的字段（忽略 id / updated_at）。"""
        return {name: getattr(row, name) for name in _PLATFORM_FIELD_NAMES}

    async def get_effective(self) -> PlatformConfig:
        """读平台有效配置。

        缓存命中（未过期）直接返回；否则读 DB 单行（``id="global"``）→ ``effective_from_raw``
        → 缓存并返回。DB 读失败降级返回全 Safe_Default（不抛错、不缓存），并记 WARNING（Req 17.6）。
        """
        if self._cache_valid():
            return self._cached  # type: ignore[return-value]

        try:
            from app.schema.db import PlatformConfigRow

            async with self._session_factory() as session:
                row = await session.get(PlatformConfigRow, _PLATFORM_ROW_ID)
                raw = self._row_to_raw(row) if row is not None else None
        except Exception as e:
            # 检索可用性优先：DB 读失败降级为全 Safe_Default，不抛错、不缓存。
            logger.warning("读取平台配置失败（降级为全默认值）: %s", e)
            return PlatformConfig.effective_from_raw(None)

        config = PlatformConfig.effective_from_raw(raw)
        self._store_cache(config)
        return config

    async def get_load_cache_ttl(self) -> int:
        """便捷读取 Load_Cache_TTL 有效值（DB 失败兜底 30，Req 17.6）。"""
        return (await self.get_effective()).load_cache_ttl

    async def update(self, patch: dict) -> PlatformConfig:
        """UPSERT 单行（仅写 patch 中的平台字段）→ 失效本进程缓存 → 返回新的有效配置。

        调用前应已通过 ``validate_platform_patch``。只接受 ``PLATFORM_FIELD_SPECS`` 中的字段。
        """
        from app.schema.db import PlatformConfigRow

        clean_patch = {k: v for k, v in patch.items() if k in PLATFORM_FIELD_SPECS}

        async with self._session_factory() as session:
            row = await session.get(PlatformConfigRow, _PLATFORM_ROW_ID)
            if row is None:
                row = PlatformConfigRow(id=_PLATFORM_ROW_ID, **clean_patch)
                session.add(row)
            else:
                for name, value in clean_patch.items():
                    setattr(row, name, value)
            await session.commit()

        self.invalidate()
        return await self.get_effective()


# ============================================================
# 进程内单例
# ============================================================

_store: RetrievalConfigStore | None = None


def get_retrieval_config_store() -> RetrievalConfigStore:
    """获取进程内 ``RetrievalConfigStore`` 单例。

    用 ``app/storage/database.py`` 的 ``async_session``（``async_sessionmaker``）构造，
    供 API / 检索层依赖注入。风格对齐 ``cache.py::get_retrieval_cache`` 的单例管理。
    """
    global _store
    if _store is None:
        from app.storage.database import async_session

        _store = RetrievalConfigStore(async_session)
    return _store


_platform_store: PlatformConfigStore | None = None


def get_platform_config_store() -> PlatformConfigStore:
    """获取进程内 ``PlatformConfigStore`` 单例。

    用 ``app/storage/database.py`` 的 ``async_session``（``async_sessionmaker``）构造，
    供平台配置 API / 检索层依赖注入。风格对齐 ``get_retrieval_config_store``。
    """
    global _platform_store
    if _platform_store is None:
        from app.storage.database import async_session

        _platform_store = PlatformConfigStore(async_session)
    return _platform_store
