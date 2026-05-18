"""sentence-transformers Embedding 实现

基于 sentence-transformers 库加载模型，跨平台兼容（Mac/Windows/Linux）。
提供稠密向量生成能力，稀疏向量返回占位值（稀疏检索建议改用 BM25）。
"""

import asyncio
import os

# 强制 HuggingFace 离线模式，避免联网检查模型更新
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

from sentence_transformers import SentenceTransformer

from app.models.provider import EmbedProvider


class SentenceTransformerEmbedder(EmbedProvider):
    """基于 sentence-transformers 的嵌入模型 Provider，跨平台本地推理"""

    def __init__(self, model_name: str = "BAAI/bge-m3", device: str = "cpu"):
        """初始化 sentence-transformers 模型

        Args:
            model_name: 模型名称或本地路径
            device: 推理设备，cuda / cpu / mps
        """
        self.model_name = model_name
        self.device = device
        self._model = SentenceTransformer(model_name, device=device)

    def _encode(self, texts: list[str]) -> list[list[float]]:
        """同步编码文本，返回稠密向量"""
        embeddings = self._model.encode(texts, normalize_embeddings=True)
        return embeddings.tolist()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """生成稠密向量

        Args:
            texts: 待编码文本列表

        Returns:
            稠密向量列表
        """
        return await asyncio.to_thread(self._encode, texts)

    async def embed_sparse(self, texts: list[str]) -> list[dict[int, float]]:
        """生成稀疏向量（占位实现）

        sentence-transformers 不原生支持 lexical weights 稀疏向量，
        返回最小占位值以兼容 Milvus 稀疏字段要求。
        实际稀疏检索建议使用 Milvus 内置 BM25 或切换到 flag-embedding provider。

        Args:
            texts: 待编码文本列表

        Returns:
            占位稀疏向量列表
        """
        return [{0: 1e-30} for _ in texts]
