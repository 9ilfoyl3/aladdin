"""远程 Embedding API Provider

通过 HTTP 调用外部 Embedding 服务（兼容 OpenAI /v1/embeddings 接口格式）。
适用于目标环境已部署独立 Embedding 服务的场景（如 TEI、Infinity、vLLM 等）。
"""

import logging

import httpx

from app.models.provider import EmbedProvider

logger = logging.getLogger(__name__)


class RemoteEmbedder(EmbedProvider):
    """远程 Embedding Provider，调用外部 OpenAI 兼容 API"""

    def __init__(self, base_url: str, model: str = "BAAI/bge-m3", api_key: str = "", timeout: float = 60.0):
        """初始化远程 Embedding Provider

        Args:
            base_url: 远程服务地址（如 http://embedding-server:8080/v1）
            model: 模型名称，传给远程服务
            api_key: API 密钥（可选）
            timeout: 请求超时时间（秒）
        """
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        """获取复用的 httpx 客户端，支持连接池"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        return self._client

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """调用远程服务生成稠密向量

        Args:
            texts: 待编码文本列表

        Returns:
            稠密向量列表
        """
        import sys
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        client = self._get_client()
        print(f"[RemoteEmbedder] 发送请求: {len(texts)} 个文本, url={self.base_url}/embeddings, timeout={self.timeout}s")
        sys.stdout.flush()
        resp = await client.post(
            f"{self.base_url}/embeddings",
            headers=headers,
            json={"input": texts, "model": self.model},
        )
        resp.raise_for_status()
        data = resp.json()
        print(f"[RemoteEmbedder] 请求返回: status={resp.status_code}, 向量数={len(data['data'])}")
        sys.stdout.flush()
        # OpenAI 格式：data[].embedding
        return [item["embedding"] for item in data["data"]]

    async def embed_sparse(self, texts: list[str]) -> list[dict[int, float]]:
        """生成稀疏向量（远程服务通常不提供，返回占位值）

        Args:
            texts: 待编码文本列表

        Returns:
            占位稀疏向量列表
        """
        return [{0: 1e-30} for _ in texts]
