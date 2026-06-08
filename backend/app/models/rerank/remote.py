"""远程 Rerank API Provider

通过 HTTP 调用外部 Rerank 服务。设计参照 WeKnora 的多后端兼容策略，
支持三种接口格式：
1. OpenAI 兼容格式（DashScope/千问等）：POST /v1/reranks，body: {model, query, documents, top_n}
2. 标准格式（TEI/Jina/Infinity）：POST /rerank，body: {query, documents/texts, top_n}
3. 自定义格式：POST /ranking_score，body: {query, candidate}

自动检测逻辑：
- URL 以 /v1 结尾 → OpenAI 兼容格式，拼接 /reranks
- URL 仅为 host:port → 标准 TEI/Jina/Infinity 格式，拼接 /rerank
- URL 含其他具体路径 → 自定义格式，直接 POST

阿里百炼 DashScope 裸域名会被自动归一化到 compatible-mode/v1（与 WeKnora 一致）。
"""

import asyncio
import logging

import httpx

from app.models.provider import RerankProvider

logger = logging.getLogger(__name__)

# 连接/超时类错误的重试次数与退避上限（参照 WeKnora doRequestWithRetry）
_MAX_RETRIES = 2
_BACKOFF_CAP_SECONDS = 8.0


def normalize_rerank_base_url(base_url: str) -> str:
    """归一化 base_url（参照 WeKnora）。

    - 去除末尾斜杠。
    - 阿里百炼 DashScope 裸域名补全为 ``/compatible-mode/v1``，走 OpenAI 兼容路由。
    - 其他服务保持原样。
    """
    url = (base_url or "").rstrip("/")
    if not url:
        return url
    host_and_path = url.split("://", 1)[-1]
    if "dashscope.aliyuncs.com" in host_and_path and "/compatible-mode" not in host_and_path:
        url = url + "/compatible-mode/v1"
    return url


def detect_rerank_format(base_url: str) -> str:
    """检测 Rerank API 格式（基于归一化后的 base_url）。

    Returns:
        "openai"   - OpenAI 兼容格式（URL 以 /v1 结尾，如 DashScope），拼接 /reranks
        "standard" - TEI/Jina/Infinity 标准格式（仅 host:port），拼接 /rerank
        "custom"   - 自定义端点（含其他具体路径），直接 POST
    """
    normalized = normalize_rerank_base_url(base_url)
    path = normalized.split("://", 1)[-1]  # 去掉协议
    segments = path.split("/")
    # 只有 host:port → TEI/Jina/Infinity 标准格式
    if len(segments) <= 1 or segments[-1] == "":
        return "standard"
    # 以 /v1 结尾 → OpenAI 兼容格式（拼接 /reranks，如 DashScope/千问）
    if segments[-1] == "v1":
        return "openai"
    # 含其他路径 → 自定义端点
    return "custom"


def rerank_url(base_url: str) -> str:
    """由 base_url 推导实际的 rerank 推理端点（归一化 + 按格式拼路径）。

    供 RemoteReranker 与「测试连通性」接口共用，保证两条路径拼出的 URL 完全一致。
    """
    normalized = normalize_rerank_base_url(base_url)
    fmt = detect_rerank_format(normalized)
    if fmt == "openai":
        return f"{normalized}/reranks"
    if fmt == "standard":
        return f"{normalized}/rerank"
    return normalized  # custom：直接用完整 URL


class RemoteReranker(RerankProvider):
    """远程 Rerank Provider，调用外部 API"""

    def __init__(self, base_url: str, model: str = "", api_key: str = "", timeout: float = 60.0):
        """初始化远程 Rerank Provider

        Args:
            base_url: 远程服务完整地址，支持多种格式：
                - OpenAI 兼容: https://dashscope.aliyuncs.com/compatible-api/v1
                - TEI/Jina 标准: http://10.30.1.3:8001
                - 自定义端点: http://10.30.1.3:8001/ranking_score
            model: 模型名称（可选，传给远程服务）
            api_key: API 密钥（可选）
            timeout: 请求超时时间（秒）
        """
        self.base_url = normalize_rerank_base_url(base_url)
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    async def rerank(
        self, query: str, documents: list[str], top_k: int = 10
    ) -> list[tuple[int, float]]:
        """调用远程服务对候选文档重排序

        自动检测接口格式：
        - URL 以 /v1 结尾 → OpenAI 兼容格式，POST /v1/reranks
        - URL 仅为 host:port → TEI/Jina 标准格式，POST /rerank
        - URL 含其他路径 → 自定义格式，直接 POST

        Args:
            query: 查询文本
            documents: 候选文档列表
            top_k: 返回前 k 个结果

        Returns:
            按分数降序排列的 (原始索引, 分数) 列表
        """
        if not documents:
            return []

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        fmt = detect_rerank_format(self.base_url)

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            if fmt == "custom":
                # 自定义格式：直接 POST 到完整 URL
                url = self.base_url
                payload = {
                    "query": query,
                    "candidate": documents,
                }
            elif fmt == "openai":
                # OpenAI 兼容格式（DashScope/千问）：POST /v1/reranks
                # body: {model, query, documents, top_n}
                url = f"{self.base_url}/reranks"
                payload = {
                    "query": query,
                    "documents": documents,
                    "top_n": top_k,
                }
                if self.model:
                    payload["model"] = self.model
            else:
                # 标准格式（TEI/Jina/Infinity）：POST /rerank
                url = f"{self.base_url}/rerank"
                payload = {
                    "query": query,
                    "documents": documents,
                    "texts": documents,  # TEI 兼容（TEI 用 texts 字段）
                    "top_n": top_k,
                }
                if self.model:
                    payload["model"] = self.model

            resp = await self._post_with_retry(client, url, headers, payload)

            if resp.status_code != 200:
                body_preview = resp.text[:500] if resp.text else ""
                raise httpx.HTTPStatusError(
                    f"Rerank 服务返回 HTTP {resp.status_code}: {body_preview}",
                    request=resp.request,
                    response=resp,
                )
            data = resp.json()

        return self._parse_response(data, documents, top_k)

    async def _post_with_retry(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        payload: dict,
    ) -> httpx.Response:
        """POST 请求，仅对「连接/超时」类网络错误重试（指数退避）。

        HTTP 错误状态码不在此重试，交由调用方读取响应体定位。参照 WeKnora。
        """
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            if attempt > 0:
                backoff = min(2.0 ** (attempt - 1), _BACKOFF_CAP_SECONDS)
                logger.info(
                    "RemoteReranker 重试请求 (%d/%d)，等待 %.1fs: %s",
                    attempt, _MAX_RETRIES, backoff, last_exc,
                )
                await asyncio.sleep(backoff)
            try:
                return await client.post(url, headers=headers, json=payload)
            except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadError,
                    httpx.RemoteProtocolError) as e:
                last_exc = e
                continue
        assert last_exc is not None
        raise last_exc

    def _parse_response(self, data: dict | list, documents: list[str], top_k: int) -> list[tuple[int, float]]:
        """解析响应，兼容多种返回格式"""
        results: list[tuple[int, float]] = []

        if isinstance(data, list):
            # 直接返回分数列表：[0.1, 0.9, 0.5] → 与 documents 一一对应
            for idx, score in enumerate(data):
                if isinstance(score, (int, float)):
                    results.append((idx, float(score)))
        elif isinstance(data, dict):
            # 尝试多种 key：ranking_scores, results, data, scores
            items = data.get("ranking_scores", data.get("results", data.get("data", data.get("scores", []))))
            if isinstance(items, list):
                if items and isinstance(items[0], (int, float)):
                    # 分数列表：与 documents 一一对应
                    for idx, score in enumerate(items):
                        results.append((idx, float(score)))
                else:
                    # 对象列表：{index, relevance_score/score}
                    for item in items:
                        if isinstance(item, dict):
                            index = item.get("index", 0)
                            score = item.get("relevance_score", item.get("score", 0.0))
                            results.append((index, float(score)))

        # 如果解析失败，返回原始顺序
        if not results:
            logger.warning("Rerank 响应解析失败，返回原始顺序。响应: %s", str(data)[:200])
            results = [(i, 1.0 / (i + 1)) for i in range(len(documents))]

        # 按分数降序排列
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]
