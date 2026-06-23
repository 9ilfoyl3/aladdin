"""OpenAI 兼容 ASR Provider

所有 ASR 厂商统一走 OpenAI 兼容的 /v1/audio/transcriptions 接口
（参考 WeKnora internal/models/asr/openai.go）。
"""

from __future__ import annotations

import logging
import os
import time

import httpx

from .provider import ASRProvider, ASRResult, ASRSegment

logger = logging.getLogger(__name__)

# 音频转写可能较慢，默认超时放宽
_DEFAULT_TIMEOUT = 300.0


class OpenAIASRProvider(ASRProvider):
    """通过 OpenAI 兼容的 /v1/audio/transcriptions 接口转写音频

    api_url 为 base_url（如 http://host:port/v1），自动拼接
    /audio/transcriptions 端点。
    """

    def __init__(
        self,
        api_url: str,
        model_name: str,
        api_key: str = "",
        language: str = "",
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._model_name = model_name
        self._api_key = api_key
        self._language = language
        self._timeout = timeout

    @property
    def name(self) -> str:
        return "openai"

    def _endpoint(self) -> str:
        """拼接转写端点：base_url 已含 /v1 时直接补 /audio/transcriptions"""
        base = self._api_url
        if base.endswith("/audio/transcriptions"):
            return base
        return f"{base}/audio/transcriptions"

    def _build_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def transcribe(self, file_path: str) -> ASRResult:
        """上传音频文件到 ASR 服务并返回转写结果"""
        endpoint = self._endpoint()
        headers = self._build_headers()
        filename = os.path.basename(file_path)

        logger.info(
            "[ASR][%s] 发送音频到 %s, 模型: %s, 文件: %s",
            self.name, endpoint, self._model_name, filename,
        )

        data: dict[str, str] = {
            "model": self._model_name,
            "response_format": "verbose_json",
        }
        if self._language:
            data["language"] = self._language

        start = time.time()
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            with open(file_path, "rb") as f:
                files = {"file": (filename, f)}
                response = await client.post(
                    endpoint, data=data, files=files, headers=headers
                )
            response.raise_for_status()
            payload = response.json()

        elapsed_ms = (time.time() - start) * 1000
        result = self._adapt_response(payload)
        logger.info(
            "[ASR][%s] 转写完成, 文本长度=%d, 片段数=%d, 耗时: %.0fms",
            self.name, len(result.full_text), len(result.segments), elapsed_ms,
        )
        return result

    def _adapt_response(self, data: dict) -> ASRResult:
        """将 OpenAI 兼容响应适配为统一 ASRResult

        verbose_json 格式：{"text": "...", "segments": [{"start","end","text"}]}
        json 格式：{"text": "..."}
        """
        full_text = (data.get("text") or "").strip()

        segments: list[ASRSegment] = []
        for seg in data.get("segments") or []:
            try:
                segments.append(ASRSegment(
                    start=float(seg.get("start", 0.0)),
                    end=float(seg.get("end", 0.0)),
                    text=(seg.get("text") or "").strip(),
                ))
            except (TypeError, ValueError):
                continue

        # 全文兜底：无 text 字段时用片段拼接
        if not full_text and segments:
            full_text = " ".join(s.text for s in segments if s.text)

        return ASRResult(
            full_text=full_text,
            segments=segments,
            provider_name=self.name,
            metadata={
                k: v for k, v in data.items()
                if k in ("language", "duration")
            },
        )

    def is_available(self) -> bool:
        return bool(self._api_url and self._model_name)
