"""查询规划器 - 意图拆分 + 分组查询生成

参考 WeKnora 的 ReAct Agent 思路，将用户查询拆分为多个独立意图组，
每个意图组生成针对性的检索查询。确保多意图查询（如"找案例+找法条"）
的每个意图都有独立的检索路径，不会被高分结果"淹没"。

替代原有的 Router + Rewriter 两步流程，合并为一步完成：
1. 判断查询复杂度（simple/complex）
2. 如果 complex，拆分为多个意图组，每组生成 1-3 个检索查询
3. 如果 simple，生成单组查询
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from app.models.provider import LLMProvider

logger = logging.getLogger(__name__)


@dataclass
class IntentGroup:
    """单个意图组"""
    intent: str  # 意图描述（如"查找相关法条"）
    queries: list[str] = field(default_factory=list)  # 该意图的检索查询列表


@dataclass
class QueryPlan:
    """查询规划结果"""
    complexity: str  # simple | complex
    intent_groups: list[IntentGroup] = field(default_factory=list)
    original_query: str = ""


_PLANNER_SYSTEM_PROMPT = """你是一个专业的查询规划器。你需要完成两个任务：
1. 判断查询复杂度（simple 或 complex）
2. 将查询拆分为独立的信息需求（意图组），并为每个意图生成适合语义检索的查询

## 复杂度判断

- **simple**：查询只有一个明确的信息需求，可以用一组关键词直接命中
  - 单一事实问题："合同签订时间"、"被告是谁"、"赔偿金额多少"
  - 明确的概念查询："什么是不当得利"、"合同违约的构成要件"
  
- **complex**：查询包含多个独立的信息需求，或需要从不同类型的文档中检索
  - 多类型信息："离婚财产纠纷的案件与涉及的法条"（案例 + 法条 = 两种文档类型）
  - 多角度对比："原告和被告的主张分别是什么"（原告视角 + 被告视角）
  - 多步骤综合："案件经过、判决结果和赔偿明细"（三个独立信息点）

## 意图拆分规则

- 每个独立的信息需求构成一个意图组
- 意图之间应互补而非重复
- 不同类型的文档（案例、法条、合同、判决书等）通常属于不同意图
- 最多拆分为 4 个意图组

## 查询生成规则

为每个意图生成 1-3 个语义检索查询。查询应该是：
- **概念性问题或陈述**，而非关键词堆砌
- **多样化**：覆盖不同的表述角度（同义词、专业术语、假设文档片段）
- **自包含**：每个查询独立可理解，不依赖其他查询
- **适度具体**：不要过于宽泛（如"法律"），也不要过于狭窄

好的查询示例：
- "离婚财产分割的典型裁判案例"
- "民法典关于夫妻共同财产分割的规定"
- "婚姻关系存续期间取得的财产如何认定"

不好的查询示例：
- "离婚 财产 法条"（关键词堆砌）
- "法律"（过于宽泛）
- "请帮我查找相关法条"（元指令，不是检索查询）

## 输出格式

严格以 JSON 格式输出，不要输出其他内容：
{
  "complexity": "simple 或 complex",
  "intent_groups": [
    {
      "intent": "意图的简短描述",
      "queries": ["查询1", "查询2", "查询3"]
    }
  ]
}"""


class QueryPlanner:
    """查询规划器：意图拆分 + 分组查询生成

    替代原有的 Router + Rewriter，一次 LLM 调用完成：
    - 复杂度判断
    - 多意图识别
    - 分组查询生成
    """

    def __init__(self, llm: LLMProvider):
        self.llm = llm

    async def plan(self, query: str) -> QueryPlan:
        """分析查询并生成检索计划

        Returns:
            QueryPlan 包含复杂度判断和分组查询
        """
        prompt = [
            {"role": "system", "content": _PLANNER_SYSTEM_PROMPT},
            {"role": "user", "content": query},
        ]

        response = await self.llm.generate(prompt)
        plan = self._parse_response(response, query)
        plan.original_query = query
        return plan

    def _parse_response(self, response: str, query: str) -> QueryPlan:
        """解析 LLM 响应为 QueryPlan"""
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

            complexity = data.get("complexity", "simple")
            if complexity not in ("simple", "complex"):
                complexity = "simple"

            intent_groups = []
            for group_data in data.get("intent_groups", []):
                intent = group_data.get("intent", "")
                queries = group_data.get("queries", [])
                # 过滤无效查询
                queries = [q.strip() for q in queries if q.strip() and 2 <= len(q.strip()) <= 200]
                if queries:
                    intent_groups.append(IntentGroup(intent=intent, queries=queries))

            # 如果解析失败或没有有效意图组，回退到单组
            if not intent_groups:
                intent_groups = [IntentGroup(intent="默认检索", queries=[query])]

            # 确保每个意图组都包含原始查询的某种变体（兜底）
            # 限制最多 4 个意图组
            intent_groups = intent_groups[:4]

            return QueryPlan(complexity=complexity, intent_groups=intent_groups)

        except (json.JSONDecodeError, KeyError, IndexError, ValueError) as e:
            logger.warning("Planner 解析 LLM 响应失败: %s, 响应: %s", e, response[:200])
            # 解析失败时回退到简单模式
            return QueryPlan(
                complexity="simple",
                intent_groups=[IntentGroup(intent="默认检索", queries=[query])],
            )
