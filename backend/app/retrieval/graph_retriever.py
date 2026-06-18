"""图谱召回检索器（HybridRetriever 第四路，design.md 4.5）。

通过「实体桥接」召回纯向量召不回的关联内容：从用户 query 抽取实体名，在 Neo4j 中
模糊匹配命中实体，沿 N 跳邻居展开子图，再把子图节点关联的原文 chunk 作为检索结果
并入 RRF 融合（match_type='graph'）。

全局/归纳类问题（task 9.2 / Req 7.1）：当 query 命中全局类启发式（如「这个库整体讲了
什么」「总结主要主题」）时，额外检索该 KB 已落库的社区摘要（task 9.1 的
``GraphStore.community_summaries``），包装为 ``match_type='graph_community'`` 的结果与
chunk 召回融合，为整库主题级问题提供高层信息。无社区摘要时该路为空，不影响 chunk 召回
（Property 8 降级零影响）。

降级策略（Property 8 / Requirements 7.1、7.2）：

- ``store`` 为 None（全局未启用 / Neo4j 不可用 / 驱动未安装）→ ``search`` 直接返回 ``[]``，
  不触达任何图查询，HybridRetriever 行为与未引入本功能时完全一致。
- KB 未开启图谱（``config.graph.enabled`` 为 False）→ 同样早返回 ``[]``（干净的早退，
  非异常）。
- 图查询过程中的异常向上抛出，由 HybridRetriever 的 ``_safe()`` 路级降级捕获（task 7.2）：
  该路返回空、其余检索路不受影响（Requirements 7.3）。实体名抽取这类「软失败」在内部
  兜底为分词回退，不抛错。

评分（供 RRF 排序）：以「图距离」近似的递减分——命中（种子）实体关联的 chunk 分数最高，
邻居实体关联的 chunk 按其在邻居子图中的接近度（中心优先、degree 降序的排序位次）递减。
RRF 仅用名次，但路内分数仍需单调反映接近度以得到合理名次。
"""

from __future__ import annotations

import json
import logging
import re
from collections import OrderedDict
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.pipeline.graph.config import read_graph_config
from app.retrieval.base import BaseRetriever, RetrievalResult
from app.retrieval.textutil import tokenize
from app.schema.db import Chunk, KnowledgeBase

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.models.provider import EmbedProvider, LLMProvider
    from app.storage.graph_store import GraphStore

logger = logging.getLogger(__name__)


# 命中（种子）实体的拉取上限：query 抽出的实体名经 CONTAINS 模糊匹配后取 top-N
# （store 内部按 degree 降序），避免一次召回过多种子膨胀子图。
_MAX_SEED_ENTITIES = 20

# query→实体名抽取结果的进程内 LRU 缓存容量（命中率高、抽取成本可观）。
_ENTITY_CACHE_CAPACITY = 256

# LLM 实体名抽取的低温度（确定性优先）。
_EXTRACT_TEMPERATURE = 0.2

# 单次 query 抽取的实体名上限（防止模型把整句拆成过多碎片）。
_MAX_QUERY_ENTITIES = 8

# 全局/归纳类问题最多融入的社区摘要条数（社区摘要由 task 9.1 落 PG，按成员数降序）。
# 社区摘要是「整库主题级」的高层信息，少量即可代表全局，避免淹没具体 chunk 召回。
_MAX_COMMUNITY_SUMMARIES = 5

# 全局/归纳类问题的轻量启发式判别词（无 IO、确定性、零额外成本）。命中任一即视为
# 全局/归纳类问题（如「这个库整体讲了什么」「总结一下主要主题」），此时融入社区摘要。
# 仅对全局类问题融入社区摘要，避免具体事实类问题被整库级摘要稀释（数据流向清晰）。
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
    """图谱召回检索器（实体桥接），作为 HybridRetriever 可选的第四路注入。

    构造参数由注入方（task 7.2）从 PlatformConfig 提供 ``hops`` / ``max_chunks``。
    ``store`` 为 None 时本检索器整体降级为「返回空」。
    """

    def __init__(
        self,
        store: "GraphStore | None",
        db_session_factory: "async_sessionmaker[AsyncSession]",
        embedder: "EmbedProvider | None",
        llm_provider: "LLMProvider | None",
        hops: int,
        max_chunks: int,
    ) -> None:
        """构造。

        Args:
            store: 图存储；None 表示图谱未启用 / 不可用 → 本路降级返回空。
            db_session_factory: 异步会话工厂（读 KB 图谱开关、查 chunk 正文）。
            embedder: 向量嵌入 provider（预留给实体名向量召回；当前实现未使用，
                保留以对齐 design.md 4.5 的构造签名与未来扩展）。
            llm_provider: 实体名抽取用 LLM；None 时回退为分词抽取。
            hops: 邻居展开跳数（由注入方从平台配置提供）。
            max_chunks: 单次 query 召回的最大 chunk 数（由注入方从平台配置提供）。
        """
        self._store = store
        self._db_session_factory = db_session_factory
        self._embedder = embedder
        self._llm = llm_provider
        self._hops = max(1, int(hops))
        self._max_chunks = max(1, int(max_chunks))
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
        """图谱召回：query→实体→邻居子图→关联 chunk→RetrievalResult(match_type='graph')。

        Args:
            query: 用户查询文本。
            kb_id: 知识库 id（隔离键）。
            top_k: 返回结果数上限（与 max_chunks 取较小者作为最终截断）。
            expr: 过滤表达式（图谱召回不适用，保留以对齐 BaseRetriever 签名）。

        Returns:
            按分数降序的图谱召回结果（match_type='graph' / 'graph_community'）；store 为
            None / KB 未启用 / 无命中时返回 ``[]``。
        """
        # 1) store 为 None → 整体降级，不触达任何图查询（Property 8 / Req 7.2）。
        if self._store is None:
            return []
        if not query or not query.strip():
            return []

        # 1.5) KB 未开启图谱 → 干净早退（非异常）。
        if not await self._kb_graph_enabled(kb_id):
            return []

        # 2) 实体桥接召回（chunk 结果）：query→实体→邻居子图→关联 chunk。
        chunk_results = await self._recall_chunks(query=query, kb_id=kb_id)

        # 3) 全局/归纳类问题：检索社区摘要并与 chunk 结果融合（task 9.2 / Req 7.1）。
        #    社区摘要是「整库主题级」的高层信息，对「整体讲了什么」这类问题最有价值；
        #    具体事实类问题不融入，避免被整库级摘要稀释。无社区摘要时为空，不影响 chunk 结果
        #    （Property 8 降级零影响）。
        community_results: list[RetrievalResult] = []
        if _is_global_query(query):
            community_results = await self._recall_community_summaries(kb_id=kb_id)

        # 4) 融合两路结果：社区摘要排在 chunk 结果之前（全局问题下整库主题更优先），
        #    各自已按分数降序；最终截断到 min(top_k, max_chunks)。
        results = community_results + chunk_results
        if not results:
            return []
        limit = min(self._max_chunks, top_k) if top_k > 0 else self._max_chunks
        results = results[:limit]

        logger.debug(
            "GraphRetriever 在 kb=%s 召回 chunk %d 条、社区摘要 %d 条",
            kb_id, len(chunk_results), len(community_results),
        )
        return results

    # ------------------------------------------------------------------
    # 实体桥接 chunk 召回
    # ------------------------------------------------------------------

    async def _recall_chunks(
        self, *, query: str, kb_id: str
    ) -> list[RetrievalResult]:
        """实体桥接召回：query→实体→邻居子图→关联 chunk→RetrievalResult。

        无实体名 / 无命中种子 / 无关联 chunk 时返回 ``[]``（不阻断社区摘要召回）。
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
        # 邻居子图至少应含种子本身；若为空（理论上不会）退回仅用种子。
        ordered_nodes = subset.nodes if subset.nodes else []

        # 按接近度顺序收集 chunk_ids（去重，截断 max_chunks），并记录每个 chunk 的分数。
        chunk_scores = await self._collect_chunk_scores(
            kb_id=kb_id, ordered_nodes=ordered_nodes, seed_chunk_map=seed_chunk_map
        )
        if not chunk_scores:
            return []

        # 查 Chunk 表取正文，包装为 RetrievalResult(match_type='graph')。
        results = await self._build_results(kb_id=kb_id, chunk_scores=chunk_scores)
        results.sort(key=lambda r: r.score, reverse=True)
        return results

    # ------------------------------------------------------------------
    # 社区摘要召回（全局/归纳类问题）
    # ------------------------------------------------------------------

    async def _recall_community_summaries(
        self, *, kb_id: str
    ) -> list[RetrievalResult]:
        """读取该 KB 已落库的社区摘要（task 9.1），包装为 RetrievalResult。

        社区摘要由 ``GraphStore.community_summaries`` 提供（强制带 kb_id 隔离，按成员数
        降序，无数据时返回 ``[]``）。每条社区摘要包装为 ``match_type='graph_community'`` 的
        结果，``chunk_id`` 用稳定的合成 id（``graph_community::<community_key>::<level>``），
        与真实 chunk_id 不冲突，便于下游去重与引用展示。

        评分：社区摘要按成员数降序的位次递减给分（``1/(1+rank)``），与 chunk 召回的图距离
        评分量纲一致，便于 RRF 与后续融合排序。
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

    async def _kb_graph_enabled(self, kb_id: str) -> bool:
        """读取 KB 的图谱开关（``config.graph.enabled``）。

        DB 查询异常向上抛出，由 HybridRetriever 的 ``_safe()`` 路级降级捕获。
        """
        async with self._db_session_factory() as session:
            kb_config = await session.scalar(
                select(KnowledgeBase.config).where(KnowledgeBase.id == kb_id)
            )
        return read_graph_config(kb_config).enabled

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

    # ------------------------------------------------------------------
    # chunk 收集与结果构建
    # ------------------------------------------------------------------

    async def _collect_chunk_scores(
        self,
        *,
        kb_id: str,
        ordered_nodes: list,
        seed_chunk_map: dict[str, list[str]],
    ) -> "OrderedDict[str, float]":
        """按接近度顺序遍历子图节点，收集去重 chunk_id 及其分数（截断 max_chunks）。

        分数以「图距离」近似：节点在有序列表中的位次越靠前（越接近种子）分数越高，
        ``score = 1 / (1 + rank)``。同一 chunk 被多个实体引用时取首个（即最近）实体的分数。
        种子实体的 chunk_ids 直接取自 ``seed_chunk_map``；邻居实体经 ``get_entity`` 拉取，
        且仅在尚未集满 max_chunks 时按需查询（有界，避免过多图查询）。
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
                entity = await self._store.get_entity(kb_id=kb_id, entity_id=node_id)  # type: ignore[union-attr]
                chunk_ids = list(entity.chunk_ids) if entity else []

            score = 1.0 / (1.0 + rank)
            for cid in chunk_ids:
                if not cid or cid in chunk_scores:
                    continue
                chunk_scores[cid] = score
                if len(chunk_scores) >= self._max_chunks:
                    break
        return chunk_scores

    async def _build_results(
        self, *, kb_id: str, chunk_scores: "OrderedDict[str, float]"
    ) -> list[RetrievalResult]:
        """查 Chunk 表取正文，包装为 RetrievalResult（match_type='graph'）。

        仅返回在该 kb 内实际存在的 chunk（图中 chunk_id 可能因清理时序短暂滞后）。
        ``match_type`` 经 metadata 承载（``RetrievalResult`` 无该字段，与既有路由元信息
        同一存放方式）。
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


# ---------------------------------------------------------------------------
# 容错解析（纯函数）
# ---------------------------------------------------------------------------

# 定位文本中首个 JSON 数组（容忍前后多余文本 / markdown 包裹）。
_ARRAY_RE = re.compile(r"\[.*?\]", re.DOTALL)


def _is_global_query(query: str) -> bool:
    """判断是否为全局/归纳类问题（task 9.2 / Req 7.1）。

    纯函数、无 IO、确定性：对 query 做小写归一后匹配 ``_GLOBAL_QUERY_KEYWORDS`` 任一关键词。
    命中即视为全局/归纳类问题（如「这个库整体讲了什么」「总结一下主要主题」），此时检索社区
    摘要并融入；否则只走实体桥接 chunk 召回，避免具体事实类问题被整库级摘要稀释。

    轻量启发式而非 LLM 判别：零额外成本、无降级风险，契合「数据流向清晰、禁止过度封装」。
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
