"""远程 Embedding API Provider

通过 HTTP 调用外部 Embedding 服务。设计参照 WeKnora 的多后端兼容策略：

- Dense 向量：统一走 OpenAI 兼容的 ``{base_url}/embeddings`` 端点。
  适配 TEI、Infinity、vLLM、SiliconFlow、OpenAI、阿里百炼（DashScope compatible-mode）等。
  ★ 关键约定：用户填写的 ``base_url`` 已包含正确前缀，本类只负责拼 ``/embeddings``：
      - OpenAI:   https://api.openai.com/v1        → /v1/embeddings
      - Infinity: http://host:7997                 → /embeddings（根路径，无 /v1）
      - TEI:      http://host/v1                    → /v1/embeddings
      - 百炼:     .../compatible-mode/v1            → /compatible-mode/v1/embeddings
  DashScope 裸域名会被自动归一化到 compatible-mode/v1（与 WeKnora 一致）。
- Sparse 向量：兼容 TEI ``/embed_sparse`` 端点格式（BGE-M3 lexical weights），
  远程服务不支持时自动降级为占位值。

注意：Ollama(``/api/embed``)、Azure(``/openai/deployments/...``)、DashScope 多模态
等非 OpenAI 兼容格式不在本类适配范围内（需独立 Provider）。
"""

import asyncio
import logging
import sys

import httpx

from app.models.provider import EmbedProvider

logger = logging.getLogger(__name__)

# 连接/超时类错误的重试次数与退避上限（参照 WeKnora doRequestWithRetry）
_MAX_RETRIES = 2
_BACKOFF_CAP_SECONDS = 8.0


def normalize_embed_base_url(base_url: str) -> str:
    """归一化 base_url，处理已知服务商的特殊约定（参照 WeKnora）。

    - 去除末尾斜杠。
    - 阿里百炼 DashScope：裸域名补全为 ``/compatible-mode/v1``，使其走 OpenAI 兼容路由。
      （多模态模型不在本类适配范围，调用方应拦截。）
    - 其他服务（TEI/Infinity/vLLM/OpenAI/自建）保持原样，由用户负责填到正确前缀。
    """
    url = (base_url or "").rstrip("/")
    if not url:
        return url
    host_and_path = url.split("://", 1)[-1]
    if "dashscope.aliyuncs.com" in host_and_path and "/compatible-mode" not in host_and_path:
        # 裸 DashScope 域名 → 补全 OpenAI 兼容前缀
        url = url + "/compatible-mode/v1"
    return url


def embeddings_url(base_url: str) -> str:
    """由 base_url 推导实际的 embeddings 推理端点（归一化 + 拼 /embeddings）。

    供 RemoteEmbedder 与「测试连通性」接口共用，保证两条路径拼出的 URL 完全一致。
    """
    return f"{normalize_embed_base_url(base_url)}/embeddings"


class RemoteEmbedder(EmbedProvider):
    """远程 Embedding Provider，支持 Dense + Sparse 双路输出

    Dense 调用 OpenAI 兼容的 /embeddings 端点；
    Sparse 调用 TEI 兼容的 /embed_sparse 端点（需服务端支持）。
    当远程服务不支持 sparse 时自动降级为占位值。
    """

    def __init__(
        self,
        base_url: str,
        model: str = "BAAI/bge-m3",
        api_key: str = "",
        timeout: float = 60.0,
        sparse_enabled: bool = True,
        max_connections: int = 20,
    ):
        """初始化远程 Embedding Provider

        Args:
            base_url: 远程服务地址（如 http://embedding-server:8080/v1）
            model: 模型名称，传给远程服务
            api_key: API 密钥（可选）
            timeout: 请求超时时间（秒）
            sparse_enabled: 是否启用 sparse 向量（调用 /embed_sparse 端点）
            max_connections: httpx 连接池上限
        """
        self.base_url = normalize_embed_base_url(base_url)
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.sparse_enabled = sparse_enabled
        self.max_connections = max_connections
        self._client: httpx.AsyncClient | None = None
        # sparse 端点可用性缓存：None=未探测, True=可用, False=不可用
        self._sparse_available: bool | None = None

    def _get_client(self) -> httpx.AsyncClient:
        """获取复用的 httpx 客户端，支持连接池"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.timeout if self.timeout and self.timeout > 0 else None,
                limits=httpx.Limits(max_connections=self.max_connections, max_keepalive_connections=self.max_connections // 2),
            )
        return self._client

    def _get_headers(self) -> dict[str, str]:
        """构造请求头"""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _get_sparse_url(self) -> str:
        """获取 sparse 端点 URL

        直接在 base_url 后拼接 /embed_sparse
        例如：http://server:7997/v1 → http://server:7997/v1/embed_sparse
        """
        return f"{self.base_url}/embed_sparse"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """调用远程服务生成稠密向量（OpenAI 兼容 /embeddings）。

        - 连接/超时类错误按指数退避重试（参照 WeKnora doRequestWithRetry）。
        - 非 2xx 响应抛错时带上响应体，便于定位 4xx/5xx 的真实原因（如 422 字段不符）。
        - 响应解析兼容 OpenAI 与 DashScope 两种主流格式。

        Args:
            texts: 待编码文本列表

        Returns:
            稠密向量列表（顺序与输入一致）
        """
        headers = self._get_headers()
        client = self._get_client()
        url = embeddings_url(self.base_url)
        payload = {"input": texts, "model": self.model, "encoding_format": "float"}

        print(f"[RemoteEmbedder] Dense 请求: {len(texts)} 个文本, url={url}")
        sys.stdout.flush()

        resp = await self._post_with_retry(client, url, headers, payload)

        if resp.status_code != 200:
            body_preview = resp.text[:500] if resp.text else ""
            raise httpx.HTTPStatusError(
                f"Embedding 服务返回 HTTP {resp.status_code}: {body_preview}",
                request=resp.request,
                response=resp,
            )

        data = resp.json()
        vectors = self._parse_dense_response(data, len(texts))
        print(f"[RemoteEmbedder] Dense 返回: status={resp.status_code}, 向量数={len(vectors)}")
        sys.stdout.flush()
        return vectors

    async def _post_with_retry(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        payload: dict,
    ) -> httpx.Response:
        """POST 请求，仅对「连接/超时」类网络错误重试（指数退避）。

        HTTP 错误状态码（4xx/5xx）不在此重试——那是服务端语义错误，重试无意义，
        交由调用方读取响应体定位。参照 WeKnora：仅 client.Do 失败才重试。
        """
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            if attempt > 0:
                backoff = min(2.0 ** (attempt - 1), _BACKOFF_CAP_SECONDS)
                logger.info(
                    "RemoteEmbedder 重试请求 (%d/%d)，等待 %.1fs: %s",
                    attempt, _MAX_RETRIES, backoff, last_exc,
                )
                await asyncio.sleep(backoff)
            try:
                return await client.post(url, headers=headers, json=payload)
            except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadError,
                    httpx.RemoteProtocolError) as e:
                last_exc = e
                continue
        # 重试用尽，抛出最后一次的网络错误
        assert last_exc is not None
        raise last_exc

    def _parse_dense_response(self, data, expected_count: int) -> list[list[float]]:
        """解析 dense 响应，兼容多种格式：

        1. OpenAI / TEI / Infinity / vLLM：{"data": [{"embedding": [...], "index": n}, ...]}
        2. 阿里百炼 DashScope（原生）：{"output": {"embeddings": [{"embedding": [...], "text_index": n}]}}
        3. 裸数组兜底：[[...], [...]]
        """
        # 格式 1：OpenAI 兼容
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            items = data["data"]
            # 按 index 排序，确保顺序与输入一致（OpenAI 保证返回 index）
            try:
                items = sorted(items, key=lambda it: it.get("index", 0))
            except (AttributeError, TypeError):
                pass
            return [item["embedding"] for item in items]

        # 格式 2：DashScope 原生
        if isinstance(data, dict) and isinstance(data.get("output"), dict):
            embs = data["output"].get("embeddings", [])
            result: list[list[float]] = [[] for _ in range(expected_count)]
            for emb in embs:
                idx = emb.get("text_index", 0)
                if 0 <= idx < expected_count:
                    result[idx] = emb.get("embedding", [])
            return result

        # 格式 3：裸数组兜底
        if isinstance(data, list) and (not data or isinstance(data[0], list)):
            return data

        raise ValueError(f"无法解析 Embedding 响应格式: {str(data)[:200]}")

    async def embed_sparse(self, texts: list[str]) -> list[dict[int, float]]:
        """调用远程服务生成稀疏向量（BGE-M3 lexical weights）

        兼容 TEI /embed_sparse 端点格式：
        - 请求：{"inputs": ["text1", "text2"], "model": "..."}
        - 响应：[[{"index": 12345, "value": 0.82}, ...], [...]]

        当远程服务不支持 sparse 端点时，自动降级为占位值并缓存状态，
        后续调用不再尝试请求，避免重复超时。

        Args:
            texts: 待编码文本列表

        Returns:
            稀疏向量列表，每个元素为 {token_id: weight} 字典
        """
        # 未启用 sparse 或已探测到不可用，直接返回占位值
        if not self.sparse_enabled or self._sparse_available is False:
            return [{0: 1e-30} for _ in texts]

        headers = self._get_headers()
        client = self._get_client()
        sparse_url = self._get_sparse_url()

        try:
            print(f"[RemoteEmbedder] Sparse 请求: {len(texts)} 个文本, url={sparse_url}")
            sys.stdout.flush()
            resp = await client.post(
                sparse_url,
                headers=headers,
                json={"inputs": texts, "model": self.model},
            )
            resp.raise_for_status()
            data = resp.json()

            # 标记 sparse 端点可用
            if self._sparse_available is None:
                self._sparse_available = True
                print("[RemoteEmbedder] Sparse 端点探测成功，已启用稀疏向量")
                sys.stdout.flush()

            # 解析响应
            sparse_vectors = self._parse_sparse_response(data, len(texts))
            print(f"[RemoteEmbedder] Sparse 返回: {len(sparse_vectors)} 个向量")
            sys.stdout.flush()
            return sparse_vectors

        except httpx.HTTPStatusError as e:
            if e.response.status_code in (404, 405, 501):
                # 端点不存在或不支持，标记为不可用
                self._sparse_available = False
                logger.warning(
                    "远程服务不支持 /embed_sparse 端点 (HTTP %d)，已降级为占位值",
                    e.response.status_code,
                )
                return [{0: 1e-30} for _ in texts]
            raise
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            # 网络问题不标记为永久不可用（可能是临时故障）
            logger.warning("Sparse 端点请求失败（网络问题），本次降级: %s", e)
            return [{0: 1e-30} for _ in texts]

    def _parse_sparse_response(self, data: list | dict, expected_count: int) -> list[dict[int, float]]:
        """解析 sparse 响应，兼容多种格式

        支持的格式：
        1. TEI 格式：[[{"index": 123, "value": 0.8}, ...], ...]
        2. 字典列表格式：[{"token_id": weight, ...}, ...]
        3. 嵌套在 data 字段中：{"data": [...]}
        """
        # 如果响应包裹在 data 字段中
        if isinstance(data, dict):
            data = data.get("data", data.get("results", []))

        if not isinstance(data, list):
            logger.warning("Sparse 响应格式异常，返回占位值")
            return [{0: 1e-30} for _ in range(expected_count)]

        sparse_vectors: list[dict[int, float]] = []

        for item in data:
            if isinstance(item, list):
                # TEI 格式：[{"index": 123, "value": 0.8}, ...]
                vec: dict[int, float] = {}
                for entry in item:
                    if isinstance(entry, dict):
                        idx = entry.get("index", entry.get("token_id", 0))
                        val = entry.get("value", entry.get("weight", 0.0))
                        if val > 0:
                            vec[int(idx)] = float(val)
                sparse_vectors.append(vec if vec else {0: 1e-30})
            elif isinstance(item, dict):
                # 直接是 {token_id: weight} 字典
                vec = {int(k): float(v) for k, v in item.items() if float(v) > 0}
                sparse_vectors.append(vec if vec else {0: 1e-30})
            else:
                sparse_vectors.append({0: 1e-30})

        # 补齐数量（防止响应数量不匹配）
        while len(sparse_vectors) < expected_count:
            sparse_vectors.append({0: 1e-30})

        return sparse_vectors[:expected_count]

    async def check_sparse_support(self) -> bool:
        """主动探测远程服务是否支持 sparse 端点

        用于配置测试时验证服务能力。

        Returns:
            True 表示支持，False 表示不支持
        """
        if not self.sparse_enabled:
            return False

        try:
            result = await self.embed_sparse(["测试"])
            # 检查返回的是否是真实 sparse 向量（非占位值）
            if result and result[0] != {0: 1e-30}:
                return True
            # 如果返回占位值，可能是探测失败
            return self._sparse_available is True
        except Exception:
            return False
