"""read_attachment 工具 - 整篇读取【本条消息绑定的附件】内容

与 knowledge_search 的根本区别：附件的 file_id 在请求入口处锚定（来自
request.attachments），不经语义检索、不与知识库文档竞争排序，而是按 file_id
直接整篇读取 session_chunks 的父块（parent_id IS NULL）。

借鉴 WeKnora 的 SearchTargetTypeKnowledge + tryDirectChunkLoading：当用户明确
针对某个文件提问（"解析附件 / 这个文档 / 这张图说了什么"）时，确定性地把整篇内容
取出，而非丢进全池语义池让正式知识库文档把它挤下去。

安全：LLM 只能选择读哪个附件（按 filename）或翻页，无法指定/伪造 file_id；可读
范围严格限定在入口锚定的本条消息附件集合内，并以 session_id 二次隔离（纵深防御）。
"""

import logging
import math

from sqlalchemy import func, select
from xml.sax.saxutils import escape as xml_escape

from app.agent.tools.base import BaseTool, ToolResult
from app.schema.db import SessionChunk
from app.storage.database import async_session

logger = logging.getLogger(__name__)

# 默认每页父块数：小附件通常一页读完；超大附件分页，单页字符再由 registry 截断兜底。
_DEFAULT_PAGE_SIZE = 50


class ReadAttachmentTool(BaseTool):
    """整篇读取本条消息绑定的附件内容（确定性命中，不经语义检索）。

    attachments：入口锚定的本条消息附件快照列表，每项含 file_id / filename。
    session_id：用于在 session_chunks 上二次隔离（纵深防御，防止 file_id 跨会话误用）。
    """

    def __init__(self, session_id: str, attachments: list[dict]) -> None:
        self._session_id = session_id
        # 仅保留带 file_id 的有效附件，保持入口传入顺序（"第一个附件"语义稳定）。
        self._attachments: list[dict] = [
            {"file_id": a.get("file_id", ""), "filename": a.get("filename", "")}
            for a in (attachments or [])
            if a.get("file_id")
        ]

    @property
    def name(self) -> str:
        return "read_attachment"

    @property
    def description(self) -> str:
        names = "、".join(a["filename"] for a in self._attachments) or "（无）"
        return (
            "Read the FULL content of a file the user attached to the CURRENT message. "
            "This is a deterministic, direct read of the attached file — NOT a semantic "
            "search — so it always returns exactly that file's content without competing "
            "with knowledge base documents. Use this FIRST whenever the user refers to "
            "an uploaded attachment / file / image / screenshot in this message "
            "(e.g. asks to parse, summarize, or explain it). "
            f"Attached files in this message: {names}."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": (
                        "Optional. When multiple files are attached, the filename to read "
                        "(case-insensitive). If omitted, the first attachment is read."
                    ),
                },
                "page": {
                    "type": "integer",
                    "description": "Page number, starting from 1. Use for large attachments.",
                    "default": 1,
                },
                "page_size": {
                    "type": "integer",
                    "description": "Number of content blocks per page.",
                    "default": _DEFAULT_PAGE_SIZE,
                },
            },
            "required": [],
        }

    def _resolve_target(self, filename: str | None) -> tuple[dict | None, str | None]:
        """根据可选 filename 解析要读取的附件。

        Returns:
            (附件, 错误信息)。命中返回 (附件, None)；未命中返回 (None, 错误信息)。
        """
        if not self._attachments:
            return None, "No attachment is bound to the current message."

        if not filename:
            # 未指定 → 默认第一个附件。
            return self._attachments[0], None

        target = filename.strip().lower()
        for a in self._attachments:
            if a["filename"].strip().lower() == target:
                return a, None

        available = "、".join(a["filename"] for a in self._attachments)
        return None, (
            f"No attachment named '{filename}' in this message. "
            f"Available attachments: {available}."
        )

    async def execute(self, args: dict) -> ToolResult:
        filename: str | None = args.get("filename")
        page: int = args.get("page", 1)
        page_size: int = args.get("page_size", _DEFAULT_PAGE_SIZE)

        if page < 1:
            page = 1
        if page_size < 1:
            page_size = _DEFAULT_PAGE_SIZE

        target, err = self._resolve_target(filename)
        if err is not None:
            return ToolResult(success=False, error=err)

        file_id = target["file_id"]
        fname = target["filename"]

        logger.info(
            "[ReadAttachment] file_id=%s, filename=%s, page=%d, page_size=%d",
            file_id,
            fname,
            page,
            page_size,
        )

        try:
            async with async_session() as session:
                # 父块总数（parent_id IS NULL），以 session_id 二次隔离（纵深防御）。
                count_stmt = (
                    select(func.count())
                    .select_from(SessionChunk)
                    .where(
                        SessionChunk.file_id == file_id,
                        SessionChunk.session_id == self._session_id,
                        SessionChunk.parent_id.is_(None),
                    )
                )
                total_chunks: int = (await session.execute(count_stmt)).scalar() or 0

                if total_chunks == 0:
                    return ToolResult(
                        success=True,
                        output=(
                            f'<attachment filename="{xml_escape(fname)}" '
                            f'total_pages="0" total_chunks="0">\n'
                            f"This attachment has no extractable content yet.\n"
                            f"</attachment>"
                        ),
                    )

                total_pages = math.ceil(total_chunks / page_size)
                if page > total_pages:
                    page = total_pages
                offset = (page - 1) * page_size

                query_stmt = (
                    select(SessionChunk)
                    .where(
                        SessionChunk.file_id == file_id,
                        SessionChunk.session_id == self._session_id,
                        SessionChunk.parent_id.is_(None),
                    )
                    .order_by(SessionChunk.chunk_index)
                    .offset(offset)
                    .limit(page_size)
                )
                chunks = (await session.execute(query_stmt)).scalars().all()

            output = self._format_xml_output(
                filename=fname,
                chunks=chunks,
                page=page,
                total_pages=total_pages,
                total_chunks=total_chunks,
            )
            return ToolResult(
                success=True,
                output=output,
                data={
                    "file_id": file_id,
                    "filename": fname,
                    "page": page,
                    "total_pages": total_pages,
                    "total_chunks": total_chunks,
                },
            )

        except Exception as e:
            logger.error("[ReadAttachment] Database query failed: %s", e)
            return ToolResult(success=False, error=f"Failed to read attachment: {e}")

    def _format_xml_output(
        self,
        filename: str,
        chunks: list,
        page: int,
        total_pages: int,
        total_chunks: int,
    ) -> str:
        """生成 XML 格式化输出。

        格式：
        <attachment filename="..." page="1" total_pages="2" total_chunks="80">
          <block position="0">...</block>
          ...
        </attachment>
        翻页提示仅在还有后续页时追加，引导 LLM 读完整篇。
        """
        lines: list[str] = []
        lines.append(
            f'<attachment filename="{xml_escape(filename)}" '
            f'page="{page}" total_pages="{total_pages}" total_chunks="{total_chunks}">'
        )
        for chunk in chunks:
            position = chunk.chunk_index if chunk.chunk_index is not None else 0
            lines.append(f'<block position="{position}">')
            lines.append(xml_escape(chunk.content))
            lines.append("</block>")
        lines.append("</attachment>")

        if page < total_pages:
            lines.append(
                f"[This attachment has more content. Call read_attachment again with "
                f'page={page + 1} (filename="{filename}") to continue reading.]'
            )
        return "\n".join(lines)
