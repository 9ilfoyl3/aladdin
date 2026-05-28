"""VllmLLM 单元测试

使用 httpx mock 验证 generate 和 stream 方法的正确性。
"""

import json

import httpx
import pytest
import pytest_asyncio

from app.models.llm.vllm import VllmLLM


@pytest_asyncio.fixture
async def vllm():
    """创建 VllmLLM 实例"""
    llm = VllmLLM(base_url="http://localhost:8000", model="Qwen/Qwen2.5-7B-Instruct")
    yield llm
    await llm.close()


class TestVllmLLMInit:
    """测试初始化逻辑"""

    def test_base_url_trailing_slash_stripped(self):
        """base_url 末尾斜杠应被去除"""
        llm = VllmLLM(base_url="http://localhost:8000/", model="test")
        assert llm.base_url == "http://localhost:8000"

    def test_model_stored(self):
        """model 名称应正确存储"""
        llm = VllmLLM(base_url="http://localhost:8000", model="Qwen/Qwen2.5-7B-Instruct")
        assert llm.model == "Qwen/Qwen2.5-7B-Instruct"


class TestVllmGenerate:
    """测试 generate 方法"""

    @pytest.mark.asyncio
    async def test_generate_success(self, vllm: VllmLLM):
        """正常生成应返回 content 字符串"""
        mock_response = {
            "choices": [{"message": {"role": "assistant", "content": "你好，有什么可以帮助你的？"}}]
        }

        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json=mock_response)
        )
        vllm._client = httpx.AsyncClient(
            base_url=vllm.base_url, transport=transport
        )

        result = await vllm.generate([{"role": "user", "content": "你好"}])
        assert result == "你好，有什么可以帮助你的？"

    @pytest.mark.asyncio
    async def test_generate_http_error(self, vllm: VllmLLM):
        """HTTP 错误应抛出 RuntimeError"""
        transport = httpx.MockTransport(
            lambda request: httpx.Response(500, text="Internal Server Error")
        )
        vllm._client = httpx.AsyncClient(
            base_url=vllm.base_url, transport=transport
        )

        with pytest.raises(RuntimeError, match="vLLM 请求失败"):
            await vllm.generate([{"role": "user", "content": "test"}])

    @pytest.mark.asyncio
    async def test_generate_invalid_response(self, vllm: VllmLLM):
        """响应格式异常应抛出 RuntimeError"""
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json={"invalid": "data"})
        )
        vllm._client = httpx.AsyncClient(
            base_url=vllm.base_url, transport=transport
        )

        with pytest.raises(RuntimeError, match="vLLM 响应格式异常"):
            await vllm.generate([{"role": "user", "content": "test"}])

    @pytest.mark.asyncio
    async def test_generate_passes_kwargs(self, vllm: VllmLLM):
        """额外参数应传递到请求体"""
        captured_body = {}

        def handler(request: httpx.Request):
            captured_body.update(json.loads(request.content))
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "ok"}}]}
            )

        transport = httpx.MockTransport(handler)
        vllm._client = httpx.AsyncClient(
            base_url=vllm.base_url, transport=transport
        )

        await vllm.generate(
            [{"role": "user", "content": "test"}], temperature=0.7
        )
        assert captured_body["temperature"] == 0.7
        assert captured_body["stream"] is False

    @pytest.mark.asyncio
    async def test_generate_request_url(self, vllm: VllmLLM):
        """请求应发送到 /v1/chat/completions"""
        captured_url = {}

        def handler(request: httpx.Request):
            captured_url["path"] = request.url.path
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "ok"}}]}
            )

        transport = httpx.MockTransport(handler)
        vllm._client = httpx.AsyncClient(
            base_url=vllm.base_url, transport=transport
        )

        await vllm.generate([{"role": "user", "content": "test"}])
        assert captured_url["path"] == "/v1/chat/completions"


class TestVllmStream:
    """测试 stream 方法"""

    @pytest.mark.asyncio
    async def test_stream_success(self, vllm: VllmLLM):
        """流式生成应逐块返回内容"""
        # SSE 格式：data: {...}\n\n
        chunks = [
            'data: {"choices":[{"delta":{"content":"你"}}]}\n\n',
            'data: {"choices":[{"delta":{"content":"好"}}]}\n\n',
            'data: {"choices":[{"delta":{"content":"！"}}]}\n\n',
            "data: [DONE]\n\n",
        ]
        stream_content = "".join(chunks).encode()

        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, stream=httpx.ByteStream(stream_content))
        )
        vllm._client = httpx.AsyncClient(
            base_url=vllm.base_url, transport=transport
        )

        result = []
        async for token in vllm.stream([{"role": "user", "content": "你好"}]):
            result.append(token)

        assert result == ["你", "好", "！"]

    @pytest.mark.asyncio
    async def test_stream_skips_empty_content(self, vllm: VllmLLM):
        """空 content 的 chunk 应被跳过"""
        chunks = [
            'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n',
            'data: {"choices":[{"delta":{"content":""}}]}\n\n',
            'data: {"choices":[{"delta":{"content":"world"}}]}\n\n',
            "data: [DONE]\n\n",
        ]
        stream_content = "".join(chunks).encode()

        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, stream=httpx.ByteStream(stream_content))
        )
        vllm._client = httpx.AsyncClient(
            base_url=vllm.base_url, transport=transport
        )

        result = []
        async for token in vllm.stream([{"role": "user", "content": "hi"}]):
            result.append(token)

        assert result == ["hello", "world"]

    @pytest.mark.asyncio
    async def test_stream_skips_empty_lines(self, vllm: VllmLLM):
        """空行应被跳过"""
        chunks = [
            'data: {"choices":[{"delta":{"content":"a"}}]}\n',
            "\n",
            'data: {"choices":[{"delta":{"content":"b"}}]}\n',
            "\n",
            "data: [DONE]\n",
        ]
        stream_content = "".join(chunks).encode()

        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, stream=httpx.ByteStream(stream_content))
        )
        vllm._client = httpx.AsyncClient(
            base_url=vllm.base_url, transport=transport
        )

        result = []
        async for token in vllm.stream([{"role": "user", "content": "test"}]):
            result.append(token)

        assert result == ["a", "b"]

    @pytest.mark.asyncio
    async def test_stream_handles_done_marker(self, vllm: VllmLLM):
        """[DONE] 标记后应停止迭代"""
        chunks = [
            'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n',
            "data: [DONE]\n\n",
            'data: {"choices":[{"delta":{"content":"不应出现"}}]}\n\n',
        ]
        stream_content = "".join(chunks).encode()

        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, stream=httpx.ByteStream(stream_content))
        )
        vllm._client = httpx.AsyncClient(
            base_url=vllm.base_url, transport=transport
        )

        result = []
        async for token in vllm.stream([{"role": "user", "content": "test"}]):
            result.append(token)

        assert result == ["ok"]


class TestVllmChatWithTools:
    """测试 chat_with_tools 方法"""

    @pytest.mark.asyncio
    async def test_chat_with_tools_returns_tool_calls(self, vllm: VllmLLM):
        """当模型返回 tool_calls 时，应正确解析为 LLMToolCall 列表"""
        mock_response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_abc123",
                                "type": "function",
                                "function": {
                                    "name": "knowledge_search",
                                    "arguments": '{"queries": ["合同违约"]}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
            },
        }

        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json=mock_response)
        )
        vllm._client = httpx.AsyncClient(
            base_url=vllm.base_url, transport=transport
        )

        tools = [
            {
                "type": "function",
                "function": {
                    "name": "knowledge_search",
                    "description": "搜索知识库",
                    "parameters": {
                        "type": "object",
                        "properties": {"queries": {"type": "array"}},
                    },
                },
            }
        ]

        result = await vllm.chat_with_tools(
            [{"role": "user", "content": "搜索合同违约"}], tools
        )

        assert result.content == ""
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].id == "call_abc123"
        assert result.tool_calls[0].function_name == "knowledge_search"
        assert result.tool_calls[0].arguments == '{"queries": ["合同违约"]}'
        assert result.finish_reason == "tool_calls"
        assert result.usage is not None
        assert result.usage.prompt_tokens == 100
        assert result.usage.completion_tokens == 20
        assert result.usage.total_tokens == 120

    @pytest.mark.asyncio
    async def test_chat_with_tools_text_response(self, vllm: VllmLLM):
        """当模型返回纯文本（无 tool_calls）时，应正确解析"""
        mock_response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "这是一个普通回复",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 50,
                "completion_tokens": 10,
                "total_tokens": 60,
            },
        }

        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json=mock_response)
        )
        vllm._client = httpx.AsyncClient(
            base_url=vllm.base_url, transport=transport
        )

        result = await vllm.chat_with_tools(
            [{"role": "user", "content": "你好"}], []
        )

        assert result.content == "这是一个普通回复"
        assert result.tool_calls == []
        assert result.finish_reason == "stop"

    @pytest.mark.asyncio
    async def test_chat_with_tools_multiple_tool_calls(self, vllm: VllmLLM):
        """多个 tool_calls 应全部正确解析"""
        mock_response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "knowledge_search",
                                    "arguments": '{"queries": ["query1"]}',
                                },
                            },
                            {
                                "id": "call_2",
                                "type": "function",
                                "function": {
                                    "name": "grep_chunks",
                                    "arguments": '{"query": "keyword"}',
                                },
                            },
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }

        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json=mock_response)
        )
        vllm._client = httpx.AsyncClient(
            base_url=vllm.base_url, transport=transport
        )

        result = await vllm.chat_with_tools(
            [{"role": "user", "content": "test"}], []
        )

        assert len(result.tool_calls) == 2
        assert result.tool_calls[0].function_name == "knowledge_search"
        assert result.tool_calls[1].function_name == "grep_chunks"
        assert result.usage is None  # 无 usage 字段

    @pytest.mark.asyncio
    async def test_chat_with_tools_http_error(self, vllm: VllmLLM):
        """HTTP 错误应抛出 RuntimeError"""
        transport = httpx.MockTransport(
            lambda request: httpx.Response(429, text="Rate limited")
        )
        vllm._client = httpx.AsyncClient(
            base_url=vllm.base_url, transport=transport
        )

        with pytest.raises(RuntimeError, match="chat_with_tools 请求失败"):
            await vllm.chat_with_tools(
                [{"role": "user", "content": "test"}], []
            )

    @pytest.mark.asyncio
    async def test_chat_with_tools_passes_kwargs(self, vllm: VllmLLM):
        """额外参数（temperature 等）应传递到请求体"""
        captured_body = {}

        def handler(request: httpx.Request):
            captured_body.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": "ok"}, "finish_reason": "stop"}
                    ]
                },
            )

        transport = httpx.MockTransport(handler)
        vllm._client = httpx.AsyncClient(
            base_url=vllm.base_url, transport=transport
        )

        tools = [{"type": "function", "function": {"name": "test", "parameters": {}}}]
        await vllm.chat_with_tools(
            [{"role": "user", "content": "test"}],
            tools,
            temperature=0.3,
            tool_choice="auto",
        )

        assert captured_body["temperature"] == 0.3
        assert captured_body["tool_choice"] == "auto"
        assert captured_body["tools"] == tools
        assert captured_body["stream"] is False


class TestVllmStreamWithTools:
    """测试 stream_with_tools 方法"""

    @pytest.mark.asyncio
    async def test_stream_with_tools_content_only(self, vllm: VllmLLM):
        """纯文本流式响应应正确返回 content chunks"""
        chunks = [
            'data: {"choices":[{"delta":{"content":"你好"},"finish_reason":null}]}\n\n',
            'data: {"choices":[{"delta":{"content":"世界"},"finish_reason":null}]}\n\n',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
            "data: [DONE]\n\n",
        ]
        stream_content = "".join(chunks).encode()

        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, stream=httpx.ByteStream(stream_content)
            )
        )
        vllm._client = httpx.AsyncClient(
            base_url=vllm.base_url, transport=transport
        )

        result = []
        async for chunk in vllm.stream_with_tools(
            [{"role": "user", "content": "hi"}], []
        ):
            result.append(chunk)

        # 应有 content chunks + finish chunk
        content_chunks = [c for c in result if c.content]
        assert len(content_chunks) == 2
        assert content_chunks[0].content == "你好"
        assert content_chunks[1].content == "世界"
        assert content_chunks[0].response_type == "content"

    @pytest.mark.asyncio
    async def test_stream_with_tools_tool_calls_accumulated(self, vllm: VllmLLM):
        """流式 tool_calls delta 应被正确累积"""
        chunks = [
            # 第一个 delta：tool_call 开始，带 id 和 function name
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_xyz","function":{"name":"knowledge_search","arguments":""}}]},"finish_reason":null}]}\n\n',
            # 第二个 delta：arguments 片段
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"queries\\""}}]},"finish_reason":null}]}\n\n',
            # 第三个 delta：arguments 继续
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":": [\\"test\\"]}"}}]},"finish_reason":null}]}\n\n',
            # finish
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n',
            "data: [DONE]\n\n",
        ]
        stream_content = "".join(chunks).encode()

        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, stream=httpx.ByteStream(stream_content)
            )
        )
        vllm._client = httpx.AsyncClient(
            base_url=vllm.base_url, transport=transport
        )

        result = []
        async for chunk in vllm.stream_with_tools(
            [{"role": "user", "content": "搜索"}], []
        ):
            result.append(chunk)

        # 最后应有一个带完整 tool_calls 的 chunk
        final_chunks = [c for c in result if c.tool_calls]
        assert len(final_chunks) >= 1
        last_with_tools = final_chunks[-1]
        assert last_with_tools.tool_calls[0].id == "call_xyz"
        assert last_with_tools.tool_calls[0].function_name == "knowledge_search"
        assert last_with_tools.tool_calls[0].arguments == '{"queries": ["test"]}'

    @pytest.mark.asyncio
    async def test_stream_with_tools_fallback_on_non_200(self, vllm: VllmLLM):
        """非 200 响应应降级为非流式调用"""
        # 第一次调用（stream）返回 400，第二次调用（非流式 fallback）返回正常
        call_count = {"n": 0}

        def handler(request: httpx.Request):
            call_count["n"] += 1
            body = json.loads(request.content)
            if body.get("stream"):
                # 流式请求返回错误
                return httpx.Response(400, text="Bad Request")
            else:
                # 非流式 fallback
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": "fallback response",
                                },
                                "finish_reason": "stop",
                            }
                        ]
                    },
                )

        transport = httpx.MockTransport(handler)
        vllm._client = httpx.AsyncClient(
            base_url=vllm.base_url, transport=transport
        )

        result = []
        async for chunk in vllm.stream_with_tools(
            [{"role": "user", "content": "test"}], []
        ):
            result.append(chunk)

        assert len(result) == 1
        assert result[0].content == "fallback response"
        assert result[0].finish_reason == "stop"


class TestBuildToolCalls:
    """测试 _build_tool_calls 辅助方法"""

    def test_empty_map_returns_empty_list(self):
        """空 map 应返回空列表"""
        llm = VllmLLM(base_url="http://localhost:8000", model="test")
        assert llm._build_tool_calls({}) == []

    def test_single_tool_call(self):
        """单个 tool_call 应正确构建"""
        llm = VllmLLM(base_url="http://localhost:8000", model="test")
        tool_map = {
            0: {
                "id": "call_1",
                "function_name": "search",
                "arguments": '{"q": "test"}',
            }
        }
        result = llm._build_tool_calls(tool_map)
        assert len(result) == 1
        assert result[0].id == "call_1"
        assert result[0].function_name == "search"
        assert result[0].arguments == '{"q": "test"}'

    def test_multiple_tool_calls_ordered_by_index(self):
        """多个 tool_calls 应按 index 排序"""
        llm = VllmLLM(base_url="http://localhost:8000", model="test")
        tool_map = {
            2: {"id": "call_3", "function_name": "tool_c", "arguments": "{}"},
            0: {"id": "call_1", "function_name": "tool_a", "arguments": "{}"},
            1: {"id": "call_2", "function_name": "tool_b", "arguments": "{}"},
        }
        result = llm._build_tool_calls(tool_map)
        assert len(result) == 3
        assert result[0].function_name == "tool_a"
        assert result[1].function_name == "tool_b"
        assert result[2].function_name == "tool_c"

    def test_skips_entries_without_function_name(self):
        """没有 function_name 的条目应被跳过"""
        llm = VllmLLM(base_url="http://localhost:8000", model="test")
        tool_map = {
            0: {"id": "call_1", "function_name": "valid", "arguments": "{}"},
            1: {"id": "call_2", "function_name": "", "arguments": "{}"},
        }
        result = llm._build_tool_calls(tool_map)
        assert len(result) == 1
        assert result[0].function_name == "valid"
