"""对象存储（MinIO / S3 兼容）封装

知识库源文件的权威存储。上传时字节直接写入 MinIO，Worker 处理时下载到临时文件，
删除时移除对象。本地磁盘不再作为源文件的权威存储（仅会话上传仍用本地临时区）。

数据流（保持清晰）：
    上传 API ──put_bytes──> MinIO ──download_to_path──> Worker 临时文件 ──> pipeline
    预览 API ──open_stream/download_to_path──> 前端
    删除 API ──remove/remove_many──> MinIO

设计：进程内单例（与 milvus.get_milvus_client 一致）。minio SDK 为同步阻塞调用，
在 async 上下文中统一用 asyncio.to_thread 卸载，避免阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import tempfile
from typing import AsyncIterator, Iterable

from app.config import get_settings

logger = logging.getLogger(__name__)

# 知识库源文件对象 key 前缀（扁平存放在 bucket 根）。缩略图、会话文件单独前缀。
_THUMBNAIL_PREFIX = "thumbnails/"
_SESSION_PREFIX = "sessions/"


def document_object_key(doc_id: str, ext: str) -> str:
    """知识库源文件的对象 key：``{doc_id}.{ext}``。"""
    return f"{doc_id}.{ext}" if ext else doc_id


def thumbnail_object_key(doc_id: str) -> str:
    """缩略图对象 key：``thumbnails/{doc_id}.png``。"""
    return f"{_THUMBNAIL_PREFIX}{doc_id}.png"


def session_file_object_key(session_id: str, file_id: str, ext: str) -> str:
    """会话上传文件的对象 key：``sessions/{session_id}/{file_id}.{ext}``。

    按 session_id 分层，使删会话时可按前缀 ``sessions/{session_id}/`` 批量清理
    （删会话后 session_files 行已被 CASCADE 删除，无法再逐个取 file_id）。
    """
    leaf = f"{file_id}.{ext}" if ext else file_id
    return f"{_SESSION_PREFIX}{session_id}/{leaf}"


def session_prefix(session_id: str) -> str:
    """某会话所有上传文件的对象 key 前缀。"""
    return f"{_SESSION_PREFIX}{session_id}/"


class ObjectStore:
    """MinIO 客户端薄封装。所有阻塞调用经 asyncio.to_thread 卸载。"""

    def __init__(self, client, bucket: str):
        self._client = client
        self._bucket = bucket

    @property
    def bucket(self) -> str:
        return self._bucket

    def _ensure_bucket_sync(self) -> None:
        if not self._client.bucket_exists(self._bucket):
            self._client.make_bucket(self._bucket)
            logger.info("已创建 MinIO bucket: %s", self._bucket)

    async def ensure_bucket(self) -> None:
        """确保 bucket 存在（幂等）。启动期调用一次。"""
        await asyncio.to_thread(self._ensure_bucket_sync)

    def _put_bytes_sync(self, key: str, data: bytes, content_type: str) -> None:
        import io

        self._client.put_object(
            self._bucket,
            key,
            io.BytesIO(data),
            length=len(data),
            content_type=content_type or "application/octet-stream",
        )

    async def put_bytes(
        self, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> None:
        """写入字节对象（覆盖同名 key）。"""
        await asyncio.to_thread(self._put_bytes_sync, key, data, content_type)

    def _download_to_path_sync(self, key: str, dest_path: str) -> None:
        self._client.fget_object(self._bucket, key, dest_path)

    async def download_to_path(self, key: str, dest_path: str) -> None:
        """下载对象到本地路径。"""
        await asyncio.to_thread(self._download_to_path_sync, key, dest_path)

    def _get_bytes_sync(self, key: str) -> bytes:
        resp = self._client.get_object(self._bucket, key)
        try:
            return resp.read()
        finally:
            resp.close()
            resp.release_conn()

    async def get_bytes(self, key: str) -> bytes:
        """读取整个对象为字节（适合小对象，如缩略图）。"""
        return await asyncio.to_thread(self._get_bytes_sync, key)

    def _exists_sync(self, key: str) -> bool:
        from minio.error import S3Error

        try:
            self._client.stat_object(self._bucket, key)
            return True
        except S3Error:
            return False

    async def exists(self, key: str) -> bool:
        """对象是否存在。"""
        return await asyncio.to_thread(self._exists_sync, key)

    def _remove_sync(self, key: str) -> None:
        from minio.error import S3Error

        try:
            self._client.remove_object(self._bucket, key)
        except S3Error as e:
            logger.warning("删除对象失败 key=%s: %s", key, e)

    async def remove(self, key: str) -> None:
        """删除单个对象（不存在时静默）。"""
        await asyncio.to_thread(self._remove_sync, key)

    def _remove_many_sync(self, keys: list[str]) -> None:
        from minio.deleteobjects import DeleteObject

        errors = self._client.remove_objects(
            self._bucket, (DeleteObject(k) for k in keys)
        )
        for err in errors:
            logger.warning("批量删除对象出错: %s", err)

    async def remove_many(self, keys: Iterable[str]) -> None:
        """批量删除对象。"""
        key_list = [k for k in keys]
        if not key_list:
            return
        await asyncio.to_thread(self._remove_many_sync, key_list)

    def _remove_prefix_sync(self, prefix: str) -> int:
        from minio.deleteobjects import DeleteObject

        objects = self._client.list_objects(self._bucket, prefix=prefix, recursive=True)
        delete_list = [DeleteObject(obj.object_name) for obj in objects]
        if not delete_list:
            return 0
        errors = self._client.remove_objects(self._bucket, (d for d in delete_list))
        err_count = 0
        for err in errors:
            err_count += 1
            logger.warning("按前缀删除对象出错 prefix=%s: %s", prefix, err)
        return len(delete_list) - err_count

    async def remove_prefix(self, prefix: str) -> int:
        """删除某前缀下的所有对象，返回成功删除数。用于删会话级联清理。"""
        return await asyncio.to_thread(self._remove_prefix_sync, prefix)

    def _list_keys_sync(self, prefix: str) -> list[str]:
        objects = self._client.list_objects(self._bucket, prefix=prefix, recursive=True)
        return [obj.object_name for obj in objects]

    async def list_keys(self, prefix: str = "") -> list[str]:
        """列出某前缀下所有对象 key（递归）。用于对账孤儿对象。"""
        return await asyncio.to_thread(self._list_keys_sync, prefix)

    def _list_objects_with_mtime_sync(self, prefix: str) -> list[tuple[str, float]]:
        objects = self._client.list_objects(self._bucket, prefix=prefix, recursive=True)
        out: list[tuple[str, float]] = []
        for obj in objects:
            ts = obj.last_modified.timestamp() if obj.last_modified else 0.0
            out.append((obj.object_name, ts))
        return out

    async def list_objects_with_mtime(self, prefix: str = "") -> list[tuple[str, float]]:
        """列出某前缀下对象 (key, last_modified_epoch)。对账时按 mtime 设宽限期，
        避免误删正在上传/处理中的新对象。"""
        return await asyncio.to_thread(self._list_objects_with_mtime_sync, prefix)


_store: ObjectStore | None = None
_init_failed = False


def get_object_store() -> ObjectStore | None:
    """进程内 ObjectStore 单例。未配置或 minio SDK 缺失时返回 None。

    返回 None 表示对象存储不可用，调用方需自行决定降级行为（上传应直接报错，
    清理/预览可记 WARNING 跳过）。
    """
    global _store, _init_failed
    if _store is not None:
        return _store
    if _init_failed:
        return None

    settings = get_settings()
    endpoint = settings.minio_endpoint
    if not endpoint:
        _init_failed = True
        return None

    try:
        from minio import Minio

        client = Minio(
            endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
        _store = ObjectStore(client, settings.minio_bucket)
        return _store
    except Exception as e:  # noqa: BLE001 - 初始化失败统一降级
        logger.error("初始化 MinIO 客户端失败: %s", e)
        _init_failed = True
        return None


@contextlib.asynccontextmanager
async def materialized_file(key: str, suffix: str) -> AsyncIterator[str]:
    """把对象下载到本地临时文件，供需要文件路径的处理（pipeline loaders）使用。

    退出时删除临时文件。suffix 需含扩展名（如 ``.pdf``），loaders 据此选择解析器。
    """
    store = get_object_store()
    if store is None:
        raise RuntimeError("对象存储不可用，无法下载文件")

    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    try:
        await store.download_to_path(key, tmp_path)
        yield tmp_path
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
