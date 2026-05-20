"""远程 Rerank API Provider

通过 HTTP 调用外部 Rerank 服务。
支持两种接口格式：
1. 标准格式（TEI/Jina）：POST /rerank，body: {query, documents, top_n}
2. 自定义格式：POST /ranking_score，body: {query, candidate}
"""

import logging

import httpx

from app.models.provider import RerankProvider

logger = logging.getLogger(__name__)


class RemoteReranker(RerankProvider):
    """远程 Rerank Provider，调用外部 API"""

    def __init__(self, base_url: str, model: str = "", api_key: str = "", timeout: float = 60.0):
        """初始化远程 Rerank Provider

        Args:
            base_url: 远程服务完整地址（如 http://10.30.1.3:8001/ranking_score 或 http://server:8080/v1）
            model: 模型名称（可选，传给远程服务）
            api_key: API 密钥（可选）
            timeout: 请求超时时间（秒）
        """
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def _is_custom_endpoint(self) -> bool:
        """判断是否为自定义端点（URL 以具体路径结尾，如 /ranking_score）"""
        # 如果 URL 以 /v1 结尾或不含特殊路径，认为是标准格式，需要拼接 /rerank
        path = self.base_url.split("://", 1)[-1]  # 去掉协议
        segments = path.split("/")
        # 只有 host:port 或以 /v1 结尾 → 标准格式
        if len(segments) <= 1 or segments[-1] in ("", "v1"):
            return False
        return True

    async def rerank(
        self, query: str, documents: list[str], top_k: int = 10
    ) -> list[tuple[int, float]]:
        """调用远程服务对候选文档重排序

        自动检测接口格式：
        - URL 含具体路径（如 /ranking_score）→ 直接 POST 到该 URL，body 用 {query, candidate}
        - URL 为基础地址 → POST /rerank，body 用标准格式 {query, documents, top_n}

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

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            if self._is_custom_endpoint():
                # 自定义格式：直接 POST 到完整 URL
                payload = {
                    "query": query,
                    "candidate": documents,
                }
                resp = await client.post(self.base_url, headers=headers, json=payload)
            else:
                # 标准格式：POST /rerank
                url = f"{self.base_url}/rerank"
                payload = {
                    "query": query,
                    "documents": documents,
                    "top_n": top_k,
                }
                if self.model:
                    payload["model"] = self.model
                resp = await client.post(url, headers=headers, json=payload)

            resp.raise_for_status()
            data = resp.json()

        return self._parse_response(data, documents, top_k)

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
