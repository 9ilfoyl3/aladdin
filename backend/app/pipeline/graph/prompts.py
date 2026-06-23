"""知识图谱抽取 prompt 模板（两步法，中文化）。

移植 WeKnora ``extract_graph`` 的两步抽取思路（先实体+属性、再关系），但重写为
干净、自包含的中文 prompt，并对齐本项目的强约束 JSON 输出 schema（design.md 4.2）：

- 第一步（实体）：从文本抽取实体，输出 ``{"entities": [{"name","type","attributes"}]}``。
- 第二步（关系）：给定第一步抽到的实体清单，抽取实体之间的关系，输出
  ``{"relations": [{"source","target","type","attributes"}]}``。

两步均把 KB 配置的实体类型 / 关系类型白名单内联进 prompt（强约束 LLM 仅在白名单内
取值），并要求**只输出 JSON、不要任何解释或 markdown**。即便如此，下游解析仍做容错
（剥 ```json fence、宽松解析），prompt 只负责把模型尽量往结构化方向引导。

本模块为纯字符串模板构造，无 IO、无副作用，便于单测（task 3.3）。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 输出 schema 片段（两步分别约束）
# ---------------------------------------------------------------------------

# 第一步实体抽取的 JSON 输出 schema（仅 entities）。
_ENTITY_SCHEMA = (
    '{\n'
    '  "entities": [\n'
    '    {"name": "实体名", "type": "实体类型", "attributes": ["属性描述1", "属性描述2"]}\n'
    '  ]\n'
    '}'
)

# 第二步关系抽取的 JSON 输出 schema（仅 relations）。
_RELATION_SCHEMA = (
    '{\n'
    '  "relations": [\n'
    '    {"source": "头实体名", "target": "尾实体名", "type": "关系类型", "attributes": ["关系属性描述"]}\n'
    '  ]\n'
    '}'
)

# 第三步事件抽取的 JSON 输出 schema（仅 events）。
_EVENT_SCHEMA = (
    '{\n'
    '  "events": [\n'
    '    {"title": "事件短标题", "summary": "一句话摘要", '
    '"content": "完整事件内容（主谓宾时地齐全）", "entities": ["关联实体名1", "关联实体名2"]}\n'
    '  ]\n'
    '}'
)


def _format_whitelist(types: list[str]) -> str:
    """把类型白名单格式化为 prompt 内联的、用顿号分隔的字符串。

    去除空白项并保序，便于直接拼进「仅限：...」约束句。空列表回退为占位提示。
    """
    cleaned = [t.strip() for t in types if isinstance(t, str) and t.strip()]
    if not cleaned:
        return "（未配置，可按文本语义判断）"
    return "、".join(cleaned)


# ---------------------------------------------------------------------------
# 第一步：实体（+属性）抽取
# ---------------------------------------------------------------------------

ENTITY_SYSTEM_PROMPT = """你是一个专业的知识图谱实体抽取引擎。你的任务是从用户给定的文本中，\
抽取出符合指定类型白名单的实体，并以严格的 JSON 格式输出。

## 实体类型白名单（type 字段只能取以下值，不得新造类型）
{entity_types}

## 抽取要求
1. 只抽取文本中明确出现的实体，不要臆造、不要推断文本中不存在的实体。
2. 每个实体包含三个字段：
   - name：实体的规范名称，使用文本中的原始表述，不要加引号等修饰。
   - type：实体类型，必须严格从上面的「实体类型白名单」中选取。
   - attributes：该实体的属性描述列表（基于文本内容的简短描述），可为空数组 []。
3. 若某实体无法归入白名单中的任何类型，则宁可跳过该实体，也不要强行归类或新造类型。
4. 同一个实体只输出一次（按规范名去重）。
5. 只输出 JSON，不要输出任何解释、说明或 markdown 代码块标记。

## 输出格式（严格遵守，仅输出该 JSON 对象）
{schema}

## 若文本中没有任何符合白名单的实体
返回：{{"entities": []}}"""

ENTITY_USER_PROMPT = """请从下面的文本中抽取实体，仅输出符合要求的 JSON：

文本：
\"\"\"
{text}
\"\"\""""


# ---------------------------------------------------------------------------
# 第二步：关系抽取（给定已抽取实体）
# ---------------------------------------------------------------------------

RELATION_SYSTEM_PROMPT = """你是一个专业的知识图谱关系抽取引擎。你的任务是：基于给定的\
文本和已抽取的实体清单，抽取实体之间明确存在的关系，并以严格的 JSON 格式输出。

## 关系类型白名单（type 字段只能取以下值，不得新造类型）
{relation_types}

## 抽取要求
1. 只抽取文本中明确表达的关系，不要臆造、不要推断文本中不存在的关系。
2. 关系的 source 与 target 必须是「已抽取实体清单」中已存在的实体名，逐字一致。
3. 每个关系包含四个字段：
   - source：头实体名（必须在实体清单中）。
   - target：尾实体名（必须在实体清单中）。
   - type：关系类型，必须严格从上面的「关系类型白名单」中选取。
   - attributes：关系的附加属性描述列表（简短描述），可为空数组 []。
4. 若某关系类型无法归入白名单，则跳过该关系，不要强行归类或新造类型。
5. 只输出 JSON，不要输出任何解释、说明或 markdown 代码块标记。

## 输出格式（严格遵守，仅输出该 JSON 对象）
{schema}

## 若实体之间没有任何符合白名单的关系
返回：{{"relations": []}}"""

RELATION_USER_PROMPT = """已抽取的实体清单（source/target 只能取其中的 name）：
{entities}

请基于下面的文本，抽取上述实体之间的关系，仅输出符合要求的 JSON：

文本：
\"\"\"
{text}
\"\"\""""


# ---------------------------------------------------------------------------
# 第三步：事件抽取（给定已抽取实体，约束关联实体取值）
# ---------------------------------------------------------------------------

EVENT_SYSTEM_PROMPT = """你是一个专业的知识图谱事件抽取引擎。你的任务是：基于给定的\
文本和已抽取的实体清单，抽取出文本中「主谓宾 + 时间地点」相对齐全的完整事件，并以\
严格的 JSON 格式输出。事件是比单个实体更完整的语义单元，用于后续检索召回完整语义。

## 抽取要求
1. 只抽取文本中明确表达的事件，不要臆造、不要推断文本中不存在的事件。
2. 每个事件包含四个字段：
   - title：事件的短标题（不超过 20 字），概括「谁做了什么」。
   - summary：一句话摘要，简要说明事件经过。
   - content：完整的事件内容，尽量包含主体、动作、客体、时间、地点等要素，保持文本原意。
   - entities：该事件关联的实体名列表，**必须**取自下面「已抽取的实体清单」，逐字一致；
     清单之外的实体名不要出现；若某事件未关联任何清单内实体，则 entities 取空数组 []。
3. 一段文本可能包含多个事件，请逐个抽取；语义高度重复的事件只保留一个。
4. content 为空的事件没有意义，请不要输出空 content 的事件。
5. 只输出 JSON，不要输出任何解释、说明或 markdown 代码块标记。

## 输出格式（严格遵守，仅输出该 JSON 对象）
{schema}

## 若文本中没有任何可抽取的完整事件
返回：{{"events": []}}"""

EVENT_USER_PROMPT = """已抽取的实体清单（entities 只能取其中的 name）：
{entities}

请基于下面的文本，抽取其中的完整事件，仅输出符合要求的 JSON：

文本：
\"\"\"
{text}
\"\"\""""


# ---------------------------------------------------------------------------
# 组装函数（供 GraphExtractor 调用）
# ---------------------------------------------------------------------------


def build_entity_messages(text: str, entity_types: list[str]) -> list[dict]:
    """构造第一步「实体抽取」的对话消息列表。

    Args:
        text: 待抽取的文本块内容。
        entity_types: 实体类型白名单（内联进系统提示）。

    Returns:
        OpenAI 风格消息列表 ``[{"role": "system", ...}, {"role": "user", ...}]``。
    """
    system = ENTITY_SYSTEM_PROMPT.format(
        entity_types=_format_whitelist(entity_types),
        schema=_ENTITY_SCHEMA,
    )
    user = ENTITY_USER_PROMPT.format(text=text)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def build_relation_messages(
    text: str, entity_names: list[str], relation_types: list[str]
) -> list[dict]:
    """构造第二步「关系抽取」的对话消息列表。

    Args:
        text: 待抽取的文本块内容（与第一步同一文本）。
        entity_names: 第一步抽取并过滤后的实体名清单（约束关系端点取值）。
        relation_types: 关系类型白名单（内联进系统提示）。

    Returns:
        OpenAI 风格消息列表。
    """
    system = RELATION_SYSTEM_PROMPT.format(
        relation_types=_format_whitelist(relation_types),
        schema=_RELATION_SCHEMA,
    )
    # 实体清单以逐行编号呈现，便于模型逐字对齐 source/target。
    entities_block = "\n".join(f"- {name}" for name in entity_names) or "（无）"
    user = RELATION_USER_PROMPT.format(entities=entities_block, text=text)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def build_event_messages(text: str, entity_names: list[str]) -> list[dict]:
    """构造第三步「事件抽取」的对话消息列表。

    Args:
        text: 待抽取的文本块内容（与前两步同一文本）。
        entity_names: 已抽取并过滤后的实体名清单（约束事件关联实体取值，须逐字一致）。

    Returns:
        OpenAI 风格消息列表 ``[{"role": "system", ...}, {"role": "user", ...}]``。
    """
    system = EVENT_SYSTEM_PROMPT.format(schema=_EVENT_SCHEMA)
    # 实体清单以逐行呈现，便于模型逐字对齐 entities 取值。
    entities_block = "\n".join(f"- {name}" for name in entity_names) or "（无）"
    user = EVENT_USER_PROMPT.format(entities=entities_block, text=text)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


# ---------------------------------------------------------------------------
# 阶段 4（GraphRAG Global）：社区摘要生成
# ---------------------------------------------------------------------------

# 社区摘要的 JSON 输出 schema（title + summary）。
_COMMUNITY_SCHEMA = (
    '{\n'
    '  "title": "社区主题（不超过 20 字）",\n'
    '  "summary": "对该社区主题与核心关联的概括性摘要"\n'
    '}'
)

COMMUNITY_SYSTEM_PROMPT = """你是一个专业的知识图谱社区摘要引擎。你的任务是：基于给定的\
一组相互关联的实体及其关系，概括出该「社区」围绕的主题，并以严格的 JSON 格式输出。

## 摘要要求
1. 综合实体列表与关系三元组，提炼该社区共同围绕的主题/领域。
2. title：用一句不超过 20 字的短语概括社区主题。
3. summary：用 2~4 句话概括该社区的核心实体、它们之间的主要关联，以及整体讲了什么，
   便于回答「这部分整体讲了什么」这类全局/归纳类问题。
4. 只依据给定的实体与关系，不要臆造未提供的信息。
5. 只输出 JSON，不要输出任何解释、说明或 markdown 代码块标记。

## 输出格式（严格遵守，仅输出该 JSON 对象）
{schema}"""

COMMUNITY_USER_PROMPT = """社区成员实体：
{entities}

社区内部关系（头实体 -[关系]-> 尾实体）：
{relations}

请概括该社区主题，仅输出符合要求的 JSON："""


def build_community_messages(
    member_names: list[str], relations: list[tuple[str, str, str]]
) -> list[dict]:
    """构造「社区摘要生成」的对话消息列表（阶段 4 GraphRAG Global）。

    Args:
        member_names: 社区成员实体规范名列表。
        relations: 社区内部关系三元组列表 ``(source_name, rel_type, target_name)``。

    Returns:
        OpenAI 风格消息列表。
    """
    system = COMMUNITY_SYSTEM_PROMPT.format(schema=_COMMUNITY_SCHEMA)
    entities_block = "、".join(n for n in member_names if n) or "（无）"
    rel_lines = [
        f"- {s} -[{t}]-> {o}" for (s, t, o) in relations if s and o
    ]
    relations_block = "\n".join(rel_lines) or "（无）"
    user = COMMUNITY_USER_PROMPT.format(entities=entities_block, relations=relations_block)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
