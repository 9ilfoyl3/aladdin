"""bge-m3 本地 Embedding 实现

使用 FlagEmbedding 库加载 BGEM3FlagModel，
提供稠密向量（dim=1024）和稀疏向量生成能力。
通过 asyncio.to_thread 包装同步调用以兼容异步接口。
"""

import asyncio
import os

# 强制 HuggingFace 离线模式，避免联网检查模型更新
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

from FlagEmbedding import BGEM3FlagModel

from app.models.provider import EmbedProvider


class BgeM3Embedder(EmbedProvider):
    """bge-m3 嵌入模型 Provider，本地推理"""

    def __init__(self, model_name: str = "BAAI/bge-m3", device: str = "cuda"):
        """初始化 bge-m3 模型

        Args:
            model_name: 模型名称或路径
            device: 推理设备，cuda 或 cpu
        """
        self.model_name = model_name
        self.device = device
        # use_fp16 仅在 cuda 设备上启用
        use_fp16 = device == "cuda"
        self._model = BGEM3FlagModel(model_name, use_fp16=use_fp16, device=device)
        # 缓存最近一次编码结果，避免 embed 和 embed_sparse 重复计算
        self._last_texts: list[str] | None = None
        self._last_output: dict | None = None

    def _encode(self, texts: list[str]) -> dict:
        """同步编码文本，返回包含稠密和稀疏向量的字典"""
        return self._model.encode(texts, return_dense=True, return_sparse=True)

    async def _get_output(self, texts: list[str]) -> dict:
        """获取编码结果，使用缓存避免重复计算"""
        if self._last_texts is not None and self._last_texts == texts:
            return self._last_output
        output = await asyncio.to_thread(self._encode, texts)
        self._last_texts = texts
        self._last_output = output
        return output

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """生成稠密向量（dim=1024）

        Args:
            texts: 待编码文本列表

        Returns:
            稠密向量列表，每个向量为 1024 维 float 列表
        """
        output = await self._get_output(texts)
        # dense_vecs 为 numpy 数组，转为 Python list
        return output["dense_vecs"].tolist()

    async def embed_sparse(self, texts: list[str]) -> list[dict[int, float]]:
        """生成稀疏向量（用于稀疏检索）

        Args:
            texts: 待编码文本列表

        Returns:
            稀疏向量列表，每个元素为 {token_id: weight} 字典
        """
        output = await self._get_output(texts)
        # lexical_weights 为 list[dict]，键为 token_id(int)，值为权重(float)
        return output["lexical_weights"]
