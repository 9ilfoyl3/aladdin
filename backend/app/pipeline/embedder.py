"""向量化管道节点

调用 EmbedProvider 对文本块批量生成稠密向量和稀疏向量，
支持可配置的批次大小以控制内存和并发。
"""

import logging
import math
from dataclasses import dataclass, field

from app.models.provider import EmbedProvider

logger = logging.getLogger(__name__)


@dataclass
class EmbedResult:
    """向量化结果"""
    dense_vectors: list[list[float]]        # 稠密向量列表
    sparse_vectors: list[dict[int, float]]  # 稀疏向量列表


class PipelineEmbedder:
    """管道向量化节点，封装 EmbedProvider 的批量调用"""

    def __init__(self, embed_provider: EmbedProvider, batch_size: int = 32):
        """初始化向量化器

        Args:
            embed_provider: 向量嵌入模型 Provider 实例
            batch_size: 每批处理的文本数量，控制内存占用
        """
        self.provider = embed_provider
        self.batch_size = batch_size

    @staticmethod
    def _sanitize_texts(texts: list[str]) -> list[str]:
        """清洗文本列表，将空白文本替换为占位符以避免模型产生 NaN"""
        sanitized = []
        for t in texts:
            cleaned = t.strip()
            if not cleaned:
                cleaned = "empty"
            sanitized.append(cleaned)
        return sanitized

    @staticmethod
    def _fix_nan_vector(vector: list[float]) -> list[float]:
        """将向量中的 NaN/Inf 值替换为 0.0"""
        return [0.0 if (math.isnan(v) or math.isinf(v)) else v for v in vector]

    @staticmethod
    def _fix_nan_sparse(sparse: dict[int, float]) -> dict[int, float]:
        """将稀疏向量中的 NaN/Inf 值移除，确保不为空"""
        cleaned = {k: v for k, v in sparse.items() if not (math.isnan(v) or math.isinf(v))}
        # Milvus 不接受空稀疏向量，填充一个最小占位值
        if not cleaned:
            cleaned = {0: 1e-30}
        return cleaned

    async def embed(self, texts: list[str]) -> EmbedResult:
        """批量生成稠密+稀疏向量

        将输入文本按 batch_size 分批调用 EmbedProvider，
        合并所有批次结果后返回。

        Args:
            texts: 待向量化的文本列表

        Returns:
            EmbedResult 包含对应的稠密向量和稀疏向量
        """
        # 空输入直接返回
        if not texts:
            return EmbedResult(dense_vectors=[], sparse_vectors=[])

        # 清洗文本，避免空文本导致 NaN
        sanitized = self._sanitize_texts(texts)

        all_dense: list[list[float]] = []
        all_sparse: list[dict[int, float]] = []

        # 按批次处理
        for i in range(0, len(sanitized), self.batch_size):
            batch = sanitized[i:i + self.batch_size]
            dense = await self.provider.embed(batch)
            sparse = await self.provider.embed_sparse(batch)
            all_dense.extend(dense)
            all_sparse.extend(sparse)

        # 检测并修复 NaN 值
        has_nan = False
        for idx, vec in enumerate(all_dense):
            if any(math.isnan(v) or math.isinf(v) for v in vec):
                has_nan = True
                all_dense[idx] = self._fix_nan_vector(vec)

        for idx, sp in enumerate(all_sparse):
            if not sp or any(math.isnan(v) or math.isinf(v) for v in sp.values()):
                has_nan = True
                all_sparse[idx] = self._fix_nan_sparse(sp)

        if has_nan:
            logger.warning("检测到 embedding 结果包含 NaN 或空稀疏向量，已修复")

        return EmbedResult(dense_vectors=all_dense, sparse_vectors=all_sparse)
