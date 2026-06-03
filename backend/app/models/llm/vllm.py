"""vLLM LLM 实现

通过 httpx 异步客户端调用 vLLM 的 OpenAI 兼容 API，
支持同步生成、流式生成、Function Calling 三种模式。
"""

import json
from typing import AsyncIterator

import httpx

from app.models.llm.json_field_extractor import JSONFieldExtractor
from app.models.provider import (
    ChatResponse,
    LLMProvider,
    LLMToolCall,
    StreamChunk,
    TokenUsage,
)


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
            print(f"[vLLM] 请求 payload keys: {list(payload.keys())}, enable_thinking={payload.get('enable_thinking', 'not set')}")
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

        如果远端不支持流式（返回非 200），自动降级为非流式调用并逐段输出。

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
                if resp.status_code != 200:
                    # 流式不支持，降级为非流式
                    print(f"[vLLM] 流式请求返回 {resp.status_code}，降级为非流式")
                    await resp.aclose()
                    result = await self.generate(messages, **kwargs)
                    # 分段输出模拟流式
                    chunk_size = 4
                    for i in range(0, len(result), chunk_size):
                        yield result[i:i + chunk_size]
                    return
                # 正常流式处理：vLLM 流式返回 SSE 格式 data: {...}\n\n
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    if not line.startswith("data:"):
                        continue
                    data_str = line[len("data:"):].strip()
                    if data_str == "[DONE]":
                        break
                    chunk = json.loads(data_str)
                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    content = choices[0].get("delta", {}).get("content", "")
                    if content:
                        yield content
        except httpx.HTTPStatusError as e:
            # raise_for_status 触发的异常，同样降级
            print(f"[vLLM] 流式 HTTPStatusError {e.response.status_code}，降级为非流式")
            result = await self.generate(messages, **kwargs)
            chunk_size = 4
            for i in range(0, len(result), chunk_size):
                yield result[i:i + chunk_size]
        except httpx.RequestError as e:
            raise RuntimeError(f"vLLM 连接失败: {e}") from e

    async def chat_with_tools(
        self, messages: list[dict], tools: list[dict], **kwargs
    ) -> ChatResponse:
        """Function Calling: 发送消息和工具定义，获取包含 tool_calls 的响应

        通过 OpenAI 兼容 API 的 tools 参数发送工具定义，
        解析 response.choices[0].message.tool_calls。

        Args:
            messages: 对话消息列表
            tools: OpenAI 格式的工具定义列表
            **kwargs: 额外参数（temperature, tool_choice 等）

        Returns:
            ChatResponse 包含 content、tool_calls、finish_reason、usage
        """
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "tools": tools,
            **kwargs,
        }
        # 如果 tools 为空列表，不发送 tools 参数（某些 API 不接受空 tools）
        if not tools:
            payload.pop("tools", None)
        try:
            url = f"{self.base_url}/chat/completions"
            print(f"[vLLM] chat_with_tools: {len(tools)} tools, url={url}")
            resp = await self._client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()

            choice = data["choices"][0]
            message = choice["message"]
            finish_reason = choice.get("finish_reason", "stop")

            # 解析 content
            content = message.get("content") or ""

            # 解析 tool_calls
            tool_calls: list[LLMToolCall] = []
            raw_tool_calls = message.get("tool_calls")
            if raw_tool_calls:
                for tc in raw_tool_calls:
                    tool_calls.append(
                        LLMToolCall(
                            id=tc["id"],
                            function_name=tc["function"]["name"],
                            arguments=tc["function"]["arguments"],
                        )
                    )

            # 解析 usage
            usage = None
            raw_usage = data.get("usage")
            if raw_usage:
                usage = TokenUsage(
                    prompt_tokens=raw_usage.get("prompt_tokens", 0),
                    completion_tokens=raw_usage.get("completion_tokens", 0),
                    total_tokens=raw_usage.get("total_tokens", 0),
                )

            return ChatResponse(
                content=content,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
                usage=usage,
            )
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"vLLM chat_with_tools 请求失败: HTTP {e.response.status_code}"
            ) from e
        except httpx.RequestError as e:
            raise RuntimeError(f"vLLM 连接失败: {e}") from e
        except (KeyError, TypeError, IndexError) as e:
            raise RuntimeError(f"vLLM 响应格式异常: {e}") from e

    async def stream_with_tools(
        self, messages: list[dict], tools: list[dict], **kwargs
    ) -> AsyncIterator[StreamChunk]:
        """Streaming Function Calling: 流式返回包含工具调用的响应片段

        通过 OpenAI 兼容 API 的 stream=true + tools 参数，
        解析 SSE 中的 tool_call deltas 并累积组装完整的 tool_calls。

        Args:
            messages: 对话消息列表
            tools: OpenAI 格式的工具定义列表
            **kwargs: 额外参数

        Yields:
            StreamChunk 包含 content/tool_calls/finish_reason/response_type
        """
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "tools": tools,
            **kwargs,
        }

        # 累积 tool_calls 的状态（按 index 存储）
        tool_call_map: dict[int, dict] = {}

        try:
            url = f"{self.base_url}/chat/completions"
            async with self._client.stream("POST", url, json=payload) as resp:
                if resp.status_code != 200:
                    # 流式不支持，降级为非流式
                    await resp.aclose()
                    result = await self.chat_with_tools(messages, tools, **kwargs)
                    # 将非流式结果转为单个 StreamChunk 输出
                    yield StreamChunk(
                        content=result.content,
                        tool_calls=result.tool_calls if result.tool_calls else None,
                        finish_reason=result.finish_reason,
                        response_type="tool_call" if result.tool_calls else "content",
                    )
                    return

                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    if not line.startswith("data:"):
                        continue
                    data_str = line[len("data:"):].strip()
                    if data_str == "[DONE]":
                        # 流结束，发送最终 chunk 携带累积的 tool_calls
                        final_tool_calls = self._build_tool_calls(tool_call_map)
                        if final_tool_calls:
                            yield StreamChunk(
                                content="",
                                tool_calls=final_tool_calls,
                                finish_reason="tool_calls",
                                response_type="tool_call",
                            )
                        break

                    chunk = json.loads(data_str)
                    choices = chunk.get("choices", [])
                    if not choices:
                        continue

                    delta = choices[0].get("delta", {})
                    finish_reason = choices[0].get("finish_reason") or ""

                    # 防御性检查：某些模型在特定 chunk 中 delta 可能是字符串而非字典
                    if not isinstance(delta, dict):
                        # delta 是字符串时，视为纯 content
                        if delta:
                            yield StreamChunk(
                                content=str(delta),
                                tool_calls=None,
                                finish_reason="",
                                response_type="content",
                            )
                        if finish_reason:
                            final_tool_calls = self._build_tool_calls(tool_call_map)
                            yield StreamChunk(
                                content="",
                                tool_calls=final_tool_calls if final_tool_calls else None,
                                finish_reason=finish_reason,
                                response_type="tool_call" if final_tool_calls else "content",
                            )
                        continue

                    # 处理 tool_calls delta（累积模式）
                    delta_tool_calls = delta.get("tool_calls")
                    if delta_tool_calls:
                        for tc_delta in delta_tool_calls:
                            idx = tc_delta.get("index", 0)
                            if idx not in tool_call_map:
                                tool_call_map[idx] = {
                                    "id": "",
                                    "function_name": "",
                                    "arguments": "",
                                }
                            entry = tool_call_map[idx]

                            # 累积 id
                            if tc_delta.get("id"):
                                entry["id"] = tc_delta["id"]

                            # 累积 function name
                            func_data = tc_delta.get("function", {})
                            if func_data.get("name"):
                                # 防御性处理：部分 vLLM 实现每个 chunk 重复发送完整名称
                                if entry["function_name"] != func_data["name"]:
                                    entry["function_name"] += func_data["name"]

                            # 累积 arguments
                            if func_data.get("arguments"):
                                entry["arguments"] += func_data["arguments"]

                                # 当 tool_name 是 final_answer 时，用 JSONFieldExtractor
                                # 从增量 arguments 中安全提取 answer 字段，逐 token 作为
                                # answer 类型流式发射。状态机正确处理跨 chunk 的转义序列，
                                # 不会吐出残留的反斜杠/半个 \uXXXX（旧手写 replace 链的乱码根因）。
                                if entry["function_name"] == "final_answer":
                                    extractor = entry.get("_answer_extractor")
                                    if extractor is None:
                                        extractor = JSONFieldExtractor("answer")
                                        entry["_answer_extractor"] = extractor
                                    answer_delta = extractor.feed(func_data["arguments"])
                                    if answer_delta:
                                        yield StreamChunk(
                                            content=answer_delta,
                                            tool_calls=None,
                                            finish_reason="",
                                            response_type="answer",
                                        )
                                    continue

                        # 发送 tool_call 类型的 StreamChunk（通知有工具调用进行中）
                        # 仅对非 final_answer 的工具发送
                        has_non_final_answer = any(
                            tool_call_map.get(tc_delta.get("index", 0), {}).get("function_name", "") != "final_answer"
                            for tc_delta in delta_tool_calls
                        )
                        if has_non_final_answer:
                            yield StreamChunk(
                                content="",
                                tool_calls=None,
                                finish_reason="",
                                response_type="tool_call",
                            )

                    # 处理普通 content delta
                    content = delta.get("content") or ""
                    if content:
                        yield StreamChunk(
                            content=content,
                            tool_calls=None,
                            finish_reason="",
                            response_type="content",
                        )

                    # 处理 reasoning_content delta（DeepSeek/doubao 等模型的思考内容）
                    reasoning = delta.get("reasoning_content") or ""
                    if reasoning:
                        yield StreamChunk(
                            content=reasoning,
                            tool_calls=None,
                            finish_reason="",
                            response_type="thinking",
                        )

                    # 处理 finish_reason（流结束信号）
                    if finish_reason:
                        final_tool_calls = self._build_tool_calls(tool_call_map)
                        yield StreamChunk(
                            content="",
                            tool_calls=final_tool_calls if final_tool_calls else None,
                            finish_reason=finish_reason,
                            response_type="tool_call" if final_tool_calls else "content",
                        )

        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"vLLM stream_with_tools 请求失败: HTTP {e.response.status_code}"
            ) from e
        except httpx.RequestError as e:
            raise RuntimeError(f"vLLM 连接失败: {e}") from e

    def _build_tool_calls(self, tool_call_map: dict[int, dict]) -> list[LLMToolCall]:
        """从累积的 tool_call_map 构建有序的 LLMToolCall 列表

        Args:
            tool_call_map: 按 index 存储的 tool_call 累积数据

        Returns:
            按 index 排序的 LLMToolCall 列表，空则返回空列表
        """
        if not tool_call_map:
            return []
        result = []
        for idx in sorted(tool_call_map.keys()):
            entry = tool_call_map[idx]
            if entry["function_name"]:  # 只包含有效的 tool_call
                result.append(
                    LLMToolCall(
                        id=entry["id"],
                        function_name=entry["function_name"],
                        arguments=entry["arguments"],
                    )
                )
        return result

    async def close(self):
        """关闭 HTTP 客户端连接"""
        await self._client.aclose()
