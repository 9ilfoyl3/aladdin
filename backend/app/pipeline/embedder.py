"""向量化管道节点

调用 EmbedProvider 对文本块批量生成稠密向量和稀疏向量，
支持可配置的批次大小和并发数以控制内存和吞吐。
"""

import asyncio
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

    # embedding 模型的最大输入字符数
    # BGE-M3 支持 8192 tokens，中文约 1.5 字符/token，保守取 8000 字符
    MAX_EMBED_CHARS = 8000

    def __init__(self, embed_provider: EmbedProvider, batch_size: int = 128, concurrency: int = 8):
        """初始化向量化器

        Args:
            embed_provider: 向量嵌入模型 Provider 实例
            batch_size: 每批处理的文本数量，控制内存占用
            concurrency: 并发请求数，控制对 embedding 服务的并行调用
        """
        self.provider = embed_provider
        self.batch_size = batch_size
        self.concurrency = concurrency

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

    def _truncate_texts(self, texts: list[str]) -> list[str]:
        """截断超长文本，确保不超过 embedding 模型的输入限制

        截断时保留前部内容（通常包含表头和关键信息）。
        """
        truncated = []
        truncate_count = 0
        for t in texts:
            if len(t) > self.MAX_EMBED_CHARS:
                truncated.append(t[:self.MAX_EMBED_CHARS])
                truncate_count += 1
            else:
                truncated.append(t)
        if truncate_count > 0:
            print(f"[Embedder] 截断了 {truncate_count}/{len(texts)} 个超长文本 (>{self.MAX_EMBED_CHARS} 字符)")
        return truncated

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

        将输入文本按 batch_size 分批，并发调用 EmbedProvider，
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

        # 截断超长文本，避免超过 embedding 模型输入限制
        sanitized = self._truncate_texts(sanitized)

        total_batches = (len(sanitized) + self.batch_size - 1) // self.batch_size
        print(f"[Embedder] 开始 embedding，共 {len(sanitized)} 个文本块，分 {total_batches} 批处理 (batch_size={self.batch_size}, 并发={self.concurrency})")

        # 构建批次
        batches = []
        for i in range(0, len(sanitized), self.batch_size):
            batches.append(sanitized[i:i + self.batch_size])

        # 并发处理所有批次，用 semaphore 控制并发数
        semaphore = asyncio.Semaphore(self.concurrency)
        results: list[tuple[list[list[float]], list[dict[int, float]]]] = [None] * len(batches)
        completed_count = 0
        progress_lock = asyncio.Lock()

        async def _process_batch(batch_idx: int, batch: list[str]):
            nonlocal completed_count
            async with semaphore:
                dense = await self.provider.embed(batch)
                sparse = await self.provider.embed_sparse(batch)
                results[batch_idx] = (dense, sparse)
            # 进度报告
            async with progress_lock:
                completed_count += 1
                if total_batches > 10 and completed_count % max(1, total_batches // 10) == 0:
                    print(f"[Embedder] 进度: {completed_count}/{total_batches} 批 ({completed_count * 100 // total_batches}%)")

        # 一次性提交所有任务，semaphore 自动控制并发窗口
        await asyncio.gather(*[_process_batch(i, batch) for i, batch in enumerate(batches)])

        # 合并结果（保持顺序）
        all_dense: list[list[float]] = []
        all_sparse: list[dict[int, float]] = []
        for dense, sparse in results:
            all_dense.extend(dense)
            all_sparse.extend(sparse)

        print(f"[Embedder] embedding 完成，共生成 {len(all_dense)} 个向量")

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
