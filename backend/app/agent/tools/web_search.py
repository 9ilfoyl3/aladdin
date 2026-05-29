"""web_search Tool - 网页搜索工具

用于搜索知识库中没有的实时信息。
优先使用 SearXNG API，fallback 到 DuckDuckGo HTML API。
"""

import logging
from urllib.parse import quote_plus
from xml.sax.saxutils import escape as xml_escape

import httpx

from app.agent.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)

# HTTP 请求超时（秒）
_REQUEST_TIMEOUT = 10.0


class WebSearchTool(BaseTool):
    """网页搜索工具 - 用于搜索知识库中没有的实时信息。

    优先调用 SearXNG API，失败时 fallback 到 DuckDuckGo。
    """

    def __init__(self, searxng_url: str = "http://localhost:8080"):
        self._searxng_url = searxng_url.rstrip("/")

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return "网页搜索工具。当知识库中没有相关信息时，可以搜索互联网获取最新信息。"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索查询关键词",
                },
                "max_results": {
                    "type": "integer",
                    "description": "最大返回结果数",
                    "default": 5,
                },
            },
            "required": ["query"],
        }

    async def execute(self, args: dict) -> ToolResult:
        """执行网页搜索：优先 SearXNG，fallback DuckDuckGo"""
        query: str = args.get("query", "")
        max_results: int = args.get("max_results", 5)

        if not query.strip():
            return ToolResult(success=False, error="query parameter is required")

        # 尝试 SearXNG
        results = await self._search_searxng(query, max_results)

        # Fallback 到 DuckDuckGo
        if results is None:
            logger.info("[WebSearch] SearXNG failed, falling back to DuckDuckGo")
            results = await self._search_duckduckgo(query, max_results)

        if results is None:
            return ToolResult(
                success=False,
                error="All search backends failed. Please try again later.",
            )

        # XML 格式化输出
        output = self._format_xml(results)
        return ToolResult(success=True, output=output)

    async def _search_searxng(
        self, query: str, max_results: int
    ) -> list[dict] | None:
        """通过 SearXNG API 搜索

        Returns:
            搜索结果列表 [{"title": ..., "url": ..., "snippet": ...}]，失败返回 None
        """
        try:
            url = f"{self._searxng_url}/search"
            params = {
                "q": query,
                "format": "json",
                "categories": "general",
            }
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()

            raw_results = data.get("results", [])
            results = []
            for item in raw_results[:max_results]:
                results.append({
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "snippet": item.get("content", ""),
                })
            return results

        except Exception as e:
            logger.warning("[WebSearch] SearXNG search failed: %s", e)
            return None

    async def _search_duckduckgo(
        self, query: str, max_results: int
    ) -> list[dict] | None:
        """通过 DuckDuckGo HTML API 搜索（简易实现）

        使用 DuckDuckGo 的 lite HTML 版本提取搜索结果。

        Returns:
            搜索结果列表，失败返回 None
        """
        try:
            url = "https://html.duckduckgo.com/html/"
            data = {"q": query}
            headers = {
                "User-Agent": "Mozilla/5.0 (compatible; ArtooBot/1.0)",
            }
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT, follow_redirects=True) as client:
                resp = await client.post(url, data=data, headers=headers)
                resp.raise_for_status()
                html = resp.text

            # 简易 HTML 解析提取结果
            results = self._parse_ddg_html(html, max_results)
            return results if results else None

        except Exception as e:
            logger.warning("[WebSearch] DuckDuckGo search failed: %s", e)
            return None

    def _parse_ddg_html(self, html: str, max_results: int) -> list[dict]:
        """从 DuckDuckGo HTML 响应中提取搜索结果"""
        import re

        results: list[dict] = []

        # 提取结果块：class="result__a" 包含链接和标题
        # class="result__snippet" 包含摘要
        link_pattern = re.compile(
            r'class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
            re.DOTALL,
        )
        snippet_pattern = re.compile(
            r'class="result__snippet"[^>]*>(.*?)</(?:td|span|div)>',
            re.DOTALL,
        )

        links = link_pattern.findall(html)
        snippets = snippet_pattern.findall(html)

        for i, (url, title) in enumerate(links[:max_results]):
            # 清理 HTML 标签
            clean_title = re.sub(r"<[^>]+>", "", title).strip()
            snippet = ""
            if i < len(snippets):
                snippet = re.sub(r"<[^>]+>", "", snippets[i]).strip()

            # DuckDuckGo 的 URL 可能是重定向链接
            if url.startswith("//duckduckgo.com/l/"):
                # 提取实际 URL
                url_match = re.search(r"uddg=([^&]+)", url)
                if url_match:
                    from urllib.parse import unquote
                    url = unquote(url_match.group(1))

            if clean_title and url:
                results.append({
                    "title": clean_title,
                    "url": url,
                    "snippet": snippet,
                })

        return results

    def _format_xml(self, results: list[dict]) -> str:
        """将搜索结果格式化为 XML"""
        count = len(results)
        lines: list[str] = [f'<web_results count="{count}">']

        for rank, item in enumerate(results, start=1):
            lines.append(f'<result rank="{rank}">')
            lines.append(f"<title>{xml_escape(item.get('title', ''))}</title>")
            lines.append(f"<url>{xml_escape(item.get('url', ''))}</url>")
            lines.append(f"<snippet>{xml_escape(item.get('snippet', ''))}</snippet>")
            lines.append("</result>")

        lines.append("</web_results>")
        return "\n".join(lines)
