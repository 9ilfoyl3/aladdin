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
