"""结果反思 - 分数快判 + LLM 深度评估

两级评估策略：
1. 快速判定（无 LLM 调用）：基于 rerank sigmoid 分数分布判断
   - top-3 平均分 >= 0.7 → 直接判定充分（高置信命中）
   - top-5 平均分 < 0.3 → 直接判定不充分（明显未命中）
2. LLM 深度评估：分数处于中间地带时，调用 LLM 多维度评估

通过快速判定减少约 60% 的 LLM 调用，显著降低迭代延迟。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from app.models.provider import LLMProvider
from app.retrieval.base import RetrievalResult

logger = logging.getLogger(__name__)


@dataclass
class ReflectionVerdict:
    """反思判定结果"""

    is_sufficient: bool  # 检索结果是否充分
    follow_up_queries: list[str] = field(default_factory=list)  # 追加查询
    relevance_score: float = 0.0  # 相关性评分 (0~1)
    coverage_score: float = 0.0  # 覆盖度评分 (0~1)
    consistency_score: float = 0.0  # 一致性评分 (0~1)
    reasoning: str = ""  # 评估推理过程


_SYSTEM_PROMPT = """你是一个专业的检索质量评估器。你需要从多个维度评估检索结果是否能充分回答用户查询。

评估维度：
1. **相关性 (relevance)**：检索结果与查询的语义相关程度。0 表示完全无关，1 表示高度相关。
2. **覆盖度 (coverage)**：检索结果是否包含了能直接回答查询的具体信息。0 表示完全未覆盖，1 表示完全覆盖。
   - 对于"谁"类问题，结果中必须包含具体的人名/机构名才算覆盖
   - 对于"什么时候"类问题，结果中必须包含具体日期才算覆盖
   - 对于"多少"类问题，结果中必须包含具体数字才算覆盖
   - 仅仅出现关键词但没有给出答案，覆盖度应该很低
3. **一致性 (consistency)**：多条检索结果之间是否存在事实矛盾。1 表示完全一致，0 表示严重矛盾。

综合判定规则：
- 当 relevance >= 0.6 且 coverage >= 0.6 且 consistency >= 0.5 时，判定为充分
- 否则判定为不充分，并生成 1-3 个针对性的追加查询来补充缺失信息

生成追加查询的策略：
- 使用不同的关键词组合
- 尝试从文档可能的表述角度出发
- 对于法律文书，尝试检索"当事人信息"、"起诉状"、"判决书"等特定段落

请严格以 JSON 格式回答，不要输出其他内容：
{
  "relevance": 0.8,
  "coverage": 0.7,
  "consistency": 0.9,
  "sufficient": true,
  "reasoning": "简要说明判断依据",
  "follow_up_queries": []
}

如果不充分，follow_up_queries 应包含 1-3 个具体的追加查询。"""


# 快速判定阈值（基于 sigmoid 归一化后的 rerank 分数）
_HIGH_CONFIDENCE_THRESHOLD = 0.7  # top-3 均分 >= 此值 → 充分
_LOW_CONFIDENCE_THRESHOLD = 0.3   # top-5 均分 < 此值 → 不充分


class Reflector:
    """结果反思器：分数快判 + LLM 深度评估"""

    def __init__(self, llm: LLMProvider):
        self.llm = llm

    async def evaluate(
        self, query: str, results: list[RetrievalResult]
    ) -> ReflectionVerdict:
        """评估检索结果质量，决定是否需要追加检索

        两级策略：先用分数快判，不确定时再调 LLM。

        Args:
            query: 用户原始查询
            results: 当前已检索到的结果列表

        Returns:
            ReflectionVerdict 包含多维度评分、是否充分及追加查询
        """
        # 无结果直接判定不充分
        if not results:
            return ReflectionVerdict(
                is_sufficient=False,
                follow_up_queries=[query],
                relevance_score=0.0,
                coverage_score=0.0,
                consistency_score=1.0,
                reasoning="无检索结果",
            )

        # === 快速判定：基于 sigmoid 分数分布 ===
        fast_verdict = self._fast_evaluate(query, results)
        if fast_verdict is not None:
            return fast_verdict

        # === LLM 深度评估：分数处于中间地带 ===
        context_text = self._build_context(results)

        prompt = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"用户查询：{query}\n\n检索结果：\n{context_text}"},
        ]

        response = await self.llm.generate(prompt)
        return self._parse_response(response, query)

    def _fast_evaluate(
        self, query: str, results: list[RetrievalResult]
    ) -> ReflectionVerdict | None:
        """基于 rerank 分数的快速判定，返回 None 表示需要 LLM 深度评估

        判定逻辑（分数已经过 sigmoid 归一化到 [0,1]）：
        - top-3 均分 >= 0.7：高置信命中，直接充分
        - top-5 均分 < 0.3：明显未命中，直接不充分
        - 其他情况：交给 LLM 判断
        """
        top3_scores = [r.score for r in results[:3]]
        top5_scores = [r.score for r in results[:5]]

        avg_top3 = sum(top3_scores) / len(top3_scores) if top3_scores else 0
        avg_top5 = sum(top5_scores) / len(top5_scores) if top5_scores else 0

        # 高置信命中：top-3 分数都很高
        if avg_top3 >= _HIGH_CONFIDENCE_THRESHOLD:
            print(f"[Reflector] 快速判定充分: top3_avg={avg_top3:.3f} >= {_HIGH_CONFIDENCE_THRESHOLD}")
            return ReflectionVerdict(
                is_sufficient=True,
                relevance_score=avg_top3,
                coverage_score=avg_top3 * 0.9,  # 保守估计覆盖度略低于相关性
                consistency_score=1.0,
                reasoning=f"快速判定：top-3 平均分 {avg_top3:.2f}，高置信命中",
            )

        # 明显未命中：top-5 分数都很低
        if avg_top5 < _LOW_CONFIDENCE_THRESHOLD:
            print(f"[Reflector] 快速判定不充分: top5_avg={avg_top5:.3f} < {_LOW_CONFIDENCE_THRESHOLD}")
            return ReflectionVerdict(
                is_sufficient=False,
                follow_up_queries=[query],
                relevance_score=avg_top5,
                coverage_score=avg_top5 * 0.5,
                consistency_score=1.0,
                reasoning=f"快速判定：top-5 平均分 {avg_top5:.2f}，检索结果与查询相关性低",
            )

        # 中间地带：需要 LLM 深度评估
        return None

    def _build_context(self, results: list[RetrievalResult]) -> str:
        """构建评估上下文，包含分数信息帮助 LLM 判断"""
        parts = []
        for i, r in enumerate(results[:8]):  # 最多取 8 条
            snippet = r.content[:400]
            # 包含分数信息，帮助 LLM 理解检索置信度
            parts.append(f"[{i + 1}] (score={r.score:.3f}) {snippet}")
        return "\n\n".join(parts)

    def _parse_response(self, response: str, query: str) -> ReflectionVerdict:
        """解析 LLM 响应为 ReflectionVerdict"""
        try:
            text = response.strip()
            # 处理 markdown 代码块包裹
            if "```" in text:
                parts = text.split("```")
                for part in parts[1:]:
                    cleaned = part.strip()
                    if cleaned.startswith("json"):
                        cleaned = cleaned[4:]
                    cleaned = cleaned.strip()
                    if cleaned.startswith("{"):
                        text = cleaned
                        break

            data = json.loads(text)

            relevance = float(data.get("relevance", 0.5))
            coverage = float(data.get("coverage", 0.5))
            consistency = float(data.get("consistency", 1.0))
            reasoning = data.get("reasoning", "")
            follow_up = data.get("follow_up_queries", [])

            # 综合判定
            is_sufficient = data.get("sufficient", False)
            # 如果 LLM 没有明确给出 sufficient 字段，用规则判定
            if "sufficient" not in data:
                is_sufficient = (
                    relevance >= 0.6 and coverage >= 0.6 and consistency >= 0.5
                )

            if not is_sufficient and not follow_up:
                follow_up = [query]

            return ReflectionVerdict(
                is_sufficient=is_sufficient,
                follow_up_queries=follow_up if not is_sufficient else [],
                relevance_score=relevance,
                coverage_score=coverage,
                consistency_score=consistency,
                reasoning=reasoning,
            )

        except (json.JSONDecodeError, KeyError, IndexError, ValueError) as e:
            logger.warning("Reflector 解析 LLM 响应失败: %s, 响应: %s", e, response[:200])
            # 解析失败时默认判定为充分，避免无限循环
            return ReflectionVerdict(
                is_sufficient=True,
                relevance_score=0.5,
                coverage_score=0.5,
                consistency_score=1.0,
                reasoning="LLM 响应解析失败，默认判定充分",
            )
