"""事件中心图谱召回检索器（HybridRetriever 第四路，design.md 3.3）。

把原「实体→chunk」的实体桥接召回升级为**事件中心**流程：

    query →（入口A 事件向量召回 + 入口B 实体桥接事件）→ 种子事件
          → 事件多跳扩展 → 粗排 → 回取关联 chunk → RetrievalResult(match_type='graph')

事件（Event）是新的一等检索单元：它的「文本与关系」存 Neo4j（多跳遍历强），它的「向量
召回」走 Milvus event 集合（``MilvusEventStore``）。两者用 ``event_id`` 对齐。命中事件后
再回取其关联的原文 chunk 作为最终检索结果，并入 RRF 第四路（接入点不变）。

两个种子入口（design.md 3.3 / Req 3.2）：

- **入口A（新增）**：query 向量在 Milvus event 集合做 ANN，直接召回事件（``event_store``
  为 None 时跳过，仅走入口B，渐进可用）。
- **入口B（实体桥接事件化）**：query→实体名→命中实体→实体 ``MENTIONS`` 的事件。

全局/归纳类问题（Req 3.7）：``_recall_community_summaries`` + ``_is_global_query`` 原样保留，
社区摘要与事件结果（或降级路的实体桥接结果）融合，互不破坏。

A/B 开关（Property 6 / Req 3.6、5.1）：KB 图谱已开启但 ``enable_events=False`` 时，本检索器
**退回纯实体桥接旧逻辑**（``query→实体→邻居子图→关联 chunk``，见 ``_recall_chunks_entity_bridge``），
便于基准脚本在同一 KB 上对比「实体桥接（旧）」与「事件中心（新）」两态召回。两态都仍输出
``match_type='graph'``、评分量纲一致（``1/(1+rank)``），并都与社区摘要融合。

降级矩阵（Property 5 / Req 3.6，对齐 design.md Error Handling）：

| 场景 | 行为 |
|---|---|
| ``store`` 为 None（Neo4j 不可用 / 驱动未安装） | ``search`` 直接返回 ``[]``，不触达任何图查询 |
| KB 未开启图谱（``config.graph.enabled``=False） | 干净早退 ``[]`` |
| KB 已开图但 ``enable_events``=False | 退回纯实体桥接旧逻辑（A/B 可切，仍与社区摘要融合） |
| ``event_store`` 为 None / 事件集合不可用 | 事件路入口A 跳过，仅走入口B（记 WARNING），渐进可用 |
| 无任何种子事件（事件路） | 事件结果为空，社区摘要仍可单独返回，否则 ``[]`` |

图查询过程中的异常向上抛出，由 HybridRetriever 的 ``_safe()`` 路级降级捕获；实体名抽取、
query 向量化、事件向量召回这类「软失败」在内部兜底，不抛错、不影响其余检索路。

评分（供 RRF 排序）：最终回取的 chunk 按其来源事件粗排后的位次给递减分
（``score = 1/(1+rank)``），与其它三路量纲一致（对齐现状）。
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import OrderedDict
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.pipeline.graph.config import (
    DEFAULT_EVENT_COARSE_TOP_K,
    DEFAULT_EVENT_MAX_EXPAND,
    DEFAULT_EVENT_SEED_K,
    read_graph_config,
)
from app.retrieval.base import BaseRetriever, RetrievalResult
from app.retrieval.textutil import tokenize
from app.schema.db import Chunk, KnowledgeBase

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.models.provider import EmbedProvider, LLMProvider
    from app.storage.graph_store import GraphStore
    from app.storage.milvus_event_store import MilvusEventStore

logger = logging.getLogger(__name__)


# 入口B 命中（种子）实体的拉取上限：query 抽出的实体名经 CONTAINS 模糊匹配后取 top-N
# （store 内部按 degree 降序），避免一次召回过多种子膨胀子图。
_MAX_SEED_ENTITIES = 20

# query→实体名抽取结果的进程内 LRU 缓存容量（命中率高、抽取成本可观）。
_ENTITY_CACHE_CAPACITY = 256

# LLM 实体名抽取的低温度（确定性优先）。
_EXTRACT_TEMPERATURE = 0.2

# 单次 query 抽取的实体名上限（防止模型把整句拆成过多碎片）。
_MAX_QUERY_ENTITIES = 8

# 全局/归纳类问题最多融入的社区摘要条数（社区摘要由 task 9.1 落 PG，按成员数降序）。
_MAX_COMMUNITY_SUMMARIES = 5

# 全局/归纳类问题的轻量启发式判别词（无 IO、确定性、零额外成本）。命中任一即视为
# 全局/归纳类问题（如「这个库整体讲了什么」「总结一下主要主题」），此时融入社区摘要。
_GLOBAL_QUERY_KEYWORDS: tuple[str, ...] = (
    "整体", "总体", "总的来说", "总结", "汇总", "概括", "概述", "综述", "归纳",
    "主旨", "主题", "大致", "大体", "整个", "整本", "全书", "所有", "全部",
    "主要内容", "主要讲", "讲了什么", "讲什么", "说了什么", "关于什么", "是什么内容",
    "核心内容", "涵盖", "包含哪些", "有哪些主题", "这个库", "知识库", "文档集", "资料库",
    "overall", "summary", "summarize", "summarise", "summarize the", "overview",
    "in general", "main theme", "main topic", "what is this about", "gist", "broadly",
)

# LLM 实体名抽取的提示词：只输出 JSON 字符串数组，便于稳健解析。
_ENTITY_EXTRACT_SYSTEM = (
    "你是一个实体名抽取器。从用户查询中抽取用于知识图谱检索的实体名"
    "（人名、组织、地点、概念、产品、技术、作品等专有名词或关键术语）。"
    "只输出一个 JSON 字符串数组，形如 [\"名称1\", \"名称2\"]，"
    "不要输出任何解释、markdown 或代码块。若无明显实体则输出 []。"
)


class GraphRetriever(BaseRetriever):
    """事件中心图谱召回检索器，作为 HybridRetriever 可选的第四路注入。

    ``store`` 为 None 时本检索器整体降级为「返回空」；``event_store`` 为 None 时入口A
    （事件向量召回）跳过，仅走入口B（实体桥接事件）。事件相关上限参数
    （``seed_k`` / ``max_events`` / ``coarse_top_k``）由注入方从 KB / 平台配置提供，
    缺省时回落 ``config.py`` 的安全默认（task 11 负责完整注入接线）。
    """

    def __init__(
        self,
        store: "GraphStore | None",
        db_session_factory: "async_sessionmaker[AsyncSession]",
        embedder: "EmbedProvider | None",
        llm_provider: "LLMProvider | None",
        hops: int,
        max_chunks: int,
        event_store: "MilvusEventStore | None" = None,
        seed_k: int = DEFAULT_EVENT_SEED_K,
        max_events: int = DEFAULT_EVENT_MAX_EXPAND,
        coarse_top_k: int = DEFAULT_EVENT_COARSE_TOP_K,
    ) -> None:
        """构造。

        Args:
            store: 图存储；None 表示图谱未启用 / 不可用 → 本路降级返回空。
            db_session_factory: 异步会话工厂（读 KB 图谱开关、查 chunk 正文）。
            embedder: 向量嵌入 provider；用于 query 向量化（入口A）与事件粗排。
                None 时入口A 跳过、粗排回落按种子/扩展分数排序。
            llm_provider: 实体名抽取用 LLM；None 时回退为分词抽取（入口B）。
            hops: 事件多跳扩展跳数（由注入方从平台配置提供）。
            max_chunks: 单次 query 召回的最大 chunk 数（最终结果截断）。
            event_store: Milvus 事件向量集合；None 时入口A 跳过（仅入口B）。
            seed_k: 两入口各自的种子事件上限。
            max_events: 事件多跳扩展事件上限。
            coarse_top_k: 粗排后进入回取的事件上限。
        """
        self._store = store
        self._db_session_factory = db_session_factory
        self._embedder = embedder
        self._llm = llm_provider
        self._hops = max(1, int(hops))
        self._max_chunks = max(1, int(max_chunks))
        self._event_store = event_store
        self._seed_k = max(1, int(seed_k))
        self._max_events = max(1, int(max_events))
        self._coarse_top_k = max(1, int(coarse_top_k))
        # query→实体名 的进程内 LRU 缓存（OrderedDict 末尾为最近使用）。
        self._entity_cache: "OrderedDict[str, list[str]]" = OrderedDict()

    async def search(
        self,
        query: str,
        kb_id: str,
        top_k: int = 10,
        expr: str | None = None,
        **kwargs,
    ) -> list[RetrievalResult]:
        """事件中心召回：query→种子事件→多跳→粗排→回取 chunk→RetrievalResult。

        Args:
            query: 用户查询文本。
            kb_id: 知识库 id（隔离键）。
            top_k: 返回结果数上限（与 max_chunks 取较小者作为最终截断）。
            expr: 过滤表达式（图谱召回不适用，保留以对齐 BaseRetriever 签名）。

        Returns:
            按分数降序的图谱召回结果（match_type='graph' / 'graph_community'）；store 为
            None / KB 未启用 / 无命中时返回 ``[]``。
        """
        # 1) store 为 None → 整体降级，不触达任何图查询（Property 5 / Req 3.6）。
        if self._store is None:
            return []
        if not query or not query.strip():
            return []

        # 1.5) KB 图谱开关：未开启 → 干净早退；已开但 enable_events=False → 退回纯实体桥接旧逻辑。
        enabled, enable_events = await self._read_kb_graph_flags(kb_id)
        if not enabled:
            return []

        # 全局/归纳类问题：检索社区摘要并与召回结果融合（Req 3.7，两态共用）。
        community_results: list[RetrievalResult] = []
        if _is_global_query(query):
            community_results = await self._recall_community_summaries(kb_id=kb_id)

        # 1.6) A/B 开关（Property 6 / Req 3.6、5.1）：enable_events=False → 纯实体桥接旧逻辑。
        if not enable_events:
            return await self._search_entity_bridge(
                query=query, kb_id=kb_id, top_k=top_k, community_results=community_results
            )

        return await self._search_event_centric(
            query=query, kb_id=kb_id, top_k=top_k, community_results=community_results
        )

    async def _search_event_centric(
        self,
        *,
        query: str,
        kb_id: str,
        top_k: int,
        community_results: list[RetrievalResult],
    ) -> list[RetrievalResult]:
        """事件中心召回（默认 enable_events=True）：

        query→（入口A 事件向量 + 入口B 实体桥接事件）→ 种子事件 → 多跳扩展 → 粗排 → 回取 chunk。
        与已检索好的社区摘要（``community_results``）融合后截断返回。
        """
        # 2) query 向量化（入口A 与粗排用）。软失败兜底为 None（仅退化入口A/粗排，不抛错）。
        qvec = await self._embed_query(query)

        # 3) 种子事件：入口A（事件向量召回）+ 入口B（实体桥接事件）。
        seed_a = await self._seed_events_by_vector(kb_id=kb_id, qvec=qvec)
        seeds_b = await self._seed_events_by_entities(query=query, kb_id=kb_id)

        # 候选事件初始映射（种子优先序：入口A 在前，入口B 在后），保留 content/chunk/doc。
        cand_map: "OrderedDict[str, dict]" = OrderedDict()
        self._merge_candidates(cand_map, seed_a)
        self._merge_candidates(cand_map, seeds_b)
        seed_ids = list(cand_map.keys())

        # 5~8) 有种子事件才做多跳扩展 / 粗排 / 回取 chunk；无种子事件时各阶段计数为 0，
        # 事件路为空（社区摘要仍可单独返回，干净降级）。各阶段计数无论有无种子都参与下方
        # 可观测日志（Req 4.1：分入口种子数 / 扩展数 / 粗排数 / 回取 chunk 数）。
        expanded: list = []
        ranked_ids: list[str] = []
        event_results: list[RetrievalResult] = []
        if seed_ids:
            # 6) 事件多跳扩展（种子→共享实体→关联事件）。
            expanded = await self._store.expand_events(  # type: ignore[union-attr]
                kb_id=kb_id, event_ids=seed_ids, hops=self._hops, max_events=self._max_events
            )
            self._merge_candidates(cand_map, expanded)

            # 7) 粗排：候选事件 content 向量 vs query 相似度，截断 coarse_top_k。
            ranked_ids = await self._coarse_rank_events(cand_map=cand_map, qvec=qvec)

            # 8) 回取事件关联 chunk → RetrievalResult(match_type='graph')。
            event_results = await self._build_results_from_events(
                kb_id=kb_id, ranked_ids=ranked_ids, cand_map=cand_map
            )

        # 可观测日志（Req 4.1，对齐现有图谱 debug 风格）：每次事件中心召回都输出
        # 种子事件数（分入口A/入口B）、扩展事件数、粗排后事件数、最终回取 chunk 数，
        # 便于调参。无种子 / 无结果时同样输出（全 0 计数也是有用的调参信号）。
        logger.debug(
            "GraphRetriever[event] kb=%s 种子A=%d 种子B=%d 种子合计=%d "
            "扩展=%d 粗排=%d 回取chunk=%d 社区=%d",
            kb_id, len(seed_a), len(seeds_b), len(seed_ids),
            len(expanded), len(ranked_ids), len(event_results), len(community_results),
        )

        # 9) 融合社区摘要（在前）与事件结果，截断到 min(top_k, max_chunks)。
        results = community_results + event_results
        if not results:
            return []
        return self._truncate(results, top_k)

    async def _search_entity_bridge(
        self,
        *,
        query: str,
        kb_id: str,
        top_k: int,
        community_results: list[RetrievalResult],
    ) -> list[RetrievalResult]:
        """纯实体桥接旧逻辑（enable_events=False，A/B 基准对比用，Property 6 / Req 3.6、5.1）。

        query→实体→邻居子图→关联 chunk，分数按子图接近度位次递减（``1/(1+rank)``），
        输出 ``match_type='graph'``，与社区摘要融合，量纲与事件中心一致。
        """
        chunk_results = await self._recall_chunks_entity_bridge(query=query, kb_id=kb_id)

        # 可观测日志（Req 4.1）：降级态（纯实体桥接）也输出回取 chunk 数与社区数，
        # 与事件中心日志风格对齐，便于 A/B 调参对比。无结果时同样输出。
        logger.debug(
            "GraphRetriever[entity-bridge] kb=%s 回取chunk=%d 社区=%d",
            kb_id, len(chunk_results), len(community_results),
        )

        results = community_results + chunk_results
        if not results:
            return []
        return self._truncate(results, top_k)

    def _truncate(
        self, results: list[RetrievalResult], top_k: int
    ) -> list[RetrievalResult]:
        """按 min(top_k, max_chunks) 截断（top_k <= 0 时仅用 max_chunks）。"""
        limit = min(self._max_chunks, top_k) if top_k > 0 else self._max_chunks
        return results[:limit]

    # ------------------------------------------------------------------
    # query 向量化
    # ------------------------------------------------------------------

    async def _embed_query(self, query: str) -> list[float] | None:
        """把 query 向量化（用于入口A 与粗排）。

        embedder 为 None 或调用失败时返回 None（软失败：入口A 跳过、粗排回落分数排序），
        不抛错、不影响入口B 与其余检索路。
        """
        if self._embedder is None:
            return None
        try:
            vecs = await self._embedder.embed([query])
        except Exception as e:  # noqa: BLE001 - 向量化软失败，退化但不阻断
            logger.warning("GraphRetriever query 向量化失败，入口A/粗排降级: %s", e)
            return None
        if not vecs or not vecs[0]:
            return None
        return list(vecs[0])

    # ------------------------------------------------------------------
    # 入口A：事件向量召回
    # ------------------------------------------------------------------

    async def _seed_events_by_vector(
        self, *, kb_id: str, qvec: list[float] | None
    ) -> list[dict]:
        """入口A：query 向量在 Milvus event 集合 ANN，召回种子事件。

        ``event_store`` 为 None / 无 query 向量时跳过（返回 []）；事件集合不可用等软失败
        记 WARNING 后返回 []（仅退化入口A，不影响入口B）。返回 ``MilvusEventStore.search``
        的原始 dict（含 event_id/doc_id/chunk_id/content/score）。
        """
        if self._event_store is None or qvec is None:
            return []
        try:
            return await self._event_store.search(kb_id, qvec, top_k=self._seed_k)
        except Exception as e:  # noqa: BLE001 - 事件集合软失败，仅退化入口A
            logger.warning("GraphRetriever 入口A 事件向量召回失败，跳过: %s", e)
            return []

    # ------------------------------------------------------------------
    # 入口B：实体桥接事件
    # ------------------------------------------------------------------

    async def _seed_events_by_entities(
        self, *, query: str, kb_id: str
    ) -> list:
        """入口B：query→实体名→命中实体→实体 ``MENTIONS`` 的事件（GraphEventDTO 列表）。

        无实体名 / 无命中实体时返回 []（不阻断入口A 与社区摘要）。
        """
        names = await self._extract_entity_names(query)
        if not names:
            return []
        entities = await self._store.find_entities_by_names(  # type: ignore[union-attr]
            kb_id=kb_id, names=names, limit=_MAX_SEED_ENTITIES
        )
        if not entities:
            return []
        return await self._store.events_by_entities(  # type: ignore[union-attr]
            kb_id=kb_id, entity_ids=[e.id for e in entities], limit=self._seed_k
        )

    # ------------------------------------------------------------------
    # 候选事件归一与粗排
    # ------------------------------------------------------------------

    @staticmethod
    def _merge_candidates(cand_map: "OrderedDict[str, dict]", items: list) -> None:
        """把一批候选事件（入口A dict 或 GraphEventDTO）并入候选映射（按 event_id 去重保序）。

        统一归一为 ``{id, content, chunk_id, doc_id, base_score}``。同一事件已存在时不覆盖
        （保留首个来源的字段），保证种子优先序稳定。
        """
        for item in items:
            if isinstance(item, dict):
                eid = item.get("event_id")
                content = item.get("content") or ""
                chunk_id = item.get("chunk_id") or ""
                doc_id = item.get("doc_id") or ""
                base_score = float(item.get("score") or 0.0)
            else:  # GraphEventDTO
                eid = getattr(item, "id", None)
                content = getattr(item, "content", "") or ""
                chunk_id = getattr(item, "chunk_id", "") or ""
                doc_id = getattr(item, "doc_id", "") or ""
                base_score = float(getattr(item, "score", 0.0) or 0.0)
            if not eid or eid in cand_map:
                continue
            cand_map[eid] = {
                "id": eid,
                "content": content,
                "chunk_id": chunk_id,
                "doc_id": doc_id,
                "base_score": base_score,
            }

    async def _coarse_rank_events(
        self, *, cand_map: "OrderedDict[str, dict]", qvec: list[float] | None
    ) -> list[str]:
        """粗排候选事件：按事件 content 向量与 query 的余弦相似度降序，截断 coarse_top_k。

        有 query 向量与 embedder 时，对候选事件 content 批量向量化并算余弦相似度排序；
        否则（embedder 不可用）回落按种子/扩展的 ``base_score`` 排序（稳定保序）。
        返回排序后的 event_id 列表。
        """
        cand_ids = list(cand_map.keys())
        if not cand_ids:
            return []

        scores: dict[str, float]
        if qvec is not None and self._embedder is not None:
            contents = [cand_map[cid]["content"] for cid in cand_ids]
            try:
                vecs = await self._embedder.embed(contents)
            except Exception as e:  # noqa: BLE001 - 粗排向量化软失败，回落分数排序
                logger.warning("GraphRetriever 粗排向量化失败，回落分数排序: %s", e)
                vecs = None
            if vecs and len(vecs) == len(cand_ids):
                scores = {
                    cid: _cosine(qvec, vecs[i]) for i, cid in enumerate(cand_ids)
                }
            else:
                scores = {cid: cand_map[cid]["base_score"] for cid in cand_ids}
        else:
            scores = {cid: cand_map[cid]["base_score"] for cid in cand_ids}

        # 稳定排序：相似度降序，相等时保留候选映射的原序（种子优先）。
        order = {cid: i for i, cid in enumerate(cand_ids)}
        ranked = sorted(cand_ids, key=lambda cid: (-scores[cid], order[cid]))
        return ranked[: self._coarse_top_k]

    async def _build_results_from_events(
        self,
        *,
        kb_id: str,
        ranked_ids: list[str],
        cand_map: "OrderedDict[str, dict]",
    ) -> list[RetrievalResult]:
        """回取粗排后事件关联的原文 chunk，包装为 RetrievalResult(match_type='graph')。

        一个事件关联一个 chunk（``chunk_id``）；多个事件可能指向同一 chunk，去重取首个
        （即粗排更靠前）事件的位次分。评分按事件粗排位次递减（``1/(1+rank)``），与其它
        三路量纲一致（对齐现状）。仅返回在该 kb 内实际存在的 chunk。
        """
        # 按粗排顺序收集去重 chunk_id 及其分数（截断 max_chunks）。
        chunk_scores: "OrderedDict[str, float]" = OrderedDict()
        chunk_event: dict[str, str] = {}
        rank = 0
        for eid in ranked_ids:
            cand = cand_map.get(eid)
            if cand is None:
                continue
            cid = cand["chunk_id"]
            if not cid or cid in chunk_scores:
                continue
            chunk_scores[cid] = 1.0 / (1.0 + rank)
            chunk_event[cid] = eid
            rank += 1
            if len(chunk_scores) >= self._max_chunks:
                break

        if not chunk_scores:
            return []

        chunk_ids = list(chunk_scores.keys())
        async with self._db_session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        Chunk.id,
                        Chunk.content,
                        Chunk.doc_id,
                        Chunk.parent_id,
                        Chunk.chunk_index,
                        Chunk.chunk_metadata,
                    ).where(Chunk.kb_id == kb_id, Chunk.id.in_(chunk_ids))
                )
            ).all()

        results: list[RetrievalResult] = []
        for row in rows:
            meta = row.chunk_metadata if isinstance(row.chunk_metadata, dict) else {}
            results.append(
                RetrievalResult(
                    chunk_id=row.id,
                    content=row.content,
                    score=chunk_scores.get(row.id, 0.0),
                    doc_id=row.doc_id,
                    metadata={
                        "match_type": "graph",
                        "parent_id": row.parent_id or "",
                        "chunk_index": row.chunk_index or 0,
                        "element_type": meta.get("element_type", "text"),
                        "event_id": chunk_event.get(row.id, ""),
                    },
                )
            )
        # DB 返回顺序不保证，按分数降序（即事件粗排位次）重排。
        results.sort(key=lambda r: r.score, reverse=True)
        return results

    # ------------------------------------------------------------------
    # 纯实体桥接旧逻辑（enable_events=False，A/B 基准对比用）
    # ------------------------------------------------------------------

    async def _recall_chunks_entity_bridge(
        self, *, query: str, kb_id: str
    ) -> list[RetrievalResult]:
        """实体桥接召回（旧逻辑）：query→实体→邻居子图→关联 chunk→RetrievalResult。

        复用仍存在的 ``GraphStore.neighbors`` / ``get_entity``。无实体名 / 无命中种子 /
        无关联 chunk 时返回 ``[]``（不阻断社区摘要召回）。
        """
        # 从 query 抽实体名（LLM 抽取，失败回退分词；带进程内缓存）。
        names = await self._extract_entity_names(query)
        if not names:
            return []

        # 模糊匹配命中实体（种子）。
        seed_entities = await self._store.find_entities_by_names(  # type: ignore[union-attr]
            kb_id=kb_id, names=names, limit=_MAX_SEED_ENTITIES
        )
        if not seed_entities:
            return []
        seed_ids = [e.id for e in seed_entities]
        # 种子实体的 chunk_ids 已随 DTO 返回，建 id→chunk_ids 映射，省去重复 get_entity。
        seed_chunk_map = {e.id: list(e.chunk_ids or []) for e in seed_entities}

        # 邻居子图（中心优先、degree 降序的有序节点集）。
        subset = await self._store.neighbors(  # type: ignore[union-attr]
            kb_id=kb_id,
            entity_ids=seed_ids,
            hops=self._hops,
            max_nodes=self._max_chunks,
        )
        ordered_nodes = subset.nodes if subset.nodes else []

        # 按接近度顺序收集 chunk_ids（去重，截断 max_chunks），并记录每个 chunk 的分数。
        chunk_scores = await self._collect_chunk_scores_entity_bridge(
            kb_id=kb_id, ordered_nodes=ordered_nodes, seed_chunk_map=seed_chunk_map
        )
        if not chunk_scores:
            return []

        results = await self._build_results_entity_bridge(
            kb_id=kb_id, chunk_scores=chunk_scores
        )
        results.sort(key=lambda r: r.score, reverse=True)
        return results

    async def _collect_chunk_scores_entity_bridge(
        self,
        *,
        kb_id: str,
        ordered_nodes: list,
        seed_chunk_map: dict[str, list[str]],
    ) -> "OrderedDict[str, float]":
        """按接近度顺序遍历子图节点，收集去重 chunk_id 及其分数（截断 max_chunks）。

        分数以「图距离」近似：节点在有序列表中的位次越靠前（越接近种子）分数越高，
        ``score = 1 / (1 + rank)``。同一 chunk 被多个实体引用时取首个（即最近）实体的分数。
        种子实体的 chunk_ids 直接取自 ``seed_chunk_map``；邻居实体经 ``get_entity`` 按需拉取，
        且仅在尚未集满 max_chunks 时查询（有界，避免过多图查询）。
        """
        chunk_scores: "OrderedDict[str, float]" = OrderedDict()
        for rank, node in enumerate(ordered_nodes):
            if len(chunk_scores) >= self._max_chunks:
                break
            node_id = node.id
            if node_id in seed_chunk_map:
                chunk_ids = seed_chunk_map[node_id]
            else:
                # 邻居实体：详情里才有 chunk_ids，按需拉取。
                entity = await self._store.get_entity(  # type: ignore[union-attr]
                    kb_id=kb_id, entity_id=node_id
                )
                chunk_ids = list(entity.chunk_ids) if entity else []

            score = 1.0 / (1.0 + rank)
            for cid in chunk_ids:
                if not cid or cid in chunk_scores:
                    continue
                chunk_scores[cid] = score
                if len(chunk_scores) >= self._max_chunks:
                    break
        return chunk_scores

    async def _build_results_entity_bridge(
        self, *, kb_id: str, chunk_scores: "OrderedDict[str, float]"
    ) -> list[RetrievalResult]:
        """查 Chunk 表取正文，包装为 RetrievalResult（match_type='graph'）。

        仅返回在该 kb 内实际存在的 chunk（图中 chunk_id 可能因清理时序短暂滞后）。
        """
        chunk_ids = list(chunk_scores.keys())
        async with self._db_session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        Chunk.id,
                        Chunk.content,
                        Chunk.doc_id,
                        Chunk.parent_id,
                        Chunk.chunk_index,
                        Chunk.chunk_metadata,
                    ).where(Chunk.kb_id == kb_id, Chunk.id.in_(chunk_ids))
                )
            ).all()

        results: list[RetrievalResult] = []
        for row in rows:
            meta = row.chunk_metadata if isinstance(row.chunk_metadata, dict) else {}
            results.append(
                RetrievalResult(
                    chunk_id=row.id,
                    content=row.content,
                    score=chunk_scores.get(row.id, 0.0),
                    doc_id=row.doc_id,
                    metadata={
                        "match_type": "graph",
                        "parent_id": row.parent_id or "",
                        "chunk_index": row.chunk_index or 0,
                        "element_type": meta.get("element_type", "text"),
                    },
                )
            )
        return results

    # ------------------------------------------------------------------
    # 社区摘要召回（全局/归纳类问题，原样保留）
    # ------------------------------------------------------------------

    async def _recall_community_summaries(
        self, *, kb_id: str
    ) -> list[RetrievalResult]:
        """读取该 KB 已落库的社区摘要（task 9.1），包装为 RetrievalResult。

        社区摘要由 ``GraphStore.community_summaries`` 提供（强制带 kb_id 隔离，按成员数
        降序，无数据时返回 ``[]``）。每条社区摘要包装为 ``match_type='graph_community'`` 的
        结果，``chunk_id`` 用稳定的合成 id（``graph_community::<community_key>::<level>``），
        与真实 chunk_id 不冲突，便于下游去重与引用展示。

        评分：社区摘要按成员数降序的位次递减给分（``1/(1+rank)``），与事件召回的量纲一致。
        """
        summaries = await self._store.community_summaries(  # type: ignore[union-attr]
            kb_id=kb_id, limit=_MAX_COMMUNITY_SUMMARIES
        )
        if not summaries:
            return []

        results: list[RetrievalResult] = []
        for rank, s in enumerate(summaries):
            if not s.summary or not s.summary.strip():
                continue
            content = f"{s.title}\n{s.summary}" if s.title else s.summary
            results.append(
                RetrievalResult(
                    chunk_id=f"graph_community::{s.community_key}::{s.level}",
                    content=content,
                    score=1.0 / (1.0 + rank),
                    doc_id="",
                    metadata={
                        "match_type": "graph_community",
                        "community_key": s.community_key,
                        "community_level": s.level,
                        "community_title": s.title or "",
                        "entity_count": s.entity_count,
                        "relation_count": s.relation_count,
                    },
                )
            )
        return results

    # ------------------------------------------------------------------
    # KB 图谱开关
    # ------------------------------------------------------------------

    async def _read_kb_graph_flags(self, kb_id: str) -> tuple[bool, bool]:
        """读取 KB 的图谱开关：``(enabled, enable_events)``。

        - ``enabled``：KB 级图谱总开关（``config.graph.enabled``），False 时本路整体早退。
        - ``enable_events``：是否走事件中心召回（``config.graph.enable_events``，默认 True）；
          False 时退回纯实体桥接旧逻辑（A/B 可切，Property 6 / Req 3.6、5.1）。

        DB 查询异常向上抛出，由 HybridRetriever 的 ``_safe()`` 路级降级捕获。
        """
        async with self._db_session_factory() as session:
            kb_config = await session.scalar(
                select(KnowledgeBase.config).where(KnowledgeBase.id == kb_id)
            )
        cfg = read_graph_config(kb_config)
        return cfg.enabled, cfg.enable_events

    # ------------------------------------------------------------------
    # 实体名抽取（LLM + 分词回退 + LRU 缓存）
    # ------------------------------------------------------------------

    async def _extract_entity_names(self, query: str) -> list[str]:
        """从 query 抽取候选实体名，带进程内 LRU 缓存。

        优先用 LLM 抽取（轻量、低温度）；LLM 不可用或抽取 / 解析失败时回退为分词
        （``textutil.tokenize`` 的词级 token）。两者都是「软失败可降级」，不抛错。
        """
        key = query.strip()
        cached = self._entity_cache.get(key)
        if cached is not None:
            self._entity_cache.move_to_end(key)  # 标记最近使用
            return cached

        names: list[str] = []
        if self._llm is not None:
            try:
                names = await self._llm_extract_names(key)
            except Exception as e:  # noqa: BLE001 - LLM 软失败，回退分词，不影响该路
                logger.warning("GraphRetriever 实体名 LLM 抽取失败，回退分词: %s", e)
                names = []

        if not names:
            # 回退：分词得到词级 token 作为候选名（CONTAINS 模糊匹配仍可命中实体）。
            names = list(tokenize(query))[:_MAX_QUERY_ENTITIES]

        self._cache_put(key, names)
        return names

    async def _llm_extract_names(self, query: str) -> list[str]:
        """调 LLM 抽取实体名，解析为去重的字符串列表（容错解析，截断到上限）。"""
        raw = await self._llm.generate(  # type: ignore[union-attr]
            [
                {"role": "system", "content": _ENTITY_EXTRACT_SYSTEM},
                {"role": "user", "content": query},
            ],
            temperature=_EXTRACT_TEMPERATURE,
            enable_thinking=False,
        )
        return _parse_name_list(raw)[:_MAX_QUERY_ENTITIES]

    def _cache_put(self, key: str, names: list[str]) -> None:
        """写入 LRU 缓存并按容量淘汰最久未使用项。"""
        self._entity_cache[key] = names
        self._entity_cache.move_to_end(key)
        while len(self._entity_cache) > _ENTITY_CACHE_CAPACITY:
            self._entity_cache.popitem(last=False)


# ---------------------------------------------------------------------------
# 容错解析与相似度（纯函数）
# ---------------------------------------------------------------------------

# 定位文本中首个 JSON 数组（容忍前后多余文本 / markdown 包裹）。
_ARRAY_RE = re.compile(r"\[.*?\]", re.DOTALL)


def _cosine(a: list[float], b: list[float]) -> float:
    """计算两个等长向量的余弦相似度（任一为零向量 / 空 / 长度不匹配返回 0.0）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def _is_global_query(query: str) -> bool:
    """判断是否为全局/归纳类问题（Req 3.7）。

    纯函数、无 IO、确定性：对 query 做小写归一后匹配 ``_GLOBAL_QUERY_KEYWORDS`` 任一关键词。
    命中即视为全局/归纳类问题，此时检索社区摘要并融入；否则只走事件中心召回，避免具体事实类
    问题被整库级摘要稀释。轻量启发式而非 LLM 判别：零额外成本、无降级风险。
    """
    if not query:
        return False
    lowered = query.lower()
    return any(kw in lowered for kw in _GLOBAL_QUERY_KEYWORDS)


def _parse_name_list(raw: str) -> list[str]:
    """把 LLM 输出解析为去重、去空白的字符串列表（容错）。

    容忍 markdown 代码块与前后多余文本：截取首个 ``[...]`` 片段解析；解析失败或非数组
    返回空列表（交由调用方回退分词）。
    """
    content = (raw or "").strip()
    if not content:
        return []

    match = _ARRAY_RE.search(content)
    candidate = match.group(0) if match else content
    try:
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []

    names: list[str] = []
    seen: set[str] = set()
    for item in parsed:
        if not isinstance(item, str):
            continue
        stripped = item.strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        names.append(stripped)
    return names
