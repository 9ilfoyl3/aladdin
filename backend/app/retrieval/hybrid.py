"""混合检索器

结合稠密向量检索与稀疏向量检索，通过 RRF 融合排序，
再经 Rerank 精排，最后执行父块扩展以返回完整上下文。
"""

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.provider import RerankProvider
from app.retrieval.base import BaseRetriever, RetrievalResult
from app.schema.db import Chunk

logger = logging.getLogger(__name__)


class HybridRetriever(BaseRetriever):
    """混合检索器：稠密 + 稀疏 + RRF 融合 + Rerank + 父块扩展"""

    def __init__(
        self,
        vector_retriever: BaseRetriever,
        sparse_retriever: BaseRetriever,
        rerank_provider: RerankProvider,
        db_session_factory: async_sessionmaker[AsyncSession],
    ):
        self.vector_retriever = vector_retriever
        self.sparse_retriever = sparse_retriever
        self.reranker = rerank_provider
        self.db_session_factory = db_session_factory

    async def search(
        self, query: str, kb_id: str, top_k: int = 10, **kwargs
    ) -> list[RetrievalResult]:
        """执行混合检索

        流程：并行稠密+稀疏检索 → RRF 融合 → Rerank 精排 → 父块扩展

        kwargs:
            skip_rerank: 跳过 rerank 和父块扩展，仅返回 RRF 融合结果（用于批量合并后统一 rerank）
        """
        skip_rerank = kwargs.pop("skip_rerank", False)
        expanded_k = top_k * 3

        # 1. 并行执行稠密检索和稀疏检索
        dense_results, sparse_results = await asyncio.gather(
            self.vector_retriever.search(query, kb_id, top_k=expanded_k, **kwargs),
            self.sparse_retriever.search(query, kb_id, top_k=expanded_k, **kwargs),
        )
        print(f"[Retrieval] 稠密检索: {len(dense_results)} 条, 稀疏检索: {len(sparse_results)} 条")

        # 2. RRF 融合两路结果
        fused = self._rrf_fusion([dense_results, sparse_results])
        print(f"[Retrieval] RRF 融合后: {len(fused)} 条")

        if not fused:
            return []

        # 快速模式：跳过 rerank，直接返回 RRF 融合结果（供 executor 批量合并后统一 rerank）
        if skip_rerank:
            return fused

        # 3. Rerank 精排（只取融合结果前 top_k*2 条送入 rerank，减少计算量）
        rerank_candidates = fused[: top_k * 2]
        try:
            reranked = await self._rerank(query, rerank_candidates, top_k)
            print(f"[Retrieval] Rerank 后（阈值过滤）: {len(reranked)} 条")
        except Exception as e:
            logger.warning("Reranker 异常，跳过重排序: %s", e)
            reranked = fused[:top_k]

        # 4. 父块扩展：用父块内容替换子块内容
        expanded = await self._expand_parent(reranked)

        logger.debug("HybridRetriever 在 kb=%s 中检索到 %d 条结果", kb_id, len(expanded))
        return expanded

    async def rerank_and_expand(
        self, query: str, results: list[RetrievalResult], top_k: int = 10
    ) -> list[RetrievalResult]:
        """对已合并的结果执行 rerank 精排 + 父块扩展

        供 executor 在批量合并子查询结果后统一调用，避免多次 rerank 锁争用。
        """
        if not results:
            return []

        rerank_candidates = results[: top_k * 2]
        try:
            reranked = await self._rerank(query, rerank_candidates, top_k)
            print(f"[Retrieval] 统一 Rerank 后: {len(reranked)} 条")
        except Exception as e:
            logger.warning("Reranker 异常，跳过重排序: %s", e)
            reranked = results[:top_k]

        expanded = await self._expand_parent(reranked)
        return expanded

    def _rrf_fusion(
        self, results_lists: list[list[RetrievalResult]], k: int = 60
    ) -> list[RetrievalResult]:
        """Reciprocal Rank Fusion 融合多路检索结果"""
        scores: dict[str, float] = {}
        items: dict[str, RetrievalResult] = {}

        for results in results_lists:
            for rank, item in enumerate(results):
                scores[item.chunk_id] = scores.get(item.chunk_id, 0) + 1.0 / (k + rank + 1)
                items[item.chunk_id] = item

        # 按 RRF 分数降序排列
        sorted_ids = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
        return [items[cid] for cid in sorted_ids]

    async def _rerank(
        self, query: str, results: list[RetrievalResult], top_k: int
    ) -> list[RetrievalResult]:
        """调用 Reranker 对融合结果精排，返回 top_k 结果

        对"结构性碎片"（标题、目录标记等无实质信息的短文本）施加分数惩罚，
        避免它们因关键词匹配获得虚高分数。
        """
        if not results:
            return []

        documents = [r.content for r in results]
        ranked_pairs = await self.reranker.rerank(query, documents, top_k=top_k)

        # 打印分数分布用于调试
        if ranked_pairs:
            scores = [score for _, score in ranked_pairs]
            print(f"[Rerank] 分数范围: {min(scores):.3f} ~ {max(scores):.3f}, 返回 top {top_k}")

        # ranked_pairs: list[(原始索引, 分数)]，按分数降序已排好
        reranked = []
        for idx, score in ranked_pairs:
            item = results[idx]
            # 对结构性碎片施加惩罚
            if self._is_structural_fragment(item.content):
                score = score * 0.5
            reranked.append(
                RetrievalResult(
                    chunk_id=item.chunk_id,
                    content=item.content,
                    score=score,
                    doc_id=item.doc_id,
                    metadata=item.metadata,
                )
            )

        # 惩罚后重新排序
        reranked.sort(key=lambda x: x.score, reverse=True)
        return reranked

    @staticmethod
    def _is_structural_fragment(content: str) -> bool:
        """判断内容是否为结构性碎片（标题、目录标记等无实质信息的短文本）

        判定条件：内容短（<15字符）且整体匹配常见的结构标记模式。
        像"2023-3378"、"386489元"这类虽短但有实质信息的内容不会被误判。
        """
        text = content.strip()
        if len(text) >= 15:
            return False
        # 匹配纯标题/角色标记：原告、被告、第三人、证据目录等
        import re
        structural_pattern = re.compile(
            r'^(原告|被告|第三人|诉讼请求|事实与理由|事实和理由|证据目录|证据清单|'
            r'判决如下|裁定如下|本院认为|经审理查明|审判长|审判员|'
            r'目录|附录|附件|备注|注|说明)$'
        )
        return bool(structural_pattern.match(text))

    async def _expand_parent(
        self, results: list[RetrievalResult]
    ) -> list[RetrievalResult]:
        """父块扩展：若 chunk 有 parent_id，用父块内容替换 content，子块内容保留到 child_content"""
        # 收集需要扩展的 parent_id
        parent_ids = set()
        for r in results:
            parent_id = r.metadata.get("parent_id", "")
            if parent_id:
                parent_ids.add(parent_id)

        if not parent_ids:
            # 没有父块，child_content 就是 content 本身
            for r in results:
                r.child_content = r.content
            return results

        # 批量查询父块内容
        parent_contents: dict[str, str] = {}
        async with self.db_session_factory() as session:
            stmt = select(Chunk.id, Chunk.content).where(Chunk.id.in_(list(parent_ids)))
            rows = await session.execute(stmt)
            for row in rows:
                parent_contents[row.id] = row.content

        # 保留子块内容，用父块内容替换 content
        expanded = []
        for r in results:
            parent_id = r.metadata.get("parent_id", "")
            child_content = r.content  # 原始子块内容
            parent_content = parent_contents.get(parent_id, r.content) if parent_id else r.content
            expanded.append(
                RetrievalResult(
                    chunk_id=r.chunk_id,
                    content=parent_content,
                    score=r.score,
                    doc_id=r.doc_id,
                    metadata=r.metadata,
                    child_content=child_content,
                )
            )
        return expanded
