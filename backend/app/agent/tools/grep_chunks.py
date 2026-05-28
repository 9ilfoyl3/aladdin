"""grep_chunks Tool - BM25 关键词精确匹配工具

基于 BM25 全文检索，适用于搜索特定术语、名称、编号等精确关键词。
不做语义理解，纯粹基于关键词匹配。
"""

import logging

from app.agent.tools.base import BaseTool, ToolResult
from app.retrieval.bm25 import BM25Retriever

logger = logging.getLogger(__name__)


class GrepChunksTool(BaseTool):
    """BM25 关键词精确匹配工具"""

    def __init__(self, retriever: BM25Retriever, kb_id: str):
        self._retriever = retriever
        self._kb_id = kb_id

    @property
    def name(self) -> str:
        return "grep_chunks"

    @property
    def description(self) -> str:
        return (
            "BM25 关键词精确匹配工具。适用于搜索特定术语、名称、编号等精确关键词。"
            "不做语义理解，纯粹基于关键词匹配。"
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "关键词查询字符串",
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回结果数量",
                    "default": 10,
                },
            },
            "required": ["query"],
        }

    async def execute(self, args: dict) -> ToolResult:
        """执行 BM25 关键词匹配检索"""
        query = args.get("query", "")
        top_k = args.get("top_k", 10)

        if not query.strip():
            return ToolResult(success=False, error="query 参数不能为空")

        try:
            results = await self._retriever.search(
                query=query, kb_id=self._kb_id, top_k=top_k
            )

            # chunk_id 去重，保留最高分
            seen_ids: dict[str, int] = {}
            deduped = []
            for r in results:
                if r.chunk_id not in seen_ids:
                    seen_ids[r.chunk_id] = len(deduped)
                    deduped.append(r)
                else:
                    idx = seen_ids[r.chunk_id]
                    if r.score > deduped[idx].score:
                        deduped[idx] = r

            # XML 格式化输出
            output = self._format_xml(deduped)

            return ToolResult(success=True, output=output)

        except Exception as e:
            logger.exception("grep_chunks 执行失败: %s", e)
            return ToolResult(success=False, error=f"grep_chunks 执行失败: {e}")

    def _format_xml(self, results: list) -> str:
        """将检索结果格式化为 XML"""
        if not results:
            return '<search_results count="0"></search_results>'

        lines = [f'<search_results count="{len(results)}">']
        for rank, r in enumerate(results, 1):
            lines.append(
                f'<chunk rank="{rank}" chunk_id="{r.chunk_id}" '
                f'doc_id="{r.doc_id}" score="{r.score:.4f}">'
            )
            lines.append(f"<content>{r.content}</content>")
            lines.append("</chunk>")
        lines.append("</search_results>")

        return "\n".join(lines)
