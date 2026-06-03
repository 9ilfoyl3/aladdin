"""MilvusClient 进程内单例工厂单元测试（Task 17）

覆盖 design C8.1 / Req 14.1：
- get_milvus_client() 多次调用返回同一实例（进程内单例）。
- 单例 host/port 取自 get_settings()。

测试隔离：用例结束时把 app.storage.milvus._client 复位为 None，避免污染其它测试。

Feature: kb-retrieval-optimization
"""

import os
import sys
from unittest.mock import MagicMock

# get_settings() 启动期 fail-fast 需要 JWT_SECRET；导入前置好。
os.environ.setdefault("JWT_SECRET", "milvus-singleton-test-secret-0123456789abcdef")

# pymilvus 在当前环境可能无法导入，用 mock 规避导入依赖（沿用现有测试模式）。
sys.modules.setdefault("pymilvus", MagicMock())

import pytest  # noqa: E402

import app.storage.milvus as milvus_module  # noqa: E402
from app.storage.milvus import MilvusClient, get_milvus_client  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_singleton():
    """每个用例前后复位进程内单例，避免跨用例污染。"""
    milvus_module._client = None
    yield
    milvus_module._client = None


def test_returns_same_instance_across_calls():
    """get_milvus_client() 多次调用返回同一实例（is 相等）。"""
    first = get_milvus_client()
    second = get_milvus_client()

    assert first is second
    assert isinstance(first, MilvusClient)


def test_singleton_uses_settings_host_port():
    """单例 host/port 取自 get_settings()。"""
    from app.config import get_settings

    settings = get_settings()
    client = get_milvus_client()

    assert client._host == settings.milvus_host
    assert client._port == settings.milvus_port


def test_reset_then_new_instance():
    """复位 _client 后再次获取应得到新实例（验证单例状态可隔离）。"""
    first = get_milvus_client()
    milvus_module._client = None
    second = get_milvus_client()

    assert first is not second
