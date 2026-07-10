"""知识图谱存储抽象接口与 DTO。

本模块只定义存储无关的抽象契约（``GraphStore`` ABC）与对外数据传输对象
（``GraphEntityDTO`` / ``GraphSubsetDTO`` / ``GraphStatsDTO``），不含任何具体图库
（Neo4j 等）实现。Neo4j 实现见同包 ``Neo4jGraphStore``（后续任务）。

设计要点（对齐 design.md 4.1）：
- 所有方法强制带 ``kb_id`` 做租户 / 知识库隔离（写入再带 ``tenant_id``），不存在
  跨 KB 返回节点 / 边的查询路径（Correctness Property 1 / Requirements 8.1）。
- 抽象接口保留可替换性，未来可换 PG+AGE 等其它图库。
- 写入以 ``upsert_graph`` 幂等 MERGE 完成（Requirements 3.1/3.2/3.3）。

``upsert_graph`` 的入参 ``ExtractedEntity`` / ``ExtractedRelation`` 定义在抽取器模块
``app.pipeline.graph.extractor``（design.md 4.2，由后续任务 3.1 创建）。为避免与抽取器
形成导入环、且本任务仅交付接口与 DTO，这里通过 ``TYPE_CHECKING`` 仅做类型期导入，
配合 ``from __future__ import annotations`` 使运行时不真正 import，保持单向依赖清晰。
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.config import get_settings

if TYPE_CHECKING:
    # 仅类型检查期可见；运行时不导入，避免与抽取器模块形成导入环。
    from app.pipeline.graph.extractor import ExtractedEntity, ExtractedRelation
    from app.retrieval.config import PlatformConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DTO：对外数据传输对象
# ---------------------------------------------------------------------------


@dataclass
class GraphNeighborDTO:
    """实体详情里的一个邻居（``get_entity`` 用）。

    Attributes:
        id: 邻居实体 id。
        name: 邻居实体规范名。
        type: 邻居实体类型。
        rel_type: 当前实体与该邻居之间关系的类型（``:REL`` 的 ``type`` 属性）。
    """

    id: str
    name: str
    type: str
    rel_type: str


@dataclass
class GraphChunkRefDTO:
    """实体关联的原文 chunk 引用（``get_entity`` 用，供前端预览 / 反查原文）。

    Attributes:
        chunk_id: 来源 child/parent chunk id。
        doc_id: 来源文档 id。
        content_preview: chunk 内容预览（截断片段，可为空）。
    """

    chunk_id: str
    doc_id: str
    content_preview: str = ""


@dataclass
class GraphEntityDTO:
    """实体数据传输对象。

    既用于 ``find_entities_by_names``（检索融合命中实体，``neighbors`` / ``chunks``
    通常为空），也用于 ``get_entity``（实体详情，填充 ``neighbors`` 与 ``chunks``）。

    Attributes:
        id: 实体 id（全局唯一 UUID）。
        name: 规范名（normalize 后）。
        type: 实体类型（白名单内）。
        aliases: 别名集合（消歧合并的其它表面形态）。
        attributes: 属性描述（LLM 抽取）。
        degree: 物化度数（入边 + 出边）。
        chunk_ids: 来源 chunk id 列表（反查原文）。
        doc_ids: 来源文档 id 列表（删除文档时按此批删）。
        neighbors: 邻居列表（详情接口填充；列表查询通常为空）。
        chunks: 关联原文 chunk 引用（详情接口填充；列表查询通常为空）。
    """

    id: str
    name: str
    type: str
    aliases: list[str] = field(default_factory=list)
    attributes: list[str] = field(default_factory=list)
    degree: int = 0
    chunk_ids: list[str] = field(default_factory=list)
    doc_ids: list[str] = field(default_factory=list)
    neighbors: list[GraphNeighborDTO] = field(default_factory=list)
    chunks: list[GraphChunkRefDTO] = field(default_factory=list)


@dataclass
class GraphEventDTO:
    """事件数据传输对象（事件中心检索的一等检索单元，对齐 design.md 3.2.1）。

    既用于实体桥接召回（``events_by_entities``）、事件多跳扩展（``expand_events``），
    也用于按 id 批量取详情（``events_by_ids``）。事件的「文本与关系」存 Neo4j（多跳遍历），
    「向量召回」走 Milvus（event 集合），两者用 ``id`` 对齐。

    Attributes:
        id: 事件 id（全局唯一，由 worker 预生成 UUID）。
        title: 事件短标题。
        summary: 一句话摘要。
        content: 完整语义内容（主谓宾时地齐全，挂向量做召回）。
        chunk_id: 来源 chunk id（回取原文用）。
        doc_id: 来源文档 id（按 doc 删除 / 重处理用）。
        entity_ids: 事件 MENTIONS 的实体 id 列表（桥接多跳用）。
        entity_names: 事件 MENTIONS 的实体规范名列表（文档详情展示用，默认空；
            仅 ``events_by_doc`` 等需要直接展示名称的查询填充，其它查询留空）。
        score: 召回 / 排序得分（实体桥接入口为被提及次数，向量入口为相似度；默认 0.0）。
    """

    id: str
    title: str = ""
    summary: str = ""
    content: str = ""
    chunk_id: str = ""
    doc_id: str = ""
    entity_ids: list[str] = field(default_factory=list)
    entity_names: list[str] = field(default_factory=list)
    score: float = 0.0


@dataclass
class GraphEventMentionDTO:
    """事件详情里被 ``MENTIONS`` 的一个实体（``get_event`` 用，前端可点击 pivot）。

    Attributes:
        id: 实体 id。
        name: 实体规范名。
        type: 实体类型。
    """

    id: str
    name: str
    type: str


@dataclass
class GraphEventDetailDTO:
    """事件详情传输对象（可视化事件详情面板用，``get_event``）。

    Attributes:
        id: 事件 id。
        title: 事件短标题。
        summary: 一句话摘要。
        content: 完整语义内容。
        chunk_id: 来源 chunk id。
        doc_id: 来源文档 id。
        mentions: 事件关联（MENTIONS）的实体列表（可点击 pivot）。
        chunk: 来源 chunk 的原文预览（可为 None）。
    """

    id: str
    title: str = ""
    summary: str = ""
    content: str = ""
    chunk_id: str = ""
    doc_id: str = ""
    mentions: list[GraphEventMentionDTO] = field(default_factory=list)
    chunk: "GraphChunkRefDTO | None" = None


@dataclass
class GraphNodeDTO:
    """子图中的一个节点（``GraphSubsetDTO.nodes`` 元素，对应可视化 API 的 node）。

    Attributes:
        id: 节点 id（实体 id 或事件 id）。
        name: 节点显示名（实体规范名或事件标题）。
        type: 节点细分类型（实体类型，或事件节点的 ``event``）。
        degree: 物化度数（节点大小映射用）。
        node_type: 节点大类，``entity``（默认）或 ``event``，供可视化区分两层节点
            （事件中心图谱，Requirements 4.3）。
    """

    id: str
    name: str
    type: str
    degree: int = 0
    node_type: str = "entity"


@dataclass
class GraphEdgeDTO:
    """子图中的一条边（``GraphSubsetDTO.edges`` 元素，对应可视化 API 的 edge）。

    Attributes:
        source: 头实体 id。
        target: 尾实体 id。
        type: 关系类型（``:REL`` 的 ``type`` 属性）。
        weight: 关系权重（同一对实体同一类型关系的累计出现次数）。
    """

    source: str
    target: str
    type: str
    weight: int = 1


@dataclass
class GraphSubsetMeta:
    """子图查询的元信息（``GraphSubsetDTO.meta``）。

    Attributes:
        mode: 查询模式（``overview`` | ``ego`` 等）。
        total: 满足条件的节点总数（截断前）。
        returned: 实际返回的节点数（截断后）。
        truncated: 是否因上限被截断（``returned < total``）。
        center: ego 模式的中心节点 id / 名称（overview 模式为 None）。
        depth: ego 模式的 BFS 深度（overview 模式为 None）。
    """

    mode: str
    total: int = 0
    returned: int = 0
    truncated: bool = False
    center: str | None = None
    depth: int | None = None


@dataclass
class GraphSubsetDTO:
    """子图数据传输对象（``neighbors`` / ``overview`` 的返回，供可视化渲染）。

    Attributes:
        nodes: 节点列表。
        edges: 边列表（仅包含两端均在 ``nodes`` 内的边，无悬挂边）。
        meta: 元信息（含截断标记，对齐 Requirements 6.1 的 total/returned/truncated）。
    """

    nodes: list[GraphNodeDTO] = field(default_factory=list)
    edges: list[GraphEdgeDTO] = field(default_factory=list)
    meta: GraphSubsetMeta = field(default_factory=lambda: GraphSubsetMeta(mode="overview"))


@dataclass
class GraphStatsDTO:
    """图谱统计数据传输对象（``stats`` 的返回，供 ``/graph/stats`` 与前端空态判断）。

    Attributes:
        entity_count: 实体总数。
        relation_count: 关系总数。
        types: 实体类型分布（type -> count）。
        orphan_count: 孤立节点数（degree 为 0 的实体数）。
        status: 该 KB 图谱状态（如 none|pending|processing|completed|failed）。
    """

    entity_count: int = 0
    relation_count: int = 0
    types: dict[str, int] = field(default_factory=dict)
    orphan_count: int = 0
    status: str = "none"


@dataclass
class GraphCommunityDTO:
    """一个社区发现结果（GDS Louvain 的输出，未含摘要，阶段 4 扩展点）。

    ``detect_communities`` 的返回元素。摘要由上层用 LLM 对 ``member_names`` /
    ``relations`` 生成后落 PG（见 ``GraphCommunitySummaryDTO``）。

    Attributes:
        community_key: Louvain 社区编号（字符串化，跨重算稳定可比对）。
        level: 社区层级（预留多级；当前单级恒为 0）。
        member_entity_ids: 社区成员实体 id 列表。
        member_names: 社区成员实体规范名列表（喂 LLM 生成摘要用）。
        relations: 社区内部关系的三元组列表 ``(source_name, rel_type, target_name)``
            （喂 LLM 生成摘要用，截断到上限）。
        entity_count: 成员实体数。
        relation_count: 社区内部关系数。
    """

    community_key: str
    level: int = 0
    member_entity_ids: list[str] = field(default_factory=list)
    member_names: list[str] = field(default_factory=list)
    relations: list[tuple[str, str, str]] = field(default_factory=list)
    entity_count: int = 0
    relation_count: int = 0


@dataclass
class GraphCommunitySummaryDTO:
    """一个社区的摘要（已落 PG 的 ``graph_communities`` 行，供全局问答检索）。

    Attributes:
        community_key: Louvain 社区编号。
        level: 社区层级。
        title: 社区标题（LLM 生成，可空）。
        summary: 社区摘要正文（LLM 生成）。
        entity_count: 成员实体数。
        relation_count: 社区内部关系数。
        member_entity_ids: 社区成员实体 id 列表。
    """

    community_key: str
    summary: str
    level: int = 0
    title: str | None = None
    entity_count: int = 0
    relation_count: int = 0
    member_entity_ids: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 抽象接口
# ---------------------------------------------------------------------------


class GraphStore(ABC):
    """图存储抽象接口。

    所有方法强制带 ``kb_id`` 做租户 / 知识库隔离（写入时再带 ``tenant_id``）。具体图库
    实现（如 ``Neo4jGraphStore``）应保证：写入幂等（MERGE）、读查询带上限与超时、
    不存在跨 KB 返回数据的路径。
    """

    @abstractmethod
    async def ensure_schema(self) -> None:
        """幂等创建约束与索引。启动时调用一次，重复调用不报错。"""
        raise NotImplementedError

    @abstractmethod
    async def upsert_graph(
        self,
        *,
        kb_id: str,
        tenant_id: str | None,
        doc_id: str,
        entities: list[ExtractedEntity],
        relations: list[ExtractedRelation],
    ) -> tuple[int, int]:
        """幂等写入实体与关系（MERGE）。

        实体按 ``(kb_id, name)`` 合并，``chunk_ids`` / ``doc_ids`` / ``attributes`` /
        ``aliases`` 取并集（重复写入不产生重复元素）；关系两端实体须存在，端点缺失的
        关系不写入，同一对实体同一类型关系重复出现时累加 weight。

        Args:
            kb_id: 知识库 id（隔离键）。
            tenant_id: 租户 id（可为 None 表示无租户）。
            doc_id: 来源文档 id（写入实体 doc_ids / 关系 doc_id）。
            entities: 抽取并归一化后的实体列表。
            relations: 抽取并归一化后的关系列表。

        Returns:
            ``(新增/更新实体数, 关系数)``。
        """
        raise NotImplementedError

    @abstractmethod
    async def delete_by_doc(self, *, kb_id: str, doc_id: str) -> int:
        """删除某文档贡献的图数据。

        删除该文档贡献的关系，并从实体 ``doc_ids`` / ``chunk_ids`` 摘除该文档；实体可能
        被多文档共享，仅当其 ``doc_ids`` 变空时才删除该实体（删除策略见 design.md 4.6）。

        Args:
            kb_id: 知识库 id。
            doc_id: 待清理的文档 id。

        Returns:
            删除的关系数。
        """
        raise NotImplementedError

    @abstractmethod
    async def delete_by_kb(self, *, kb_id: str) -> None:
        """删除整个 KB 的图（KB 删除时调用），按 ``kb_id`` 批量删除全部实体与关系。"""
        raise NotImplementedError

    @abstractmethod
    async def upsert_events(
        self,
        *,
        kb_id: str,
        tenant_id: str | None,
        doc_id: str,
        events: list[dict],
    ) -> int:
        """幂等写入事件节点与 ``(:Event)-[:MENTIONS]->(:Entity)`` 边（事件中心图谱）。

        事件按 ``id`` 合并（id 由 worker 预生成、跨重处理稳定）；MENTIONS 端点实体须存在
        （按 ``(kb_id, name)`` 对齐实体合并键），端点缺失的关联在写入时被丢弃（无悬挂边，
        Correctness Property 1 / Requirements 1.2、2.1）。读写强制带 ``kb_id`` 隔离，
        写入带 ``tenant_id``（Requirements 2.5）。

        Args:
            kb_id: 知识库 id（隔离键）。
            tenant_id: 租户 id（可为 None，仅 ON CREATE 写入）。
            doc_id: 来源文档 id（写入事件 ``doc_id``，供按 doc 删除 / 重处理）。
            events: 事件行字典列表，每行含
                ``id`` / ``title`` / ``summary`` / ``content`` / ``chunk_id`` /
                ``entity_names``（关联实体规范名列表，对齐实体合并键）。

        Returns:
            写入（入参）的事件数。
        """
        raise NotImplementedError

    @abstractmethod
    async def events_by_entities(
        self, *, kb_id: str, entity_ids: list[str], limit: int,
    ) -> list[GraphEventDTO]:
        """实体桥接入口：取这些实体被 ``MENTIONS`` 的事件（去重，按被提及次数降序）。

        强制带 ``kb_id`` 隔离。``score`` 取命中实体数（被多少个输入实体提及），由检索层
        再做评分归一。

        Args:
            kb_id: 知识库 id（隔离键）。
            entity_ids: 桥接实体 id 列表。
            limit: 返回事件数上限。

        Returns:
            事件 DTO 列表（按被提及次数降序）；无命中时 []。
        """
        raise NotImplementedError

    @abstractmethod
    async def expand_events(
        self, *, kb_id: str, event_ids: list[str], hops: int, max_events: int,
    ) -> list[GraphEventDTO]:
        """事件多跳扩展：``(:Event)-[:MENTIONS]->(:Entity)<-[:MENTIONS]-(:Event2)`` 路径。

        从种子事件出发，沿共享实体桥接到关联事件，做可配置跳数扩展（默认 1 跳）。强制带
        ``kb_id`` 隔离；结果去重、排除种子集合自身、截断到 ``max_events``。

        Args:
            kb_id: 知识库 id（隔离键）。
            event_ids: 种子事件 id 列表。
            hops: 事件层扩展跳数（一次「事件→共享实体→关联事件」算一跳）。
            max_events: 返回扩展事件数上限。

        Returns:
            扩展得到的事件 DTO 列表（不含种子自身）；无扩展时 []。
        """
        raise NotImplementedError

    @abstractmethod
    async def events_by_ids(
        self, *, kb_id: str, event_ids: list[str],
    ) -> list[GraphEventDTO]:
        """按 id 批量取事件详情（``title`` / ``summary`` / ``content`` / ``chunk_id`` / 关联实体）。

        Args:
            kb_id: 知识库 id（隔离键）。
            event_ids: 事件 id 列表。

        Returns:
            事件 DTO 列表（顺序不保证，调用方按需重排）；无命中时 []。
        """
        raise NotImplementedError

    @abstractmethod
    async def events_by_doc(
        self, *, kb_id: str, doc_id: str, limit: int,
    ) -> list[GraphEventDTO]:
        """取某文档抽取的全部事件（文档详情展示用，Requirements 4.2）。

        强制带 ``kb_id`` + ``doc_id`` 双重隔离。返回的事件 DTO 额外填充 ``entity_names``
        （关联实体规范名列表，供前端直接展示，无需二次反查实体）。

        Args:
            kb_id: 知识库 id（隔离键）。
            doc_id: 文档 id（隔离键）。
            limit: 返回事件数上限。

        Returns:
            事件 DTO 列表（按 chunk_id / title 稳定排序）；无命中 / 无效入参时 []。
        """
        raise NotImplementedError

    @abstractmethod
    async def get_event(self, *, kb_id: str, event_id: str) -> "GraphEventDetailDTO | None":
        """单个事件详情（可视化事件详情面板用）。

        强制带 ``kb_id`` 隔离（Property 1）。返回事件本体（title/summary/content）+ 关联实体
        （MENTIONS，可点击 pivot）+ 来源 chunk 原文预览。事件不存在（或不属于该 kb）返回 None。

        Args:
            kb_id: 知识库 id（隔离键）。
            event_id: 事件 id。

        Returns:
            事件详情 DTO，不存在时 None。
        """
        raise NotImplementedError

    @abstractmethod
    async def find_entities_by_names(
        self, *, kb_id: str, names: list[str], limit: int,
    ) -> list[GraphEntityDTO]:
        """按名称模糊匹配实体（检索融合用）。

        Args:
            kb_id: 知识库 id。
            names: 待匹配的实体名列表。
            limit: 返回实体数上限。

        Returns:
            命中的实体 DTO 列表（``neighbors`` / ``chunks`` 通常为空）。
        """
        raise NotImplementedError

    @abstractmethod
    async def neighbors(
        self,
        *,
        kb_id: str,
        entity_ids: list[str],
        hops: int,
        max_nodes: int,
        types: list[str] | None = None,
    ) -> GraphSubsetDTO:
        """取实体的 N 跳邻居子图（检索召回与 ego 可视化共用）。

        Args:
            kb_id: 知识库 id。
            entity_ids: 中心实体 id 列表。
            hops: BFS 跳数（服务端 clamp 到平台硬上限）。
            max_nodes: 返回节点数上限（截断时 meta.truncated=True）。
            types: 可选类型过滤白名单。

        Returns:
            邻居子图 DTO。
        """
        raise NotImplementedError

    @abstractmethod
    async def overview(
        self, *, kb_id: str, limit: int, types: list[str] | None = None,
    ) -> GraphSubsetDTO:
        """取度数最高的 top-N 节点及其内部边（overview 可视化）。

        Args:
            kb_id: 知识库 id。
            limit: 返回节点数上限。
            types: 可选类型过滤白名单。

        Returns:
            概览子图 DTO（meta.mode='overview'）。
        """
        raise NotImplementedError

    @abstractmethod
    async def get_entity(self, *, kb_id: str, entity_id: str) -> GraphEntityDTO | None:
        """实体详情（含别名、属性、邻居与关联原文 chunk）。

        Args:
            kb_id: 知识库 id。
            entity_id: 实体 id。

        Returns:
            实体 DTO（填充 ``neighbors`` 与 ``chunks``），不存在时返回 None。
        """
        raise NotImplementedError

    @abstractmethod
    async def stats(self, *, kb_id: str) -> GraphStatsDTO:
        """统计：实体数 / 关系数 / 类型分布 / 孤立节点数 / 状态。"""
        raise NotImplementedError

    @abstractmethod
    async def detect_communities(self, *, kb_id: str) -> list[GraphCommunityDTO]:
        """对某 KB 的实体图做社区发现（GraphRAG Global，阶段 4 扩展点）。

        默认实现用 Neo4j GDS Louvain（需 GDS 插件）。GDS 不可用时**优雅降级返回空列表**
        （warning 不抛错），不影响阶段 1~3 的图谱能力（对齐 design.md「优雅降级」原则）。

        Args:
            kb_id: 知识库 id（隔离键）。

        Returns:
            社区列表（按成员数降序，已过滤过小社区）；GDS 不可用或无图数据时返回 []。
        """
        raise NotImplementedError

    @abstractmethod
    async def community_summaries(
        self, *, kb_id: str, limit: int | None = None,
    ) -> list[GraphCommunitySummaryDTO]:
        """读取某 KB 已落库（PG ``graph_communities``）的社区摘要（全局问答检索用）。

        Args:
            kb_id: 知识库 id（隔离键）。
            limit: 返回条数上限（None 表示不限）。

        Returns:
            社区摘要列表（按成员实体数降序）；无数据时返回 []。
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Neo4j 实现
# ---------------------------------------------------------------------------


# 启动时幂等创建的约束与索引（对齐 design.md 3.1）。
# 全部带 IF NOT EXISTS，重复执行不报错（Requirements 3.4）。
_SCHEMA_STATEMENTS: tuple[str, ...] = (
    # 实体唯一性：同一 KB 内规范名唯一（实体合并的依据，Requirements 3.2）
    "CREATE CONSTRAINT entity_kb_name_unique IF NOT EXISTS "
    "FOR (e:Entity) REQUIRE (e.kb_id, e.name) IS UNIQUE",
    # 按 id 查询（详情接口）
    "CREATE CONSTRAINT entity_id_unique IF NOT EXISTS "
    "FOR (e:Entity) REQUIRE e.id IS UNIQUE",
    # 按 (kb_id, type) 过滤（可视化类型筛选 + 按类型统计）
    "CREATE INDEX entity_kb_type_idx IF NOT EXISTS "
    "FOR (e:Entity) ON (e.kb_id, e.type)",
    # 按 degree 排序（overview top-N）
    "CREATE INDEX entity_kb_degree_idx IF NOT EXISTS "
    "FOR (e:Entity) ON (e.kb_id, e.degree)",
    # 按 name 做 CONTAINS 模糊匹配（实体桥接召回 + 图搜索框）
    "CREATE INDEX entity_name_idx IF NOT EXISTS "
    "FOR (e:Entity) ON (e.name)",
    # ---- 事件节点（事件中心图谱，对齐 design.md 3.2.1）----
    # 事件唯一性：事件 id 全局唯一（事件按 (kb_id, id) 合并，id 由 worker 预生成）
    "CREATE CONSTRAINT event_id_unique IF NOT EXISTS "
    "FOR (ev:Event) REQUIRE ev.id IS UNIQUE",
    # 按 kb_id 过滤（事件读写强制带 kb_id 隔离，Requirements 2.5）
    "CREATE INDEX event_kb_idx IF NOT EXISTS "
    "FOR (ev:Event) ON (ev.kb_id)",
)


# upsert 幂等写入的 Cypher（对齐 design.md 4.1）。大批量用 apoc.periodic.iterate 分批
# （batchSize 1000）抗压，避免单事务过大；params 同时传给内外两段语句。

# 实体：按 (kb_id, name) MERGE；attributes/aliases/chunk_ids/doc_ids 经 apoc.coll.toSet
# 取并集（幂等：重复 upsert 不产生重复元素，Property 2 / Requirements 3.1）。
_UPSERT_ENTITIES_CYPHER = (
    "CALL apoc.periodic.iterate("
    "  'UNWIND $entities AS row RETURN row',"
    "  'MERGE (e:Entity {kb_id: $kb_id, name: row.name}) "
    "   ON CREATE SET e.id = row.id, e.tenant_id = $tenant_id, "
    "                 e.display_name = row.display_name, e.type = row.type, "
    "                 e.created_at = datetime() "
    "   SET e.attributes = apoc.coll.toSet(coalesce(e.attributes, []) + row.attributes), "
    "       e.aliases    = apoc.coll.toSet(coalesce(e.aliases, []) + row.aliases), "
    "       e.chunk_ids  = apoc.coll.toSet(coalesce(e.chunk_ids, []) + row.chunk_ids), "
    "       e.doc_ids    = apoc.coll.toSet(coalesce(e.doc_ids, []) + [$doc_id]), "
    "       e.updated_at = datetime()',"
    "  {batchSize: 1000, params: {entities: $entities, kb_id: $kb_id, "
    "                             tenant_id: $tenant_id, doc_id: $doc_id}}"
    ")"
)

# 关系：两端按 (kb_id, name) MATCH（端点缺失则该行无匹配、关系不写入 → 无悬挂边，
# Property 3 / Requirements 3.3）；按 (type) MERGE，weight 累加，记录证据 chunk/doc。
_UPSERT_RELATIONS_CYPHER = (
    "CALL apoc.periodic.iterate("
    "  'UNWIND $relations AS row RETURN row',"
    "  'MATCH (s:Entity {kb_id: $kb_id, name: row.source}) "
    "   MATCH (t:Entity {kb_id: $kb_id, name: row.target}) "
    "   MERGE (s)-[r:REL {type: row.type}]->(t) "
    "   ON CREATE SET r.weight = 0, r.created_at = datetime() "
    "   SET r.weight = r.weight + 1, r.chunk_id = row.chunk_id, r.doc_id = $doc_id, "
    "       r.attributes = apoc.coll.toSet(coalesce(r.attributes, []) + row.attributes), "
    "       r.confidence = coalesce(row.confidence, 1.0), r.updated_at = datetime()',"
    "  {batchSize: 1000, params: {relations: $relations, kb_id: $kb_id, doc_id: $doc_id}}"
    ")"
)

# 后置刷新本批受影响实体的物化 degree（= 实际 (e)-[:REL]-() 计数，Property 4）。
_REFRESH_DEGREE_CYPHER = (
    "CALL apoc.periodic.iterate("
    "  'UNWIND $names AS nm RETURN nm',"
    "  'MATCH (e:Entity {kb_id: $kb_id, name: nm}) "
    "   SET e.degree = COUNT { (e)-[:REL]-() }',"
    "  {batchSize: 1000, params: {names: $names, kb_id: $kb_id}}"
    ")"
)


# ---------------------------------------------------------------------------
# 事件节点与边相关 Cypher（事件中心图谱，对齐 design.md 2.1/3.2.1）
# ---------------------------------------------------------------------------
# 事件读写全部强制带 kb_id 隔离（写入再带 tenant_id，Requirements 2.5），大批量写入
# 复用 apoc.periodic.iterate 分批（batchSize 1000）+ graph_query_timeout，风格与实体/
# 关系 upsert 段一致。MENTIONS 端点实体须存在（内层 MATCH 无匹配则丢弃，无悬挂边）。

# 事件详情统一 RETURN 片段：取本体属性 + 经 MENTIONS 关联的实体 id 列表（pattern
# comprehension，端点必为已存在的 :Entity）。所有字段 coalesce 兜底，避免历史脏数据 None。
_EVENT_RETURN_FIELDS = (
    "ev.id AS id, coalesce(ev.title, '') AS title, coalesce(ev.summary, '') AS summary, "
    "coalesce(ev.content, '') AS content, coalesce(ev.chunk_id, '') AS chunk_id, "
    "coalesce(ev.doc_id, '') AS doc_id, "
    "[(ev)-[:MENTIONS]->(me:Entity) | me.id] AS entity_ids"
)

# 1) 事件节点幂等 MERGE：按 id 合并（id 由 worker 预生成、跨重处理稳定）。
#    kb_id/tenant_id 仅 ON CREATE 写入（隔离键不可变）；正文字段每次 SET（重处理覆盖最新）。
_UPSERT_EVENTS_CYPHER = (
    "CALL apoc.periodic.iterate("
    "  'UNWIND $events AS row RETURN row',"
    "  'MERGE (ev:Event {id: row.id}) "
    "   ON CREATE SET ev.kb_id = $kb_id, ev.tenant_id = $tenant_id, "
    "                 ev.created_at = datetime() "
    "   SET ev.doc_id = $doc_id, ev.chunk_id = row.chunk_id, ev.title = row.title, "
    "       ev.summary = row.summary, ev.content = row.content, "
    "       ev.updated_at = datetime()',"
    "  {batchSize: 1000, params: {events: $events, kb_id: $kb_id, "
    "                             tenant_id: $tenant_id, doc_id: $doc_id}}"
    ")"
)

# 2) MENTIONS 边幂等 MERGE：事件关联实体名对齐实体合并键 (kb_id, name)；端点实体缺失
#    （内层 MATCH 无匹配）则该边不写入 → 无悬挂边（Property 1 / Requirements 1.2、2.1）。
_UPSERT_EVENT_MENTIONS_CYPHER = (
    "CALL apoc.periodic.iterate("
    "  'UNWIND $events AS row UNWIND row.entity_names AS nm "
    "   RETURN row.id AS event_id, nm AS name',"
    "  'MATCH (ev:Event {id: event_id, kb_id: $kb_id}) "
    "   MATCH (e:Entity {kb_id: $kb_id, name: name}) "
    "   MERGE (ev)-[:MENTIONS]->(e)',"
    "  {batchSize: 1000, params: {events: $events, kb_id: $kb_id}}"
    ")"
)

# events_by_entities（实体桥接入口）：取这些实体被 MENTIONS 的事件，按被提及（命中实体）
# 次数降序去重、截断到 limit。score 取命中实体数（与现状评分量纲解耦，检索层再归一）。
_EVENTS_BY_ENTITIES_CYPHER = (
    "UNWIND $entity_ids AS eid "
    "MATCH (e:Entity {kb_id: $kb_id, id: eid})<-[:MENTIONS]-(ev:Event {kb_id: $kb_id}) "
    "WITH ev, count(DISTINCT eid) AS mention_count "
    "RETURN " + _EVENT_RETURN_FIELDS + ", mention_count AS score "
    "ORDER BY mention_count DESC "
    "LIMIT $limit"
)

# expand_events（事件多跳）：种子事件经共享实体桥接到关联事件
# （(:Event)-[:MENTIONS]->(:Entity)<-[:MENTIONS]-(:Event2)）。用 apoc.path.expandConfig
# 沿 MENTIONS 无向扩展，labelFilter '/Event' 仅返回以事件为终点的路径；maxLevel=2*hops
# （一来一回算一跳），minLevel=2 跳过中间实体与种子，过滤掉种子集合自身，截断到 max_events。
# 注意：必须用 expandConfig 而非 subgraphNodes —— 后者要求 minLevel ∈ {0,1}，而事件多跳的
# 「事件→实体→事件」单跳对应底层 2 个 MENTIONS（minLevel=2），故只能用支持任意 minLevel 的
# expandConfig。uniqueness=NODE_GLOBAL 保证每个事件全局仅访问一次（去重 + 抗膨胀）。
_EXPAND_EVENTS_CYPHER = (
    "MATCH (seed:Event {kb_id: $kb_id}) WHERE seed.id IN $event_ids "
    "CALL apoc.path.expandConfig(seed, "
    "  {relationshipFilter: 'MENTIONS', labelFilter: '/Event', "
    "   minLevel: 2, maxLevel: $max_level, uniqueness: 'NODE_GLOBAL'}) YIELD path "
    "WITH last(nodes(path)) AS ev "
    "WITH DISTINCT ev "
    "WHERE ev.kb_id = $kb_id AND NOT ev.id IN $event_ids "
    "RETURN " + _EVENT_RETURN_FIELDS + " "
    "LIMIT $max_events"
)

# events_by_ids：按 id 批量取事件详情（kb 内）。
_EVENTS_BY_IDS_CYPHER = (
    "MATCH (ev:Event {kb_id: $kb_id}) WHERE ev.id IN $event_ids "
    "RETURN " + _EVENT_RETURN_FIELDS
)

# get_event：按单个 id 取事件详情，附带关联实体的 id + 规范名 + 类型（前端详情面板用，
# 邻居可点击 pivot）。强制带 kb_id 隔离。
_GET_EVENT_CYPHER = (
    "MATCH (ev:Event {kb_id: $kb_id, id: $event_id}) "
    "RETURN ev.id AS id, coalesce(ev.title, '') AS title, "
    "coalesce(ev.summary, '') AS summary, coalesce(ev.content, '') AS content, "
    "coalesce(ev.chunk_id, '') AS chunk_id, coalesce(ev.doc_id, '') AS doc_id, "
    "[(ev)-[:MENTIONS]->(me:Entity) | {id: me.id, name: me.name, type: me.type}] AS entities"
)

# events_by_doc（文档详情事件展示，Requirements 4.2）：取某文档抽取的全部事件，
# 附带关联实体「规范名」列表（前端直接展示，无需二次反查实体）。按 chunk_id / title
# 稳定排序，截断到 limit。强制带 kb_id + doc_id 双重隔离。
_EVENTS_BY_DOC_CYPHER = (
    "MATCH (ev:Event {kb_id: $kb_id, doc_id: $doc_id}) "
    "RETURN ev.id AS id, coalesce(ev.title, '') AS title, "
    "coalesce(ev.summary, '') AS summary, coalesce(ev.content, '') AS content, "
    "coalesce(ev.chunk_id, '') AS chunk_id, coalesce(ev.doc_id, '') AS doc_id, "
    "[(ev)-[:MENTIONS]->(me:Entity) | me.id] AS entity_ids, "
    "[(ev)-[:MENTIONS]->(me:Entity) | me.name] AS entity_names "
    "ORDER BY ev.chunk_id, ev.title "
    "LIMIT $limit"
)


# ---------------------------------------------------------------------------
# 删除相关 Cypher（对齐 design.md 4.6，Property 7 / Requirements 5.1、5.3）
# ---------------------------------------------------------------------------
# 全部强制带 kb_id 做隔离（Property 1 / Requirements 8.1），大批量用
# apoc.periodic.iterate 分批（batchSize 1000）抗压，风格与 upsert 段一致。

# 删前先精确统计该 doc 贡献的关系数（作为 delete_by_doc 返回值）。关系按建图方向
# (s)-[:REL]->(t) 存储且仅记录单一 doc_id（最后写入者），用有向模式计数避免无向
# 模式对同一条边正反两次匹配导致的重复计数。
_COUNT_DOC_RELATIONS_CYPHER = (
    "MATCH (:Entity {kb_id: $kb_id})-[r:REL {doc_id: $doc_id}]->"
    "(:Entity {kb_id: $kb_id}) "
    "RETURN count(r) AS cnt"
)

# 删前收集受该 doc 影响的实体 id（doc_ids 含本 doc 者，已涵盖该 doc 关系的两端，
# 因关系两端在 upsert 时必然把本 doc 写入了各自 doc_ids）。用于删除完成后按 id
# 刷新「存活」实体的 degree（已被删除的实体 MATCH 不到、自然跳过）。
_COLLECT_AFFECTED_ENTITY_IDS_CYPHER = (
    "MATCH (e:Entity {kb_id: $kb_id}) WHERE $doc_id IN e.doc_ids "
    "RETURN collect(e.id) AS ids"
)

# 1) 删该 doc 贡献的关系（有向匹配，DELETE r）。
_DELETE_DOC_RELATIONS_CYPHER = (
    "CALL apoc.periodic.iterate("
    "  'MATCH (:Entity {kb_id: $kb_id})-[r:REL {doc_id: $doc_id}]->"
    "(:Entity {kb_id: $kb_id}) RETURN r',"
    "  'DELETE r',"
    "  {batchSize: 1000, params: {kb_id: $kb_id, doc_id: $doc_id}}"
    ")"
)

# 2) 从实体 doc_ids 摘除该 doc（共享实体保留，仅剔除本 doc 的贡献，Property 7）。
_STRIP_DOC_FROM_ENTITIES_CYPHER = (
    "CALL apoc.periodic.iterate("
    "  'MATCH (e:Entity {kb_id: $kb_id}) WHERE $doc_id IN e.doc_ids RETURN e',"
    "  'SET e.doc_ids = [x IN e.doc_ids WHERE x <> $doc_id]',"
    "  {batchSize: 1000, params: {kb_id: $kb_id, doc_id: $doc_id}}"
    ")"
)

# 3) 删除 doc_ids 摘空后不再被任何文档引用的实体（DETACH 连带其残余关系）。
_DELETE_EMPTIED_ENTITIES_CYPHER = (
    "CALL apoc.periodic.iterate("
    "  'MATCH (e:Entity {kb_id: $kb_id}) WHERE size(e.doc_ids) = 0 RETURN e',"
    "  'DETACH DELETE e',"
    "  {batchSize: 1000, params: {kb_id: $kb_id}}"
    ")"
)

# 4) 按 id 刷新存活受影响实体的物化 degree（已删除实体匹配不到、自动跳过，Property 4）。
_REFRESH_DEGREE_BY_ID_CYPHER = (
    "CALL apoc.periodic.iterate("
    "  'UNWIND $ids AS eid RETURN eid',"
    "  'MATCH (e:Entity {kb_id: $kb_id, id: eid}) "
    "   SET e.degree = COUNT { (e)-[:REL]-() }',"
    "  {batchSize: 1000, params: {ids: $ids, kb_id: $kb_id}}"
    ")"
)

# 按 kb_id 批量删除整个 KB 的图：DETACH DELETE 全部 Entity 即连带删除其全部关系
# （REL 仅存在于同 KB 的 Entity 之间），分批抗压（Requirements 5.3）。
_DELETE_KB_CYPHER = (
    "CALL apoc.periodic.iterate("
    "  'MATCH (e:Entity {kb_id: $kb_id}) RETURN e',"
    "  'DETACH DELETE e',"
    "  {batchSize: 1000, params: {kb_id: $kb_id}}"
    ")"
)

# 删该 doc 贡献的事件节点（事件中心图谱，Requirements 2.3/2.4，Property 3 幂等重处理）。
# 事件 doc_id 归属单一来源文档（重处理走「先按 doc_id 删后写」），故按 (kb_id, doc_id)
# 精确删除；DETACH DELETE 连带删除其 (:Event)-[:MENTIONS]->(:Entity) 边（无孤儿边残留）。
# 实体节点不在此删除——实体可能被其它事件 / 关系共享，其生命周期由实体侧删除逻辑管理。
_DELETE_DOC_EVENTS_CYPHER = (
    "CALL apoc.periodic.iterate("
    "  'MATCH (ev:Event {kb_id: $kb_id, doc_id: $doc_id}) RETURN ev',"
    "  'DETACH DELETE ev',"
    "  {batchSize: 1000, params: {kb_id: $kb_id, doc_id: $doc_id}}"
    ")"
)

# 按 kb_id 批量删除整个 KB 的事件节点（DETACH DELETE 连带其全部 MENTIONS 边）。
# 与 _DELETE_KB_CYPHER 配套：删 KB 时实体与事件均清空，不留孤儿（Requirements 2.4/5.3）。
_DELETE_KB_EVENTS_CYPHER = (
    "CALL apoc.periodic.iterate("
    "  'MATCH (ev:Event {kb_id: $kb_id}) RETURN ev',"
    "  'DETACH DELETE ev',"
    "  {batchSize: 1000, params: {kb_id: $kb_id}}"
    ")"
)


# ---------------------------------------------------------------------------
# 读查询相关 Cypher（对齐 design.md 4.1 / API 5.1，Property 1/9 / Requirements 6.x、8.1、9.1）
# ---------------------------------------------------------------------------
# 全部强制带 kb_id 做隔离（Property 1 / Requirements 8.1）；上限 / 跳数由调用前服务端
# clamp 到平台硬上限（Property 9 / Requirements 9.1），并经 session.run(timeout=...) 设
# 事务超时。所有返回字段对 list / 数值做 coalesce 兜底，避免历史脏数据导致 None。

# find_entities_by_names：在 kb 内对每个输入名做 CONTAINS 模糊匹配（走 entity_name_idx），
# 多名命中去重，按 degree 降序取 top-limit（检索融合命中实体，neighbors/chunks 留空）。
_FIND_ENTITIES_CYPHER = (
    "UNWIND $names AS nm "
    "MATCH (e:Entity {kb_id: $kb_id}) "
    "WHERE e.name CONTAINS nm "
    "WITH DISTINCT e "
    "RETURN e.id AS id, e.name AS name, e.type AS type, "
    "       coalesce(e.aliases, []) AS aliases, coalesce(e.attributes, []) AS attributes, "
    "       coalesce(e.degree, 0) AS degree, coalesce(e.chunk_ids, []) AS chunk_ids, "
    "       coalesce(e.doc_ids, []) AS doc_ids "
    "ORDER BY degree DESC "
    "LIMIT $limit"
)

# neighbors：从中心实体集合做 N 跳 BFS 子图（APOC subgraphNodes，REL 双向）。
# 在 kb 内过滤、可选 type 过滤（中心节点恒保留）；先 collect 求全邻域 total，再按
# 「中心优先、degree 降序」截断到 max_nodes（截断时 returned < total → truncated）。
_NEIGHBORS_NODES_CYPHER = (
    "MATCH (c:Entity {kb_id: $kb_id}) WHERE c.id IN $entity_ids "
    "CALL apoc.path.subgraphNodes(c, "
    "  {relationshipFilter: 'REL', minLevel: 0, maxLevel: $hops}) YIELD node "
    "WITH node WHERE node.kb_id = $kb_id "
    "  AND ($types IS NULL OR node.type IN $types OR node.id IN $entity_ids) "
    "WITH collect(DISTINCT node) AS ns "
    "WITH ns, size(ns) AS total "
    "UNWIND ns AS n "
    "WITH total, n "
    "ORDER BY (CASE WHEN n.id IN $entity_ids THEN 1 ELSE 0 END) DESC, coalesce(n.degree, 0) DESC "
    "LIMIT $max_nodes "
    "RETURN total, collect({id: n.id, name: n.name, type: n.type, "
    "                       degree: coalesce(n.degree, 0)}) AS nodes"
)

# 子图内部边：仅取两端均在返回节点集合内的边（无悬挂边，Property 3）。neighbors / overview 共用。
_INTERNAL_EDGES_CYPHER = (
    "MATCH (s:Entity {kb_id: $kb_id})-[r:REL]->(t:Entity {kb_id: $kb_id}) "
    "WHERE s.id IN $node_ids AND t.id IN $node_ids "
    "RETURN s.id AS source, t.id AS target, r.type AS type, coalesce(r.weight, 1) AS weight"
)

# overview：kb 内（可选 type 过滤）实体总数，作为 meta.total（截断前）。
_OVERVIEW_TOTAL_CYPHER = (
    "MATCH (e:Entity {kb_id: $kb_id}) "
    "WHERE ($types IS NULL OR e.type IN $types) "
    "RETURN count(e) AS total"
)

# overview：按 degree 降序取 top-limit（走 entity_kb_degree_idx）。
_OVERVIEW_NODES_CYPHER = (
    "MATCH (e:Entity {kb_id: $kb_id}) "
    "WHERE ($types IS NULL OR e.type IN $types) "
    "WITH e ORDER BY coalesce(e.degree, 0) DESC "
    "LIMIT $limit "
    "RETURN collect({id: e.id, name: e.name, type: e.type, "
    "                degree: coalesce(e.degree, 0)}) AS nodes"
)

# get_entity：按 id 取实体本体属性（kb 内）。不存在则无记录返回 None。
_GET_ENTITY_CYPHER = (
    "MATCH (e:Entity {kb_id: $kb_id, id: $entity_id}) "
    "RETURN e.id AS id, e.name AS name, e.type AS type, "
    "       coalesce(e.aliases, []) AS aliases, coalesce(e.attributes, []) AS attributes, "
    "       coalesce(e.degree, 0) AS degree, coalesce(e.chunk_ids, []) AS chunk_ids, "
    "       coalesce(e.doc_ids, []) AS doc_ids"
)

# get_entity：实体的直接邻居（一跳，含关系类型），按 degree 降序限量返回。
_GET_ENTITY_NEIGHBORS_CYPHER = (
    "MATCH (e:Entity {kb_id: $kb_id, id: $entity_id})-[r:REL]-(n:Entity {kb_id: $kb_id}) "
    "RETURN DISTINCT n.id AS id, n.name AS name, n.type AS type, r.type AS rel_type, "
    "       coalesce(n.degree, 0) AS degree "
    "ORDER BY degree DESC "
    "LIMIT $limit"
)

# stats：kb 内按 type 分组的实体数与孤立（degree=0）数；Python 侧汇总 entity_count/types/orphan_count。
_STATS_ENTITIES_CYPHER = (
    "MATCH (e:Entity {kb_id: $kb_id}) "
    "RETURN e.type AS type, count(e) AS cnt, "
    "       sum(CASE WHEN coalesce(e.degree, 0) = 0 THEN 1 ELSE 0 END) AS orphans"
)

# stats：kb 内关系总数（有向匹配，避免无向重复计数）。
_STATS_RELATIONS_CYPHER = (
    "MATCH (:Entity {kb_id: $kb_id})-[r:REL]->(:Entity {kb_id: $kb_id}) "
    "RETURN count(r) AS cnt"
)


# ---------------------------------------------------------------------------
# 阶段 4（GraphRAG Global）：GDS Louvain 社区发现相关 Cypher
# ---------------------------------------------------------------------------
# 需 Neo4j GDS 插件（enterprise 或社区 GDS）。GDS 不可用时这些过程调用会抛错，由
# detect_communities 捕获并优雅降级返回空（warning 不 crash），不影响阶段 1~3。
# 全部强制带 kb_id 隔离（Property 1 / Requirements 8.1）。

# 用 Cypher 投影建命名子图：节点 / 关系查询都强制带 kb_id，保证社区发现只在本 KB 图上跑，
# 不跨 KB。投影名带 kb 标识 + 随机后缀，避免并发重算时撞名。
_GDS_PROJECT_CYPHER = (
    "CALL gds.graph.project.cypher("
    "  $graph_name,"
    "  'MATCH (e:Entity {kb_id: $kb_id}) RETURN id(e) AS id',"
    "  'MATCH (s:Entity {kb_id: $kb_id})-[:REL]->(t:Entity {kb_id: $kb_id}) "
    "   RETURN id(s) AS source, id(t) AS target',"
    "  {parameters: {kb_id: $kb_id}}"
    ") YIELD graphName, nodeCount, relationshipCount "
    "RETURN nodeCount, relationshipCount"
)

# 在投影图上跑 Louvain，流式返回每个节点的社区编号（连带取回业务 id/name）。
_GDS_LOUVAIN_STREAM_CYPHER = (
    "CALL gds.louvain.stream($graph_name) YIELD nodeId, communityId "
    "RETURN gds.util.asNode(nodeId).id AS entity_id, "
    "       gds.util.asNode(nodeId).name AS name, communityId AS community_id"
)

# 用完即删投影图（释放 GDS 内存）。failIfMissing=false 容忍重复 drop。
_GDS_DROP_CYPHER = "CALL gds.graph.drop($graph_name, false) YIELD graphName RETURN graphName"

# 取 kb 内全部内部关系（用于按社区分组、喂 LLM 生成摘要）。
_KB_RELATIONS_CYPHER = (
    "MATCH (s:Entity {kb_id: $kb_id})-[r:REL]->(t:Entity {kb_id: $kb_id}) "
    "RETURN s.id AS source_id, s.name AS source_name, r.type AS rel_type, "
    "       t.id AS target_id, t.name AS target_name"
)


# get_entity 关联原文 chunk 的预览拉取上限与预览长度（防一次返回过多 / 过长内容）。
_GET_ENTITY_MAX_CHUNKS = 20
_CHUNK_PREVIEW_LEN = 200

# 喂给 LLM 生成单社区摘要的内部关系三元组数上限（控制 prompt 长度，与成员数上限呼应）。
_COMMUNITY_MAX_RELATIONS_FOR_SUMMARY = 50


def _parse_community_summary(text: str) -> tuple[str | None, str]:
    """从社区摘要 LLM 输出文本中容错解析 ``(title, summary)``。

    模型按 ``COMMUNITY_SYSTEM_PROMPT`` 约定输出 ``{"title": ..., "summary": ...}``。
    解析策略与抽取器一致：剥 markdown fence、宽松定位首个 JSON 对象。解析失败时降级——
    把原始文本（去 fence、截断）整体当作 summary，title 置 None，保证不因解析失败丢摘要。

    Returns:
        ``(title, summary)``；summary 恒为非空字符串（兜底用原文），title 可为 None。
    """
    raw = (text or "").strip()
    if not raw:
        return None, ""

    # 剥 markdown 代码围栏。
    fence = re.search(r"```(?:json|JSON)?\s*(?P<body>.*?)```", raw, re.DOTALL)
    candidate = fence.group("body").strip() if fence else raw

    parsed: object = None
    try:
        parsed = json.loads(candidate)
    except (ValueError, TypeError):
        # 括号配对定位首个完整对象子串。
        start = candidate.find("{")
        if start != -1:
            depth = 0
            in_str = False
            escape = False
            for i in range(start, len(candidate)):
                ch = candidate[i]
                if in_str:
                    if escape:
                        escape = False
                    elif ch == "\\":
                        escape = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            parsed = json.loads(candidate[start : i + 1])
                        except (ValueError, TypeError):
                            parsed = None
                        break

    if isinstance(parsed, dict):
        title_val = parsed.get("title")
        summary_val = parsed.get("summary")
        title = str(title_val).strip() if title_val is not None else ""
        summary = str(summary_val).strip() if summary_val is not None else ""
        if summary:
            return (title or None), summary

    # 解析不到结构化 summary：降级用原文（截断）兜底，避免丢摘要。
    return None, candidate[:2000]


def _clamp(value: int, lo: int, hi: int) -> int:
    """把 value 夹到闭区间 ``[lo, hi]``（服务端硬 clamp，Property 9 / Requirements 9.1）。

    当 ``lo > hi``（理论上不应发生，平台配置范围保证 lo<=hi）时，以 lo 为准。
    """
    if hi < lo:
        return lo
    return max(lo, min(value, hi))


class Neo4jGraphStore(GraphStore):
    """``GraphStore`` 的 Neo4j 实现。

    通过 ``create()` 工厂构造并做优雅降级：全局开关未开启或 Neo4j 不可用 / 驱动未安装
    时返回 None，调用方据此整体关闭图谱功能（主链路零影响，对齐 design.md「优雅降级」与
    Requirements 7.2 / 9.3）。

    连接走 async 驱动并受连接池上限（``neo4j_max_pool_size``）与连接超时
    （``neo4j_conn_timeout``）约束（Requirements 9.2）。

    Note:
        ``create()`` 工厂、降级、``ensure_schema()`` 与进程内单例见 task 2.2；写入
        （``upsert_graph``）见 task 2.3，删除（``delete_*``）见 task 2.4，查询
        （``find_entities_by_names`` / ``neighbors`` / ``overview`` / ``get_entity`` /
        ``stats``）见 task 2.5。所有读查询强制带 ``kb_id``、设事务超时、对 depth/limit/
        max_nodes 做服务端 clamp（Property 1/9 / Requirements 6.x、8.1、9.1）。
    """

    def __init__(self, driver) -> None:
        """构造（一般经 ``create()`` 调用，不直接 new）。

        Args:
            driver: 已建立并通过连通性校验的 ``neo4j.AsyncDriver`` 实例。
        """
        self._driver = driver

    @classmethod
    async def create(cls) -> "Neo4jGraphStore | None":
        """工厂方法，沿用 ``TaskQueue.create()`` 的优雅降级范式。

        以下任一情况返回 None（图谱功能整体降级关闭，调用方据此跳过）：
        - 全局开关 ``GRAPH_ENABLE`` 不为 true（Requirements 9.3）；
        - ``neo4j`` 驱动未安装（optional extra，见任务 8.1）；
        - 连接 / 连通性校验失败。

        成功时返回已 ``ensure_schema()`` 的实例。
        """
        settings = get_settings()
        if not settings.graph_enable:
            # 全局未启用：不连 Neo4j、零额外成本（Requirements 9.3）
            return None

        # 懒导入：neo4j 为 optional extra（任务 8.1），未安装时本模块仍可正常 import，
        # 仅在真正需要连接时降级返回 None。
        try:
            import neo4j
        except ImportError:
            logger.warning(
                "GRAPH_ENABLE=true 但未安装 neo4j 驱动，知识图谱功能降级关闭"
                "（请安装 optional extra）"
            )
            return None

        driver = None
        try:
            driver = neo4j.AsyncGraphDatabase.driver(
                settings.neo4j_uri,
                auth=(settings.neo4j_username, settings.neo4j_password),
                max_connection_pool_size=settings.neo4j_max_pool_size,
                connection_timeout=settings.neo4j_conn_timeout,
            )
            await driver.verify_connectivity()
        except Exception as e:  # noqa: BLE001 - 任意连接异常都降级关闭，不影响主链路
            logger.warning("Neo4j 不可用，知识图谱功能降级关闭: %s", e)
            if driver is not None:
                try:
                    await driver.close()
                except Exception:  # noqa: BLE001 - 关闭失败仅忽略
                    pass
            return None

        store = cls(driver)
        try:
            await store.ensure_schema()
        except Exception as e:  # noqa: BLE001 - schema 创建失败仅 warning，整体降级
            logger.warning("Neo4j 约束/索引创建失败，知识图谱功能降级关闭: %s", e)
            try:
                await driver.close()
            except Exception:  # noqa: BLE001
                pass
            return None
        return store

    async def close(self) -> None:
        """关闭底层驱动（进程退出时调用，幂等容错）。"""
        try:
            await self._driver.close()
        except Exception as e:  # noqa: BLE001
            logger.warning("关闭 Neo4j 驱动失败: %s", e)

    async def ensure_schema(self) -> None:
        """幂等创建约束与索引（design.md 3.1），重复调用不报错（Requirements 3.4）。"""
        async with self._driver.session() as session:
            for stmt in _SCHEMA_STATEMENTS:
                await session.run(stmt)
        logger.info("Neo4j 约束与索引已就绪（%d 条）", len(_SCHEMA_STATEMENTS))

    # ------------------------------------------------------------------
    # 以下读写方法由后续任务实现（2.3/2.4/2.5）；当前为桩，保证本类可实例化。
    # ------------------------------------------------------------------

    async def upsert_graph(
        self,
        *,
        kb_id: str,
        tenant_id: str | None,
        doc_id: str,
        entities: list[ExtractedEntity],
        relations: list[ExtractedRelation],
    ) -> tuple[int, int]:
        """幂等写入实体与关系（APOC MERGE），后置刷新 degree（design.md 4.1）。

        将 duck-typed 的 ``ExtractedEntity`` / ``ExtractedRelation`` 转为 Cypher 行字典：

        - 实体行：``name`` 取规范名（``normalized_name`` 优先，回退 ``name``）作为
          ``(kb_id, name)`` 合并键；``display_name`` 取原始 ``name``；``id`` 在 ON CREATE
          时落库（这里为每行预生成 uuid，已存在实体不会覆盖既有 id）。
        - 关系行：``source`` / ``target`` 同样用规范名对齐实体合并键；端点缺失的关系因
          内层 ``MATCH`` 无匹配而被丢弃（无悬挂边，Property 3）。

        chunk/doc 来源契约（供 task 4.2 worker）：worker 在调用前可在每个实体上挂
        ``chunk_ids``（list）或单个 ``chunk_id``，在每条关系上挂 ``chunk_id``（单个）。
        这里以 ``getattr`` 防御式读取，缺失时回退空集合 / None，保持对未实现的抽取器模块
        的解耦。

        Args:
            kb_id: 知识库 id（隔离键）。
            tenant_id: 租户 id（可为 None）。
            doc_id: 来源文档 id。
            entities: 抽取并归一化后的实体列表（duck-typed）。
            relations: 抽取并归一化后的关系列表（duck-typed）。

        Returns:
            ``(写入实体数, 写入关系数)``。注意关系数为入参条数，端点缺失被丢弃的关系
            仍计入入参；精确落库计数由集成测试（task 2.6）覆盖。
        """
        # ---- 构造实体行字典 ----
        entity_rows: list[dict] = []
        # 本批涉及的规范名集合（实体 + 关系两端），用于后置 degree 刷新。
        affected_names: set[str] = set()
        for e in entities:
            # 规范名优先 normalized_name，回退原始 name，作为 (kb_id, name) 合并键。
            canonical = getattr(e, "normalized_name", "") or getattr(e, "name", "")
            if not canonical:
                # 无名实体跳过（防脏数据生成无意义节点）。
                continue
            # chunk_ids 防御式读取：优先现成 list，其次单个 chunk_id 包装为单元素 list。
            chunk_ids = list(getattr(e, "chunk_ids", None) or [])
            if not chunk_ids:
                single_chunk = getattr(e, "chunk_id", None)
                if single_chunk:
                    chunk_ids = [single_chunk]
            entity_rows.append(
                {
                    "id": str(uuid.uuid4()),  # 仅 ON CREATE 生效，不覆盖既有节点 id
                    "name": canonical,
                    "display_name": getattr(e, "name", canonical),
                    "type": getattr(e, "type", ""),
                    "aliases": list(getattr(e, "aliases", None) or []),
                    "attributes": list(getattr(e, "attributes", None) or []),
                    "chunk_ids": chunk_ids,
                }
            )
            affected_names.add(canonical)

        # ---- 构造关系行字典 ----
        relation_rows: list[dict] = []
        for r in relations:
            source = getattr(r, "source", "")
            target = getattr(r, "target", "")
            rel_type = getattr(r, "type", "")
            if not source or not target or not rel_type:
                continue
            relation_rows.append(
                {
                    "source": source,
                    "target": target,
                    "type": rel_type,
                    "attributes": list(getattr(r, "attributes", None) or []),
                    "chunk_id": getattr(r, "chunk_id", None),
                    "confidence": getattr(r, "confidence", 1.0),
                }
            )
            # 关系两端也纳入 degree 刷新（端点不在实体集合内时刷新无副作用）。
            affected_names.add(source)
            affected_names.add(target)

        settings = get_settings()
        timeout = settings.graph_query_timeout

        async with self._driver.session() as session:
            # 1) 实体幂等 MERGE（apoc.periodic.iterate 分批）。
            if entity_rows:
                await session.run(
                    _UPSERT_ENTITIES_CYPHER,
                    entities=entity_rows,
                    kb_id=kb_id,
                    tenant_id=tenant_id,
                    doc_id=doc_id,
                    timeout=timeout,
                )
            # 2) 关系幂等 MERGE（端点缺失自动丢弃 → 无悬挂边）。
            if relation_rows:
                await session.run(
                    _UPSERT_RELATIONS_CYPHER,
                    relations=relation_rows,
                    kb_id=kb_id,
                    doc_id=doc_id,
                    timeout=timeout,
                )
            # 3) 后置刷新受影响实体的物化 degree（Property 4）。
            if affected_names:
                await session.run(
                    _REFRESH_DEGREE_CYPHER,
                    names=list(affected_names),
                    kb_id=kb_id,
                    timeout=timeout,
                )

        return len(entity_rows), len(relation_rows)

    async def delete_by_doc(self, *, kb_id: str, doc_id: str) -> int:
        """删除某文档贡献的图数据（对齐 design.md 4.6，Property 7 / Requirements 5.1）。

        流程（全程强制带 ``kb_id`` 隔离，apoc.periodic.iterate 分批）：

        1. 先 COUNT 该 doc 贡献的关系数作为返回值；
        2. 收集 ``doc_ids`` 含本 doc 的实体 id（受影响实体，含该 doc 关系两端）；
        3. 删除该 doc 贡献的关系（``DELETE r``）；
        4. 从实体 ``doc_ids`` 摘除本 doc（共享实体保留，仅剔除本 doc 贡献）；
        5. ``DETACH DELETE`` ``doc_ids`` 摘空后的实体（不再被任何文档引用）；
        6. 按 id 刷新仍存活的受影响实体 degree（已删实体匹配不到自动跳过）；
        7. ``DETACH DELETE`` 该 doc 贡献的 ``:Event`` 节点（连带其 ``MENTIONS`` 边，
           事件中心图谱重处理「先删后写」的删旧步骤，Requirements 2.3/2.4）。

        删除完成后保证无 ``doc_id`` 残留：任何实体 ``doc_ids`` 与任何边 ``doc_id`` 都不
        再包含本 ``doc_id``；被多文档共享的实体仅在 ``doc_ids`` 变空时才删除（Property 7）。

        chunk_ids 说明：实体 ``chunk_ids`` 为跨文档累计并集、未按 doc 维度切分来源，
        无法仅凭 doc_id 精确摘除其中属于本 doc 的 chunk；遵循 design.md 4.6 的 Cypher，
        摘除以 ``doc_ids`` 为准，``doc_ids`` 摘空即整实体删除（其 ``chunk_ids`` 随之消失），
        故不存在跨 doc 的 chunk 残留风险。

        Args:
            kb_id: 知识库 id（隔离键）。
            doc_id: 待清理的文档 id。

        Returns:
            删除的关系数。
        """
        settings = get_settings()
        timeout = settings.graph_query_timeout

        async with self._driver.session() as session:
            # 1) 先统计待删关系数（作为返回值）。
            count_result = await session.run(
                _COUNT_DOC_RELATIONS_CYPHER,
                kb_id=kb_id,
                doc_id=doc_id,
                timeout=timeout,
            )
            count_record = await count_result.single()
            deleted_relations = int(count_record["cnt"]) if count_record else 0

            # 2) 收集受影响实体 id（删除前，含本 doc 关系两端）。
            ids_result = await session.run(
                _COLLECT_AFFECTED_ENTITY_IDS_CYPHER,
                kb_id=kb_id,
                doc_id=doc_id,
                timeout=timeout,
            )
            ids_record = await ids_result.single()
            affected_ids = list(ids_record["ids"]) if ids_record else []

            # 3) 删除该 doc 贡献的关系。
            await session.run(
                _DELETE_DOC_RELATIONS_CYPHER,
                kb_id=kb_id,
                doc_id=doc_id,
                timeout=timeout,
            )
            # 4) 从实体 doc_ids 摘除本 doc（共享实体保留）。
            await session.run(
                _STRIP_DOC_FROM_ENTITIES_CYPHER,
                kb_id=kb_id,
                doc_id=doc_id,
                timeout=timeout,
            )
            # 5) 删除 doc_ids 摘空后不再被引用的实体。
            await session.run(
                _DELETE_EMPTIED_ENTITIES_CYPHER,
                kb_id=kb_id,
                timeout=timeout,
            )
            # 6) 刷新仍存活的受影响实体 degree（已删除实体匹配不到、自动跳过）。
            if affected_ids:
                await session.run(
                    _REFRESH_DEGREE_BY_ID_CYPHER,
                    ids=affected_ids,
                    kb_id=kb_id,
                    timeout=timeout,
                )

            # 7) 删除该 doc 贡献的事件节点（连带 MENTIONS 边，事件中心图谱）。
            #    事件 doc_id 归属单一来源文档，按 (kb_id, doc_id) 精确删除即可；
            #    重处理走「先删后写」，删旧事件保证幂等无孤儿（Requirements 2.3/2.4）。
            await session.run(
                _DELETE_DOC_EVENTS_CYPHER,
                kb_id=kb_id,
                doc_id=doc_id,
                timeout=timeout,
            )

        return deleted_relations

    async def delete_by_kb(self, *, kb_id: str) -> None:
        """删除整个 KB 的图（KB 删除时调用，design.md 4.6 / Requirements 5.3）。

        按 ``kb_id`` ``DETACH DELETE`` 全部 ``Entity`` 即连带删除其全部关系（``:REL``
        仅存在于同 KB 实体之间），并 ``DETACH DELETE`` 全部 ``:Event`` 节点（连带其
        ``MENTIONS`` 边），保证实体与事件均清空、不留孤儿（Requirements 2.4）。
        两段均用 apoc.periodic.iterate 分批（batchSize 1000）抗压。

        Args:
            kb_id: 待清空的知识库 id。
        """
        settings = get_settings()
        timeout = settings.graph_query_timeout

        async with self._driver.session() as session:
            await session.run(
                _DELETE_KB_CYPHER,
                kb_id=kb_id,
                timeout=timeout,
            )
            # 连带删除该 KB 的事件节点（含 MENTIONS 边），不留孤儿（事件中心图谱）。
            await session.run(
                _DELETE_KB_EVENTS_CYPHER,
                kb_id=kb_id,
                timeout=timeout,
            )

    # ------------------------------------------------------------------
    # 事件节点与边（事件中心图谱，design.md 3.2.1，Requirements 2.1/2.5）
    # ------------------------------------------------------------------

    async def upsert_events(
        self,
        *,
        kb_id: str,
        tenant_id: str | None,
        doc_id: str,
        events: list[dict],
    ) -> int:
        """幂等写入事件节点 + ``(:Event)-[:MENTIONS]->(:Entity)`` 边（design.md 3.2.1）。

        两段 apoc.periodic.iterate（分批 1000）+ ``graph_query_timeout``，风格与
        ``upsert_graph`` 一致：

        1. 事件节点按 ``id`` MERGE（id 由 worker 预生成、跨重处理稳定）；``kb_id`` /
           ``tenant_id`` 仅 ON CREATE 写入（隔离键不可变），正文字段每次 SET（重处理覆盖）。
        2. MENTIONS 边：事件关联实体名对齐实体合并键 ``(kb_id, name)``；端点实体缺失
           （内层 MATCH 无匹配）则该边不写入 → 无悬挂边（Property 1 / Requirements 1.2、2.1）。

        入参防御：``id`` 缺失的事件行跳过（无 id 无法合并）；``entity_names`` 去空白去重，
        缺失时按空列表处理（仅写事件节点、无 MENTIONS 边）。

        Args:
            kb_id: 知识库 id（隔离键）。
            tenant_id: 租户 id（可为 None，仅 ON CREATE 写入）。
            doc_id: 来源文档 id。
            events: 事件行字典列表（``id`` / ``title`` / ``summary`` / ``content`` /
                ``chunk_id`` / ``entity_names``）。

        Returns:
            写入（入参有效）的事件数。
        """
        # ---- 构造事件行字典（防御式读取，缺 id 跳过）----
        event_rows: list[dict] = []
        for ev in events or []:
            ev_id = ev.get("id")
            if not ev_id:
                # 无 id 无法按 id 合并，跳过（防脏数据生成无意义节点）。
                continue
            # 关联实体名去空白、去空、去重（保持顺序），对齐实体合并键。
            raw_names = ev.get("entity_names") or []
            seen: set[str] = set()
            entity_names: list[str] = []
            for n in raw_names:
                name = (n or "").strip()
                if name and name not in seen:
                    seen.add(name)
                    entity_names.append(name)
            event_rows.append(
                {
                    "id": ev_id,
                    "title": ev.get("title") or "",
                    "summary": ev.get("summary") or "",
                    "content": ev.get("content") or "",
                    "chunk_id": ev.get("chunk_id") or "",
                    "entity_names": entity_names,
                }
            )

        if not event_rows:
            return 0

        settings = get_settings()
        timeout = settings.graph_query_timeout

        async with self._driver.session() as session:
            # 1) 事件节点幂等 MERGE（apoc.periodic.iterate 分批）。
            await session.run(
                _UPSERT_EVENTS_CYPHER,
                events=event_rows,
                kb_id=kb_id,
                tenant_id=tenant_id,
                doc_id=doc_id,
                timeout=timeout,
            )
            # 2) MENTIONS 边幂等 MERGE（端点缺失自动丢弃 → 无悬挂边）。
            #    仅当存在待挂关联实体时才发查询（省一次空写）。
            if any(row["entity_names"] for row in event_rows):
                await session.run(
                    _UPSERT_EVENT_MENTIONS_CYPHER,
                    events=event_rows,
                    kb_id=kb_id,
                    timeout=timeout,
                )

        return len(event_rows)

    async def events_by_entities(
        self, *, kb_id: str, entity_ids: list[str], limit: int,
    ) -> list[GraphEventDTO]:
        """实体桥接入口：取这些实体被 MENTIONS 的事件（去重，按被提及次数降序）。

        强制带 ``kb_id`` 隔离（Property 1）。空 id 过滤后无有效 id、或 limit<=0 直接返回空。
        ``score`` 取命中实体数（被多少个输入实体提及），检索层再归一。

        Args:
            kb_id: 知识库 id（隔离键）。
            entity_ids: 桥接实体 id 列表。
            limit: 返回事件数上限。

        Returns:
            事件 DTO 列表（按被提及次数降序）；无命中 / 无有效入参时 []。
        """
        ids = [i for i in (entity_ids or []) if i]
        safe_limit = max(0, int(limit))
        if not ids or safe_limit == 0:
            return []

        settings = get_settings()
        timeout = settings.graph_query_timeout

        events: list[GraphEventDTO] = []
        async with self._driver.session() as session:
            result = await session.run(
                _EVENTS_BY_ENTITIES_CYPHER,
                kb_id=kb_id,
                entity_ids=ids,
                limit=safe_limit,
                timeout=timeout,
            )
            async for record in result:
                events.append(self._record_to_event_dto(record))
        return events

    async def expand_events(
        self, *, kb_id: str, event_ids: list[str], hops: int, max_events: int,
    ) -> list[GraphEventDTO]:
        """事件多跳：``(:Event)-[:MENTIONS]->(:Entity)<-[:MENTIONS]-(:Event2)`` 扩展。

        强制带 ``kb_id`` 隔离（Property 1）。用 APOC ``expandConfig`` 沿 MENTIONS 无向
        扩展、取以 ``:Event`` 为终点的路径终点；``hops`` 是事件层跳数（一次「事件→共享实体
        →关联事件」为一跳，对应底层 2 个 MENTIONS 关系），故 ``maxLevel = 2 * hops``、
        ``minLevel = 2`` 跳过中间实体与种子自身。结果去重、排除种子集合、截断到 ``max_events``。

        实现说明：用 ``expandConfig`` 而非 ``subgraphNodes`` —— 后者要求 ``minLevel ∈ {0,1}``，
        无法表达「跳过中间实体」的 ``minLevel = 2``；``expandConfig`` 支持任意 minLevel，
        并以 ``uniqueness=NODE_GLOBAL`` 保证每个事件全局仅访问一次（去重 + 抗膨胀）。

        Args:
            kb_id: 知识库 id（隔离键）。
            event_ids: 种子事件 id 列表。
            hops: 事件层扩展跳数（至少 1）。
            max_events: 返回扩展事件数上限。

        Returns:
            扩展事件 DTO 列表（不含种子）；无种子 / 无扩展 / 上限为 0 时 []。
        """
        ids = [i for i in (event_ids or []) if i]
        safe_hops = max(1, int(hops))
        safe_max = max(0, int(max_events))
        if not ids or safe_max == 0:
            return []

        max_level = 2 * safe_hops  # 事件→实体→事件 为一跳（两个 MENTIONS）

        settings = get_settings()
        timeout = settings.graph_query_timeout

        events: list[GraphEventDTO] = []
        async with self._driver.session() as session:
            result = await session.run(
                _EXPAND_EVENTS_CYPHER,
                kb_id=kb_id,
                event_ids=ids,
                max_level=max_level,
                max_events=safe_max,
                timeout=timeout,
            )
            async for record in result:
                events.append(self._record_to_event_dto(record))
        return events

    async def events_by_ids(
        self, *, kb_id: str, event_ids: list[str],
    ) -> list[GraphEventDTO]:
        """按 id 批量取事件详情（kb 内，强制带 ``kb_id`` 隔离，Property 1）。

        Args:
            kb_id: 知识库 id（隔离键）。
            event_ids: 事件 id 列表。

        Returns:
            事件 DTO 列表（顺序不保证）；无有效入参 / 无命中时 []。
        """
        ids = [i for i in (event_ids or []) if i]
        if not ids:
            return []

        settings = get_settings()
        timeout = settings.graph_query_timeout

        events: list[GraphEventDTO] = []
        async with self._driver.session() as session:
            result = await session.run(
                _EVENTS_BY_IDS_CYPHER,
                kb_id=kb_id,
                event_ids=ids,
                timeout=timeout,
            )
            async for record in result:
                events.append(self._record_to_event_dto(record))
        return events

    async def get_event(self, *, kb_id: str, event_id: str) -> "GraphEventDetailDTO | None":
        """单个事件详情（可视化事件详情面板用，强制带 ``kb_id`` 隔离，Property 1）。

        返回事件本体 + 关联实体（MENTIONS）+ 来源 chunk 原文预览。事件不存在则 None。
        chunk 预览复用 ``_fetch_chunk_refs`` 的防御式拉取（缺失 / DB 异常不影响主体）。

        Args:
            kb_id: 知识库 id（隔离键）。
            event_id: 事件 id。

        Returns:
            事件详情 DTO，不存在时 None。
        """
        if not event_id:
            return None

        settings = get_settings()
        timeout = settings.graph_query_timeout

        async with self._driver.session() as session:
            result = await session.run(
                _GET_EVENT_CYPHER,
                kb_id=kb_id,
                event_id=event_id,
                timeout=timeout,
            )
            record = await result.single()
        if record is None:
            return None

        mentions: list[GraphEventMentionDTO] = []
        for ent in record["entities"] or []:
            eid = ent.get("id") if isinstance(ent, dict) else None
            if not eid:
                continue
            mentions.append(
                GraphEventMentionDTO(
                    id=eid,
                    name=(ent.get("name") or "") if isinstance(ent, dict) else "",
                    type=(ent.get("type") or "") if isinstance(ent, dict) else "",
                )
            )

        chunk_id = record["chunk_id"] or ""
        chunk = None
        if chunk_id:
            refs = await self._fetch_chunk_refs(kb_id=kb_id, chunk_ids=[chunk_id])
            chunk = refs[0] if refs else None

        return GraphEventDetailDTO(
            id=record["id"],
            title=record["title"] or "",
            summary=record["summary"] or "",
            content=record["content"] or "",
            chunk_id=chunk_id,
            doc_id=record["doc_id"] or "",
            mentions=mentions,
            chunk=chunk,
        )

    # ------------------------------------------------------------------

    async def events_by_doc(
        self, *, kb_id: str, doc_id: str, limit: int,
    ) -> list[GraphEventDTO]:
        """取某文档抽取的全部事件（kb_id + doc_id 双重隔离，Property 1 / Requirements 4.2）。

        额外填充 ``entity_names``（关联实体规范名），供文档详情前端直接展示。空入参 /
        limit<=0 直接返回空。

        Args:
            kb_id: 知识库 id（隔离键）。
            doc_id: 文档 id（隔离键）。
            limit: 返回事件数上限。

        Returns:
            事件 DTO 列表（按 chunk_id / title 排序）；无命中 / 无效入参时 []。
        """
        safe_limit = max(0, int(limit))
        if not kb_id or not doc_id or safe_limit == 0:
            return []

        settings = get_settings()
        timeout = settings.graph_query_timeout

        events: list[GraphEventDTO] = []
        async with self._driver.session() as session:
            result = await session.run(
                _EVENTS_BY_DOC_CYPHER,
                kb_id=kb_id,
                doc_id=doc_id,
                limit=safe_limit,
                timeout=timeout,
            )
            async for record in result:
                events.append(self._record_to_event_with_names_dto(record))
        return events
    # 平台硬上限读取（服务端 clamp 用，Property 9 / Requirements 9.1）
    # ------------------------------------------------------------------

    @staticmethod
    async def _platform_caps() -> "PlatformConfig":
        """读取生效的平台配置（全局单行 id='global'），供读查询服务端 clamp 取硬上限。

        平台配置全局共享、不分租户，``get_platform_config_store().get_effective()`` 自带
        DB 失败兜底（返回全 Safe_Default），故此处无需额外 try/except；防御性降级已在
        ``PlatformConfigStore`` 内完成。
        """
        from app.retrieval.config import get_platform_config_store

        return await get_platform_config_store().get_effective()

    async def find_entities_by_names(
        self, *, kb_id: str, names: list[str], limit: int,
    ) -> list[GraphEntityDTO]:
        """按名称在 kb 内做 CONTAINS 模糊匹配（检索融合命中实体，design.md 4.1/4.5）。

        强制带 ``kb_id`` 隔离（Property 1）；空白名过滤后无有效名直接返回空。返回结果按
        degree 降序、截断到 ``limit``（与查询硬上限一致的有界返回，Property 9），
        ``neighbors`` / ``chunks`` 留空（详情由 ``get_entity`` 单独拉取）。

        Args:
            kb_id: 知识库 id（隔离键）。
            names: 待匹配实体名列表（去空白、去空）。
            limit: 返回实体数上限（调用方传入，这里再 max(0) 兜底）。

        Returns:
            命中实体 DTO 列表（可能为空）。
        """
        clean_names = [n.strip() for n in (names or []) if n and n.strip()]
        if not clean_names:
            return []
        safe_limit = max(0, int(limit))
        if safe_limit == 0:
            return []

        settings = get_settings()
        timeout = settings.graph_query_timeout

        entities: list[GraphEntityDTO] = []
        async with self._driver.session() as session:
            result = await session.run(
                _FIND_ENTITIES_CYPHER,
                kb_id=kb_id,
                names=clean_names,
                limit=safe_limit,
                timeout=timeout,
            )
            async for record in result:
                entities.append(self._record_to_entity_dto(record))
        return entities

    async def neighbors(
        self,
        *,
        kb_id: str,
        entity_ids: list[str],
        hops: int,
        max_nodes: int,
        types: list[str] | None = None,
    ) -> GraphSubsetDTO:
        """取中心实体集合的 N 跳邻居子图（检索召回与 ego 可视化共用，design.md 4.1）。

        服务端 clamp（防御纵深，调用方可能已 clamp，这里再夹一次，Property 9 /
        Requirements 6.2/9.1）：``hops`` clamp 到 ``[1, graph_ego_max_depth]``，
        ``max_nodes`` clamp 到 ``[1, graph_ego_max_nodes]``。强制带 ``kb_id`` 隔离
        （Property 1）。返回节点经「中心优先、degree 降序」截断到上限，边仅取两端均在
        返回节点内者（无悬挂边，Property 3）。meta.mode='ego'，``total`` 为全邻域节点数、
        ``returned`` 为截断后数、``truncated = returned < total``。

        Args:
            kb_id: 知识库 id（隔离键）。
            entity_ids: 中心实体 id 列表。
            hops: BFS 跳数（服务端 clamp 到平台硬上限）。
            max_nodes: 返回节点数上限（服务端 clamp 到平台硬上限）。
            types: 可选类型过滤白名单（中心节点恒保留，不被该过滤剔除）。

        Returns:
            ego 子图 DTO（中心实体不存在 / 无邻居时 nodes/edges 为空，meta 仍含 center/depth）。
        """
        ids = [i for i in (entity_ids or []) if i]
        caps = await self._platform_caps()
        eff_hops = _clamp(int(hops), 1, caps.graph_ego_max_depth)
        eff_max_nodes = _clamp(int(max_nodes), 1, caps.graph_ego_max_nodes)
        center = ids[0] if len(ids) == 1 else (",".join(ids) if ids else None)

        meta = GraphSubsetMeta(mode="ego", center=center, depth=eff_hops)
        if not ids:
            return GraphSubsetDTO(nodes=[], edges=[], meta=meta)

        # 类型过滤：空列表视为「不过滤」（None），避免 IN [] 把全部节点过滤掉。
        type_filter = list(types) if types else None

        settings = get_settings()
        timeout = settings.graph_query_timeout

        async with self._driver.session() as session:
            nodes_result = await session.run(
                _NEIGHBORS_NODES_CYPHER,
                kb_id=kb_id,
                entity_ids=ids,
                hops=eff_hops,
                max_nodes=eff_max_nodes,
                types=type_filter,
                timeout=timeout,
            )
            nodes_record = await nodes_result.single()
            total = int(nodes_record["total"]) if nodes_record else 0
            raw_nodes = list(nodes_record["nodes"]) if nodes_record else []
            nodes = [self._dict_to_node_dto(n) for n in raw_nodes]

            edges = await self._fetch_internal_edges(
                session, kb_id=kb_id, node_ids=[n.id for n in nodes], timeout=timeout
            )

        meta.total = total
        meta.returned = len(nodes)
        meta.truncated = len(nodes) < total
        return GraphSubsetDTO(nodes=nodes, edges=edges, meta=meta)

    async def overview(
        self, *, kb_id: str, limit: int, types: list[str] | None = None,
    ) -> GraphSubsetDTO:
        """取度数最高的 top-N 节点及其内部边（overview 可视化，design.md 4.1/API 5.1）。

        服务端 clamp（防御纵深，Property 9 / Requirements 6.1/9.1）：``limit`` clamp 到
        ``[1, graph_overview_max_nodes]``。强制带 ``kb_id`` 隔离（Property 1）；节点走
        ``entity_kb_degree_idx`` 按 degree 降序限量，边仅取两端均在返回节点内者（无悬挂边）。
        meta.mode='overview'，``total`` 为（可选 type 过滤后）kb 内实体总数、``returned`` 为
        实际返回数、``truncated = returned < total``。

        Args:
            kb_id: 知识库 id（隔离键）。
            limit: 返回节点数上限（服务端 clamp 到平台硬上限）。
            types: 可选类型过滤白名单。

        Returns:
            概览子图 DTO（meta.mode='overview'）。
        """
        caps = await self._platform_caps()
        eff_limit = _clamp(int(limit), 1, caps.graph_overview_max_nodes)
        type_filter = list(types) if types else None

        settings = get_settings()
        timeout = settings.graph_query_timeout

        async with self._driver.session() as session:
            total_result = await session.run(
                _OVERVIEW_TOTAL_CYPHER,
                kb_id=kb_id,
                types=type_filter,
                timeout=timeout,
            )
            total_record = await total_result.single()
            total = int(total_record["total"]) if total_record else 0

            nodes_result = await session.run(
                _OVERVIEW_NODES_CYPHER,
                kb_id=kb_id,
                types=type_filter,
                limit=eff_limit,
                timeout=timeout,
            )
            nodes_record = await nodes_result.single()
            raw_nodes = list(nodes_record["nodes"]) if nodes_record else []
            nodes = [self._dict_to_node_dto(n) for n in raw_nodes]

            edges = await self._fetch_internal_edges(
                session, kb_id=kb_id, node_ids=[n.id for n in nodes], timeout=timeout
            )

        meta = GraphSubsetMeta(
            mode="overview",
            total=total,
            returned=len(nodes),
            truncated=len(nodes) < total,
        )
        return GraphSubsetDTO(nodes=nodes, edges=edges, meta=meta)

    async def get_entity(self, *, kb_id: str, entity_id: str) -> GraphEntityDTO | None:
        """实体详情（属性 / 别名 / 邻居 / 关联原文 chunk 预览，design.md 4.1/API 5.1）。

        强制带 ``kb_id`` 隔离（Property 1）。不存在则返回 None。邻居取一跳、按 degree 降序
        限量（上限取 ``graph_ego_max_nodes``，有界返回 Property 9）；关联 chunk 从 PG
        ``Chunk`` 表按实体 ``chunk_ids`` 拉取（最多 ``_GET_ENTITY_MAX_CHUNKS`` 条），
        content 截断到 ``_CHUNK_PREVIEW_LEN`` 作预览。chunk 拉取防御式容错（缺失 / DB 异常
        不影响实体详情主体返回）。

        Args:
            kb_id: 知识库 id（隔离键）。
            entity_id: 实体 id。

        Returns:
            实体 DTO（填充 ``neighbors`` 与 ``chunks``），不存在时 None。
        """
        if not entity_id:
            return None

        caps = await self._platform_caps()
        neighbor_limit = caps.graph_ego_max_nodes
        settings = get_settings()
        timeout = settings.graph_query_timeout

        async with self._driver.session() as session:
            entity_result = await session.run(
                _GET_ENTITY_CYPHER,
                kb_id=kb_id,
                entity_id=entity_id,
                timeout=timeout,
            )
            entity_record = await entity_result.single()
            if entity_record is None:
                return None
            dto = self._record_to_entity_dto(entity_record)

            neighbors_result = await session.run(
                _GET_ENTITY_NEIGHBORS_CYPHER,
                kb_id=kb_id,
                entity_id=entity_id,
                limit=neighbor_limit,
                timeout=timeout,
            )
            async for record in neighbors_result:
                dto.neighbors.append(
                    GraphNeighborDTO(
                        id=record["id"],
                        name=record["name"],
                        type=record["type"],
                        rel_type=record["rel_type"],
                    )
                )

        # 关联原文 chunk 预览（从 PG Chunk 表拉取，独立于 Neo4j 会话，防御式容错）。
        dto.chunks = await self._fetch_chunk_refs(kb_id=kb_id, chunk_ids=dto.chunk_ids)
        return dto

    async def stats(self, *, kb_id: str) -> GraphStatsDTO:
        """统计：实体数 / 关系数 / 类型分布 / 孤立节点数 / 状态（design.md 4.1/API 5.1）。

        强制带 ``kb_id`` 隔离（Property 1）。按 type 分组聚合实体数与孤立（degree=0）数，
        Python 侧汇总 ``entity_count`` / ``types`` / ``orphan_count``；关系数单独有向计数。

        ``status`` 取值约定：图谱抽取的权威状态在 ``Document.graph_status`` /
        ``GraphExtractJob`` 上，本统计聚焦图计数本身，故按数据态给简单启发：实体数 > 0
        记 ``completed``，否则 ``none``（前端空态判断已主要依赖 ``entity_count``）。

        Args:
            kb_id: 知识库 id（隔离键）。

        Returns:
            图谱统计 DTO。
        """
        settings = get_settings()
        timeout = settings.graph_query_timeout

        entity_count = 0
        orphan_count = 0
        types: dict[str, int] = {}
        relation_count = 0

        async with self._driver.session() as session:
            entities_result = await session.run(
                _STATS_ENTITIES_CYPHER,
                kb_id=kb_id,
                timeout=timeout,
            )
            async for record in entities_result:
                cnt = int(record["cnt"])
                entity_count += cnt
                orphan_count += int(record["orphans"] or 0)
                etype = record["type"] or "未知"
                types[etype] = types.get(etype, 0) + cnt

            relations_result = await session.run(
                _STATS_RELATIONS_CYPHER,
                kb_id=kb_id,
                timeout=timeout,
            )
            relations_record = await relations_result.single()
            relation_count = int(relations_record["cnt"]) if relations_record else 0

        return GraphStatsDTO(
            entity_count=entity_count,
            relation_count=relation_count,
            types=types,
            orphan_count=orphan_count,
            status="completed" if entity_count > 0 else "none",
        )

    # ------------------------------------------------------------------
    # 读查询辅助：record / dict → DTO，内部边拉取，chunk 预览拉取
    # ------------------------------------------------------------------

    @staticmethod
    def _record_to_entity_dto(record) -> GraphEntityDTO:
        """把 Neo4j record（实体本体字段）解析为 ``GraphEntityDTO``（neighbors/chunks 留空）。"""
        return GraphEntityDTO(
            id=record["id"],
            name=record["name"],
            type=record["type"],
            aliases=list(record["aliases"] or []),
            attributes=list(record["attributes"] or []),
            degree=int(record["degree"] or 0),
            chunk_ids=list(record["chunk_ids"] or []),
            doc_ids=list(record["doc_ids"] or []),
        )

    @staticmethod
    def _dict_to_node_dto(node: dict) -> GraphNodeDTO:
        """把 Cypher 返回的节点 map 解析为 ``GraphNodeDTO``（实体节点，node_type=entity）。"""
        return GraphNodeDTO(
            id=node["id"],
            name=node["name"],
            type=node["type"],
            degree=int(node.get("degree") or 0),
            node_type="entity",
        )

    @staticmethod
    def _record_to_event_dto(record) -> GraphEventDTO:
        """把 Neo4j record（事件本体字段 + 关联实体 id 列表）解析为 ``GraphEventDTO``。

        所有字段对 list / 数值 / 字符串做兜底，避免历史脏数据或缺省 None。``score`` 仅在
        events_by_entities 返回中存在（命中实体数），其它查询无该字段时回退 0.0。
        """
        try:
            score = float(record["score"])
        except (KeyError, TypeError, ValueError):
            score = 0.0
        return GraphEventDTO(
            id=record["id"],
            title=record["title"] or "",
            summary=record["summary"] or "",
            content=record["content"] or "",
            chunk_id=record["chunk_id"] or "",
            doc_id=record["doc_id"] or "",
            entity_ids=list(record["entity_ids"] or []),
            score=score,
        )

    @staticmethod
    def _record_to_event_with_names_dto(record) -> GraphEventDTO:
        """同 ``_record_to_event_dto``，但额外解析 ``entity_names``（文档详情展示用）。

        ``entity_names`` 去 None / 去空白后保留（保持 Cypher 返回顺序）；缺失时回退空列表。
        """
        dto = Neo4jGraphStore._record_to_event_dto(record)
        try:
            raw_names = record["entity_names"] or []
        except (KeyError, TypeError):
            raw_names = []
        dto.entity_names = [
            n for n in (str(x).strip() for x in raw_names if x is not None) if n
        ]
        return dto

    async def _fetch_internal_edges(
        self, session, *, kb_id: str, node_ids: list[str], timeout: float,
    ) -> list[GraphEdgeDTO]:
        """拉取节点集合内部边（两端均在 ``node_ids`` 内，无悬挂边，Property 3）。

        节点不足两个时不可能有内部边，直接返回空，省一次查询。
        """
        if len(node_ids) < 2:
            return []
        edges_result = await session.run(
            _INTERNAL_EDGES_CYPHER,
            kb_id=kb_id,
            node_ids=node_ids,
            timeout=timeout,
        )
        edges: list[GraphEdgeDTO] = []
        async for record in edges_result:
            edges.append(
                GraphEdgeDTO(
                    source=record["source"],
                    target=record["target"],
                    type=record["type"],
                    weight=int(record["weight"] or 1),
                )
            )
        return edges

    @staticmethod
    async def _fetch_chunk_refs(
        *, kb_id: str, chunk_ids: list[str],
    ) -> list[GraphChunkRefDTO]:
        """从 PG ``Chunk`` 表拉取实体关联 chunk 的预览（get_entity 用）。

        强制带 ``kb_id`` 过滤（隔离）；最多取 ``_GET_ENTITY_MAX_CHUNKS`` 条，content 截断到
        ``_CHUNK_PREVIEW_LEN``。防御式容错：DB 异常 / chunk 缺失只记 warning 并返回已得结果，
        不影响实体详情主体（design.md 要求 chunk 拉取保持防御）。

        Args:
            kb_id: 知识库 id（隔离键）。
            chunk_ids: 实体的来源 chunk id 列表。

        Returns:
            chunk 引用 DTO 列表（按查询返回顺序，可能少于入参数量）。
        """
        ids = [c for c in (chunk_ids or []) if c][:_GET_ENTITY_MAX_CHUNKS]
        if not ids:
            return []

        try:
            from sqlalchemy import select

            from app.schema.db import Chunk
            from app.storage.database import async_session

            refs: list[GraphChunkRefDTO] = []
            async with async_session() as db:
                result = await db.execute(
                    select(Chunk.id, Chunk.doc_id, Chunk.content).where(
                        Chunk.kb_id == kb_id, Chunk.id.in_(ids)
                    )
                )
                for row in result.all():
                    content = row.content or ""
                    preview = content[:_CHUNK_PREVIEW_LEN]
                    refs.append(
                        GraphChunkRefDTO(
                            chunk_id=row.id,
                            doc_id=row.doc_id,
                            content_preview=preview,
                        )
                    )
            return refs
        except Exception as e:  # noqa: BLE001 - chunk 预览拉取失败不影响实体详情主体
            logger.warning("拉取 chunk 预览失败（kb_id=%s）: %s", kb_id, e)
            return []

    # ------------------------------------------------------------------
    # 阶段 4（GraphRAG Global）：社区发现 + 社区摘要落库（task 9.1）
    # ------------------------------------------------------------------

    async def detect_communities(self, *, kb_id: str) -> list[GraphCommunityDTO]:
        """对某 KB 的实体图做社区发现（GDS Louvain），优雅降级（design.md 阶段 4）。

        流程（全程强制带 ``kb_id`` 隔离，Property 1 / Requirements 8.1）：

        1. Cypher 投影建命名子图（节点 / 关系查询都带 ``kb_id``，社区发现只在本 KB 图上跑）；
        2. 在投影图上跑 ``gds.louvain.stream``，流式取每个实体的社区编号；
        3. 拉本 KB 内全部内部关系，按社区分组（两端同社区的关系计入该社区）；
        4. 按社区聚合成员实体 / 关系，过滤成员数 < ``graph_community_min_size`` 的噪声社区，
           按成员数降序、截断到 ``graph_community_max_communities``；
        5. 用完即删投影图（``finally`` 保证释放 GDS 内存）。

        GDS 插件不可用 / 投影或 Louvain 过程调用抛错时**优雅降级返回空列表**（warning 不
        crash），不影响阶段 1~3 的图谱能力（对齐 design.md「优雅降级」原则）。

        Args:
            kb_id: 知识库 id（隔离键）。

        Returns:
            社区列表（按成员数降序，已过滤过小社区与超量社区）；GDS 不可用 / 无图数据时 []。
        """
        settings = get_settings()
        timeout = settings.graph_query_timeout
        min_size = max(1, int(settings.graph_community_min_size))
        max_communities = max(0, int(settings.graph_community_max_communities))
        if max_communities == 0:
            return []

        # 投影图名带 kb 标识 + 随机后缀，避免并发重算撞名。
        graph_name = f"kg_community_{kb_id}_{uuid.uuid4().hex[:8]}"

        try:
            async with self._driver.session() as session:
                # 1) Cypher 投影（节点 / 关系都强制带 kb_id）。
                await session.run(
                    _GDS_PROJECT_CYPHER,
                    graph_name=graph_name,
                    kb_id=kb_id,
                    timeout=timeout,
                )
                try:
                    # 2) Louvain 流式取每个实体的社区编号。
                    louvain_result = await session.run(
                        _GDS_LOUVAIN_STREAM_CYPHER,
                        graph_name=graph_name,
                        timeout=timeout,
                    )
                    # entity_id -> community_key；同时累计成员 id / name。
                    members_by_community: dict[str, list[tuple[str, str]]] = {}
                    community_of_entity: dict[str, str] = {}
                    async for record in louvain_result:
                        entity_id = record["entity_id"]
                        name = record["name"]
                        community_key = str(record["community_id"])
                        if entity_id is None:
                            continue
                        community_of_entity[entity_id] = community_key
                        members_by_community.setdefault(community_key, []).append(
                            (entity_id, name or "")
                        )

                    # 3) 拉本 KB 内全部内部关系，按「两端同社区」归入该社区。
                    relations_by_community: dict[str, list[tuple[str, str, str]]] = {}
                    relations_result = await session.run(
                        _KB_RELATIONS_CYPHER,
                        kb_id=kb_id,
                        timeout=timeout,
                    )
                    async for record in relations_result:
                        s_id = record["source_id"]
                        t_id = record["target_id"]
                        s_comm = community_of_entity.get(s_id)
                        t_comm = community_of_entity.get(t_id)
                        if s_comm is not None and s_comm == t_comm:
                            relations_by_community.setdefault(s_comm, []).append(
                                (
                                    record["source_name"] or "",
                                    record["rel_type"] or "",
                                    record["target_name"] or "",
                                )
                            )
                finally:
                    # 5) 用完即删投影图（释放 GDS 内存，failIfMissing=false 容忍缺失）。
                    try:
                        await session.run(_GDS_DROP_CYPHER, graph_name=graph_name)
                    except Exception as drop_err:  # noqa: BLE001
                        logger.warning(
                            "删除 GDS 投影图失败（graph=%s）: %s", graph_name, drop_err
                        )
        except Exception as e:  # noqa: BLE001 - GDS 不可用 / 过程缺失 → 优雅降级返回空
            logger.warning(
                "社区发现降级（GDS 不可用或执行失败，kb_id=%s）: %s", kb_id, e
            )
            return []

        # 4) 聚合 + 过滤过小社区 + 按成员数降序截断。
        communities: list[GraphCommunityDTO] = []
        for community_key, members in members_by_community.items():
            if len(members) < min_size:
                continue
            member_ids = [m[0] for m in members]
            member_names = [m[1] for m in members if m[1]]
            rels = relations_by_community.get(community_key, [])
            communities.append(
                GraphCommunityDTO(
                    community_key=community_key,
                    level=0,
                    member_entity_ids=member_ids,
                    member_names=member_names,
                    relations=rels,
                    entity_count=len(member_ids),
                    relation_count=len(rels),
                )
            )

        communities.sort(key=lambda c: c.entity_count, reverse=True)
        return communities[:max_communities]

    async def build_community_summaries(
        self, *, kb_id: str, tenant_id: str | None = None, llm=None,
    ) -> int:
        """社区发现 + LLM 生成摘要 + 落库 PG（写路径，design.md 阶段 4 / task 9.1）。

        流程：

        1. ``detect_communities`` 做 GDS Louvain 社区发现（GDS 不可用时返回空 → 本方法 no-op）；
        2. 对每个社区调 LLM（``build_community_messages``）生成 ``title`` / ``summary``，
           成员 / 关系喂入前按平台上限截断（控制 prompt 长度与成本）；
        3. 按 ``kb_id`` 先清后插（整库重算语义）落 PG ``graph_communities`` 表。

        优雅降级：无社区 → 直接返回 0（不动 PG）；单个社区 LLM 失败 → 跳过该社区（warning），
        不影响其它社区；整体 PG 写入失败向上抛（由调用方/管理 API 处理）。

        Args:
            kb_id: 知识库 id（隔离键）。
            tenant_id: 租户 id（落 ``GraphCommunity.tenant_id``，可为 None）。
            llm: 生成摘要用 LLMProvider；None 时按 KB / 系统默认惰性获取（解耦，便于测试注入）。

        Returns:
            成功落库的社区摘要条数。
        """
        communities = await self.detect_communities(kb_id=kb_id)
        if not communities:
            # 无社区（GDS 不可用 / 图过小）：不动 PG，返回 0。
            return 0

        settings = get_settings()
        max_members = max(1, int(settings.graph_community_max_members_for_summary))

        # 惰性获取 LLM（解耦：测试可显式注入 llm，避免触达模型管理）。
        if llm is None:
            llm = await self._get_summary_llm()

        from app.pipeline.graph import prompts

        summaries: list[GraphCommunitySummaryDTO] = []
        for community in communities:
            member_names = community.member_names[:max_members]
            relations = community.relations[:_COMMUNITY_MAX_RELATIONS_FOR_SUMMARY]
            try:
                messages = prompts.build_community_messages(member_names, relations)
                raw = await llm.generate(messages, temperature=0.3, enable_thinking=False)
            except Exception as e:  # noqa: BLE001 - 单社区摘要失败跳过，不影响其它社区
                logger.warning(
                    "社区摘要生成失败（kb_id=%s, community=%s）: %s",
                    kb_id, community.community_key, e,
                )
                continue
            title, summary = _parse_community_summary(raw)
            if not summary:
                continue
            summaries.append(
                GraphCommunitySummaryDTO(
                    community_key=community.community_key,
                    summary=summary,
                    level=community.level,
                    title=title,
                    entity_count=community.entity_count,
                    relation_count=community.relation_count,
                    member_entity_ids=community.member_entity_ids,
                )
            )

        if not summaries:
            return 0

        await self._persist_community_summaries(
            kb_id=kb_id, tenant_id=tenant_id, summaries=summaries
        )
        return len(summaries)

    @staticmethod
    async def _get_summary_llm():
        """取社区摘要生成用 LLM（复用 chat 的默认模型选择逻辑，懒导入避免循环依赖）。"""
        from app.api.chat import _get_llm_for_request

        llm, _stream, _max_ctx = await _get_llm_for_request(None)
        return llm

    @staticmethod
    async def _persist_community_summaries(
        *, kb_id: str, tenant_id: str | None,
        summaries: list[GraphCommunitySummaryDTO],
    ) -> None:
        """把社区摘要按 ``kb_id`` 先清后插落 PG ``graph_communities``（整库重算语义）。

        强制带 ``kb_id`` 隔离；单事务内先删该 KB 旧社区、再插新社区，保证重建一致
        （不残留上次重算的陈旧社区）。``community_key`` 来自 Louvain，可能跨重算变化，
        故用整库先清后插而非逐行 upsert。

        Args:
            kb_id: 知识库 id（隔离键）。
            tenant_id: 租户 id（落 ``GraphCommunity.tenant_id``）。
            summaries: 待落库的社区摘要 DTO 列表。
        """
        from sqlalchemy import delete

        from app.schema.db import GraphCommunity
        from app.storage.database import async_session

        async with async_session() as db:
            # 先清该 KB 旧社区（整库重算语义，强制带 kb_id）。
            await db.execute(delete(GraphCommunity).where(GraphCommunity.kb_id == kb_id))
            for s in summaries:
                db.add(
                    GraphCommunity(
                        id=str(uuid.uuid4()),
                        tenant_id=tenant_id,
                        kb_id=kb_id,
                        community_key=s.community_key,
                        level=s.level,
                        title=s.title,
                        summary=s.summary,
                        entity_count=s.entity_count,
                        relation_count=s.relation_count,
                        member_entity_ids=list(s.member_entity_ids),
                    )
                )
            await db.commit()

    async def community_summaries(
        self, *, kb_id: str, limit: int | None = None,
    ) -> list[GraphCommunitySummaryDTO]:
        """读取某 KB 已落库（PG ``graph_communities``）的社区摘要（全局问答检索用）。

        强制带 ``kb_id`` 过滤（Property 1 / Requirements 8.1），按成员实体数降序返回。
        DB 不可用 / 异常时防御式降级返回空（不影响主链路）。

        Args:
            kb_id: 知识库 id（隔离键）。
            limit: 返回条数上限（None 表示不限）。

        Returns:
            社区摘要 DTO 列表（按成员实体数降序）；无数据 / 异常时 []。
        """
        try:
            from sqlalchemy import select

            from app.schema.db import GraphCommunity
            from app.storage.database import async_session

            stmt = (
                select(GraphCommunity)
                .where(GraphCommunity.kb_id == kb_id)
                .order_by(GraphCommunity.entity_count.desc())
            )
            if limit is not None and int(limit) > 0:
                stmt = stmt.limit(int(limit))

            out: list[GraphCommunitySummaryDTO] = []
            async with async_session() as db:
                result = await db.execute(stmt)
                for row in result.scalars().all():
                    out.append(
                        GraphCommunitySummaryDTO(
                            community_key=row.community_key,
                            summary=row.summary,
                            level=row.level,
                            title=row.title,
                            entity_count=row.entity_count or 0,
                            relation_count=row.relation_count or 0,
                            member_entity_ids=list(row.member_entity_ids or []),
                        )
                    )
            return out
        except Exception as e:  # noqa: BLE001 - 读摘要失败降级返回空，不影响主链路
            logger.warning("读取社区摘要失败（kb_id=%s）: %s", kb_id, e)
            return []


# ---------------------------------------------------------------------------
# 进程内单例（同 get_retrieval_config_store 范式）
# ---------------------------------------------------------------------------

_graph_store: GraphStore | None = None
_graph_store_inited = False


async def get_graph_store() -> GraphStore | None:
    """获取进程内 ``GraphStore`` 单例（首次调用经 ``Neo4jGraphStore.create()` 构造）。

    返回 None 表示图谱功能降级关闭（全局未启用 / Neo4j 不可用 / 驱动未安装），
    调用方据此跳过图谱相关逻辑（构建跳过、检索路级降级、可视化 API 返回明确不可用）。

    用 ``_graph_store_inited`` 标记防止每次调用都重试连接（与 design.md 单例范式一致）。
    """
    global _graph_store, _graph_store_inited
    if not _graph_store_inited:
        _graph_store = await Neo4jGraphStore.create()
        _graph_store_inited = True
    return _graph_store
