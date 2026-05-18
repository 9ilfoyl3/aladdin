"""Ollama LLM 实现

通过 httpx 异步客户端调用 Ollama 的 /api/chat 接口，
支持同步生成和流式生成两种模式。
"""

import json
from typing import AsyncIterator

import httpx

from app.models.provider import LLMProvider


class OllamaLLM(LLMProvider):
    """Ollama LLM Provider，基于 httpx 异步调用"""

    def __init__(self, base_url: str, model: str):
        """初始化 Ollama 客户端

        Args:
            base_url: Ollama 服务地址，如 http://localhost:11434
            model: 模型名称，如 qwen2.5:7b
        """
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=120.0)

    async def generate(self, messages: list[dict], **kwargs) -> str:
        """同步生成完整回复

        Args:
            messages: 对话消息列表，格式 [{"role": "user", "content": "..."}]

        Returns:
            模型生成的完整文本
        """
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            **kwargs,
        }
        try:
            resp = await self._client.post("/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data["message"]["content"]
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"Ollama 请求失败: HTTP {e.response.status_code}") from e
        except httpx.RequestError as e:
            raise RuntimeError(f"Ollama 连接失败: {e}") from e
        except (KeyError, TypeError) as e:
            raise RuntimeError(f"Ollama 响应格式异常: {e}") from e

    async def stream(self, messages: list[dict], **kwargs) -> AsyncIterator[str]:
        """流式生成回复，逐块返回内容

        Args:
            messages: 对话消息列表

        Yields:
            模型生成的文本片段
        """
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            **kwargs,
        }
        try:
            async with self._client.stream("POST", "/api/chat", json=payload) as resp:
                resp.raise_for_status()
                # Ollama 流式返回 NDJSON，每行一个 JSON 对象
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    content = chunk.get("message", {}).get("content", "")
                    if content:
                        yield content
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"Ollama 流式请求失败: HTTP {e.response.status_code}") from e
        except httpx.RequestError as e:
            raise RuntimeError(f"Ollama 连接失败: {e}") from e

    async def close(self):
        """关闭 HTTP 客户端连接"""
        await self._client.aclose()
