"""KB 级知识图谱配置（存入现有 ``KnowledgeBase.config`` JSON 的 ``graph`` 子字典）。

不新增表，沿用 ``KnowledgeBase.config`` 字典的 ``config["graph"]`` 承载 KB 级图谱开关
与抽取参数（design.md 3.3）。本模块是该子配置的**单一事实源**：

- ``DEFAULT_ENTITY_TYPES`` / ``DEFAULT_RELATION_TYPES``：默认实体/关系类型白名单常量，
  可被 KB 配置覆盖。
- ``GraphKBConfig``：承载 KB 级图谱配置的 dataclass（enabled / entity_types /
  relation_types / extract_granularity / extract_model_id / enable_alias_dedup /
  alias_sim_threshold），默认值取自本模块常量。
- ``read_graph_config``：从 ``KnowledgeBase.config`` dict 逐字段独立兜底读出有效配置
  （缺失 / None / 类型错误 / 越界 → 回退该字段安全默认），结果恒合法。
- ``write_graph_config``：把 ``GraphKBConfig`` 写回 ``config["graph"]`` 子字典，返回新的
  顶层 config dict（不原地修改入参），供调用方持久化到 ``KnowledgeBase.config``。

风格对齐 ``app.retrieval.config`` 的 ``effective_from_raw`` 逐字段兜底范式，但作用域是
单个 KB 的 config JSON，而非平台/租户级配置表。
"""

import logging

logger = logging.getLogger(__name__)


# ============================================================
# 默认实体/关系类型白名单（design.md 3.3，可被 KB 配置覆盖）
# ============================================================

DEFAULT_ENTITY_TYPES: list[str] = [
    "人物", "组织", "地点", "概念", "产品", "事件", "时间", "作品", "技术", "其它",
]
DEFAULT_RELATION_TYPES: list[str] = [
    "属于", "包含", "位于", "参与", "创建", "关联", "导致", "使用", "别名", "前身",
]

# 抽取粒度合法取值（design.md 3.3：默认 parent）
EXTRACT_GRANULARITY_PARENT = "parent"
EXTRACT_GRANULARITY_CHILD = "child"
_VALID_GRANULARITIES = (EXTRACT_GRANULARITY_PARENT, EXTRACT_GRANULARITY_CHILD)

# 别名合并相似度阈值范围（0~1 闭区间）。
_ALIAS_SIM_THRESHOLD_LO = 0.0
_ALIAS_SIM_THRESHOLD_HI = 1.0

# KB config 中承载图谱配置的子键。
GRAPH_CONFIG_KEY = "graph"


# ============================================================
# 安全默认值（单一事实源）
# ============================================================

# KB 级总开关默认关闭（design.md 3.3）：仅显式开启的 KB 才触发抽取。
DEFAULT_ENABLED = False
DEFAULT_EXTRACT_GRANULARITY = EXTRACT_GRANULARITY_PARENT
DEFAULT_EXTRACT_MODEL_ID: str | None = None  # None 表示用 KB 默认 LLM
DEFAULT_ENABLE_ALIAS_DEDUP = True
DEFAULT_ALIAS_SIM_THRESHOLD = 0.92


class GraphKBConfig:
    """KB 级知识图谱有效配置（design.md 3.3）。

    所有字段恒为合法有效值（由 ``read_graph_config`` 逐字段兜底保证）：

    - ``enabled``：KB 级总开关（默认 False）。
    - ``entity_types``：实体类型白名单（默认 ``DEFAULT_ENTITY_TYPES``）。
    - ``relation_types``：关系类型白名单（默认 ``DEFAULT_RELATION_TYPES``）。
    - ``extract_granularity``：抽取粒度 ``"parent"``（默认）| ``"child"``。
    - ``extract_model_id``：指定抽取用 LLM；None 用 KB 默认。
    - ``enable_alias_dedup``：是否启用向量别名消歧（默认 True）。
    - ``alias_sim_threshold``：别名合并相似度阈值（默认 0.92，范围 [0, 1]）。
    """

    __slots__ = (
        "enabled",
        "entity_types",
        "relation_types",
        "extract_granularity",
        "extract_model_id",
        "enable_alias_dedup",
        "alias_sim_threshold",
    )

    def __init__(
        self,
        *,
        enabled: bool = DEFAULT_ENABLED,
        entity_types: list[str] | None = None,
        relation_types: list[str] | None = None,
        extract_granularity: str = DEFAULT_EXTRACT_GRANULARITY,
        extract_model_id: str | None = DEFAULT_EXTRACT_MODEL_ID,
        enable_alias_dedup: bool = DEFAULT_ENABLE_ALIAS_DEDUP,
        alias_sim_threshold: float = DEFAULT_ALIAS_SIM_THRESHOLD,
    ):
        self.enabled = enabled
        self.entity_types = entity_types if entity_types is not None else list(DEFAULT_ENTITY_TYPES)
        self.relation_types = relation_types if relation_types is not None else list(DEFAULT_RELATION_TYPES)
        self.extract_granularity = extract_granularity
        self.extract_model_id = extract_model_id
        self.enable_alias_dedup = enable_alias_dedup
        self.alias_sim_threshold = alias_sim_threshold

    def to_dict(self) -> dict:
        """序列化为可写入 ``config["graph"]`` 的 dict。"""
        return {
            "enabled": self.enabled,
            "entity_types": list(self.entity_types),
            "relation_types": list(self.relation_types),
            "extract_granularity": self.extract_granularity,
            "extract_model_id": self.extract_model_id,
            "enable_alias_dedup": self.enable_alias_dedup,
            "alias_sim_threshold": self.alias_sim_threshold,
        }

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, GraphKBConfig):
            return NotImplemented
        return self.to_dict() == other.to_dict()

    def __repr__(self) -> str:
        return f"GraphKBConfig({self.to_dict()!r})"


# 哨兵：表示该键在 config 中缺失（与显式 None 区分，缺失/None 都静默用默认）。
_MISSING = object()


def _coerce_bool(value: object, default: bool, field: str) -> bool:
    """读出 bool 字段：缺失/None 静默用默认；类型正确原样保留；其余非法回退默认并记 WARNING。"""
    if value is _MISSING or value is None:
        return default
    if isinstance(value, bool):
        return value
    _log_fallback(field, value, default)
    return default


def _coerce_str_list(value: object, default: list[str], field: str) -> list[str]:
    """读出字符串列表白名单：

    - 缺失/None → 静默用默认。
    - 必须是 list 且每项为非空字符串（去首尾空白后非空）；空 list 或非法 → 回退默认并记 WARNING。
    - 合法时去重保序、去首尾空白。
    """
    if value is _MISSING or value is None:
        return list(default)
    if not isinstance(value, list):
        _log_fallback(field, value, default)
        return list(default)

    cleaned: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        stripped = item.strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        cleaned.append(stripped)

    if not cleaned:
        # 空 list / 全非法 → 回退默认（白名单不可为空，否则 LLM 输出全被丢弃）
        _log_fallback(field, value, default)
        return list(default)
    return cleaned


def _coerce_granularity(value: object) -> str:
    """读出抽取粒度：缺失/None 用默认；必须在 ``{parent, child}`` 内，否则回退 parent。"""
    if value is _MISSING or value is None:
        return DEFAULT_EXTRACT_GRANULARITY
    if isinstance(value, str) and value in _VALID_GRANULARITIES:
        return value
    _log_fallback("extract_granularity", value, DEFAULT_EXTRACT_GRANULARITY)
    return DEFAULT_EXTRACT_GRANULARITY


def _coerce_model_id(value: object) -> str | None:
    """读出 extract_model_id：缺失/None/空串 → None（用 KB 默认）；非空字符串保留；其余回退 None。"""
    if value is _MISSING or value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    _log_fallback("extract_model_id", value, DEFAULT_EXTRACT_MODEL_ID)
    return DEFAULT_EXTRACT_MODEL_ID


def _coerce_threshold(value: object) -> float:
    """读出别名相似度阈值：缺失/None 用默认；float/int（非 bool）且落在 [0, 1] 内合法，否则回退默认。"""
    if value is _MISSING or value is None:
        return DEFAULT_ALIAS_SIM_THRESHOLD
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _log_fallback("alias_sim_threshold", value, DEFAULT_ALIAS_SIM_THRESHOLD)
        return DEFAULT_ALIAS_SIM_THRESHOLD
    numeric = float(value)
    if not (_ALIAS_SIM_THRESHOLD_LO <= numeric <= _ALIAS_SIM_THRESHOLD_HI):
        _log_fallback("alias_sim_threshold", value, DEFAULT_ALIAS_SIM_THRESHOLD)
        return DEFAULT_ALIAS_SIM_THRESHOLD
    return numeric


def _log_fallback(field: str, original: object, fallback: object) -> None:
    """记录一条 KB 图谱配置字段兜底回退的 WARNING（含 field、原值、回退值）。"""
    logger.warning(
        "KB 图谱配置字段 graph.%s 触发兜底回退：原值=%r，回退值=%r",
        field,
        original,
        fallback,
    )


def read_graph_config(kb_config: dict | None) -> GraphKBConfig:
    """从 ``KnowledgeBase.config`` dict 读出 KB 级图谱有效配置，逐字段独立兜底。

    逐字段规则（缺失 / None / 类型错误 / 越界 → 回退该字段安全默认；合法值原样保留）：

    - ``enabled`` / ``enable_alias_dedup``：必须为 bool，否则回退默认。
    - ``entity_types`` / ``relation_types``：必须为非空字符串 list（去重去空白），否则回退默认。
    - ``extract_granularity``：必须为 ``"parent"`` | ``"child"``，否则回退 ``"parent"``。
    - ``extract_model_id``：None 或非空字符串合法；其余回退 None。
    - ``alias_sim_threshold``：[0, 1] 内的数值合法；其余回退 0.92。

    Args:
        kb_config: ``KnowledgeBase.config`` 顶层 dict（可为 None，表示 KB 无配置）。

    Returns:
        所有字段均为合法有效值的 ``GraphKBConfig`` 实例。
    """
    top = kb_config if isinstance(kb_config, dict) else {}
    raw = top.get(GRAPH_CONFIG_KEY)
    graph = raw if isinstance(raw, dict) else {}

    return GraphKBConfig(
        enabled=_coerce_bool(graph.get("enabled", _MISSING), DEFAULT_ENABLED, "enabled"),
        entity_types=_coerce_str_list(graph.get("entity_types", _MISSING), DEFAULT_ENTITY_TYPES, "entity_types"),
        relation_types=_coerce_str_list(graph.get("relation_types", _MISSING), DEFAULT_RELATION_TYPES, "relation_types"),
        extract_granularity=_coerce_granularity(graph.get("extract_granularity", _MISSING)),
        extract_model_id=_coerce_model_id(graph.get("extract_model_id", _MISSING)),
        enable_alias_dedup=_coerce_bool(
            graph.get("enable_alias_dedup", _MISSING), DEFAULT_ENABLE_ALIAS_DEDUP, "enable_alias_dedup"
        ),
        alias_sim_threshold=_coerce_threshold(graph.get("alias_sim_threshold", _MISSING)),
    )


def write_graph_config(kb_config: dict | None, graph_config: GraphKBConfig) -> dict:
    """把 ``GraphKBConfig`` 写入顶层 config 的 ``graph`` 子键，返回新的顶层 config dict。

    不原地修改入参 ``kb_config``（浅拷贝顶层后写入），保留其余子键不变，供调用方
    持久化回 ``KnowledgeBase.config``。

    Args:
        kb_config: 现有 ``KnowledgeBase.config`` 顶层 dict（可为 None）。
        graph_config: 待写入的 KB 级图谱配置。

    Returns:
        合并后的新顶层 config dict（``config["graph"]`` 为序列化后的图谱配置）。
    """
    new_config = dict(kb_config) if isinstance(kb_config, dict) else {}
    new_config[GRAPH_CONFIG_KEY] = graph_config.to_dict()
    return new_config
