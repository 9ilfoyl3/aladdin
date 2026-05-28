"""knowledge_search 工具 - 语义检索

通过 HybridRetriever（Dense + Sparse + BM25 + RRF + Rerank）执行语义检索，
支持多 query 并发检索、chunk_id 去重、跨调用 seen_chunks 去重，
输出 XML 格式供 LLM 解析。
"""

import asyncio
import logging
from xml.sax.saxutils import escape as xml_escape

from app.agent.state import AgentState
from app.agent.tools.base import BaseTool, ToolResult
from app.retrieval.base import RetrievalResult
from app.retrieval.hybrid import HybridRetriever

logger = logging.getLogger(__name__)


class KnowledgeSearchTool(BaseTool):
    """语义检索工具 - 基于 HybridRetriever 的多 query 语义搜索

    使用向量嵌入理解查询意图，从知识库中检索语义相关的内容块。
    内部调用 HybridRetriever（Dense + BM25 + 可选 Sparse + RRF 融合 + Rerank）。
    支持多知识库并发检索。
    """

    def __init__(
        self,
        retriever: HybridRetriever,
        kb_id: str,
        state: AgentState,
        knowledge_base_ids: list[str] | None = None,
    ):
        self._retriever = retriever
        self._kb_id = kb_id
        self._state = state
        # 多知识库 ID 列表（可选，用于跨库检索）
        self._knowledge_base_ids = knowledge_base_ids

    @property
    def name(self) -> str:
        return "knowledge_search"

    @property
    def description(self) -> str:
        return (
            "Semantic search tool for retrieving knowledge by meaning, intent, and conceptual relevance. "
            "Uses embeddings to find semantically similar content across knowledge base chunks. "
            "Designed for conceptual explanations, topic overviews, reasoning-based information needs, "
            "and intent-driven retrieval. Searches by MEANING rather than exact text. "
            "Each query should be a short, well-formed semantic question or conceptual statement. "
            "Avoid keyword lists or raw text from user messages."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array",
                    "description": "1-5 semantic questions or conceptual statements for retrieval",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 5,
                },
                "top_k": {
                    "type": "integer",
                    "description": "Number of top results to return per query",
                    "default": 10,
                },
                "knowledge_base_ids": {
                    "type": "array",
                    "description": "Optional list of knowledge base IDs to search across. If not provided, searches the default knowledge base.",
                    "items": {"type": "string"},
                },
            },
            "required": ["queries"],
        }

    async def execute(self, args: dict) -> ToolResult:
        """执行语义检索

        流程：多 query 并发检索 → 合并 → chunk_id 去重 → seen_chunks 跨调用去重 → XML 格式化
        支持多知识库并发检索：如果指定了 knowledge_base_ids，对每个 kb_id 并行检索后合并。
        """
        queries: list[str] = args.get("queries", [])
        top_k: int = args.get("top_k", 10)
        kb_ids_param: list[str] | None = args.get("knowledge_base_ids")

        if not queries:
            return ToolResult(success=False, error="queries parameter is required")

        # 确定要检索的知识库列表
        if kb_ids_param:
            kb_ids = kb_ids_param
        elif self._knowledge_base_ids:
            kb_ids = self._knowledge_base_ids
        else:
            kb_ids = [self._kb_id]

        logger.info(
            "[KnowledgeSearch] Executing with %d queries, top_k=%d, kb_ids=%s",
            len(queries),
            top_k,
            kb_ids,
        )

        # 对每个 kb_id × query 组合并发检索
        try:
            search_tasks = [
                self._retriever.search(query=q, kb_id=kb, top_k=top_k)
                for kb in kb_ids
                for q in queries
            ]
            all_results_nested: list[list[RetrievalResult]] = await asyncio.gather(
                *search_tasks, return_exceptions=True
            )
        except Exception as e:
            logger.error("[KnowledgeSearch] Search failed: %s", e)
            return ToolResult(success=False, error=f"Search failed: {e}")

        # 合并所有结果，过滤异常
        merged: list[RetrievalResult] = []
        for i, result in enumerate(all_results_nested):
            if isinstance(result, Exception):
                logger.warning(
                    "[KnowledgeSearch] Query '%s' failed: %s", queries[i], result
                )
                continue
            merged.extend(result)

        logger.info("[KnowledgeSearch] Merged %d raw results from %d queries", len(merged), len(queries))

        # chunk_id 去重：保留最高分
        deduped = self._deduplicate_by_chunk_id(merged)

        # 按 score 降序排序
        deduped.sort(key=lambda r: r.score, reverse=True)

        # 截取 top_k
        deduped = deduped[:top_k]

        logger.info("[KnowledgeSearch] After dedup and top_k: %d results", len(deduped))

        # 跨调用 seen_chunks 去重标记
        results_with_status = self._mark_seen_chunks(deduped)

        # 无结果处理
        if not results_with_status:
            output = (
                "<search_results count=\"0\">\n"
                "</search_results>\n"
                "No relevant content found in knowledge base.\n"
                "DO NOT use training data or general knowledge to answer. "
                "State that no relevant information was found."
            )
            return ToolResult(success=True, output=output)

        # XML 格式化输出
        output = self._format_xml_output(results_with_status)

        # 收集引用到 AgentState
        for result, _seen in results_with_status:
            if not _seen:
                self._state.knowledge_refs.append(result)

        return ToolResult(
            success=True,
            output=output,
            data={"count": len(results_with_status)},
        )

    def _deduplicate_by_chunk_id(
        self, results: list[RetrievalResult]
    ) -> list[RetrievalResult]:
        """按 chunk_id 去重，保留最高分的结果"""
        best: dict[str, RetrievalResult] = {}
        for r in results:
            if r.chunk_id not in best or r.score > best[r.chunk_id].score:
                best[r.chunk_id] = r
        return list(best.values())

    def _mark_seen_chunks(
        self, results: list[RetrievalResult]
    ) -> list[tuple[RetrievalResult, bool]]:
        """标记已在之前调用中返回过的 chunk

        Returns:
            list of (result, is_already_seen) tuples
        """
        marked: list[tuple[RetrievalResult, bool]] = []
        for r in results:
            already_seen = r.chunk_id in self._state.seen_chunk_ids
            # 无论是否 seen，都记录到 seen_chunk_ids
            self._state.seen_chunk_ids.add(r.chunk_id)
            marked.append((r, already_seen))
        return marked

    def _format_xml_output(
        self, results_with_status: list[tuple[RetrievalResult, bool]]
    ) -> str:
        """生成 XML 格式化输出

        格式：
        <search_results count="N">
          <chunk rank="1" chunk_id="..." doc_id="..." score="0.85">
            <content>...</content>
          </chunk>
          <chunk rank="2" chunk_id="..." doc_id="..." score="0.80" status="already_retrieved">
            <content>[Already retrieved - see above]</content>
          </chunk>
        </search_results>
        """
        lines: list[str] = []
        count = len(results_with_status)
        lines.append(f'<search_results count="{count}">')

        for rank, (result, is_seen) in enumerate(results_with_status, start=1):
            if is_seen:
                lines.append(
                    f'<chunk rank="{rank}" chunk_id="{xml_escape(result.chunk_id)}" '
                    f'doc_id="{xml_escape(result.doc_id)}" '
                    f'score="{result.score:.2f}" status="already_retrieved">'
                )
                lines.append("<content>[Already retrieved - see above]</content>")
            else:
                lines.append(
                    f'<chunk rank="{rank}" chunk_id="{xml_escape(result.chunk_id)}" '
                    f'doc_id="{xml_escape(result.doc_id)}" '
                    f'score="{result.score:.2f}">'
                )
                # 优先使用 child_content（精准命中部分），fallback 到 content（父块完整内容）
                content = result.child_content if result.child_content else result.content
                lines.append(f"<content>{xml_escape(content)}</content>")
            lines.append("</chunk>")

        lines.append("</search_results>")
        return "\n".join(lines)
