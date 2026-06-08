"""单轮检索链路的查询理解（改写 + 意图分类）

仅服务于 direct / hybrid 这类「单轮检索式 RAG」链路：这类链路没有 ReAct 自我修正
的机会，必须在检索前一次性把当前用户输入理解清楚——否则闲聊也会触发检索、指代词
（它/这个/那个）会直接砸进检索导致召回崩坏。

设计原则：
- 单一职责：只输出「是否需要检索」+「用于检索的改写后查询」，不碰答案生成。
- 一次 LLM 调用完成改写 + 意图判别，失败时安全降级为「用原始 query 检索」。
- 纯函数式，不持有状态、不写库，数据流向清晰。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from app.models.provider import LLMProvider

logger = logging.getLogger(__name__)

# 不需要检索的意图：直接基于历史/通用对话作答
_NO_RETRIEVAL_INTENTS = {"greeting", "chitchat", "follow_up"}

# 改写结果最大长度保护（防止模型把整段历史灌进来）
_MAX_REWRITE_CHARS = 200

# 历史窗口：最近 N 条消息参与改写判别（每轮 user+assistant 共 2 条）
_HISTORY_WINDOW = 6

_QUERY_UNDERSTAND_PROMPT = """\
你是一个查询理解助手，需要基于对话历史，对用户「当前问题」完成两件事：

## 任务 1：意图分类（intent）
从以下类别中选择 **唯一** 最匹配的一个：
- greeting：纯问候、致谢、告别，无实质问题（如「你好」「谢谢」「再见」「好的」）
- chitchat：闲聊或寒暄，无需查知识库（如「你是谁」「讲个笑话」）
- follow_up：仅要求对【你之前已经回答过的内容】做展开/改写/翻译/换格式，无需新检索
  （如「第二点再详细说说」「把刚才的回答翻成英文」）
- kb_search：需要从知识库检索事实/领域信息才能回答（**不确定时一律选这个**）

## 任务 2：查询改写（rewrite_query）
将「当前问题」改写为一个独立、可直接用于知识库检索的查询：
- 消解指代：把「它/这个/那个/他们/上面提到的/前面说的」替换为历史中的具体实体
- 补全省略的主语/宾语，保持原意
- 必须保留具体实体与关键词（人名、产品名、术语、编号等），**禁止**输出「请在知识库
  中查找…」这类元指令
- 改写后仍是一个问题或检索短语，30 字以内
- intent 为 greeting/chitchat/follow_up 时，rewrite_query 直接等于原始问题即可

## 输出格式
**只**输出一个 JSON 对象，不要 markdown、不要代码块、不要任何解释：
{"intent":"...","rewrite_query":"..."}

## 示例
当前问题：你好
输出：{"intent":"greeting","rewrite_query":"你好"}

当前问题：什么是RAG架构（无历史）
输出：{"intent":"kb_search","rewrite_query":"什么是RAG架构"}

当前问题：它和传统搜索有什么区别（历史在讨论 RAG 架构）
输出：{"intent":"kb_search","rewrite_query":"RAG架构和传统搜索有什么区别"}

当前问题：再帮我查查他的信息（历史在讨论张三）
输出：{"intent":"kb_search","rewrite_query":"张三的详细信息"}

当前问题：第二点再展开讲讲（历史里你已给出带编号的回答）
输出：{"intent":"follow_up","rewrite_query":"第二点再展开讲讲"}

## 对话历史
{conversation}"""


@dataclass
class QueryUnderstanding:
    """查询理解结果"""

    intent: str
    rewrite_query: str

    @property
    def needs_retrieval(self) -> bool:
        """是否需要走检索链路"""
        return self.intent not in _NO_RETRIEVAL_INTENTS


def _format_history(history: list[dict] | None) -> str:
    """将历史消息格式化为改写提示词所需的文本"""
    if not history:
        return "（无历史对话）"
    recent = history[-_HISTORY_WINDOW:]
    lines = []
    for m in recent:
        role = m.get("role", "")
        content = (m.get("content") or "")[:200]
        if role and content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines) if lines else "（无历史对话）"


def _parse_output(raw: str, original_query: str) -> QueryUnderstanding | None:
    """解析模型输出的 JSON，容忍 markdown 包裹与额外文本"""
    content = (raw or "").strip()
    if not content:
        return None

    # 容忍 ```json ... ``` 包裹与前后多余文本：截取首个 {...}
    start = content.find("{")
    end = content.rfind("}")
    if start < 0 or end <= start:
        return None
    candidate = content[start : end + 1]

    try:
        obj = json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        return None

    intent = str(obj.get("intent", "")).strip().lower()
    rewrite = str(obj.get("rewrite_query", "")).strip()

    if intent not in (_NO_RETRIEVAL_INTENTS | {"kb_search"}):
        # 未知意图按需检索处理，最安全
        intent = "kb_search"
    if not rewrite or len(rewrite) > _MAX_REWRITE_CHARS:
        rewrite = original_query

    return QueryUnderstanding(intent=intent, rewrite_query=rewrite)


async def understand_query(
    llm: LLMProvider,
    query: str,
    history: list[dict] | None,
) -> QueryUnderstanding:
    """对单轮检索链路的当前 query 做改写 + 意图判别。

    无历史时跳过 LLM 调用（无指代可消解、无闲聊上下文歧义），直接当作需检索的新问题；
    任何失败都安全降级为「用原始 query 检索」，绝不阻断主流程。

    Args:
        llm: LLM Provider
        query: 当前用户原始查询
        history: 历史对话消息列表 [{"role","content"}, ...]，可为 None

    Returns:
        QueryUnderstanding（intent + rewrite_query）
    """
    # 无历史：没有指代/闲聊歧义需要解析，省一次 LLM 调用，直接按新问题检索
    if not history:
        return QueryUnderstanding(intent="kb_search", rewrite_query=query)

    prompt = _QUERY_UNDERSTAND_PROMPT.replace(
        "{conversation}", _format_history(history)
    )
    user_msg = f"当前问题：{query}\n输出："

    try:
        raw = await llm.generate(
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
        )
    except Exception as e:
        logger.warning("[QueryUnderstand] 调用失败，降级为原始 query 检索: %s", e)
        return QueryUnderstanding(intent="kb_search", rewrite_query=query)

    result = _parse_output(raw, query)
    if result is None:
        logger.warning(
            "[QueryUnderstand] 解析失败，降级为原始 query 检索: raw=%r", (raw or "")[:120]
        )
        return QueryUnderstanding(intent="kb_search", rewrite_query=query)

    logger.info(
        "[QueryUnderstand] intent=%s, query=%r -> rewrite=%r",
        result.intent,
        query[:50],
        result.rewrite_query[:50],
    )
    return result
