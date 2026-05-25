"""查询改写 - 多策略查询扩展

通过 LLM 将用户查询改写为多个更适合检索的查询，综合运用：
- 关键词扩展：提取核心实体和同义词
- 假设文档生成 (HyDE)：生成可能包含答案的文档片段
- 子问题分解：将复杂问题拆解为可独立检索的子问题
- 视角转换：从文档作者的角度思考内容如何表述
"""

from app.models.provider import LLMProvider


_REWRITE_SYSTEM_PROMPT = """你是一个查询改写助手，将用户问题改写为 2-3 个适合语义检索的查询。

## 改写策略

1. **语义扩展**：用不同的表述方式表达相同的信息需求
2. **假设文档片段**：想象目标文档中可能包含答案的那段文字是怎么写的
3. **视角转换**：从文档作者的角度思考信息的表述方式

## 规则

- 每个改写查询独占一行，不要编号、不要加引号
- 改写应该是**概念性问题或陈述**，不要堆砌关键词
- 不要生成元指令（如"请搜索..."、"请查找..."）
- 保留原始查询中的核心实体和专业术语
- 只输出 2-3 个最有价值的改写

## 示例

用户问题：原被告是谁
改写结果：
案件当事人的基本信息
原告和被告的身份情况
本案原告为 被告为

用户问题：合同签订时间是什么时候
改写结果：
合同的签订日期和生效时间
双方于何时签订本协议
本合同签署日期

用户问题：离婚财产如何分割
改写结果：
离婚时夫妻共同财产的分割方式
婚姻关系存续期间取得的财产如何处理
法院判决财产分割的原则和标准"""


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
