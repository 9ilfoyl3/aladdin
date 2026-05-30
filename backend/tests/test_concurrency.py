"""进程级全局并发限流（concurrency 模块）单元测试

验证 Embedding/OCR 全局信号量的核心契约：
- 同一事件循环内返回同一个共享信号量实例（跨文档共享）
- 信号量并发上限等于配置值
- 切换事件循环时自动重建（避免跨循环 Future 错误）
- reset_for_tests 清空缓存
"""

import asyncio

import pytest

from app.pipeline import concurrency
from app.config import get_settings


@pytest.fixture(autouse=True)
def _reset_semaphores():
    """每个用例前后清空缓存，避免相互污染。"""
    concurrency.reset_for_tests()
    yield
    concurrency.reset_for_tests()


@pytest.mark.asyncio
async def test_embed_semaphore_is_shared_within_loop():
    """同一事件循环内多次获取返回同一实例（所有文档共享同一阀门）。"""
    sem1 = concurrency.get_embed_semaphore()
    sem2 = concurrency.get_embed_semaphore()
    assert sem1 is sem2


@pytest.mark.asyncio
async def test_ocr_semaphore_is_shared_within_loop():
    """OCR 全局信号量同样在循环内共享。"""
    sem1 = concurrency.get_ocr_semaphore()
    sem2 = concurrency.get_ocr_semaphore()
    assert sem1 is sem2


@pytest.mark.asyncio
async def test_embed_and_ocr_are_distinct():
    """Embedding 与 OCR 是两个独立的信号量。"""
    assert concurrency.get_embed_semaphore() is not concurrency.get_ocr_semaphore()


@pytest.mark.asyncio
async def test_embed_semaphore_value_matches_config():
    """信号量并发上限等于配置 pipeline_embed_concurrency。"""
    expected = get_settings().pipeline_embed_concurrency
    sem = concurrency.get_embed_semaphore()
    # 连续 acquire expected 次应全部成功且不阻塞
    for _ in range(expected):
        await asyncio.wait_for(sem.acquire(), timeout=0.5)
    # 第 expected+1 次应当阻塞（无可用额度）
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(sem.acquire(), timeout=0.1)
    # 释放，避免影响后续
    for _ in range(expected):
        sem.release()


@pytest.mark.asyncio
async def test_ocr_semaphore_value_matches_config():
    """OCR 信号量并发上限等于配置 pipeline_ocr_concurrency。"""
    expected = get_settings().pipeline_ocr_concurrency
    sem = concurrency.get_ocr_semaphore()
    for _ in range(expected):
        await asyncio.wait_for(sem.acquire(), timeout=0.5)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(sem.acquire(), timeout=0.1)
    for _ in range(expected):
        sem.release()


def test_semaphore_rebuilds_across_loops():
    """不同事件循环获取的信号量是不同实例（避免跨循环 Future 错误）。"""
    async def _grab():
        return concurrency.get_embed_semaphore()

    sem_a = asyncio.run(_grab())
    sem_b = asyncio.run(_grab())
    assert sem_a is not sem_b


@pytest.mark.asyncio
async def test_reset_for_tests_clears_cache():
    """reset_for_tests 后再次获取应得到新实例。"""
    sem1 = concurrency.get_embed_semaphore()
    concurrency.reset_for_tests()
    sem2 = concurrency.get_embed_semaphore()
    assert sem1 is not sem2
