"""进程级全局并发限流

为 Embedding 和 OCR 这类「重资源」提供跨文档共享的全局信号量，
与文档级准入并发（PipelineWorker.semaphore）解耦：

- 文档级信号量只控制「同时推进多少个文档」，可以放大，让小文件在大文件
  等待 Embedding I/O 返回的间隙插入处理，消除队头阻塞。
- 全局 Embedding/OCR 信号量控制「同时打多少个远程请求」，保护后端服务不被打爆，
  无论有多少文档在并发处理，对外的并发压力都恒定可控。

为什么需要单独一层：
原先 Embedding 的并发信号量是「每个文档各自新建」的，所以并发上限其实是
「每文档 N」而非「全局 N」，文档一多就会把远程 Embedding 服务打爆，且无法
安全地放大文档准入数。本模块把它收敛成进程内单例的全局阀门。

事件循环安全：
asyncio.Semaphore 绑定在创建它的事件循环上。生产是单循环长驻进程，没问题；
但测试常为每个用例新建事件循环。这里按「当前运行循环」缓存信号量，循环变化时
自动重建，避免「got Future attached to a different loop」错误。
"""

from __future__ import annotations

import asyncio

from app.config import get_settings


class _LoopBoundSemaphore:
    """按事件循环缓存的信号量包装。

    每个 (运行中的事件循环) 对应一个 asyncio.Semaphore 实例；切换循环时重建。
    value 通过工厂函数延迟读取，保证读取到的是运行期最新配置。
    """

    def __init__(self, value_factory):
        self._value_factory = value_factory
        self._sem: asyncio.Semaphore | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def get(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if self._sem is None or self._loop is not loop:
            self._sem = asyncio.Semaphore(max(1, int(self._value_factory())))
            self._loop = loop
        return self._sem


_embed_global = _LoopBoundSemaphore(
    lambda: get_settings().pipeline_embed_concurrency
)
_ocr_global = _LoopBoundSemaphore(
    lambda: get_settings().pipeline_ocr_concurrency
)
_asr_global = _LoopBoundSemaphore(
    lambda: get_settings().pipeline_asr_concurrency
)


def get_embed_semaphore() -> asyncio.Semaphore:
    """获取进程级全局 Embedding 并发信号量（所有文档共享）。"""
    return _embed_global.get()


def get_ocr_semaphore() -> asyncio.Semaphore:
    """获取进程级全局 OCR 并发信号量（所有文档共享）。"""
    return _ocr_global.get()


def get_asr_semaphore() -> asyncio.Semaphore:
    """获取进程级全局 ASR 并发信号量（所有文档共享）。"""
    return _asr_global.get()


def reset_for_tests() -> None:
    """测试辅助：清空缓存的信号量，强制下次按当前配置/循环重建。"""
    _embed_global._sem = None
    _embed_global._loop = None
    _ocr_global._sem = None
    _ocr_global._loop = None
    _asr_global._sem = None
    _asr_global._loop = None
