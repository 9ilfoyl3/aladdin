"""Embedding/Rerank 服务探活

统一的连通性探测策略，供「连通性测试接口」和「Worker 启动健康检查」共用，
保证两条路径行为一致。

核心策略（兼顾自建服务与云端网关）：
1. 优先探测 /health 端点 —— 自建服务（TEI / Infinity / embedding-rerank-server）
   都暴露 /health，且该端点不进推理队列，即使推理队列打满也能正常探活，不会卡住。
2. 若 /health 不存在（404/405）—— 多为云端 OpenAI 兼容网关（如阿里云百炼 DashScope），
   它们只暴露推理端点。云端服务是弹性的，不存在"队列打满卡住"问题（过载返回 429），
   因此降级为发一条最小推理请求来验证。

这样：
- 自建服务 + 队列打满：走 /health，秒回，不占推理队列。
- 云端网关：/health 404 → 改发最小推理请求，正常验证。
- 服务真正不可达：连接报错，明确判定为下线。
"""

import logging

import httpx

logger = logging.getLogger(__name__)


def health_url(base_url: str) -> str:
    """由服务地址推导 /health 端点

    去掉末尾的 /v1 路径段（若有）再拼 /health：
    - http://server:8080/v1 → http://server:8080/health
    - http://server:7997     → http://server:7997/health（infinity 根路径，无 /v1）

    仅当 /v1 是「末尾的完整路径段」时才剥离，避免误伤 host 或 /v1beta 这类子串。
    """
    trimmed = base_url.rstrip("/")
    if trimmed.endswith("/v1"):
        trimmed = trimmed[: -len("/v1")]
    return trimmed + "/health"


async def check_health(base_url: str, timeout: float = 5.0) -> bool | None:
    """探测 /health 端点

    Args:
        base_url: 服务地址（填到 /v1 或裸 host:port）
        timeout: 探活超时（秒）

    Returns:
        True  - /health 返回 200 且状态健康（自建服务在线）
        False - /health 可达但状态未就绪（明确不健康）
        None  - 无 /health 路由（404/405），多为云端网关，调用方应改用最小推理请求探活

    Raises:
        httpx.ConnectError / httpx.TimeoutException - host 层面不可达，由调用方处理
    """
    url = health_url(base_url)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url)

    if resp.status_code == 200:
        # 兼容多种 health 响应格式：
        # - embedding-rerank-server: {"status": "ready"}
        # - Infinity: {"unix": 1748490407.766}（200 即健康）
        # - TEI: 200 即健康（响应体可能为空或纯文本）
        try:
            data = resp.json()
        except (ValueError, TypeError):
            data = {}
        status = data.get("status") if isinstance(data, dict) else None
        if status and status not in ("ready", "ok"):
            return False
        return True

    # 无 /health 路由 → 云端网关，交给调用方降级为推理探活
    if resp.status_code in (404, 405):
        return None

    # 其他状态码（500 等）视为不健康
    return False
