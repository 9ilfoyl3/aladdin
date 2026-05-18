"""CrossEncoder 重排序实现

基于 sentence-transformers 的 CrossEncoder，跨平台兼容（Mac/Windows/Linux）。
对候选文档按相关性重新排序，分数经 sigmoid 归一化到 [0, 1] 区间。
"""

import asyncio
import math
import os
import threading

# 强制 HuggingFace 离线模式，避免联网检查模型更新
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

from sentence_transformers import CrossEncoder

from app.models.provider import RerankProvider


def _sigmoid(x: float) -> float:
    """Sigmoid 归一化：将原始 logit 映射到 [0, 1] 概率区间"""
    return 1.0 / (1.0 + math.exp(-x))


class CrossEncoderReranker(RerankProvider):
    """基于 sentence-transformers CrossEncoder 的重排序 Provider，跨平台本地推理"""

    def __init__(self, model_name: str = "BAAI/bge-reranker-v2-m3", device: str = "cpu"):
        """初始化 CrossEncoder 模型

        Args:
            model_name: 模型名称或本地路径
            device: 推理设备，cuda / cpu / mps
        """
        self.model_name = model_name
        self.device = device
        self._model = CrossEncoder(model_name, device=device)
        # CrossEncoder 的 tokenizer 非线程安全，并发调用需串行化
        self._lock = threading.Lock()

    def _compute(self, query: str, documents: list[str]) -> list[float]:
        """同步计算 query 与每个文档的相关性分数（线程安全）

        返回 sigmoid 归一化后的分数，范围 [0, 1]。
        """
        pairs = [(query, doc) for doc in documents]
        with self._lock:
            raw_scores = self._model.predict(pairs)
        # numpy array → list，sigmoid 归一化
        return [_sigmoid(float(s)) for s in raw_scores]

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
        indexed_scores = list(enumerate(scores))
        indexed_scores.sort(key=lambda x: x[1], reverse=True)
        return indexed_scores[:top_k]
