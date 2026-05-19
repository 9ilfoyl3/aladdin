"""bge-m3 本地 Embedding 实现

使用 sentence-transformers 加载 BAAI/bge-m3 模型，
提供稠密向量（dim=1024）和稀疏向量生成能力。
通过 asyncio.to_thread 包装同步调用以兼容异步接口。

兼容 Windows / macOS / Linux 全平台。
"""

import os

# 强制离线模式：模型已下载到本地，不联网检查更新
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import asyncio
import os
from collections import defaultdict

import torch
from transformers import AutoTokenizer
from sentence_transformers import SentenceTransformer

from app.models.provider import EmbedProvider


class BgeM3Embedder(EmbedProvider):
    """bge-m3 嵌入模型 Provider，本地推理（基于 sentence-transformers）"""

    def __init__(self, model_name: str = "BAAI/bge-m3", device: str = "cuda"):
        """初始化 bge-m3 模型

        Args:
            model_name: 模型名称或本地路径
            device: 推理设备，cuda 或 cpu
        """
        self.model_name = model_name
        self.device = device

        # 加载 sentence-transformers 模型（稠密向量）
        self._model = SentenceTransformer(model_name, device=device)

        # 加载 tokenizer（用于生成稀疏向量）
        self._tokenizer = AutoTokenizer.from_pretrained(model_name)

        # 缓存最近一次编码结果，避免 embed 和 embed_sparse 重复计算
        self._last_texts: list[str] | None = None
        self._last_dense: list[list[float]] | None = None
        self._last_sparse: list[dict[int, float]] | None = None

    def _encode_dense(self, texts: list[str]) -> list[list[float]]:
        """同步生成稠密向量"""
        embeddings = self._model.encode(
            texts,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return embeddings.tolist()

    def _encode_sparse(self, texts: list[str]) -> list[dict[int, float]]:
        """同步生成稀疏向量（基于词频的 BM25 风格表示）

        使用 tokenizer 将文本分词，统计 token 出现频率作为权重。
        这是对 bge-m3 lexical weights 的近似实现。
        """
        sparse_vectors: list[dict[int, float]] = []

        for text in texts:
            # tokenize 获取 token ids
            encoding = self._tokenizer(
                text,
                add_special_tokens=False,
                return_attention_mask=False,
            )
            token_ids = encoding["input_ids"]

            # 统计词频作为权重
            token_weights: dict[int, float] = defaultdict(float)
            for tid in token_ids:
                token_weights[tid] += 1.0

            # 归一化权重
            if token_weights:
                max_weight = max(token_weights.values())
                token_weights = {
                    tid: w / max_weight for tid, w in token_weights.items()
                }

            sparse_vectors.append(dict(token_weights))

        return sparse_vectors

    def _encode_all(self, texts: list[str]) -> tuple[list[list[float]], list[dict[int, float]]]:
        """同步编码：同时生成稠密和稀疏向量"""
        dense = self._encode_dense(texts)
        sparse = self._encode_sparse(texts)
        return dense, sparse

    async def _get_outputs(self, texts: list[str]) -> tuple[list[list[float]], list[dict[int, float]]]:
        """获取编码结果，使用缓存避免重复计算"""
        if self._last_texts is not None and self._last_texts == texts:
            return self._last_dense, self._last_sparse

        dense, sparse = await asyncio.to_thread(self._encode_all, texts)
        self._last_texts = texts
        self._last_dense = dense
        self._last_sparse = sparse
        return dense, sparse

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """生成稠密向量（dim=1024）

        Args:
            texts: 待编码文本列表

        Returns:
            稠密向量列表，每个向量为 1024 维 float 列表
        """
        dense, _ = await self._get_outputs(texts)
        return dense

    async def embed_sparse(self, texts: list[str]) -> list[dict[int, float]]:
        """生成稀疏向量（用于稀疏检索）

        Args:
            texts: 待编码文本列表

        Returns:
            稀疏向量列表，每个元素为 {token_id: weight} 字典
        """
        _, sparse = await self._get_outputs(texts)
        return sparse
