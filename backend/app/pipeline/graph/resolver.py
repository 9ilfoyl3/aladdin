"""EntityResolver：实体归一化与两级消歧（图谱质量命门）。

对齐 design.md 4.3 与 Requirements 2.1 / 2.2 / 2.3 / 2.4：

- **确定性归一化**（``normalize_name``，Req 2.1）：去首尾空白、全角→半角统一
  （Unicode NFKC）、统一大小写、合并连续空白、去除装饰性标点。**纯函数、无 IO、
  幂等**（``normalize_name(normalize_name(x)) == normalize_name(x)``）。
- **批内合并**（Req 2.2）：同一次抽取内 normalize 后同名的实体合并，属性 / 别名取
  并集，关系端点同步改写为规范名。该步亦为纯函数（``merge_by_normalized_name``），
  便于 task 3.3 表驱动单测。
- **跨批别名消歧**（可选，Req 2.3）：对每个实体名做 embedding，在同 KB、同 type 的
  已有实体中做向量近邻；相似度 ≥ ``sim_threshold`` 判为别名，规范名改写为已存在实体
  的规范名，原名加入其 ``aliases``。候选来自 ``store.find_entities_by_names``（名称模糊
  匹配）过滤到同类型，实体名向量临时计算 + 内存 LRU 缓存（不落 Milvus，design.md 4.3）。
- **优雅降级**（Req 2.4）：``enable_alias_dedup`` 关闭、embedder 为 None、或 embedding /
  候选查询过程中抛错时，降级为仅做批内同名合并并记录 warning，**不阻断抽取**。

设计取舍：消歧只改写 ``ExtractedGraph`` 内的名称映射（设置每个实体的 ``normalized_name``
为规范名、改写关系端点），**不直接写库**；跨 chunk / 跨文档的同名收敛由
``GraphStore.upsert_graph`` 的 ``MERGE`` 完成（design.md 4.3）。
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import TYPE_CHECKING

from app.pipeline.graph.extractor import (
    ExtractedEntity,
    ExtractedGraph,
    ExtractedRelation,
)

if TYPE_CHECKING:
    # 仅类型检查期可见，避免运行时不必要的耦合/导入开销。
    from app.models.provider import EmbedProvider
    from app.storage.graph_store import GraphStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 归一化（纯函数，无 IO，幂等）—— Requirements 2.1
# ---------------------------------------------------------------------------

# 装饰性标点：包裹 / 强调类符号，归一化时整体剔除（不替换为空格，避免无谓断词）。
# 刻意**不含**中点 ``·``（人名分隔如「玛丽·居里」具语义，保留）。NFKC 已将全角 ASCII
# 引号 / 括号折叠为半角，故此处同时覆盖半角 ASCII 引号与中日文专用标点。
_DECORATIVE_CHARS = (
    "「」『』《》【】〔〕〈〉"          # 中日文书名号 / 方括号 / 角括号
    "“”‘’„‟«»‹›"                      # 各类弯引号 / 书名引号
    "\"'`"                            # 半角直引号 / 反引号
    "•※★☆◆◇●○■□▲△"                # 项目符号 / 强调装饰
)
# 预编译：一次性剔除所有装饰字符。
_DECORATIVE_RE = re.compile("[" + re.escape(_DECORATIVE_CHARS) + "]")
# 连续空白（含 NFKC 后的普通空格、制表符、换行等）折叠为单个空格。
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    """对实体名做确定性、幂等的规范化（Requirements 2.1）。

    步骤（顺序保证幂等）：

    1. ``None`` / 空 → 返回空字符串；
    2. Unicode **NFKC** 归一化：统一全角→半角、兼容字符（如全角空格 U+3000→普通空格）；
    3. 统一大小写：``lower()``（英文大小写视为同一实体）；
    4. 去除装饰性标点（引号 / 书名号 / 项目符号等，剔除为空）；
    5. 合并连续空白为单个空格；
    6. 去首尾空白。

    幂等性：每一步均幂等，且步骤 4~6 在 NFKC / lower 之后执行，二次调用时输入已无
    装饰标点、无多余空白、已是 NFKC + 小写形态，输出不变，故
    ``normalize_name(normalize_name(x)) == normalize_name(x)``。

    该函数纯计算、无 IO，可独立单测（task 3.3）。

    Args:
        name: 原始实体名（可能含全角 / 装饰标点 / 多余空白）。

    Returns:
        规范名；输入为空或归一化后为空时返回空字符串。
    """
    if not name:
        return ""
    s = unicodedata.normalize("NFKC", name)
    s = s.lower()
    s = _DECORATIVE_RE.sub("", s)
    s = _WHITESPACE_RE.sub(" ", s)
    return s.strip()


def _extend_unique(target: list[str], items) -> None:
    """把 ``items`` 中尚未出现在 ``target`` 的非空字符串按序追加到 ``target``（原地、保序去重）。"""
    seen = set(target)
    for it in items or []:
        if not it:
            continue
        if it not in seen:
            target.append(it)
            seen.add(it)


def merge_by_normalized_name(graph: ExtractedGraph) -> ExtractedGraph:
    """批内合并：同一次抽取内 normalize 后同名的实体合并（Requirements 2.2）。

    - 实体：按 ``normalize_name(name)`` 分组，每组产出一个实体；``name`` 取该组首次出现
      的原始表面形态（display），``normalized_name`` 为规范名，``type`` 取首个非空类型，
      ``attributes`` / ``aliases`` 取并集（保序去重），并把与规范名不同的原始表面形态并入
      ``aliases``。
    - 关系：端点改写为规范名（``normalize_name(source/target)``）；改写后两端均落在合并后
      实体集合内才保留（端点缺失的关系丢弃，维持无悬挂边，Property 3）。

    纯函数、无 IO，可独立单测（task 3.3）。

    Args:
        graph: 抽取得到的原始图（归一化前）。

    Returns:
        批内合并后的新 ``ExtractedGraph``（不修改入参）。
    """
    merged: dict[str, ExtractedEntity] = {}
    for e in graph.entities:
        canonical = normalize_name(e.name)
        if not canonical:
            # 归一化后为空的名称跳过（防生成无意义节点）。
            continue
        ent = merged.get(canonical)
        if ent is None:
            ent = ExtractedEntity(
                name=e.name,  # 保留首次出现的原始表面形态作为 display
                type=e.type,
                attributes=list(dict.fromkeys(a for a in e.attributes if a)),
                normalized_name=canonical,
                aliases=list(dict.fromkeys(a for a in e.aliases if a)),
            )
            merged[canonical] = ent
        else:
            # 合并：类型缺失时回填，属性 / 别名取并集（保序）。
            if not ent.type and e.type:
                ent.type = e.type
            _extend_unique(ent.attributes, e.attributes)
            _extend_unique(ent.aliases, e.aliases)
        # 原始表面形态与规范名不同 → 记入别名。
        if e.name and e.name != canonical:
            _extend_unique(ent.aliases, [e.name])

    # 关系端点改写为规范名，丢弃端点缺失的关系（无悬挂边）。
    canon_set = set(merged.keys())
    new_relations: list[ExtractedRelation] = []
    for r in graph.relations:
        s = normalize_name(r.source)
        t = normalize_name(r.target)
        if s in canon_set and t in canon_set:
            new_relations.append(
                ExtractedRelation(
                    source=s,
                    target=t,
                    type=r.type,
                    attributes=list(r.attributes),
                    confidence=r.confidence,
                )
            )

    return ExtractedGraph(entities=list(merged.values()), relations=new_relations)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """两向量余弦相似度（纯 Python，避免引入 numpy 依赖）。

    任一向量为空 / 维度不一致 / 模长为 0 时返回 0.0（视为不相似）。结果未做 clamp，
    正常浮点输入落在 [-1, 1]。
    """
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
    return dot / ((na ** 0.5) * (nb ** 0.5))


# 候选查询时每个名称的拉取上限（控制 embedding 计算量与查询开销）。
_CANDIDATE_LIMIT = 10
# 实体名向量内存缓存的默认容量上限（有界，防长跑内存膨胀）。
_DEFAULT_CACHE_SIZE = 2048


class EntityResolver:
    """实体归一化与两级消歧器（design.md 4.3）。

    用法::

        resolver = EntityResolver(embedder, store, enable_alias_dedup=True, sim_threshold=0.92)
        resolved = await resolver.resolve(kb_id=kb_id, graph=extracted_graph)

    ``resolve`` 返回的 ``ExtractedGraph`` 中每个实体的 ``normalized_name`` 已填为规范名、
    关系端点已改写为规范名；写库由 ``GraphStore.upsert_graph`` 的 MERGE 完成（本类不写库）。
    """

    def __init__(
        self,
        embedder: "EmbedProvider | None",
        store: "GraphStore",
        enable_alias_dedup: bool,
        sim_threshold: float,
        *,
        cache_size: int = _DEFAULT_CACHE_SIZE,
    ) -> None:
        """构造。

        Args:
            embedder: 嵌入提供方（实体名向量计算）；为 None 时跳过别名消歧（降级）。
            store: 图存储，用于查跨批已有实体候选（``find_entities_by_names``）。
            enable_alias_dedup: 是否启用向量别名消歧（KB 级开关）。
            sim_threshold: 别名合并相似度阈值（[0, 1]）。
            cache_size: 实体名向量内存缓存容量上限。
        """
        self._embedder = embedder
        self._store = store
        self._enable_alias_dedup = enable_alias_dedup
        self._sim_threshold = sim_threshold
        self._cache_size = max(1, cache_size)
        # 实体名 -> 向量 的有界 LRU 缓存（命中时移到末尾，超容量淘汰最旧）。
        self._embed_cache: dict[str, list[float]] = {}

    @staticmethod
    def normalize_name(name: str) -> str:
        """名称规范化（委托模块级 :func:`normalize_name`，确定性、无 IO、幂等）。

        以 staticmethod 暴露，既可 ``resolver.normalize_name(x)`` 也可
        ``EntityResolver.normalize_name(x)`` 调用，便于单测。
        """
        return normalize_name(name)

    async def resolve(self, *, kb_id: str, graph: ExtractedGraph) -> ExtractedGraph:
        """两级消歧：批内合并 +（可选）跨批向量别名消歧。

        1. **批内合并**（Req 2.2）：normalize 后同名实体合并、关系端点改写为规范名。
        2. **跨批别名消歧**（Req 2.3，可选）：每个实体名在同 KB、同 type 的已有实体中
           做向量近邻，相似度 ≥ 阈值则改写为已有规范名、原名入 aliases。
        3. **降级**（Req 2.4）：``enable_alias_dedup`` 关闭 / embedder 不可用 / 过程抛错 →
           仅保留批内合并结果并记 warning，不阻断抽取。

        Args:
            kb_id: 知识库 id（候选查询隔离键）。
            graph: 抽取得到的原始图（归一化前）。

        Returns:
            消歧后的 ``ExtractedGraph``（每个实体 ``normalized_name`` 已填、关系端点已改写）。
        """
        merged = merge_by_normalized_name(graph)

        if not self._enable_alias_dedup:
            return merged
        if self._embedder is None:
            # embedder 不可用：降级为仅同名合并（Req 2.4）。
            logger.warning("别名消歧 embedder 不可用，降级为仅做批内同名合并（kb_id=%s）", kb_id)
            return merged

        return await self._alias_dedup(kb_id, merged)

    async def _alias_dedup(self, kb_id: str, merged: ExtractedGraph) -> ExtractedGraph:
        """跨批向量别名消歧（Req 2.3）；任一步骤失败则就地降级返回已合并结果（Req 2.4）。

        对每个实体：查同 KB 名称模糊匹配候选 → 过滤同 type 且非自身规范名 → 计算实体名
        与候选名的余弦相似度 → 取 ≥ 阈值的最高分候选作为已有规范名，改写本实体
        ``normalized_name`` 并把原规范名 / 原始名并入 ``aliases``。最后按改写映射同步关系端点。
        """
        rename: dict[str, str] = {}  # 旧规范名 -> 已有实体规范名
        for ent in merged.entities:
            canonical = ent.normalized_name
            if not canonical:
                continue
            try:
                candidates = await self._store.find_entities_by_names(
                    kb_id=kb_id, names=[canonical], limit=_CANDIDATE_LIMIT
                )
            except Exception as e:  # noqa: BLE001 - 候选查询失败 → 整体降级，不阻断抽取
                logger.warning("别名消歧候选查询失败，降级为仅同名合并（kb_id=%s）: %s", kb_id, e)
                return merged

            # 同类型、且不是与自身完全同名的候选（同名由 upsert MERGE 处理，无需别名改写）。
            same_type = [c for c in candidates if c.type == ent.type and c.name != canonical]
            if not same_type:
                continue

            try:
                base_vec = await self._embed_cached(canonical)
                best_name: str | None = None
                best_sim = self._sim_threshold
                for c in same_type:
                    sim = _cosine_similarity(base_vec, await self._embed_cached(c.name))
                    if sim >= best_sim:
                        best_sim = sim
                        best_name = c.name
            except Exception as e:  # noqa: BLE001 - embedding 失败 → 整体降级（Req 2.4）
                logger.warning("别名消歧 embedding 失败，降级为仅同名合并（kb_id=%s）: %s", kb_id, e)
                return merged

            if best_name is not None:
                # 判为已有实体的别名：改写规范名，原规范名 / 原始名入别名。
                _extend_unique(ent.aliases, [canonical, ent.name])
                ent.normalized_name = best_name
                if canonical != best_name:
                    rename[canonical] = best_name

        # 按改写映射同步关系端点（指向旧规范名的端点改为已有规范名）。
        if rename:
            for r in merged.relations:
                r.source = rename.get(r.source, r.source)
                r.target = rename.get(r.target, r.target)

        return merged

    async def _embed_cached(self, text: str) -> list[float]:
        """取实体名向量，带有界 LRU 内存缓存（design.md 4.3：临时计算 + 内存 LRU，不落 Milvus）。

        命中则移到末尾（LRU）；未命中则调 embedder 计算并写入，超容量时淘汰最旧条目。
        embedder 调用异常向上抛出，由 ``_alias_dedup`` 统一降级处理。
        """
        cached = self._embed_cache.get(text)
        if cached is not None:
            # 命中：移到末尾标记为最近使用。
            self._embed_cache.pop(text, None)
            self._embed_cache[text] = cached
            return cached

        vectors = await self._embedder.embed([text])  # type: ignore[union-attr]
        vec = vectors[0] if vectors else []
        self._embed_cache[text] = vec
        if len(self._embed_cache) > self._cache_size:
            # 淘汰最旧（插入顺序最前）。
            oldest = next(iter(self._embed_cache))
            self._embed_cache.pop(oldest, None)
        return vec
