"""上下文增强 Embedding 构造模块

为 child chunk 拼接文件名、章节路径和父块上下文，
生成更具语义信息的 embedding 输入文本，提升检索准确率。
"""

from __future__ import annotations

from app.pipeline.metadata import ChunkMetadata


class ContextualEmbedder:
    """上下文增强的 embedding 构造器"""

    PARENT_CONTEXT_CHARS = 150  # 父块上下文截取字符数

    def build_embed_text(
        self,
        child_chunk: str,
        metadata: ChunkMetadata,
        parent_chunk: str | None = None,
        context_header: str | None = None,
    ) -> str:
        """构造增强后的 embedding 输入文本

        当 context_header 有值时，格式为: {context_header}\n\n{child_chunk}
        当 context_header 为空/None 时，fallback 到现有逻辑:
            [文件名 | 章节路径]\n{parent[:150]}\n{child_chunk}

        Args:
            child_chunk: 子块原始文本
            metadata: 子块的结构化元数据
            parent_chunk: 父块文本（可选）
            context_header: chunker 生成的面包屑标题上下文（可选），优先使用

        Returns:
            拼接了上下文前缀的 embedding 输入文本
        """
        # 优先使用 chunker 生成的 context_header
        if context_header:
            return f"{context_header}\n\n{child_chunk}"

        # Fallback: 使用现有 metadata 拼接逻辑
        # 构造标题路径前缀
        parts = [metadata.filename]
        if metadata.section_path:
            parts.extend(metadata.section_path)
        prefix = " | ".join(parts)

        # 构造最终 embedding 文本
        segments = [f"[{prefix}]"]

        if parent_chunk:
            parent_context = parent_chunk[: self.PARENT_CONTEXT_CHARS].strip()
            if parent_context and parent_context != child_chunk[: self.PARENT_CONTEXT_CHARS]:
                segments.append(parent_context)

        segments.append(child_chunk)

        return "\n".join(segments)
