"""会话累计配额加减一致性的属性测试（任务 7.1）

被测对象：``app/session_upload/service.py`` 的 :class:`SessionUploadService` 在添加 /
移除会话文件序列下的"累计文件数 / 累计 chunk 数"配额账面与文件数判定边界。

不变量（对照 design C4 / requirements Req 6.4 / 6.5 / 6.7）：

- ``used_files(sid) == COUNT(session_files WHERE session_id == sid)``。
- ``used_chunks(sid) == SUM(session_files.chunk_count WHERE session_id == sid)``。
- 上述两值在任意 ADD / REMOVE 序列后**恒等于**当前留存 ``SessionFile`` 行的计数与
  ``chunk_count`` 之和（移除即释放配额，Req 6.7）。
- 文件数闸门：当且仅当 ``used_files + 1 > session_max_files`` 时 ADD 被拒
  （:class:`SessionFileCountExceeded`，Req 6.5）；``used_files < session_max_files`` 时通过。

测试策略：用内存 SQLite + AsyncMock 的 Milvus 客户端 + 注入的"伪 pipeline"（
``process_to_vectors`` 返回精确 N 个 child chunks 的 :class:`ProcessedDocument`，避开
真实 Load / OCR / Embed），驱动**真实** :class:`SessionUploadService` 完成 add /
remove 操作。每条操作后查询 DB 并断言 service.used_files / used_chunks 与 ground truth
一致；ADD 在文件数已满时必抛 :class:`SessionFileCountExceeded`，反之必成功。

为隔离 Property 5（文件数与 chunk 计数账面一致性 + 文件数边界）与 Property 4（chunk
Pre_Embed_Gate 在 Embed 前判定），本测试把 ``session_chunk_cap`` 取得极大，使 chunk
闸门不影响 ADD 决策（Property 4 已在 ``test_pre_embed_gate_property.py`` 中独立覆盖）。

Property 5（会话累计配额的加减一致性）：
*For any* 一系列会话文件的添加与移除操作序列，该会话的累计已用文件数与累计 chunk 数
SHALL 恒等于当前留存 ``session_files`` 行的计数与 ``chunk_count`` 之和（移除即释放，
Req 6.7）；且文件数判定 SHALL 当且仅当 ``已留存数 + 本次 > session_max_files`` 时拒绝。

Feature: session-file-upload
Validates: Requirements 6.4, 6.5, 6.7 (Property 5)
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

# get_settings() 启动期 fail-fast 需要 JWT_SECRET（与 test_upload_file_size_gate 一致）。
os.environ.setdefault("JWT_SECRET", "session-upload-quota-test-secret-0123456789ab")
# pymilvus 在测试中不可用时打 stub，避免导入 milvus 时失败（与 test_pre_embed_gate_property 一致）。
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
from app.session_upload.service import (  # noqa: E402
    SessionFileCountExceeded,
    SessionUploadService,
)


# ============================================================
# 工具：异步 hypothesis 例 + DB 初始化
# ============================================================


def _run_async(coro):
    """在同步 hypothesis 测试中运行异步代码（每例独立事件循环，避免跨例污染）。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _make_engine_and_factory():
    """新建内存 SQLite + create_all。返回 (engine, factory)；调用方负责 dispose。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, factory


async def _seed_chat_session(factory, session_id: str) -> None:
    """为 SessionFile FK 预先建一行 ChatSession（避免 FK 拒绝插入）。"""
    async with factory() as session:
        session.add(ChatSession(id=session_id, title="测试会话"))
        await session.commit()


# ============================================================
# 伪 pipeline：精确产出 N 个 child chunks，避开 Load / OCR / Embed
# ============================================================


def _build_processed_document(
    *, child_count: int, filename: str = "fake.txt"
) -> ProcessedDocument:
    """构造一个 :class:`ProcessedDocument`，使 ``len(enriched_children) == child_count``。

    所有子块挂在同一个父块下；metadata / embed_result 与 child_count 一一对应；不模拟
    任何 Loader / Embedder 的真实行为，仅满足 ``SessionUploadService.upload`` 对结构字段的
    访问需求（``chunk_result.parent_chunks`` / ``enriched_children`` / ``metadata_list`` /
    ``embed_result.dense_vectors`` 等）。
    """
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
    """伪 pipeline 工厂：每次调用 ``process_to_vectors`` 返回 ``holder["child_count"]`` 个 chunk。

    用 holder（外部可变 dict）传 child_count，避免每次 ADD 重建工厂。Pre_Embed_Gate 由
    本伪 pipeline **不实施**（Property 5 与 Property 4 解耦；Property 4 由
    ``test_pre_embed_gate_property.py`` 独立覆盖）。
    """
    pipeline = MagicMock()

    async def _fake_process_to_vectors(*args, **kwargs):
        return _build_processed_document(child_count=child_count_holder["child_count"])

    pipeline.process_to_vectors = AsyncMock(side_effect=_fake_process_to_vectors)

    async def _factory():
        return pipeline

    return _factory


def _make_fake_milvus():
    """伪 Milvus 客户端：覆盖 service 用到的方法（insert / delete_by_doc_id /
    ensure_session_files_collection / delete_session），均为 AsyncMock。"""
    milvus = MagicMock()
    milvus.insert = AsyncMock(return_value=None)
    milvus.delete_by_doc_id = AsyncMock(return_value=None)
    milvus.ensure_session_files_collection = AsyncMock(return_value=None)
    milvus.delete_session = AsyncMock(return_value=None)
    return milvus


# ============================================================
# 操作序列重放（在内存库 + 真实 service 上）
# ============================================================


_SESSION_ID = "session-quota-test"
# 取一个远超操作序列里任何 chunk 之和的上限，让 chunk 闸门不影响文件数边界判定。
_HUGE_SESSION_CHUNK_CAP = 10_000_000


def _build_limits(session_max_files: int) -> UploadLimits:
    """构造测试用 ``UploadLimits``：文件大小 / KB cap 取宽松默认，session_chunk_cap 极大。"""
    return UploadLimits(
        upload_max_file_bytes=10 * 1024 * 1024,
        session_max_files=session_max_files,
        session_chunk_cap=_HUGE_SESSION_CHUNK_CAP,
        kb_chunk_cap=1_000_000,
    )


async def _query_db_state(factory) -> tuple[int, int]:
    """直接查 DB：返回 (used_files, used_chunks) ground truth。"""
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


async def _replay_ops(ops: list[tuple[str, int]], session_max_files: int) -> None:
    """构造内存库 + 真实 service，按序应用 ops，每步断言不变量与边界。

    Args:
        ops: ``("ADD", chunk_count)`` 或 ``("REMOVE", index_seed)`` 的列表。
            ``index_seed`` 为 0..N（任意整数），用于在已存在文件中按 ``seed % N`` 选一个移除；
            空列表时 REMOVE 视为 no-op。
        session_max_files: 文件数闸门生效值。
    """
    engine, factory = await _make_engine_and_factory()
    try:
        await _seed_chat_session(factory, _SESSION_ID)

        # holder 在伪 pipeline 工厂内被读取，每次 ADD 前更新为本次 chunk_count。
        child_count_holder = {"child_count": 1}

        service = SessionUploadService(
            milvus_client=_make_fake_milvus(),
            db_session_factory=factory,
            pipeline_factory=_make_fake_pipeline(child_count_holder),
        )

        # 维护一份内存 ground truth：active_files = [(file_id, chunk_count), ...]。
        active_files: list[tuple[str, int]] = []
        limits = _build_limits(session_max_files=session_max_files)

        # 屏蔽失效广播（默认无 Redis 时本就 None；显式 patch 确保稳定）。
        with patch(
            "app.session_upload.service.get_invalidation_bus",
            return_value=None,
        ):
            for step_idx, (kind, payload) in enumerate(ops):
                if kind == "ADD":
                    chunk_count = payload
                    child_count_holder["child_count"] = chunk_count
                    used_before = len(active_files)

                    if used_before + 1 > session_max_files:
                        # 文件数闸门：必拒（Req 6.5）
                        with pytest.raises(SessionFileCountExceeded) as exc_info:
                            await service.upload(
                                session_id=_SESSION_ID,
                                tenant_id=None,
                                owner_user_id=None,
                                filename=f"step-{step_idx}.txt",
                                content=b"x" * 16,  # 远小于 upload_max_file_bytes
                                limits=limits,
                            )
                        # 异常字段如实反映边界
                        assert exc_info.value.cap == session_max_files
                        assert exc_info.value.used == used_before
                        assert exc_info.value.incoming == 1
                        # 拒绝后状态不变
                    else:
                        # 文件数闸门：必通过
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
                        # 空集 REMOVE 视为 no-op（payload 无意义）
                        continue
                    seed = payload
                    idx = seed % len(active_files)
                    file_id, _ = active_files.pop(idx)
                    await service.remove_file(
                        session_id=_SESSION_ID, file_id=file_id
                    )

                # ── 不变量断言（每步执行）──
                expected_files = len(active_files)
                expected_chunks = sum(c for _, c in active_files)

                # 1) DB 实际状态 == ground truth（验证服务端写入正确）
                db_files, db_chunks = await _query_db_state(factory)
                assert db_files == expected_files, (
                    f"step {step_idx} DB used_files 不一致: 期望 {expected_files}, "
                    f"实际 {db_files}; ops={ops!r}"
                )
                assert db_chunks == expected_chunks, (
                    f"step {step_idx} DB used_chunks 不一致: 期望 {expected_chunks}, "
                    f"实际 {db_chunks}; ops={ops!r}"
                )

                # 2) service.used_files / used_chunks == ground truth
                #    （这是配额校验真正读取的值，对外承诺一致）
                svc_files = await service.used_files(_SESSION_ID)
                svc_chunks = await service.used_chunks(_SESSION_ID)
                assert svc_files == expected_files
                assert svc_chunks == expected_chunks
    finally:
        await engine.dispose()


# ============================================================
# 生成器
# ============================================================


# ADD 的 chunk_count：取小区间，确保 sum 不溢出 session_chunk_cap（极大），重点驱动文件数边界。
_ADD_OP = st.tuples(st.just("ADD"), st.integers(min_value=1, max_value=50))
# REMOVE 的 index_seed：[0, 99]，由 _replay_ops 内 ``seed % len(active_files)`` 选目标。
_REMOVE_OP = st.tuples(st.just("REMOVE"), st.integers(min_value=0, max_value=99))


@st.composite
def _ops_and_limit(draw):
    """生成 (ops, session_max_files)。

    - session_max_files: 1..6（覆盖小上限边界场景，命中"满 -> 拒"频率高）。
    - ops 长度: 0..30（足以触发多次 ADD/REMOVE 交错而不爆例时长）。
    - ADD vs REMOVE 比例: ADD 偏多（约 3:1），保证常常打到文件数上限触发拒绝路径。
    """
    session_max_files = draw(st.integers(min_value=1, max_value=6))
    ops = draw(
        st.lists(
            st.one_of(_ADD_OP, _ADD_OP, _ADD_OP, _REMOVE_OP),
            min_size=0,
            max_size=30,
        )
    )
    return ops, session_max_files


# ============================================================
# Property 5：会话累计配额的加减一致性
# ============================================================


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(data=_ops_and_limit())
def test_property_session_quota_add_remove_consistency(data):
    """Feature: session-file-upload, Property 5: 会话累计配额加减一致性 + 文件数边界

    For any 一系列 ADD / REMOVE 操作序列：
    - ``used_files(sid)``  == 当前留存 ``session_files`` 行计数；
    - ``used_chunks(sid)`` == 当前留存 ``session_files.chunk_count`` 之和；
    - 上述两值在每一步操作后都恒等于内存 ground truth（移除即释放配额，Req 6.7）；
    - ADD 在 ``used_files + 1 > session_max_files`` 时拒（``SessionFileCountExceeded``，
      Req 6.5），``used_files < session_max_files`` 时通过。

    Validates: Requirements 6.4, 6.5, 6.7
    """
    ops, session_max_files = data
    _run_async(_replay_ops(ops, session_max_files))


# ============================================================
# 边界单元测试（锚定关键端点，补充属性测试）
# ============================================================


def test_quota_release_on_remove_restores_capacity():
    """边界：满载后移除一项即释放配额，再次 ADD 通过（Req 6.7 显式锚定）。

    场景：session_max_files=2。先 ADD 两次塞满，再 ADD 必拒；移除任一文件后，下次 ADD 通过。
    """

    async def _scenario():
        ops = [
            ("ADD", 5),
            ("ADD", 7),
            ("ADD", 3),     # 应拒（已满）
            ("REMOVE", 0),  # 释放第 0 个
            ("ADD", 9),     # 再次 ADD 通过
        ]
        await _replay_ops(ops, session_max_files=2)

    _run_async(_scenario())


def test_chunk_count_sum_matches_after_mixed_sequence():
    """边界：混合序列下 used_chunks 始终等于留存文件 chunk_count 之和（Req 6.4 / 6.7 锚定）。

    手工构造一个固定序列，便于断言中间状态：
    - ADD 10 → used=(1,10)
    - ADD 20 → used=(2,30)
    - REMOVE 第 0 个（chunk=10） → used=(1,20)
    - ADD 5  → used=(2,25)
    """

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
            limits = _build_limits(session_max_files=10)

            with patch(
                "app.session_upload.service.get_invalidation_bus", return_value=None
            ):
                # ADD 10
                holder["child_count"] = 10
                vo1 = await service.upload(
                    session_id=_SESSION_ID, tenant_id=None, owner_user_id=None,
                    filename="a.txt", content=b"x", limits=limits,
                )
                assert (await _query_db_state(factory)) == (1, 10)
                assert await service.used_chunks(_SESSION_ID) == 10
                assert await service.used_files(_SESSION_ID) == 1

                # ADD 20
                holder["child_count"] = 20
                vo2 = await service.upload(
                    session_id=_SESSION_ID, tenant_id=None, owner_user_id=None,
                    filename="b.txt", content=b"x", limits=limits,
                )
                assert (await _query_db_state(factory)) == (2, 30)

                # REMOVE vo1（chunk_count=10）
                await service.remove_file(session_id=_SESSION_ID, file_id=vo1.id)
                assert (await _query_db_state(factory)) == (1, 20)
                assert await service.used_chunks(_SESSION_ID) == 20

                # ADD 5
                holder["child_count"] = 5
                vo3 = await service.upload(
                    session_id=_SESSION_ID, tenant_id=None, owner_user_id=None,
                    filename="c.txt", content=b"x", limits=limits,
                )
                assert (await _query_db_state(factory)) == (2, 25)
                # 留存的是 vo2 与 vo3
                assert {vo2.id, vo3.id} <= {vo2.id, vo3.id}
        finally:
            await engine.dispose()

    _run_async(_scenario())


def test_file_count_gate_at_exact_limit_rejects():
    """边界：``used_files == session_max_files`` 时 ADD 必拒（Req 6.5 + 异常字段验证）。

    断言异常字段（cap / used / incoming）如实反映拒绝点。
    """

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
            session_max_files = 3
            limits = _build_limits(session_max_files=session_max_files)

            with patch(
                "app.session_upload.service.get_invalidation_bus", return_value=None
            ):
                # 塞满
                for i in range(session_max_files):
                    await service.upload(
                        session_id=_SESSION_ID, tenant_id=None, owner_user_id=None,
                        filename=f"f{i}.txt", content=b"x", limits=limits,
                    )

                # 第 N+1 次 ADD 必拒
                with pytest.raises(SessionFileCountExceeded) as exc_info:
                    await service.upload(
                        session_id=_SESSION_ID, tenant_id=None, owner_user_id=None,
                        filename="overflow.txt", content=b"x", limits=limits,
                    )
                assert exc_info.value.cap == session_max_files
                assert exc_info.value.used == session_max_files
                assert exc_info.value.incoming == 1
        finally:
            await engine.dispose()

    _run_async(_scenario())


def test_file_count_gate_at_limit_minus_one_passes():
    """边界：``used_files == session_max_files - 1`` 时 ADD 必过（Req 6.5 通过侧锚定）。"""

    async def _scenario():
        engine, factory = await _make_engine_and_factory()
        try:
            await _seed_chat_session(factory, _SESSION_ID)
            holder = {"child_count": 4}
            service = SessionUploadService(
                milvus_client=_make_fake_milvus(),
                db_session_factory=factory,
                pipeline_factory=_make_fake_pipeline(holder),
            )
            session_max_files = 3
            limits = _build_limits(session_max_files=session_max_files)

            with patch(
                "app.session_upload.service.get_invalidation_bus", return_value=None
            ):
                # 先放 N-1 个
                for i in range(session_max_files - 1):
                    await service.upload(
                        session_id=_SESSION_ID, tenant_id=None, owner_user_id=None,
                        filename=f"f{i}.txt", content=b"x", limits=limits,
                    )
                # 此时 used == N-1，再放一个必过（恰好达上限）
                vo = await service.upload(
                    session_id=_SESSION_ID, tenant_id=None, owner_user_id=None,
                    filename="last.txt", content=b"x", limits=limits,
                )
                assert vo.chunk_count == 4
                used_files, used_chunks = await _query_db_state(factory)
                assert used_files == session_max_files
                assert used_chunks == 4 * session_max_files
        finally:
            await engine.dispose()

    _run_async(_scenario())


def test_remove_nonexistent_file_raises():
    """边界：移除不存在的 file_id 抛 ValueError，且不影响其他文件配额。"""

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
            limits = _build_limits(session_max_files=5)

            with patch(
                "app.session_upload.service.get_invalidation_bus", return_value=None
            ):
                vo = await service.upload(
                    session_id=_SESSION_ID, tenant_id=None, owner_user_id=None,
                    filename="exists.txt", content=b"x", limits=limits,
                )
                assert (await _query_db_state(factory)) == (1, 7)

                with pytest.raises(ValueError):
                    await service.remove_file(
                        session_id=_SESSION_ID, file_id=str(uuid.uuid4())
                    )
                # 状态不变
                assert (await _query_db_state(factory)) == (1, 7)
                assert await service.used_chunks(_SESSION_ID) == 7
        finally:
            await engine.dispose()

    _run_async(_scenario())
