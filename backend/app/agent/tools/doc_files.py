"""检索结果 doc_id → 文件信息解析

检索工具（knowledge_search / grep_chunks）的结果只带 doc_id，不带文件名。
此处统一按 doc_id 反查文件名与来源，供工具在 tool_result 事件里直接带出"读到了哪些文件"，
让前端在工具调用步骤行内联展示可点击预览的文件（粒度到文件，不到 chunk）。

doc_id 可能来自两类表：
- documents（正式知识库文档）→ source="document"，预览按文档原件接口取。
- session_files（本会话上传的临时文件）→ source="session-file"，预览按会话+文件接口取。
"""

from sqlalchemy import select

from app.storage.database import async_session


async def resolve_doc_files(doc_ids: list[str]) -> dict[str, dict]:
    """按 doc_id 批量解析文件信息。

    Returns:
        dict: ``{doc_id: {"id": doc_id, "filename": str, "source": "document"|"session-file"}}``。
        未在任一表命中的 doc_id 不会出现在结果中。
    """
    uniq = list({d for d in doc_ids if d})
    if not uniq:
        return {}

    files: dict[str, dict] = {}
    async with async_session() as session:
        from app.schema.db import Document, SessionFile

        doc_rows = await session.execute(
            select(Document.id, Document.filename).where(Document.id.in_(uniq))
        )
        for row in doc_rows:
            files[row.id] = {"id": row.id, "filename": row.filename, "source": "document"}

        # 未命中正式文档表的，再查会话临时文件表（其 doc_id 即 SessionFile.id）。
        missing = [d for d in uniq if d not in files]
        if missing:
            sf_rows = await session.execute(
                select(SessionFile.id, SessionFile.filename).where(SessionFile.id.in_(missing))
            )
            for row in sf_rows:
                files[row.id] = {"id": row.id, "filename": row.filename, "source": "session-file"}

    return files


async def build_tool_files(doc_ids: list[str]) -> list[dict]:
    """解析并按输入顺序去重返回文件列表（供 ToolResult.data["files"]）。

    保持首次出现顺序（与检索排序一致），同一文件只出现一次。
    """
    resolved = await resolve_doc_files(doc_ids)
    seen: set[str] = set()
    ordered: list[dict] = []
    for d in doc_ids:
        if not d or d in seen:
            continue
        info = resolved.get(d)
        if info:
            seen.add(d)
            ordered.append(info)
    return ordered
