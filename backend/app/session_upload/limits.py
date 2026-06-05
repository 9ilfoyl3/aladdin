"""上传生效限制求解

本模块集中求解某租户在一次具体上传校验中实际采用的各项限制（Effective_Limit），把
「租户配置 / 平台配置 / 安全默认」的组合逻辑封装在一处，供会话上传入口、知识库上传
入口与 Pre_Embed_Gate 复用，保证同一套兜底规则不散落各处。

求解规则：
- ``upload_max_file_bytes`` = 租户 ``upload_max_file_mb`` × 1024 × 1024（会话与 KB 共用）。
- ``kb_chunk_cap`` = 平台 ``kb_chunk_cap``（会话与 KB 共用同一上限，临时文件 = 会话级 KB）。

安全兜底：
- ``tenant_id`` 为 None → 租户侧全部取安全默认。
- 任一 Store 读取失败 → 降级为安全默认并记 WARNING，**绝不放行无限制上传**。
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

_BYTES_PER_MB = 1024 * 1024


@dataclass(frozen=True)
class UploadLimits:
    """某租户在一次上传校验中的生效限制快照（不可变，单次取一份全程复用）。

    Attributes:
        upload_max_file_bytes: 单文件大小上限（字节）= 租户 ``upload_max_file_mb`` × 1024 × 1024。
            会话上传与知识库上传共用同一上限。
        kb_chunk_cap: 单库/单会话累计 child chunk 上限 = 平台 ``kb_chunk_cap``。
            临时文件本质 = 会话级知识库，与正式 KB 共用同一 chunk 硬上限。
    """

    upload_max_file_bytes: int
    kb_chunk_cap: int


class UploadLimitResolver:
    """求解某租户在一次校验中的生效上传限制（``UploadLimits`` 快照）。"""

    async def resolve(self, tenant_id: str | None) -> UploadLimits:
        """读租户 + 平台配置，组合产出生效限制快照。

        - 文件大小 = 租户 ``upload_max_file_mb`` × 1024 × 1024 字节。
        - chunk 上限 = 平台 ``kb_chunk_cap``（会话与 KB 共用）。
        - ``tenant_id`` 为 None → 租户侧全安全默认。
        - 任一 Store 异常 → 降级安全默认并记 WARNING，不放行无限制。
        """
        retrieval_cfg = await self._safe_retrieval_config(tenant_id)
        platform_cfg = await self._safe_platform_config()

        upload_max_file_bytes = retrieval_cfg.upload_max_file_mb * _BYTES_PER_MB
        kb_chunk_cap = platform_cfg.kb_chunk_cap

        return UploadLimits(
            upload_max_file_bytes=upload_max_file_bytes,
            kb_chunk_cap=kb_chunk_cap,
        )

    @staticmethod
    async def _safe_retrieval_config(tenant_id: str | None) -> RetrievalConfig:
        """读租户检索配置，异常降级为全安全默认。"""
        try:
            return await get_retrieval_config_store().get_effective(tenant_id)
        except Exception:
            logger.warning(
                "读取租户 %s 上传限制配置失败，降级为安全默认",
                tenant_id,
                exc_info=True,
            )
            return RetrievalConfig()

    @staticmethod
    async def _safe_platform_config() -> PlatformConfig:
        """读平台配置，异常降级为全安全默认。"""
        try:
            return await get_platform_config_store().get_effective()
        except Exception:
            logger.warning(
                "读取平台上传限制配置失败，降级为安全默认",
                exc_info=True,
            )
            return PlatformConfig()


_resolver: UploadLimitResolver | None = None


def get_upload_limit_resolver() -> UploadLimitResolver:
    """获取进程内 ``UploadLimitResolver`` 单例。"""
    global _resolver
    if _resolver is None:
        _resolver = UploadLimitResolver()
    return _resolver
