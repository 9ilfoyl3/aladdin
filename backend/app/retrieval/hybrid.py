"""混合检索器

结合三路检索：稠密向量 + 稀疏向量 + BM25 全文检索，
通过 RRF 融合排序，再经 Rerank 精排，最后执行父块扩展以返回完整上下文。

参考 WeKnora / RAGFlow 的三路检索架构：
- Dense（语义相似度）：擅长理解意图和语义匹配
- Sparse（BGE-M3 稀疏向量）：擅长 subword 级别的模糊匹配
- BM25（全文检索）：擅长精确关键词匹配（条款编号、人名、案号等）
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
    """混合检索器：稠密 + 稀疏 + BM25 三路融合 + Rerank + 父块扩展"""

    def __init__(
        self,
        vector_retriever: BaseRetriever,
        sparse_retriever: BaseRetriever,
        rerank_provider: RerankProvider,
        db_session_factory: async_sessionmaker[AsyncSession],
        bm25_retriever: BaseRetriever | None = None,
    ):
        self.vector_retriever = vector_retriever
        self.sparse_retriever = sparse_retriever
        self.bm25_retriever = bm25_retriever
        self.reranker = rerank_provider
        self.db_session_factory = db_session_factory

    async def search(
        self, query: str, kb_id: str, top_k: int = 10, expr: str | None = None, **kwargs
    ) -> list[RetrievalResult]:
        """执行混合检索

        流程：并行三路检索 → RRF 融合 → Rerank 精排 → 父块扩展

        三路检索（参考 WeKnora / RAGFlow）：
        - Dense：语义相似度，每路取 top_k * 3
        - Sparse：BGE-M3 稀疏向量，每路取 top_k * 3
        - BM25：全文检索（精确关键词匹配），每路取 top_k * 3

        Args:
            expr: Milvus pre-filter 表达式，传递给子检索器进行元数据过滤
            skip_rerank: 跳过 rerank 和父块扩展，仅返回 RRF 融合结果
        """
        skip_rerank = kwargs.pop("skip_rerank", False)
        expanded_k = top_k * 3

        # 1. 并行执行三路检索
        tasks = [
            self.vector_retriever.search(query, kb_id, top_k=expanded_k, expr=expr, **kwargs),
            self.sparse_retriever.search(query, kb_id, top_k=expanded_k, expr=expr, **kwargs),
        ]
        # BM25 是可选的（兼容旧 schema collection）
        has_bm25 = self.bm25_retriever is not None
        if has_bm25:
            tasks.append(self.bm25_retriever.search(query, kb_id, top_k=expanded_k, expr=expr, **kwargs))

        results_list = await asyncio.gather(*tasks)

        dense_results = results_list[0]
        sparse_results = results_list[1]
        bm25_results = results_list[2] if has_bm25 else []

        print(f"[Retrieval] 稠密检索: {len(dense_results)} 条, "
              f"稀疏检索: {len(sparse_results)} 条, "
              f"BM25 检索: {len(bm25_results)} 条")

        # 2. RRF 融合多路结果
        all_results = [dense_results, sparse_results]
        if bm25_results:
            all_results.append(bm25_results)

        fused = self._rrf_fusion(all_results)
        print(f"[Retrieval] RRF 融合后: {len(fused)} 条")

        if not fused:
            return []

        # 快速模式：跳过 rerank，直接返回 RRF 融合结果
        if skip_rerank:
            return fused

        # 3. Rerank 精排（取融合结果前 top_k*2 条送入 rerank）
        rerank_candidates = fused[: top_k * 2]
        try:
            reranked = await self._rerank(query, rerank_candidates, top_k)
            print(f"[Retrieval] Rerank 后: {len(reranked)} 条")
        except Exception as e:
            logger.warning("Reranker 异常，跳过重排序: %s", e)
            reranked = fused[:top_k]

        # 4. 父块扩展
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
        self,
        results_lists: list[list[RetrievalResult]],
        k: int = 60,
        type_weights: dict[str, float] | None = None,
    ) -> list[RetrievalResult]:
        """Reciprocal Rank Fusion 融合多路检索结果，支持按元素类型施加权重

        Args:
            results_lists: 多路检索结果列表
            k: RRF 参数，默认 60
            type_weights: 元素类型权重映射，默认对 table 类型施加 0.8 降权
        """
        if type_weights is None:
            type_weights = {"table": 0.8}

        scores: dict[str, float] = {}
        items: dict[str, RetrievalResult] = {}

        for results in results_lists:
            for rank, item in enumerate(results):
                rrf_score = 1.0 / (k + rank + 1)
                scores[item.chunk_id] = scores.get(item.chunk_id, 0) + rrf_score
                items[item.chunk_id] = item

        # 施加类型权重
        for chunk_id, item in items.items():
            element_type = item.metadata.get("element_type", "text")
            weight = type_weights.get(element_type, 1.0)
            scores[chunk_id] *= weight

        # 按分数降序排列
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
