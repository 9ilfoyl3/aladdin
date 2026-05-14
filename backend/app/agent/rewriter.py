"""查询改写 - 多策略查询扩展

通过 LLM 将用户查询改写为多个更适合检索的查询，综合运用：
- 关键词扩展：提取核心实体和同义词
- 假设文档生成 (HyDE)：生成可能包含答案的文档片段
- 子问题分解：将复杂问题拆解为可独立检索的子问题
- 视角转换：从文档作者的角度思考内容如何表述
"""

from app.models.provider import LLMProvider


_REWRITE_SYSTEM_PROMPT = """你是一个专业的查询改写助手，专门为文档检索系统优化查询。

你的任务是将用户的自然语言问题改写为 2-3 个更适合向量检索和关键词匹配的查询。

改写策略：
1. **关键词提取**：从问题中提取核心实体、关键术语，组合为简洁的检索词
2. **假设答案片段**：想象文档中可能包含答案的那段文字是怎么写的，生成类似的片段
3. **视角转换**：从文档作者的角度思考，这个信息在文档中通常如何表述

重要规则：
- 每个改写查询独占一行，不要编号、不要加引号
- 改写应该多样化，覆盖不同的检索角度
- 对于"谁/什么/哪里"类问题，要生成可能包含答案的文档片段形式
- 对于法律、合同等专业文档，使用该领域的专业术语
- 不要生成过于宽泛或过于具体的查询
- 只输出 2-3 个最有价值的改写，不要凑数

示例：
用户问题：原被告是谁
改写结果：
原告 被告 当事人信息
原告：XX 被告：XX
案件当事人 自然人 法人

用户问题：合同签订时间是什么时候
改写结果：
合同签订日期 签署时间
本合同于 年 月 日签订
双方于 签订本协议"""


class QueryRewriter:
    """查询改写器：多策略查询扩展，返回多个检索查询"""

    def __init__(self, llm: LLMProvider):
        self.llm = llm

    async def rewrite(self, query: str) -> list[str]:
        """改写查询：多策略扩展，返回多个适合检索的查询

        策略包括关键词扩展、假设文档生成、视角转换。
        始终保留原始查询作为兜底。限制最多 4 个查询以平衡召回和延迟。
        """
        prompt = [
            {"role": "system", "content": _REWRITE_SYSTEM_PROMPT},
            {"role": "user", "content": query},
        ]
        response = await self.llm.generate(prompt)
        queries = [q.strip() for q in response.strip().split("\n") if q.strip()]

        # 过滤掉过短或过长的查询
        queries = [q for q in queries if 2 <= len(q) <= 200]

        # 始终包含原始查询
        if query not in queries:
            queries.insert(0, query)

        # 限制最多 4 个查询（原始 + 3 个改写），平衡召回率和响应速度
        return queries[:4]
