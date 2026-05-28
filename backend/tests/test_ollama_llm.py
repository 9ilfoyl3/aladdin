"""OllamaLLM 单元测试

使用 httpx mock 验证 generate 和 stream 方法的正确性。
"""

import json

import httpx
import pytest
import pytest_asyncio

from app.models.llm.ollama import OllamaLLM


@pytest_asyncio.fixture
async def ollama():
    """创建 OllamaLLM 实例"""
    llm = OllamaLLM(base_url="http://localhost:11434", model="qwen2.5:7b")
    yield llm
    await llm.close()


class TestOllamaLLMInit:
    """测试初始化逻辑"""

    def test_base_url_trailing_slash_stripped(self):
        """base_url 末尾斜杠应被去除"""
        llm = OllamaLLM(base_url="http://localhost:11434/", model="test")
        assert llm.base_url == "http://localhost:11434"

    def test_model_stored(self):
        """model 名称应正确存储"""
        llm = OllamaLLM(base_url="http://localhost:11434", model="qwen2.5:7b")
        assert llm.model == "qwen2.5:7b"


class TestOllamaGenerate:
    """测试 generate 方法"""

    @pytest.mark.asyncio
    async def test_generate_success(self, ollama: OllamaLLM):
        """正常生成应返回 content 字符串"""
        # 使用 httpx mock transport
        mock_response = {
            "message": {"role": "assistant", "content": "你好，有什么可以帮助你的？"}
        }

        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json=mock_response)
        )
        ollama._client = httpx.AsyncClient(
            base_url=ollama.base_url, transport=transport
        )

        result = await ollama.generate([{"role": "user", "content": "你好"}])
        assert result == "你好，有什么可以帮助你的？"

    @pytest.mark.asyncio
    async def test_generate_http_error(self, ollama: OllamaLLM):
        """HTTP 错误应抛出 RuntimeError"""
        transport = httpx.MockTransport(
            lambda request: httpx.Response(500, text="Internal Server Error")
        )
        ollama._client = httpx.AsyncClient(
            base_url=ollama.base_url, transport=transport
        )

        with pytest.raises(RuntimeError, match="Ollama 请求失败"):
            await ollama.generate([{"role": "user", "content": "test"}])

    @pytest.mark.asyncio
    async def test_generate_invalid_response(self, ollama: OllamaLLM):
        """响应格式异常应抛出 RuntimeError"""
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json={"invalid": "data"})
        )
        ollama._client = httpx.AsyncClient(
            base_url=ollama.base_url, transport=transport
        )

        with pytest.raises(RuntimeError, match="Ollama 响应格式异常"):
            await ollama.generate([{"role": "user", "content": "test"}])

    @pytest.mark.asyncio
    async def test_generate_passes_kwargs(self, ollama: OllamaLLM):
        """额外参数应传递到请求体"""
        captured_body = {}

        def handler(request: httpx.Request):
            captured_body.update(json.loads(request.content))
            return httpx.Response(
                200, json={"message": {"content": "ok"}}
            )

        transport = httpx.MockTransport(handler)
        ollama._client = httpx.AsyncClient(
            base_url=ollama.base_url, transport=transport
        )

        await ollama.generate(
            [{"role": "user", "content": "test"}], temperature=0.7
        )
        assert captured_body["temperature"] == 0.7
        assert captured_body["stream"] is False


class TestOllamaStream:
    """测试 stream 方法"""

    @pytest.mark.asyncio
    async def test_stream_success(self, ollama: OllamaLLM):
        """流式生成应逐块返回内容"""
        chunks = [
            json.dumps({"message": {"content": "你"}}) + "\n",
            json.dumps({"message": {"content": "好"}}) + "\n",
            json.dumps({"message": {"content": "！"}}) + "\n",
        ]
        stream_content = "".join(chunks).encode()

        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, stream=httpx.ByteStream(stream_content))
        )
        ollama._client = httpx.AsyncClient(
            base_url=ollama.base_url, transport=transport
        )

        result = []
        async for token in ollama.stream([{"role": "user", "content": "你好"}]):
            result.append(token)

        assert result == ["你", "好", "！"]

    @pytest.mark.asyncio
    async def test_stream_skips_empty_content(self, ollama: OllamaLLM):
        """空 content 的 chunk 应被跳过"""
        chunks = [
            json.dumps({"message": {"content": "hello"}}) + "\n",
            json.dumps({"message": {"content": ""}}) + "\n",
            json.dumps({"message": {"content": "world"}}) + "\n",
        ]
        stream_content = "".join(chunks).encode()

        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, stream=httpx.ByteStream(stream_content))
        )
        ollama._client = httpx.AsyncClient(
            base_url=ollama.base_url, transport=transport
        )

        result = []
        async for token in ollama.stream([{"role": "user", "content": "hi"}]):
            result.append(token)

        assert result == ["hello", "world"]

    @pytest.mark.asyncio
    async def test_stream_skips_empty_lines(self, ollama: OllamaLLM):
        """空行应被跳过"""
        chunks = [
            json.dumps({"message": {"content": "a"}}) + "\n",
            "\n",
            json.dumps({"message": {"content": "b"}}) + "\n",
        ]
        stream_content = "".join(chunks).encode()

        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, stream=httpx.ByteStream(stream_content))
        )
        ollama._client = httpx.AsyncClient(
            base_url=ollama.base_url, transport=transport
        )

        result = []
        async for token in ollama.stream([{"role": "user", "content": "test"}]):
            result.append(token)

        assert result == ["a", "b"]


class TestOllamaChatWithTools:
    """测试 chat_with_tools 方法"""

    @pytest.mark.asyncio
    async def test_chat_with_tools_returns_tool_calls(self, ollama: OllamaLLM):
        """当模型返回 tool_calls 时，应正确解析为 LLMToolCall 列表"""
        mock_response = {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": "knowledge_search",
                            "arguments": {"queries": ["合同违约"], "top_k": 5},
                        }
                    }
                ],
            },
            "prompt_eval_count": 100,
            "eval_count": 50,
        }

        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json=mock_response)
        )
        ollama._client = httpx.AsyncClient(
            base_url=ollama.base_url, transport=transport
        )

        tools = [
            {
                "type": "function",
                "function": {
                    "name": "knowledge_search",
                    "description": "搜索知识库",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "queries": {"type": "array", "items": {"type": "string"}},
                            "top_k": {"type": "integer"},
                        },
                    },
                },
            }
        ]

        result = await ollama.chat_with_tools(
            [{"role": "user", "content": "合同违约怎么处理"}], tools
        )

        assert result.finish_reason == "tool_calls"
        assert len(result.tool_calls) == 1
        tc = result.tool_calls[0]
        assert tc.function_name == "knowledge_search"
        assert tc.id.startswith("call_")
        # arguments 应为 JSON 字符串
        args = json.loads(tc.arguments)
        assert args["queries"] == ["合同违约"]
        assert args["top_k"] == 5

    @pytest.mark.asyncio
    async def test_chat_with_tools_no_tool_calls(self, ollama: OllamaLLM):
        """当模型不返回 tool_calls 时，finish_reason 应为 stop"""
        mock_response = {
            "message": {
                "role": "assistant",
                "content": "你好，有什么可以帮助你的？",
            },
            "prompt_eval_count": 50,
            "eval_count": 20,
        }

        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json=mock_response)
        )
        ollama._client = httpx.AsyncClient(
            base_url=ollama.base_url, transport=transport
        )

        result = await ollama.chat_with_tools(
            [{"role": "user", "content": "你好"}], []
        )

        assert result.finish_reason == "stop"
        assert result.content == "你好，有什么可以帮助你的？"
        assert result.tool_calls == []

    @pytest.mark.asyncio
    async def test_chat_with_tools_usage_parsed(self, ollama: OllamaLLM):
        """token 用量应正确解析"""
        mock_response = {
            "message": {"role": "assistant", "content": "ok"},
            "prompt_eval_count": 120,
            "eval_count": 30,
        }

        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json=mock_response)
        )
        ollama._client = httpx.AsyncClient(
            base_url=ollama.base_url, transport=transport
        )

        result = await ollama.chat_with_tools(
            [{"role": "user", "content": "test"}], []
        )

        assert result.usage is not None
        assert result.usage.prompt_tokens == 120
        assert result.usage.completion_tokens == 30
        assert result.usage.total_tokens == 150

    @pytest.mark.asyncio
    async def test_chat_with_tools_http_error(self, ollama: OllamaLLM):
        """HTTP 错误应抛出 RuntimeError"""
        transport = httpx.MockTransport(
            lambda request: httpx.Response(500, text="Internal Server Error")
        )
        ollama._client = httpx.AsyncClient(
            base_url=ollama.base_url, transport=transport
        )

        with pytest.raises(RuntimeError, match="Ollama Function Calling 请求失败"):
            await ollama.chat_with_tools(
                [{"role": "user", "content": "test"}], []
            )

    @pytest.mark.asyncio
    async def test_chat_with_tools_multiple_tool_calls(self, ollama: OllamaLLM):
        """多个 tool_calls 应全部解析"""
        mock_response = {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": "knowledge_search",
                            "arguments": {"queries": ["query1"]},
                        }
                    },
                    {
                        "function": {
                            "name": "grep_chunks",
                            "arguments": {"query": "keyword"},
                        }
                    },
                ],
            },
        }

        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json=mock_response)
        )
        ollama._client = httpx.AsyncClient(
            base_url=ollama.base_url, transport=transport
        )

        result = await ollama.chat_with_tools(
            [{"role": "user", "content": "test"}], []
        )

        assert len(result.tool_calls) == 2
        assert result.tool_calls[0].function_name == "knowledge_search"
        assert result.tool_calls[1].function_name == "grep_chunks"
        # 每个 tool_call 应有唯一 id
        assert result.tool_calls[0].id != result.tool_calls[1].id

    @pytest.mark.asyncio
    async def test_chat_with_tools_passes_tools_in_payload(self, ollama: OllamaLLM):
        """tools 参数应正确传递到请求体"""
        captured_body = {}

        def handler(request: httpx.Request):
            captured_body.update(json.loads(request.content))
            return httpx.Response(
                200, json={"message": {"content": "ok"}}
            )

        transport = httpx.MockTransport(handler)
        ollama._client = httpx.AsyncClient(
            base_url=ollama.base_url, transport=transport
        )

        tools = [{"type": "function", "function": {"name": "test_tool"}}]
        await ollama.chat_with_tools(
            [{"role": "user", "content": "test"}], tools
        )

        assert captured_body["tools"] == tools
        assert captured_body["stream"] is False


class TestOllamaStreamWithTools:
    """测试 stream_with_tools 方法"""

    @pytest.mark.asyncio
    async def test_stream_with_tools_content_only(self, ollama: OllamaLLM):
        """纯 content 流式返回，最终 done=true 时 finish_reason=stop"""
        chunks = [
            json.dumps({"message": {"content": "你好"}, "done": False}) + "\n",
            json.dumps({"message": {"content": "世界"}, "done": False}) + "\n",
            json.dumps({"message": {"content": ""}, "done": True}) + "\n",
        ]
        stream_content = "".join(chunks).encode()

        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, stream=httpx.ByteStream(stream_content)
            )
        )
        ollama._client = httpx.AsyncClient(
            base_url=ollama.base_url, transport=transport
        )

        results = []
        async for chunk in ollama.stream_with_tools(
            [{"role": "user", "content": "hi"}], []
        ):
            results.append(chunk)

        # 应有 2 个 content chunk + 1 个 stop chunk
        content_chunks = [r for r in results if r.content]
        assert len(content_chunks) == 2
        assert content_chunks[0].content == "你好"
        assert content_chunks[1].content == "世界"

        # 最后一个应是 finish_reason=stop
        assert results[-1].finish_reason == "stop"

    @pytest.mark.asyncio
    async def test_stream_with_tools_tool_calls_in_final_chunk(
        self, ollama: OllamaLLM
    ):
        """tool_calls 在最终 chunk 中一次性返回"""
        chunks = [
            json.dumps({"message": {"content": ""}, "done": False}) + "\n",
            json.dumps(
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "knowledge_search",
                                    "arguments": {"queries": ["test"]},
                                }
                            }
                        ],
                    },
                    "done": True,
                }
            )
            + "\n",
        ]
        stream_content = "".join(chunks).encode()

        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, stream=httpx.ByteStream(stream_content)
            )
        )
        ollama._client = httpx.AsyncClient(
            base_url=ollama.base_url, transport=transport
        )

        results = []
        async for chunk in ollama.stream_with_tools(
            [{"role": "user", "content": "test"}], []
        ):
            results.append(chunk)

        # 应有 tool_call chunk
        tool_chunks = [r for r in results if r.tool_calls]
        assert len(tool_chunks) == 1
        assert tool_chunks[0].tool_calls[0].function_name == "knowledge_search"
        assert tool_chunks[0].finish_reason == "tool_calls"
        assert tool_chunks[0].response_type == "tool_call"

    @pytest.mark.asyncio
    async def test_stream_with_tools_http_error(self, ollama: OllamaLLM):
        """HTTP 错误应抛出 RuntimeError"""
        transport = httpx.MockTransport(
            lambda request: httpx.Response(500, text="error")
        )
        ollama._client = httpx.AsyncClient(
            base_url=ollama.base_url, transport=transport
        )

        with pytest.raises(RuntimeError, match="Ollama 流式 Function Calling 请求失败"):
            async for _ in ollama.stream_with_tools(
                [{"role": "user", "content": "test"}], []
            ):
                pass

    @pytest.mark.asyncio
    async def test_stream_with_tools_content_then_tool_calls(
        self, ollama: OllamaLLM
    ):
        """先返回 content 再返回 tool_calls 的场景"""
        chunks = [
            json.dumps(
                {"message": {"content": "让我搜索一下"}, "done": False}
            )
            + "\n",
            json.dumps(
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "grep_chunks",
                                    "arguments": {"query": "关键词", "top_k": 10},
                                }
                            }
                        ],
                    },
                    "done": True,
                }
            )
            + "\n",
        ]
        stream_content = "".join(chunks).encode()

        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, stream=httpx.ByteStream(stream_content)
            )
        )
        ollama._client = httpx.AsyncClient(
            base_url=ollama.base_url, transport=transport
        )

        results = []
        async for chunk in ollama.stream_with_tools(
            [{"role": "user", "content": "搜索"}], []
        ):
            results.append(chunk)

        # 第一个是 content
        assert results[0].content == "让我搜索一下"
        assert results[0].response_type == "content"

        # 第二个是 tool_call
        assert results[1].tool_calls is not None
        assert results[1].tool_calls[0].function_name == "grep_chunks"
        args = json.loads(results[1].tool_calls[0].arguments)
        assert args["query"] == "关键词"
        assert args["top_k"] == 10
