"""能力配置热生效：本进程重载 + 跨进程广播

四类能力配置（embedding / rerank / OCR / ASR）属平台底座，改动后必须免重启生效。
写侧统一走 :func:`apply_and_broadcast`：

1. **本进程按数据库现状重载**——注意是"读 DB 现状"而不是"用刚写入的那条配置"。
   删除或停用 active 配置时，需要重新查库才知道"现在谁在生效"，用传入对象无法覆盖
   这两类场景。
2. **广播失效信号**通知其他进程（API 多 worker / Worker 进程）各自重载。
   ``InvalidationBus`` 会跳过与发起方 origin 相同的消息，故本进程不会重复重载。

Redis 不可用时 ``publish`` 自动 no-op 降级（沿用 kb_data / tenant_config 的既有契约），
此时仅发起进程生效，其余进程需重启；广播失败绝不影响配置保存结果。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# InvalidationBus 消息 type
CAPABILITY_CHANNEL = "capability_config"

# 消息 key：标识哪一类能力配置发生了变更
CAPABILITY_EMBEDDING = "embedding"
CAPABILITY_RERANK = "rerank"
CAPABILITY_OCR = "ocr"
CAPABILITY_ASR = "asr"
CAPABILITY_MCP = "mcp"

VALID_CAPABILITIES = frozenset(
    {CAPABILITY_EMBEDDING, CAPABILITY_RERANK, CAPABILITY_OCR, CAPABILITY_ASR, CAPABILITY_MCP}
)


async def reload_capability_locally(capability: str) -> None:
    """按数据库现状重载本进程持有的该能力运行时对象

    - ``embedding`` / ``rerank``：走 ``load_embed_configs()``，它读 DB 中 active 的配置
      并重建 ModelManager 单例的 embedder / reranker。
    - ``ocr`` / ``asr``：API 进程不持有常驻 Manager（降级路径每次重建），此处 no-op；
      Worker 进程由自身注册的 handler 对 pipeline 持有的 Manager 调
      ``reload_from_configs``（见 ``worker_main.py``）。

    任何异常只记 WARNING，不向上抛：配置已成功落库，重载失败不应让请求失败。
    """
    try:
        if capability in (CAPABILITY_EMBEDDING, CAPABILITY_RERANK):
            from app.startup import load_embed_configs

            await load_embed_configs()
            logger.info("能力配置本进程已重载: %s", capability)
        elif capability in (CAPABILITY_OCR, CAPABILITY_ASR):
            # API 进程无常驻 OCR/ASR Manager，无需动作
            logger.debug("能力配置 %s 本进程无常驻实例，跳过本地重载", capability)
        elif capability == CAPABILITY_MCP:
            # MCP 工具发现缓存是进程内模块级单例，直接失效，下次 get 重新查库发现
            from app.agent.tools.mcp_client import invalidate_mcp_tools_cache

            invalidate_mcp_tools_cache()
            logger.info("MCP 工具发现缓存已失效，下次请求重新发现")
        else:
            logger.warning("未知能力配置类型，跳过重载: %s", capability)
    except Exception as e:  # noqa: BLE001 — 重载失败不影响配置保存
        logger.warning("能力配置 %s 本进程重载失败: %s", capability, e)


async def apply_and_broadcast(capability: str) -> None:
    """让能力配置变更立即生效：本进程重载 + 广播通知其他进程

    调用时机必须在**数据库提交之后**，否则其他进程收到信号时读到的仍是旧数据。

    Args:
        capability: ``embedding`` / ``rerank`` / ``ocr`` / ``asr``
    """
    if capability not in VALID_CAPABILITIES:
        logger.warning("未知能力配置类型，跳过热生效: %s", capability)
        return

    await reload_capability_locally(capability)

    try:
        from app.storage.invalidation import get_invalidation_bus

        bus = get_invalidation_bus()
        if bus:
            await bus.publish(CAPABILITY_CHANNEL, capability)
            logger.info("能力配置变更已广播: %s", capability)
    except Exception as e:  # noqa: BLE001 — 广播失败降级为"仅本进程生效"
        logger.warning("能力配置 %s 广播失败（其他进程需重启才生效）: %s", capability, e)
