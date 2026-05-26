"""查询路由 - 简单/复杂/探索性查询分类

通过 LLM 判断用户查询的类型，决定走快路径、完整 Agent 流程还是探索性检索。
"""

from app.models.provider import LLMProvider


_ROUTER_SYSTEM_PROMPT = """你是一个查询分类器。判断用户查询应该走哪种检索策略。

分类标准：

- **simple**：查询意图明确、聚焦单一信息点，直接检索即可命中。包括：
  - 关键词或短语："合同违约条款"、"第三条规定"
  - 单一事实问题："原被告是谁"、"赔偿金额是多少"、"案号是什么"、"事故时间"
  - 查询中已包含足够的检索关键词

- **exploratory**：用户没有指定具体文档/案件，而是希望从知识库中**发现、筛选、列举**符合条件的内容。包括：
  - 筛选类："有哪些是严重的盗窃案"、"找一下涉及合同诈骗的案件"、"哪些文档提到了知识产权"
  - 列举类："帮我找关于劳动仲裁的内容"、"有没有关于环境污染的案例"
  - 探索类："知识库里有什么关于XX的资料"、"有哪些类型的合同纠纷"
  - 特征：用户在"找东西"而不是"问问题"，期望得到多个结果的列表

- **complex**：查询需要多角度拆解、综合多个信息源才能回答。包括：
  - 需要汇总多方面信息："案件的争议焦点有哪些，各方观点分别是什么"
  - 需要对比分析："原告和被告的主张有什么区别"
  - 需要推理或归纳："这个案件胜诉的可能性如何"
  - 涉及多个子问题："案件经过、判决结果和赔偿明细分别是什么"

判断关键：
- 用户在"找东西/筛选内容" → exploratory
- 用户在"问一个具体问题" → simple
- 用户的问题需要多角度综合 → complex

只回答 simple、exploratory 或 complex，不要输出其他内容。"""


class QueryRouter:
    """查询路由器：将查询分类为 simple、exploratory 或 complex"""

    def __init__(self, llm: LLMProvider):
        self.llm = llm

    async def classify(self, query: str) -> str:
        """判断查询类型，返回 'simple'、'exploratory' 或 'complex'"""
        prompt = [
            {"role": "system", "content": _ROUTER_SYSTEM_PROMPT},
            {"role": "user", "content": query},
        ]
        response = await self.llm.generate(prompt)
        result = response.strip().lower()
        if "exploratory" in result:
            return "exploratory"
        if "complex" in result:
            return "complex"
        return "simple"
