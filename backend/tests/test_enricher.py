"""Enricher 单元测试"""

import pytest

from app.pipeline.enricher import Enricher


@pytest.mark.asyncio
async def test_enrich_disabled_returns_chunks_unchanged():
    """禁用状态下直接返回原文"""
    enricher = Enricher(llm=None, enabled=False)
    chunks = ["第一段文本", "第二段文本", "第三段文本"]
    result = await enricher.enrich(chunks)
    assert result == chunks


@pytest.mark.asyncio
async def test_enrich_enabled_but_no_llm_returns_unchanged():
    """启用但无 LLM 时直接返回原文"""
    enricher = Enricher(llm=None, enabled=True)
    chunks = ["测试内容"]
    result = await enricher.enrich(chunks)
    assert result == chunks


@pytest.mark.asyncio
async def test_enrich_empty_list():
    """空列表输入返回空列表"""
    enricher = Enricher()
    result = await enricher.enrich([])
    assert result == []


@pytest.mark.asyncio
async def test_default_init_is_disabled():
    """默认初始化为禁用状态"""
    enricher = Enricher()
    assert enricher.enabled is False
    assert enricher.llm is None
