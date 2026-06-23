"""GraphExtractor：LLM 结构化两步抽取（实体 + 属性、关系）。

对齐 design.md 4.2 与 Requirements 1.2 / 1.3 / 1.4：

- **两步法**：先抽实体（+属性），再以「已抽实体清单」为约束抽关系。两步法相比单次
  结构化抽取，能让关系抽取阶段把端点约束在已确认的实体集合内，显著减少悬挂边与
  幻觉关系（WeKnora 同思路）。
- **强约束 JSON 输出**：prompt 强制只输出 JSON；调用 LLM 时关闭思考（``enable_thinking
  =False``）、低温度（``temperature≈0.3``）以稳定结构化输出。
- **容错解析**：剥 markdown ```json fence、宽松定位首个 JSON 对象/数组、字段缺失兜底，
  兼容模型把结果包在解释文字里、用数组而非对象包裹等常见越界形态。
- **白名单过滤**：丢弃 type 不在 ``entity_types`` / ``relation_types`` 白名单内的项。
- **无悬挂边**：丢弃 source/target 端点不在已抽取实体集合内的关系（Property 3）。
- **彻底失败抛错**：两步中任一步的 LLM 文本都无法解析出 JSON 时抛 ``GraphExtractError``，
  交由 worker 重试 / DLQ（design.md Error Handling）。

设计取舍说明：解析与过滤逻辑拆成模块级**纯函数**（``parse_entities`` /
``parse_relations`` / ``filter_entities`` / ``filter_relations`` / ``drop_dangling_relations``），
不依赖 LLM、无 IO，便于 task 3.3 表驱动单测；``GraphExtractor.extract`` 仅负责编排
（两次 LLM 调用 + 串联纯函数）。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from app.models.provider import LLMProvider
from app.pipeline.graph import prompts

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 数据模型（dataclass）——字段名须与 graph_store.upsert_graph 读取的契约一致
# （normalized_name / name / type / attributes / aliases；source / target / type /
#  attributes / confidence）。chunk_ids / chunk_id 由 worker（task 4.2）后置打标，
# 抽取器本身不产出，故此处不含这些字段（upsert 用 getattr 防御式读取）。
# ---------------------------------------------------------------------------


@dataclass
class ExtractedEntity:
    """抽取出的实体（归一化前）。

    Attributes:
        name: 原始名（文本中的表面形态）。
        type: 实体类型，必须落在 KB 配置的实体类型白名单内。
        attributes: 属性描述列表（LLM 抽取）。
        normalized_name: 归一化后的规范名，由 EntityResolver（task 3.2）填充，
            抽取阶段留空字符串。
        aliases: 别名集合，由 EntityResolver 消歧时填充。
    """

    name: str
    type: str
    attributes: list[str]
    # 以下由 EntityResolver 填充（抽取阶段留默认值）
    normalized_name: str = ""
    aliases: list[str] = field(default_factory=list)


@dataclass
class ExtractedRelation:
    """抽取出的关系（归一化前）。

    Attributes:
        source: 头实体名。
        target: 尾实体名。
        type: 关系类型，必须落在 KB 配置的关系类型白名单内。
        attributes: 关系附加属性描述列表。
        confidence: 抽取置信度（0~1，缺省 1.0）。
    """

    source: str
    target: str
    type: str
    attributes: list[str] = field(default_factory=list)
    confidence: float = 1.0


@dataclass
class ExtractedEvent:
    """从一个 chunk 抽取的完整事件（归一化前）。

    对齐 design.md 3.1.1 与 Requirements 1.1 / 1.2：事件是「主谓宾 + 时地」齐全的
    完整语义单元，作为新的图谱检索单元。``entity_names`` 须落在本 chunk 已抽实体集合内
    （由 ``filter_events`` 约束，无悬挂关联）。

    Attributes:
        title: 事件短标题。
        summary: 一句话摘要。
        content: 完整语义内容（主谓宾时地齐全），为空的事件会被 ``filter_events`` 丢弃。
        entity_names: 关联实体名列表（须落在本 chunk 已抽实体集合内）。
    """

    title: str
    summary: str
    content: str
    entity_names: list[str] = field(default_factory=list)


@dataclass
class ExtractedGraph:
    """一次抽取得到的图（实体 + 关系 + 事件）。"""

    entities: list[ExtractedEntity]
    relations: list[ExtractedRelation]
    events: list[ExtractedEvent] = field(default_factory=list)


class GraphExtractError(Exception):
    """图谱抽取失败（LLM 输出彻底无法解析为结构化 JSON）。

    抛出后由 GraphExtractWorker（task 4.2）走重试 / DLQ，不污染其它 chunk 的抽取。
    """


# ---------------------------------------------------------------------------
# 容错解析（纯函数，无 IO，可单测）
# ---------------------------------------------------------------------------

# markdown 代码围栏：```json ... ``` 或 ``` ... ```。剥离后取内部内容。
_FENCE_RE = re.compile(
    r"```(?:json|JSON)?\s*(?P<body>.*?)```",
    re.DOTALL,
)


def _strip_code_fence(text: str) -> str:
    """剥离 markdown 代码围栏，返回栅栏内部内容；无围栏时原样返回（去首尾空白）。

    模型常把 JSON 包在 ```json ...``` 里。若存在围栏，取**第一个**围栏的内部内容；
    否则返回原文（后续再做宽松 JSON 定位）。
    """
    if not text:
        return ""
    m = _FENCE_RE.search(text)
    if m:
        return m.group("body").strip()
    return text.strip()


def _extract_json_blob(text: str) -> str | None:
    """从文本中尽力定位一段可解析的 JSON（对象或数组）。

    策略：先剥代码围栏；尝试直接 ``json.loads``；失败则用括号配对定位首个完整的
    ``{...}`` 或 ``[...]`` 子串。返回候选 JSON 字符串，定位不到返回 None。
    """
    if not text:
        return None
    candidate = _strip_code_fence(text)

    # 1) 直接整体可解析则直接返回。
    try:
        json.loads(candidate)
        return candidate
    except (ValueError, TypeError):
        pass

    # 2) 括号配对定位首个完整的对象 / 数组子串。对象优先（本项目 schema 为对象）。
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = candidate.find(open_ch)
        if start == -1:
            continue
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
            elif ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    blob = candidate[start : i + 1]
                    try:
                        json.loads(blob)
                        return blob
                    except (ValueError, TypeError):
                        break  # 该候选不可解析，尝试下一种括号
    return None


def _loads_lenient(text: str) -> object | None:
    """宽松解析：定位并解析文本中的 JSON，失败返回 None（不抛错）。"""
    blob = _extract_json_blob(text)
    if blob is None:
        return None
    try:
        return json.loads(blob)
    except (ValueError, TypeError):
        return None


def _as_str_list(value: object) -> list[str]:
    """把任意值兜底转为字符串列表（属性字段容错）。

    - list：逐项转字符串、去空白、丢空项。
    - 单个非空字符串：包成单元素列表。
    - 其它（None / 数字 / dict 等）：空列表。
    """
    if isinstance(value, list):
        out: list[str] = []
        for v in value:
            s = str(v).strip() if v is not None else ""
            if s:
                out.append(s)
        return out
    if isinstance(value, str):
        s = value.strip()
        return [s] if s else []
    return []


def _coerce_records(parsed: object, key: str) -> list[dict]:
    """从宽松解析结果中提取记录列表（容忍多种包裹形态）。

    兼容：
    - ``{"entities": [...]}`` / ``{"relations": [...]}``（标准 schema）。
    - 顶层直接是数组 ``[...]``（模型省略外层对象）。
    - 顶层是单个记录对象 ``{...}``（模型只抽到一条且未包数组）。

    Args:
        parsed: ``json.loads`` 的结果。
        key: 期望的包裹键（``"entities"`` 或 ``"relations"``）。

    Returns:
        记录 dict 列表（非 dict 元素被过滤）。
    """
    records: object
    if isinstance(parsed, dict):
        if key in parsed:
            records = parsed[key]
        else:
            # 无包裹键但本身像一条记录 → 当单元素处理。
            records = [parsed]
    elif isinstance(parsed, list):
        records = parsed
    else:
        return []
    if not isinstance(records, list):
        return []
    return [r for r in records if isinstance(r, dict)]


def parse_entities(text: str) -> list[ExtractedEntity]:
    """从第一步 LLM 输出文本解析实体列表（容错，不抛错；解析不到返回 []）。

    字段兜底：``name`` 缺失/空则丢弃该条；``type`` 缺失回退空字符串（后续白名单过滤
    会将其丢弃）；``attributes`` 兜底为字符串列表。
    """
    parsed = _loads_lenient(text)
    if parsed is None:
        return []
    entities: list[ExtractedEntity] = []
    for rec in _coerce_records(parsed, "entities"):
        name = str(rec.get("name", "")).strip()
        if not name:
            continue
        etype = str(rec.get("type", "")).strip()
        entities.append(
            ExtractedEntity(
                name=name,
                type=etype,
                attributes=_as_str_list(rec.get("attributes")),
            )
        )
    return entities


def parse_relations(text: str) -> list[ExtractedRelation]:
    """从第二步 LLM 输出文本解析关系列表（容错，不抛错；解析不到返回 []）。

    字段兜底：``source``/``target``/``type`` 任一缺失/空则丢弃该条；``attributes`` 兜底；
    ``confidence`` 非法时回退 1.0 并 clamp 到 [0,1]。
    """
    parsed = _loads_lenient(text)
    if parsed is None:
        return []
    relations: list[ExtractedRelation] = []
    for rec in _coerce_records(parsed, "relations"):
        source = str(rec.get("source", "")).strip()
        target = str(rec.get("target", "")).strip()
        rtype = str(rec.get("type", "")).strip()
        if not source or not target or not rtype:
            continue
        relations.append(
            ExtractedRelation(
                source=source,
                target=target,
                type=rtype,
                attributes=_as_str_list(rec.get("attributes")),
                confidence=_coerce_confidence(rec.get("confidence")),
            )
        )
    return relations


def _coerce_confidence(value: object) -> float:
    """把置信度兜底为 [0,1] 浮点，非法回退 1.0。"""
    try:
        c = float(value)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return 1.0
    if c < 0.0:
        return 0.0
    if c > 1.0:
        return 1.0
    return c


def parse_events(text: str) -> list[ExtractedEvent]:
    """从事件抽取步骤的 LLM 输出文本解析事件列表（容错，不抛错；解析不到返回 []）。

    复用与实体/关系一致的宽松解析（剥 fence、宽松定位 JSON、容忍数组/单对象包裹）。

    字段兜底：
    - ``content`` 缺失/空则丢弃该条（事件无完整语义内容则无意义；与 ``filter_events``
      的空 content 丢弃语义一致，这里提前剔除）。
    - ``title``/``summary`` 缺失回退空字符串。
    - 关联实体名键兼容 ``entities`` 与 ``entity_names`` 两种写法，兜底为字符串列表。
      实体名是否落在已抽实体集合内由 ``filter_events`` 负责约束。
    """
    parsed = _loads_lenient(text)
    if parsed is None:
        return []
    events: list[ExtractedEvent] = []
    for rec in _coerce_records(parsed, "events"):
        content = str(rec.get("content", "")).strip()
        if not content:
            continue
        title = str(rec.get("title", "")).strip()
        summary = str(rec.get("summary", "")).strip()
        raw_names = rec.get("entities")
        if raw_names is None:
            raw_names = rec.get("entity_names")
        events.append(
            ExtractedEvent(
                title=title,
                summary=summary,
                content=content,
                entity_names=_as_str_list(raw_names),
            )
        )
    return events


# ---------------------------------------------------------------------------
# 白名单过滤与悬挂边丢弃（纯函数，可单测）
# ---------------------------------------------------------------------------


def filter_entities(
    entities: list[ExtractedEntity], entity_types: list[str]
) -> list[ExtractedEntity]:
    """丢弃 type 不在白名单内的实体（Requirements 1.3）。

    白名单为空时不过滤（视为不约束类型，与配置层「空白名单回退默认」互补，这里防御性
    地不误删）。
    """
    allowed = {t.strip() for t in entity_types if isinstance(t, str) and t.strip()}
    if not allowed:
        return list(entities)
    return [e for e in entities if e.type in allowed]


def filter_relations(
    relations: list[ExtractedRelation], relation_types: list[str]
) -> list[ExtractedRelation]:
    """丢弃 type 不在白名单内的关系（Requirements 1.3）。"""
    allowed = {t.strip() for t in relation_types if isinstance(t, str) and t.strip()}
    if not allowed:
        return list(relations)
    return [r for r in relations if r.type in allowed]


def drop_dangling_relations(
    relations: list[ExtractedRelation], entities: list[ExtractedEntity]
) -> list[ExtractedRelation]:
    """丢弃端点（source/target）不在实体集合内的关系（无悬挂边，Property 3）。"""
    names = {e.name for e in entities}
    return [r for r in relations if r.source in names and r.target in names]


# 单 chunk 事件抽取数量上限的默认值（与 GraphKBConfig.max_events_per_chunk 默认一致）。
_DEFAULT_MAX_EVENTS_PER_CHUNK = 3


def filter_events(
    events: list[ExtractedEvent],
    entity_names: list[str],
    *,
    max_events: int = _DEFAULT_MAX_EVENTS_PER_CHUNK,
) -> list[ExtractedEvent]:
    """过滤事件：关联实体须落在已抽实体集合内、空 content 丢弃、按上限封顶。

    对齐 design.md 3.1.2 与 Requirements 1.2 / 1.3、Property 1（无悬挂边）：

    - **空 content 丢弃**：``content`` 为空（或纯空白）的事件无完整语义内容，丢弃。
    - **关联实体对齐**：逐事件把 ``entity_names`` 收敛为「落在 ``entity_names`` 集合内」
      的子集（缺失的关联被丢弃，无悬挂关联）；保序去重。
    - **封顶**：保留前 ``max_events`` 个事件（``max_events <= 0`` 视为不封顶）。

    注意：仅过滤关联实体、不要求事件至少关联一个实体——一个事件即便关联实体全部缺失，
    只要 content 非空仍保留（其向量召回入口仍有效），仅其 ``MENTIONS`` 边为空。

    Args:
        events: 待过滤事件列表。
        entity_names: 本 chunk 已抽实体名集合（白名单）。
        max_events: 单 chunk 事件数上限，<=0 表示不封顶。

    Returns:
        过滤并封顶后的事件列表（新对象，不修改入参）。
    """
    allowed = {n for n in entity_names if isinstance(n, str) and n.strip()}
    out: list[ExtractedEvent] = []
    for ev in events:
        content = ev.content.strip() if ev.content else ""
        if not content:
            continue
        aligned: list[str] = []
        seen: set[str] = set()
        for name in ev.entity_names:
            if name in allowed and name not in seen:
                aligned.append(name)
                seen.add(name)
        out.append(
            ExtractedEvent(
                title=ev.title,
                summary=ev.summary,
                content=ev.content,
                entity_names=aligned,
            )
        )
        if max_events > 0 and len(out) >= max_events:
            break
    return out


# ---------------------------------------------------------------------------
# GraphExtractor
# ---------------------------------------------------------------------------

# 抽取调用的稳定性参数：低温度 + 关闭思考，让结构化 JSON 输出更稳定。
# enable_thinking 由各 LLMProvider 翻译为对应方言（vLLM chat_template_kwargs /
# Ollama think 字段）；不支持的 provider 会忽略该 kwarg，无副作用。
_EXTRACT_TEMPERATURE = 0.3


class GraphExtractor:
    """LLM 结构化图谱抽取器（两步法）。

    用法::

        extractor = GraphExtractor(llm_provider)
        graph = await extractor.extract(
            text=chunk_text,
            entity_types=cfg.entity_types,
            relation_types=cfg.relation_types,
        )

    抽取器只产出归一化前的 ``ExtractedGraph``（``normalized_name`` 留空、无 chunk 来源）；
    归一化 / 消歧由 EntityResolver（task 3.2）完成，chunk/doc 来源由 worker（task 4.2）打标。
    """

    def __init__(self, llm: LLMProvider):
        """构造。

        Args:
            llm: 抽取用 LLMProvider（由 worker 按 KB 的 ``extract_model_id`` 选取）。
        """
        self._llm = llm

    async def extract(
        self,
        *,
        text: str,
        entity_types: list[str],
        relation_types: list[str],
    ) -> ExtractedGraph:
        """两步法抽取并返回过滤后的 ``ExtractedGraph``。

        流程：
        1. 文本为空 → 直接返回空图（不调 LLM）。
        2. 第一步 LLM 抽实体 → 容错解析 → 白名单过滤 → 批内按 name 去重。
        3. 无实体 → 返回空图（无实体则无从抽关系）。
        4. 第二步 LLM 抽关系（注入已抽实体名清单）→ 容错解析 → 白名单过滤 →
           丢弃悬挂边（端点须在实体集合内）。
        5. 第三步 LLM 抽事件（注入已抽实体名清单约束关联）→ 容错解析 →
           ``filter_events`` 过滤（空 content 丢弃、关联实体对齐、按上限封顶）。

        Raises:
            GraphExtractError: 当某步 LLM 返回了**非空文本但完全无法解析出 JSON**时抛出，
                交由 worker 重试 / DLQ。空图（``{"entities": []}``）属正常结果不抛错。
        """
        if not text or not text.strip():
            return ExtractedGraph(entities=[], relations=[])

        # ---- 第一步：实体抽取 ----
        entity_messages = prompts.build_entity_messages(text, entity_types)
        entity_raw = await self._call_llm(entity_messages, step="实体抽取")
        entities = self._parse_or_raise(
            entity_raw, parse_entities, step="实体抽取"
        )
        entities = filter_entities(entities, entity_types)
        entities = _dedup_entities(entities)

        if not entities:
            # 无实体则无法抽关系，直接返回空图（正常情况，非错误）。
            return ExtractedGraph(entities=[], relations=[])

        # ---- 第二步：关系抽取（约束端点为已抽实体名）----
        entity_names = [e.name for e in entities]
        relation_messages = prompts.build_relation_messages(
            text, entity_names, relation_types
        )
        relation_raw = await self._call_llm(relation_messages, step="关系抽取")
        relations = self._parse_or_raise(
            relation_raw, parse_relations, step="关系抽取"
        )
        relations = filter_relations(relations, relation_types)
        relations = drop_dangling_relations(relations, entities)

        # ---- 第三步：事件抽取（注入已抽实体名约束关联）----
        event_messages = prompts.build_event_messages(text, entity_names)
        event_raw = await self._call_llm(event_messages, step="事件抽取")
        events = self._parse_or_raise(event_raw, parse_events, step="事件抽取")
        events = filter_events(events, entity_names)

        return ExtractedGraph(entities=entities, relations=relations, events=events)

    async def _call_llm(self, messages: list[dict], *, step: str) -> str:
        """调用 LLM 做一次结构化抽取（低温度、关闭思考），返回原始文本。

        LLM 连接 / 调用异常向上抛出（由 worker 视为可重试失败）。
        """
        try:
            return await self._llm.generate(
                messages,
                temperature=_EXTRACT_TEMPERATURE,
                enable_thinking=False,
            )
        except Exception as e:  # noqa: BLE001 - 统一交由 worker 重试，这里仅记录
            logger.warning("图谱抽取 LLM 调用失败（%s）: %s", step, e)
            raise

    @staticmethod
    def _parse_or_raise(raw: str, parser, *, step: str):
        """解析 LLM 文本：解析得到列表（含空列表）即返回；

        仅当 LLM 返回了**非空文本却完全无法定位出 JSON**时抛 ``GraphExtractError``。
        模型规范地返回 ``{"entities": []}`` 这类空结果会被解析为空列表，属正常。
        """
        # 先判断是否存在可解析 JSON：无 JSON 且原文非空 → 视为彻底失败。
        if _loads_lenient(raw) is None:
            if raw and raw.strip():
                raise GraphExtractError(
                    f"{step}：LLM 输出无法解析为 JSON（前 200 字：{raw.strip()[:200]}）"
                )
            # 空输出按空结果处理（少数 provider 偶发空串），不抛错。
            return []
        return parser(raw)


def _dedup_entities(entities: list[ExtractedEntity]) -> list[ExtractedEntity]:
    """按 ``name`` 在批内去重，保留首次出现并合并后续同名实体的属性（取并集、保序）。

    抽取阶段的轻量去重（确定性归一化 / 跨批消歧由 EntityResolver 负责）。
    """
    by_name: dict[str, ExtractedEntity] = {}
    for e in entities:
        existing = by_name.get(e.name)
        if existing is None:
            by_name[e.name] = ExtractedEntity(
                name=e.name,
                type=e.type,
                attributes=list(e.attributes),
            )
            continue
        # 合并属性（保序去重）。
        seen = set(existing.attributes)
        for attr in e.attributes:
            if attr not in seen:
                existing.attributes.append(attr)
                seen.add(attr)
    return list(by_name.values())
