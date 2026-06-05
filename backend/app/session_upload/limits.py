"""上传生效限制求解（design C2）

本模块集中求解某租户在一次具体上传校验中实际采用的各项限制（Effective_Limit），把
「租户配置 / 平台配置 / 安全默认 + 取下界（min）」的组合逻辑封装在一处，供会话上传入口、
知识库上传入口与 Pre_Embed_Gate 复用，保证同一套兜底/取 min 规则不散落各处。

求解规则（对照 requirements Req 6.2 / 6.3 / 6.10 / 6.11 / 9.1 / 9.2 / 9.3）：

- ``upload_max_file_bytes`` = 租户 ``upload_max_file_mb`` × 1024 × 1024（会话上传与知识库上传共用）。
- ``session_max_files`` = 租户 ``session_max_files``（**无平台天花板**，文件数不累加占用常驻内存）。
- ``session_chunk_cap`` = min(租户 ``session_chunk_cap``, 平台 ``session_chunk_ceiling``)（取下界）。
- ``kb_chunk_cap`` = 平台 ``kb_chunk_cap``。

安全兜底（Req 9.1 / 9.2）：

- ``tenant_id`` 为 None（无租户上下文）→ 租户侧全部取安全默认（由 Store 内部直接返回默认配置）。
- 任一 Store 读取失败 → 该侧降级为安全默认并记 WARNING，**绝不放行无限制上传**。

底层 ``RetrievalConfigStore.get_effective`` / ``PlatformConfigStore.get_effective`` 本身已实现
「tenant_id 为 None / DB 读失败 → 全 Safe_Default」的降级，且返回值恒落在各自 Valid_Range 内
（见 ``app/retrieval/config.py``）。本模块在其之上再兜一层 try/except，防止任何未预期异常导致
求解中断或放行无限制（Req 9.2）。``resolve`` 一次性产出 :class:`UploadLimits` 快照，供单次上传
校验全程复用（Req 9.3）。
"""

import logging
from dataclasses import dataclass

from app.retrieval.config import (
    PlatformConfig,
    RetrievalConfig,
    get_platform_config_store,
    get_retrieval_config_store,
)

logger = logging.getLogger(__name__)


# ============================================================
# 模块常量（单一事实源，避免魔法值）
# ============================================================

# MB → 字节换算因子（文件大小上限以 MB 配置、以字节校验）
_BYTES_PER_MB = 1024 * 1024


@dataclass(frozen=True)
class UploadLimits:
    """某租户在一次上传校验中的生效限制快照（不可变，单次取一份全程复用）。

    Attributes:
        upload_max_file_bytes: 单文件大小上限（字节）= 租户 ``upload_max_file_mb`` × 1024 × 1024。
            会话上传与知识库上传共用同一上限（Req 3.5）。
        session_max_files: 单会话累计文件数上限 = 租户 ``session_max_files``（无平台天花板，Req 6.2）。
        session_chunk_cap: 单会话累计 child chunk 上限 = min(租户值, 平台天花板)（取下界，Req 6.3 / 6.11）。
        kb_chunk_cap: 单库 child chunk 硬上限 = 平台 ``kb_chunk_cap``（Req 4.1）。
    """

    upload_max_file_bytes: int
    session_max_files: int
    session_chunk_cap: int
    kb_chunk_cap: int


class UploadLimitResolver:
    """求解某租户在一次校验中的生效上传限制（``UploadLimits`` 快照）。

    封装「租户 / 平台 / 默认 + 取 min」逻辑，复用既有
    ``RetrievalConfigStore`` / ``PlatformConfigStore``（二者已实现按租户分键缓存、写后失效、
    即时热生效与 DB 失败降级）。本类不持有额外缓存，每次 ``resolve`` 取一份快照。

    设计依据：design.md Components C2。
    """

    async def resolve(self, tenant_id: str | None) -> UploadLimits:
        """读租户 ``RetrievalConfig`` + 平台 ``PlatformConfig``，组合产出生效限制快照。

        - 会话 chunk 取 min(租户 ``session_chunk_cap``, 平台 ``session_chunk_ceiling``)（Req 6.3 / 6.11）。
        - 会话文件数 = 租户 ``session_max_files``（无平台天花板，Req 6.2）。
        - 文件大小 = 租户 ``upload_max_file_mb`` × 1024 × 1024 字节（Req 3.1 / 3.5）。
        - 单库 chunk 上限 = 平台 ``kb_chunk_cap``（Req 4.1）。
        - ``tenant_id`` 为 None → 租户侧全安全默认（Store 内部返回默认配置，Req 9.1）。
        - 任一 Store 读取异常 → 该侧降级安全默认并记 WARNING，不放行无限制（Req 9.2）。

        Args:
            tenant_id: 目标租户 ID；None 表示无租户上下文（取安全默认）。

        Returns:
            ``UploadLimits`` 快照，各项恒落在各自 Valid_Range 内（由底层配置兜底保证）。
        """
        retrieval_cfg = await self._safe_retrieval_config(tenant_id)
        platform_cfg = await self._safe_platform_config()

        upload_max_file_bytes = retrieval_cfg.upload_max_file_mb * _BYTES_PER_MB
        session_max_files = retrieval_cfg.session_max_files
        # 取下界：租户配置不得超过平台天花板（Req 6.3 / 6.11）。
        session_chunk_cap = min(retrieval_cfg.session_chunk_cap, platform_cfg.session_chunk_ceiling)
        kb_chunk_cap = platform_cfg.kb_chunk_cap

        return UploadLimits(
            upload_max_file_bytes=upload_max_file_bytes,
            session_max_files=session_max_files,
            session_chunk_cap=session_chunk_cap,
            kb_chunk_cap=kb_chunk_cap,
        )

    @staticmethod
    async def _safe_retrieval_config(tenant_id: str | None) -> RetrievalConfig:
        """读租户检索配置，任何未预期异常降级为全安全默认（不放行无限制，Req 9.2）。

        底层 ``get_effective`` 已处理 tenant_id 为 None 与 DB 读失败的降级；此处再兜一层，
        防止 Store 单例构造或其他未预期异常导致求解中断。
        """
        try:
            return await get_retrieval_config_store().get_effective(tenant_id)
        except Exception:  # noqa: BLE001 - 限制求解必须安全降级，绝不放行无限制
            logger.warning(
                "读取租户 %s 上传限制配置失败，降级为安全默认（不放行无限制）",
                tenant_id,
                exc_info=True,
            )
            return RetrievalConfig()

    @staticmethod
    async def _safe_platform_config() -> PlatformConfig:
        """读平台配置，任何未预期异常降级为全安全默认（不放行无限制，Req 9.2）。"""
        try:
            return await get_platform_config_store().get_effective()
        except Exception:  # noqa: BLE001 - 限制求解必须安全降级，绝不放行无限制
            logger.warning(
                "读取平台上传限制配置失败，降级为安全默认（不放行无限制）",
                exc_info=True,
            )
            return PlatformConfig()


# ============================================================
# 进程内单例
# ============================================================

_resolver: UploadLimitResolver | None = None


def get_upload_limit_resolver() -> UploadLimitResolver:
    """获取进程内 ``UploadLimitResolver`` 单例。

    无状态（不持额外缓存），单例仅为复用与依赖注入便利，风格对齐
    ``get_retrieval_config_store`` / ``get_platform_config_store``。
    """
    global _resolver
    if _resolver is None:
        _resolver = UploadLimitResolver()
    return _resolver
