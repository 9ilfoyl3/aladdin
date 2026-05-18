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
