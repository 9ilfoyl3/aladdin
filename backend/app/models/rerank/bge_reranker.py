"""bge-reranker-v2-m3 本地重排序实现

使用 FlagEmbedding 库加载 FlagReranker，
对候选文档按相关性重新排序。
通过 asyncio.to_thread 包装同步调用以兼容异步接口。

分数经 sigmoid 归一化到 [0, 1] 区间，便于设置统一阈值和跨查询比较。

注意：FlagReranker 底层 tokenizer 非线程安全，
并发调用会触发 "Already borrowed" 错误，需用锁串行化。
"""

import asyncio
import math
import threading

from FlagEmbedding import FlagReranker

from app.models.provider import RerankProvider


def _sigmoid(x: float) -> float:
    """Sigmoid 归一化：将 reranker 原始 logit 映射到 [0, 1] 概率区间"""
    return 1.0 / (1.0 + math.exp(-x))


class BgeReranker(RerankProvider):
    """bge-reranker 重排序 Provider，本地推理，分数经 sigmoid 归一化"""

    def __init__(self, model_name: str = "BAAI/bge-reranker-v2-m3", device: str = "cuda"):
        """初始化 reranker 模型

        Args:
            model_name: 模型名称或路径
            device: 推理设备，cuda 或 cpu
        """
        self.model_name = model_name
        self.device = device
        # use_fp16 仅在 cuda 设备上启用
        use_fp16 = device == "cuda"
        self._model = FlagReranker(model_name, use_fp16=use_fp16, device=device)
        # FlagReranker 的 tokenizer 非线程安全，并发调用需串行化
        self._lock = threading.Lock()

    def _compute(self, query: str, documents: list[str]) -> list[float]:
        """同步计算 query 与每个文档的相关性分数（线程安全）

        返回 sigmoid 归一化后的分数，范围 [0, 1]。
        """
        pairs = [[query, doc] for doc in documents]
        with self._lock:
            raw_scores = self._model.compute_score(pairs)
        # 单文档时返回 float，统一转为 list
        if isinstance(raw_scores, (int, float)):
            raw_scores = [raw_scores]
        # sigmoid 归一化
        return [_sigmoid(s) for s in raw_scores]

    async def rerank(
        self, query: str, documents: list[str], top_k: int = 10
    ) -> list[tuple[int, float]]:
        """对候选文档重排序

        Args:
            query: 查询文本
            documents: 候选文档列表
            top_k: 返回前 k 个结果

        Returns:
            按分数降序排列的 (原始索引, sigmoid归一化分数) 列表
        """
        if not documents:
            return []

        scores = await asyncio.to_thread(self._compute, query, documents)
        # 组合索引和分数，按分数降序排列
        indexed_scores = list(enumerate(scores))
        indexed_scores.sort(key=lambda x: x[1], reverse=True)
        return indexed_scores[:top_k]
