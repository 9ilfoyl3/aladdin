"""vLLM LLM 实现

通过 httpx 异步客户端调用 vLLM 的 OpenAI 兼容 API，
支持同步生成和流式生成两种模式。
"""

import json
from typing import AsyncIterator

import httpx

from app.models.provider import LLMProvider


class VllmLLM(LLMProvider):
    """vLLM LLM Provider，基于 OpenAI 兼容接口"""

    def __init__(self, base_url: str, model: str, api_key: str = ""):
        """初始化 vLLM 客户端

        Args:
            base_url: API 基础地址，代码会自动拼接 /chat/completions
                      如 http://localhost:8000/v1
                      或 https://ark.cn-beijing.volces.com/api/coding/v3
            model: 模型名称
            api_key: API 密钥（远端服务需要）
        """
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        print(f"[vLLM] 初始化 - base_url: {self.base_url}, model: {self.model}, api_key 长度: {len(api_key) if api_key else 0}")
        self._client = httpx.AsyncClient(timeout=120.0, headers=headers)

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
            url = f"{self.base_url}/chat/completions"
            print(f"[vLLM] 请求 URL: {url}")
            print(f"[vLLM] Client Headers: {dict(self._client.headers)}")
            resp = await self._client.post(url, json=payload)
            print(f"[vLLM] 实际请求 Headers: {dict(resp.request.headers)}")
            print(f"[vLLM] 响应状态码: {resp.status_code}")
            print(f"[vLLM] 响应 Headers: {resp.headers}")
            print(f"[vLLM] 响应内容: {resp.text[:1000]}")
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"vLLM 请求失败: HTTP {e.response.status_code}") from e
        except httpx.RequestError as e:
            raise RuntimeError(f"vLLM 连接失败: {e}") from e
        except (KeyError, TypeError, IndexError) as e:
            raise RuntimeError(f"vLLM 响应格式异常: {e}") from e

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
            url = f"{self.base_url}/chat/completions"
            print(f"[vLLM] 流式请求 URL: {url}")
            async with self._client.stream("POST", url, json=payload) as resp:
                resp.raise_for_status()
                # vLLM 流式返回 SSE 格式：data: {...}\n\n
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    # 跳过非 data 行
                    if not line.startswith("data:"):
                        continue
                    data_str = line[len("data:"):].strip()
                    # 结束标记
                    if data_str == "[DONE]":
                        break
                    chunk = json.loads(data_str)
                    content = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                    if content:
                        yield content
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"vLLM 流式请求失败: HTTP {e.response.status_code}") from e
        except httpx.RequestError as e:
            raise RuntimeError(f"vLLM 连接失败: {e}") from e

    async def close(self):
        """关闭 HTTP 客户端连接"""
        await self._client.aclose()
