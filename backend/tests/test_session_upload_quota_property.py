"""会话累计配额加减一致性的属性测试

被测对象：``app/session_upload/service.py`` 的 :class:`SessionUploadService` 在添加 /
移除会话文件序列下的"累计文件数 / 累计 chunk 数"账面一致性。

不变量（移除文件数闸门后；临时文件统一由 kb_chunk_cap 的 chunk 闸门约束）：

- ``used_files(sid) == COUNT(session_files WHERE session_id == sid)``。
- ``used_chunks(sid) == SUM(session_files.chunk_count WHERE session_id == sid)``。
- 上述两值在任意 ADD / REMOVE 序列后**恒等于**当前留存 ``SessionFile`` 行的计数与
  ``chunk_count`` 之和（移除即释放）。
- 不再有"文件数上限"闸门：任意数量的 ADD 都应成功（chunk 上限本测试取极大隔离）。

测试策略：内存 SQLite + AsyncMock Milvus + 伪 pipeline（process_to_vectors 返回精确 N 个
child chunks），驱动真实 SessionUploadService 完成 add / remove。每条操作后查 DB 并断言
service.used_files / used_chunks 与 ground truth 一致。

Feature: session-file-upload
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("JWT_SECRET", "session-upload-quota-test-secret-0123456789ab")
sys.modules.setdefault("pymilvus", MagicMock())

import pytest  # noqa: E402
from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.pipeline.chunker import ChunkResult  # noqa: E402
from app.pipeline.embedder import EmbedResult  # noqa: E402
from app.pipeline.metadata import ChunkMetadata  # noqa: E402
from app.pipeline.pipeline import ProcessedDocument  # noqa: E402
from app.schema.db import Base, ChatSession, SessionFile  # noqa: E402
from app.session_upload.limits import UploadLimits  # noqa: E402
from app.session_upload.service import SessionUploadService  # noqa: E402


# ============================================================
# 工具：异步 hypothesis 例 + DB 初始化
# ============================================================


def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _make_engine_and_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, factory


async def _seed_chat_session(factory, session_id: str) -> None:
    async with factory() as session:
        session.add(ChatSession(id=session_id, title="测试会话"))
        await session.commit()


# ============================================================
# 伪 pipeline：精确产出 N 个 child chunks
# ============================================================


def _build_processed_document(
    *, child_count: int, filename: str = "fake.txt"
) -> ProcessedDocument:
    parent_chunks = ["parent-text"]
    child_chunks = [f"child-{i}" for i in range(child_count)]
    chunk_result = ChunkResult(
        parent_chunks=parent_chunks,
        child_chunks=child_chunks,
        parent_child_map={0: list(range(child_count))},
    )
    metadata_list = [
        ChunkMetadata(
            filename=filename,
            file_type="txt",
            chunker_type="hierarchical",
            chunk_index=i,
        )
        for i in range(child_count)
    ]
    embed_result = EmbedResult(
        dense_vectors=[[0.0] * 1024 for _ in range(child_count)],
        sparse_vectors=[{} for _ in range(child_count)],
    )
    return ProcessedDocument(
        chunk_result=chunk_result,
        enriched_children=list(child_chunks),
        metadata_list=metadata_list,
        embed_result=embed_result,
        doc_metadata={"filename": filename},
        child_to_parent={i: 0 for i in range(child_count)},
    )


def _make_fake_pipeline(child_count_holder: dict):
    pipeline = MagicMock()

    async def _fake_process_to_vectors(*args, **kwargs):
        return _build_processed_document(child_count=child_count_holder["child_count"])

    pipeline.process_to_vectors = AsyncMock(side_effect=_fake_process_to_vectors)

    async def _factory():
        return pipeline

    return _factory


def _make_fake_milvus():
    milvus = MagicMock()
    milvus.insert = AsyncMock(return_value=None)
    milvus.delete_by_doc_id = AsyncMock(return_value=None)
    milvus.ensure_session_files_collection = AsyncMock(return_value=None)
    milvus.delete_session = AsyncMock(return_value=None)
    return milvus


# ============================================================
# 操作序列重放
# ============================================================


_SESSION_ID = "session-quota-test"
# 取极大 chunk 上限，让 chunk 闸门不影响 ADD（本测试聚焦账面一致性）。
_HUGE_KB_CHUNK_CAP = 10_000_000


def _build_limits() -> UploadLimits:
    """构造测试用 UploadLimits：文件大小宽松，kb_chunk_cap 极大。"""
    return UploadLimits(
        upload_max_file_bytes=10 * 1024 * 1024,
        kb_chunk_cap=_HUGE_KB_CHUNK_CAP,
    )


async def _query_db_state(factory) -> tuple[int, int]:
    async with factory() as session:
        used_files = await session.scalar(
            select(func.count(SessionFile.id)).where(
                SessionFile.session_id == _SESSION_ID
            )
        )
        used_chunks = await session.scalar(
            select(func.coalesce(func.sum(SessionFile.chunk_count), 0)).where(
                SessionFile.session_id == _SESSION_ID
            )
        )
        return int(used_files or 0), int(used_chunks or 0)


async def _replay_ops(ops: list[tuple[str, int]]) -> None:
    """构造内存库 + 真实 service，按序应用 ops，每步断言账面一致性。

    ops: ``("ADD", chunk_count)`` 或 ``("REMOVE", index_seed)``。
    """
    engine, factory = await _make_engine_and_factory()
    try:
        await _seed_chat_session(factory, _SESSION_ID)

        child_count_holder = {"child_count": 1}
        service = SessionUploadService(
            milvus_client=_make_fake_milvus(),
            db_session_factory=factory,
            pipeline_factory=_make_fake_pipeline(child_count_holder),
        )

        active_files: list[tuple[str, int]] = []
        limits = _build_limits()

        with patch(
            "app.session_upload.service.get_invalidation_bus",
            return_value=None,
        ):
            for step_idx, (kind, payload) in enumerate(ops):
                if kind == "ADD":
                    chunk_count = payload
                    child_count_holder["child_count"] = chunk_count
                    # 无文件数闸门：ADD 总是成功
                    vo = await service.upload(
                        session_id=_SESSION_ID,
                        tenant_id=None,
                        owner_user_id=None,
                        filename=f"step-{step_idx}.txt",
                        content=b"x" * 16,
                        limits=limits,
                    )
                    assert vo.chunk_count == chunk_count
                    active_files.append((vo.id, chunk_count))
                else:  # REMOVE
                    if not active_files:
                        continue
                    idx = payload % len(active_files)
                    file_id, _ = active_files.pop(idx)
                    await service.remove_file(
                        session_id=_SESSION_ID, file_id=file_id
                    )

                expected_files = len(active_files)
                expected_chunks = sum(c for _, c in active_files)

                db_files, db_chunks = await _query_db_state(factory)
                assert db_files == expected_files, (
                    f"step {step_idx} DB used_files 不一致: 期望 {expected_files}, "
                    f"实际 {db_files}; ops={ops!r}"
                )
                assert db_chunks == expected_chunks, (
                    f"step {step_idx} DB used_chunks 不一致: 期望 {expected_chunks}, "
                    f"实际 {db_chunks}; ops={ops!r}"
                )

                svc_files = await service.used_files(_SESSION_ID)
                svc_chunks = await service.used_chunks(_SESSION_ID)
                assert svc_files == expected_files
                assert svc_chunks == expected_chunks
    finally:
        await engine.dispose()


# ============================================================
# 生成器
# ============================================================


_ADD_OP = st.tuples(st.just("ADD"), st.integers(min_value=1, max_value=50))
_REMOVE_OP = st.tuples(st.just("REMOVE"), st.integers(min_value=0, max_value=99))


@st.composite
def _ops(draw):
    return draw(
        st.lists(
            st.one_of(_ADD_OP, _ADD_OP, _ADD_OP, _REMOVE_OP),
            min_size=0,
            max_size=30,
        )
    )


# ============================================================
# Property：会话累计配额的加减一致性
# ============================================================


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(ops=_ops())
def test_property_session_quota_add_remove_consistency(ops):
    """Feature: session-file-upload: 会话累计配额加减一致性（无文件数闸门）

    For any 一系列 ADD / REMOVE 操作序列：
    - used_files / used_chunks 在每步后恒等于留存文件的计数与 chunk_count 之和；
    - ADD 不再受文件数上限限制，总是成功（移除即释放）。
    """
    _run_async(_replay_ops(ops))


# ============================================================
# 边界单元测试
# ============================================================


def test_chunk_count_sum_matches_after_mixed_sequence():
    """混合序列下 used_chunks 始终等于留存文件 chunk_count 之和。"""

    async def _scenario():
        engine, factory = await _make_engine_and_factory()
        try:
            await _seed_chat_session(factory, _SESSION_ID)
            holder = {"child_count": 1}
            service = SessionUploadService(
                milvus_client=_make_fake_milvus(),
                db_session_factory=factory,
                pipeline_factory=_make_fake_pipeline(holder),
            )
            limits = _build_limits()

            with patch(
                "app.session_upload.service.get_invalidation_bus", return_value=None
            ):
                holder["child_count"] = 10
                vo1 = await service.upload(
                    session_id=_SESSION_ID, tenant_id=None, owner_user_id=None,
                    filename="a.txt", content=b"x", limits=limits,
                )
                assert (await _query_db_state(factory)) == (1, 10)

                holder["child_count"] = 20
                vo2 = await service.upload(
                    session_id=_SESSION_ID, tenant_id=None, owner_user_id=None,
                    filename="b.txt", content=b"x", limits=limits,
                )
                assert (await _query_db_state(factory)) == (2, 30)

                await service.remove_file(session_id=_SESSION_ID, file_id=vo1.id)
                assert (await _query_db_state(factory)) == (1, 20)

                holder["child_count"] = 5
                vo3 = await service.upload(
                    session_id=_SESSION_ID, tenant_id=None, owner_user_id=None,
                    filename="c.txt", content=b"x", limits=limits,
                )
                assert (await _query_db_state(factory)) == (2, 25)
                assert {vo2.id, vo3.id}
        finally:
            await engine.dispose()

    _run_async(_scenario())


def test_many_files_no_count_gate():
    """无文件数闸门：连续上传远超旧上限（20）的文件数也应全部成功。"""

    async def _scenario():
        engine, factory = await _make_engine_and_factory()
        try:
            await _seed_chat_session(factory, _SESSION_ID)
            holder = {"child_count": 2}
            service = SessionUploadService(
                milvus_client=_make_fake_milvus(),
                db_session_factory=factory,
                pipeline_factory=_make_fake_pipeline(holder),
            )
            limits = _build_limits()

            with patch(
                "app.session_upload.service.get_invalidation_bus", return_value=None
            ):
                for i in range(30):
                    await service.upload(
                        session_id=_SESSION_ID, tenant_id=None, owner_user_id=None,
                        filename=f"f{i}.txt", content=b"x", limits=limits,
                    )
                used_files, used_chunks = await _query_db_state(factory)
                assert used_files == 30
                assert used_chunks == 60
        finally:
            await engine.dispose()

    _run_async(_scenario())


def test_remove_nonexistent_file_raises():
    """移除不存在的 file_id 抛 ValueError，且不影响其他文件配额。"""

    async def _scenario():
        engine, factory = await _make_engine_and_factory()
        try:
            await _seed_chat_session(factory, _SESSION_ID)
            holder = {"child_count": 7}
            service = SessionUploadService(
                milvus_client=_make_fake_milvus(),
                db_session_factory=factory,
                pipeline_factory=_make_fake_pipeline(holder),
            )
            limits = _build_limits()

            with patch(
                "app.session_upload.service.get_invalidation_bus", return_value=None
            ):
                await service.upload(
                    session_id=_SESSION_ID, tenant_id=None, owner_user_id=None,
                    filename="exists.txt", content=b"x", limits=limits,
                )
                assert (await _query_db_state(factory)) == (1, 7)

                with pytest.raises(ValueError):
                    await service.remove_file(
                        session_id=_SESSION_ID, file_id=str(uuid.uuid4())
                    )
                assert (await _query_db_state(factory)) == (1, 7)
        finally:
            await engine.dispose()

    _run_async(_scenario())
