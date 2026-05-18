"""查询路由 - 简单/复杂查询分类

通过 LLM 判断用户查询的复杂度，决定走快路径还是完整 Agent 流程。
"""

from app.models.provider import LLMProvider


_ROUTER_SYSTEM_PROMPT = """你是一个查询分类器。判断用户查询应该走简单检索还是复杂检索。

分类标准：
- **simple**：查询意图明确、聚焦单一信息点，直接检索即可命中。包括：
  - 关键词或短语："合同违约条款"、"第三条规定"
  - 单一事实问题："原被告是谁"、"案件主体是谁"、"赔偿金额是多少"、"案号是什么"、"事故时间"
  - 查询中已包含足够的检索关键词

- **complex**：查询需要多角度拆解、综合多个信息源才能回答。包括：
  - 需要汇总多方面信息："案件的争议焦点有哪些，各方观点分别是什么"
  - 需要对比分析："原告和被告的主张有什么区别"
  - 需要推理或归纳："这个案件胜诉的可能性如何"
  - 涉及多个子问题："案件经过、判决结果和赔偿明细分别是什么"

判断关键：如果用户的问题可以用一个明确的关键词组合直接检索到答案，就是 simple；如果需要从不同角度检索再综合，才是 complex。

只回答 simple 或 complex，不要输出其他内容。"""


class QueryRouter:
    """查询路由器：将查询分类为 simple 或 complex"""

    def __init__(self, llm: LLMProvider):
        self.llm = llm

    async def classify(self, query: str) -> str:
        """判断查询复杂度，返回 'simple' 或 'complex'"""
        prompt = [
            {"role": "system", "content": _ROUTER_SYSTEM_PROMPT},
            {"role": "user", "content": query},
        ]
        response = await self.llm.generate(prompt)
        result = response.strip().lower()
        return "complex" if "complex" in result else "simple"
