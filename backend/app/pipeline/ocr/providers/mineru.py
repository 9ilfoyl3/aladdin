"""MinerU（自部署 mineru-api）Provider

请求：``POST {api_url}``，multipart 文件字段名为 ``files``（复数），
附带表单 ``backend`` / ``lang_list`` / ``return_md_content``。

契约（固定，不做探测）::

    {"results": {"<文件名 stem>": {"md_content": "markdown..."}}}

取 ``results`` 下与上传文件同名的条目，取不到则取首个条目。
缺 ``results`` 或拿不到 ``md_content`` 一律抛 :class:`OCRResponseFormatError`。

说明：这里适配的是**自部署 mineru-api 的同步接口**；
mineru.net 云端 v4 的"提交任务 + 轮询 + 下载 zip"是另一套交互模型，
将来若需要，作为独立 provider_type 注册即可。
"""

from __future__ import annotations

import os

from ..http_provider import HTTPOCRProvider
from ..provider import (
    INPUT_IMAGE,
    INPUT_PDF,
    OCRCapability,
    OCRResult,
    PageOCRResult,
)
from ..registry import register_provider

# CPU 部署必须用 pipeline 后端（默认 hybrid-auto-engine 需要 GPU）
_DEFAULT_BACKEND = "pipeline"
_DEFAULT_LANG = "ch"


@register_provider("mineru")
class MinerUProvider(HTTPOCRProvider):
    """MinerU 文档解析服务（接受 PDF 与图片，输出 Markdown）"""

    FILE_FIELD = "files"

    capability = OCRCapability(
        accepts=frozenset({INPUT_IMAGE, INPUT_PDF}),
        outputs_markdown=True,
        paginated=False,
        recommended_timeout=600.0,
    )

    @property
    def name(self) -> str:
        return "mineru"

    def build_form_data(self, file_path: str) -> dict[str, str]:
        """backend / lang_list 可由 extra_config 覆盖，其余固定"""
        return {
            "backend": str(self._extra_config.get("backend") or _DEFAULT_BACKEND),
            "lang_list": str(self._extra_config.get("lang_list") or _DEFAULT_LANG),
            "return_md_content": "true",
        }

    def _adapt_response(self, data: object, file_path: str) -> OCRResult:
        if not isinstance(data, dict):
            raise self._format_error(
                f"期望顶层为 JSON 对象（含 results 字段），实际为 {type(data).__name__}",
                data,
            )

        results = data.get("results")
        if not isinstance(results, dict):
            raise self._format_error(
                "响应缺少 results 对象（确认端点为 mineru-api 的 /file_parse）", data
            )
        if not results:
            raise self._format_error("results 为空，未返回任何解析结果", data)

        stem = os.path.splitext(os.path.basename(file_path))[0]
        entry = results.get(stem)
        if entry is None:
            entry = next(iter(results.values()))

        if not isinstance(entry, dict):
            raise self._format_error(
                f"期望 results 条目为对象，实际为 {type(entry).__name__}", data
            )

        md_content = entry.get("md_content")
        if md_content is None:
            raise self._format_error(
                "results 条目缺少 md_content（请求需带 return_md_content=true）", data
            )
        if not isinstance(md_content, str):
            raise self._format_error(
                f"期望 md_content 为字符串，实际为 {type(md_content).__name__}", data
            )

        return OCRResult(
            full_text=md_content,
            pages=(
                [PageOCRResult(page_num=1, blocks=[], full_text=md_content)]
                if md_content
                else []
            ),
            avg_confidence=1.0 if md_content else 0.0,
            provider_name=self.name,
        )
