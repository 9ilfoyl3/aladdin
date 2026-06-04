"""Pre_Embed_Gate 容量判定的属性测试（任务 6.1）

被测对象：``app/pipeline/pipeline.py`` 的 :meth:`DocumentPipeline.process_to_vectors`
中的 Pre_Embed_Gate 容量闸门（design C5）。

闸门的纯决策逻辑（对照 requirements Req 4.2 / 4.3 / 6.6 / 9.4）：

- KB 上传：``cap = limits.kb_chunk_cap``，``used`` = ``Document.chunk_count`` 之和（按 kb_id
  聚合，可排除 ``incoming_doc_id`` 自身以避免重处理双计）。
- Session 上传：``cap = limits.session_chunk_cap``，``used`` = ``SessionFile.chunk_count``
  之和（按 session_id 聚合）。
- 当且仅当 ``used + incoming > cap`` 时抛 :class:`UploadCapExceeded`，且发生在 Embed 之前
  （决策点位于 Chunk 阶段后、Enrich/Embed 之前）。
- 该判定对 KB 与 Session 两种 scope 一致成立（仅 ``cap`` 来源与 ``used`` 聚合范围不同）。

测试策略：用内存 SQLite + 假 Loader（直接通过 ``LoadResult.pre_chunked`` 提供 N 个 chunk，
跳过 Chunker，使 ``child_count`` 精确可控）+ stub 的 Embedder（被调说明闸门已放行）调用真实
``process_to_vectors``，从而既测决策规则、又测拒绝时点（Embed 之前）。

Property 4（嵌入前容量判定的正确性）：
*For any* 已用 chunk 数 ``used``、本次 child chunk 数 ``incoming``、上限 ``cap``，
Pre_Embed_Gate SHALL 当且仅当 ``used + incoming > cap`` 时拒绝；通过时不拒绝；该判定对
KB_Upload（cap=kb_chunk_cap）与 Session_Upload（cap=session_chunk_cap）一致成立。

Feature: session-file-upload
Validates: Requirements 4.2, 4.3, 6.6, 9.4
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# pymilvus 在测试中不可用时打 stub，避免导入 milvus 时失败
sys.modules.setdefault("pymilvus", MagicMock())
sys.modules.setdefault("pymilvus.connections", MagicMock())
sys.modules.setdefault("pymilvus.utility", MagicMock())

from app.pipeline.embedder import EmbedResult
from app.pipeline.pipeline import DocumentPipeline, UploadCapExceeded
from app.schema.db import Base, Document, KnowledgeBase, SessionFile, ChatSession


# ============================================================
# 测试夹具与工具
# ============================================================


def _run_async(coro):
    """在同步 hypothesis 测试中运行异步代码（每例独立事件循环）。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@dataclass
class _FakeLoadResult:
    """伪 LoadResult：内容非空跳过 OCR，无图片、无 pre_chunked（让 chunker 走 else 分支被替换）。"""

    content: str
    metadata: dict = field(default_factory=dict)
    images: list = field(default_factory=list)
    page_texts: list = field(default_factory=list)
    pre_chunked: list[str] = field(default_factory=list)
    page_blocks: list | None = None


class _FakeLoader:
    """伪 Loader：返回非空 LoadResult（足以跳过 OCR），具体 chunk 数由替换的伪 Chunker 决定。"""

    def load(self, file_path: str):
        return _FakeLoadResult(
            content="这是一段足够长的伪文本内容，用于跳过 OCR 触发条件",
            metadata={"filename": "fake.txt"},
        )


class _FakeChunker:
    """伪 Chunker：返回精确 N 个 child chunks，使 Pre_Embed_Gate 观察到 incoming = N。

    用于完全控制 ``child_count``（包括 N=0 边界），避免依赖真实 chunker 的切分行为
    （真实 chunker 对短文本至少产生 1 个 chunk，无法直接构造 incoming=0 边界）。
    """

    def __init__(self, n: int):
        self._n = n

    def chunk(self, text, metadata=None):
        from app.pipeline.chunker import ChunkResult

        chunks = [f"chunk-{i}" for i in range(self._n)]
        return ChunkResult(
            parent_chunks=chunks,
            child_chunks=chunks,
            parent_child_map={i: [i] for i in range(self._n)},
        )


def _build_limits(*, kb_cap: int = 1_000_000, session_cap: int = 6000):
    """构造一个轻量 UploadLimits（避开 Resolver/Store，专注闸门决策测试）。"""
    from app.session_upload.limits import UploadLimits

    return UploadLimits(
        upload_max_file_bytes=10 * 1024 * 1024,
        session_max_files=5,
        session_chunk_cap=session_cap,
        kb_chunk_cap=kb_cap,
    )


def _make_pipeline(db_session_factory, *, incoming: int):
    """构造最小可用 DocumentPipeline，跳过模型加载、Milvus、真实 Chunker。

    Args:
        db_session_factory: 内存 SQLite 的 session 工厂。
        incoming: 期望本次的 child chunk 数；伪 Chunker 严格输出 N 个 chunk，让 Pre_Embed_Gate
            观察到精确的 ``child_count = incoming``（包括 0 边界）。
    """
    mock_model_manager = MagicMock()
    mock_model_manager.embedder = AsyncMock()
    mock_milvus = AsyncMock()
    pipeline = DocumentPipeline(mock_model_manager, mock_milvus, db_session_factory)

    # Stub 出 _embed_with_progress：被调说明闸门已放行（用于断言"未拒绝时进入 embed"）。
    embed_calls = {"count": 0}

    async def _fake_embed(texts, tracker=None, doc_id=""):
        embed_calls["count"] += 1
        return EmbedResult(
            dense_vectors=[[0.0] * 1024 for _ in texts],
            sparse_vectors=[{} for _ in texts],
        )

    pipeline._embed_with_progress = _fake_embed  # type: ignore[assignment]

    # Stub 出 _select_chunker_for_source：返回伪 Chunker（精确产出 incoming 个 child chunks）。
    fake_chunker = _FakeChunker(incoming)

    async def _fake_select_chunker(*args, **kwargs):
        return fake_chunker

    pipeline._select_chunker_for_source = _fake_select_chunker  # type: ignore[assignment]
    return pipeline, embed_calls


async def _seed_kb_used(factory, *, kb_id: str, used: int):
    """在 ``Document.chunk_count`` 中累计 ``used`` 个 chunk（拆成两条文档行测求和聚合）。"""
    async with factory() as session:
        kb = KnowledgeBase(id=kb_id, name="测试 KB")
        session.add(kb)
        if used > 0:
            half = used // 2
            session.add(Document(
                id=f"doc-{uuid.uuid4()}", kb_id=kb_id, filename="a.txt",
                file_type="txt", status="completed", chunk_count=half,
            ))
            session.add(Document(
                id=f"doc-{uuid.uuid4()}", kb_id=kb_id, filename="b.txt",
                file_type="txt", status="completed", chunk_count=used - half,
            ))
        await session.commit()


async def _seed_session_used(factory, *, session_id: str, used: int):
    """在 ``SessionFile.chunk_count`` 中累计 ``used`` 个 chunk（按会话聚合）。"""
    async with factory() as session:
        chat_session = ChatSession(id=session_id, title="测试会话")
        session.add(chat_session)
        if used > 0:
            half = used // 2
            session.add(SessionFile(
                id=f"sf-{uuid.uuid4()}", session_id=session_id,
                filename="a.txt", file_type="txt", chunk_count=half,
                status="completed",
            ))
            session.add(SessionFile(
                id=f"sf-{uuid.uuid4()}", session_id=session_id,
                filename="b.txt", file_type="txt", chunk_count=used - half,
                status="completed",
            ))
        await session.commit()


async def _make_factory():
    """新建内存 SQLite + create_all。返回 (engine, factory)，调用方负责 dispose。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, factory


async def _invoke_gate(
    *,
    source_kind: str,
    used: int,
    incoming: int,
    cap: int,
):
    """在新建的内存库中喂入 used，调用 process_to_vectors 测闸门决策。

    Returns:
        (raised_exc_or_none, embed_called: bool)
    """
    engine, factory = await _make_factory()
    try:
        if source_kind == "kb":
            kb_id = "test-kb-001"
            await _seed_kb_used(factory, kb_id=kb_id, used=used)
            source_id = kb_id
            limits = _build_limits(kb_cap=cap)
            incoming_doc_id = None  # 不排除任何文档（使用全部已用）
        else:  # session
            sid = "test-session-001"
            await _seed_session_used(factory, session_id=sid, used=used)
            source_id = sid
            limits = _build_limits(session_cap=cap)
            incoming_doc_id = None

        pipeline, embed_calls = _make_pipeline(factory, incoming=incoming)

        # patch get_loader 返回伪 Loader（内容非空跳过 OCR，由替换的 Chunker 决定 child_count）
        with patch("app.pipeline.pipeline.get_loader", return_value=_FakeLoader()):
            try:
                await pipeline.process_to_vectors(
                    "/fake/path/test.txt",
                    source_kind=source_kind,
                    source_id=source_id,
                    tenant_id=None,
                    limits=limits,
                    incoming_doc_id=incoming_doc_id,
                )
                return None, embed_calls["count"] > 0
            except UploadCapExceeded as exc:
                return exc, embed_calls["count"] > 0
    finally:
        await engine.dispose()


# ============================================================
# 生成器：(used, incoming, cap) 元组覆盖边界与跨越条件
# ============================================================


@st.composite
def _gate_inputs(draw):
    """生成 (used, incoming, cap)，覆盖通过/边界/拒绝三类。

    - cap：在合法 chunk_cap 区间内取（KB 与 Session 共享同一逻辑，故用统一区间生成）
    - used：[0, cap+小余量]
    - incoming：[0, cap+小余量]

    分布偏向边界附近（cap、cap-1、cap+1）以提高覆盖率。
    """
    # 测试用紧凑区间（避免 PG 大数表的查询代价；逻辑与生产区间无关）
    cap = draw(st.integers(min_value=1, max_value=10_000))

    # used 与 incoming 在 [0, cap*2] 内取，使生成的 (used+incoming) 既能 ≤cap 也能 >cap
    used = draw(st.integers(min_value=0, max_value=cap * 2))
    incoming = draw(st.integers(min_value=0, max_value=cap * 2))
    return used, incoming, cap


@st.composite
def _boundary_inputs(draw):
    """专门生成 used+incoming ∈ {cap-1, cap, cap+1} 的边界例（确保边界覆盖率）。"""
    cap = draw(st.integers(min_value=2, max_value=10_000))
    delta = draw(st.sampled_from([-1, 0, 1]))  # used+incoming = cap+delta
    target = cap + delta
    # 拆 used / incoming，二者 ≥0 且 ≤ target
    used = draw(st.integers(min_value=0, max_value=target))
    incoming = target - used
    return used, incoming, cap


# ============================================================
# Property 4：嵌入前容量判定的正确性
# ============================================================


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(data=_gate_inputs())
def test_property_kb_gate_iff_used_plus_incoming_exceeds_cap(data):
    """Feature: session-file-upload, Property 4: KB 闸门当且仅当 used+incoming>cap 时拒绝

    For any (used, incoming, cap) ∈ ℤ³≥0：
    - used + incoming  > cap  → SHALL 抛 UploadCapExceeded(scope="kb", cap, used, incoming)；
    - used + incoming ≤ cap  → SHALL 不抛；
    - 拒绝发生在 Embed 之前（embed 未被调用）。

    Validates: Requirements 4.2, 4.3, 9.4
    """
    used, incoming, cap = data

    exc, embed_called = _run_async(
        _invoke_gate(source_kind="kb", used=used, incoming=incoming, cap=cap)
    )

    if used + incoming > cap:
        # 拒绝：抛 UploadCapExceeded 且未进入 Embed 阶段（Req 9.4 闸门发生在 Embed 之前）
        assert exc is not None, f"应拒绝但未抛: used={used}, incoming={incoming}, cap={cap}"
        assert exc.scope == "kb"
        assert exc.cap == cap
        assert exc.used == used
        assert exc.incoming == incoming
        assert not embed_called, "拒绝时不应进入 Embed 阶段（Req 9.4）"
    else:
        # 通过：不抛 UploadCapExceeded
        assert exc is None, f"应通过但抛了 {exc}: used={used}, incoming={incoming}, cap={cap}"


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(data=_gate_inputs())
def test_property_session_gate_iff_used_plus_incoming_exceeds_cap(data):
    """Feature: session-file-upload, Property 4: Session 闸门当且仅当 used+incoming>cap 时拒绝

    For any (used, incoming, cap) ∈ ℤ³≥0：
    - used + incoming  > cap  → SHALL 抛 UploadCapExceeded(scope="session", cap, used, incoming)；
    - used + incoming ≤ cap  → SHALL 不抛；
    - 拒绝发生在 Embed 之前。

    Validates: Requirements 6.6, 9.4
    """
    used, incoming, cap = data

    exc, embed_called = _run_async(
        _invoke_gate(source_kind="session", used=used, incoming=incoming, cap=cap)
    )

    if used + incoming > cap:
        assert exc is not None, f"应拒绝但未抛: used={used}, incoming={incoming}, cap={cap}"
        assert exc.scope == "session"
        assert exc.cap == cap
        assert exc.used == used
        assert exc.incoming == incoming
        assert not embed_called, "拒绝时不应进入 Embed 阶段（Req 9.4）"
    else:
        assert exc is None, f"应通过但抛了 {exc}: used={used}, incoming={incoming}, cap={cap}"


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(data=_boundary_inputs())
def test_property_gate_boundary_kb_and_session_consistent(data):
    """Feature: session-file-upload, Property 4: 边界一致性（KB 与 Session 决策同构）

    For any (used, incoming, cap) 满足 used+incoming ∈ {cap-1, cap, cap+1}：
    - KB 与 Session 两种 scope 在同一 (used, incoming, cap) 下决策一致：要么都拒绝、要么都通过；
    - cap 临界点正确：== cap 通过、== cap+1 拒绝。

    Validates: Requirements 4.2, 4.3, 6.6（KB 与 Session 决策同构）
    """
    used, incoming, cap = data

    exc_kb, _ = _run_async(
        _invoke_gate(source_kind="kb", used=used, incoming=incoming, cap=cap)
    )
    exc_sess, _ = _run_async(
        _invoke_gate(source_kind="session", used=used, incoming=incoming, cap=cap)
    )

    # 两种 scope 的决策必须一致（同构）：要么都通过、要么都拒绝
    assert (exc_kb is None) == (exc_sess is None), (
        f"KB 与 Session 决策不一致: used={used}, incoming={incoming}, cap={cap}; "
        f"kb_raised={exc_kb is not None}, session_raised={exc_sess is None}"
    )

    # 边界正确性
    if used + incoming <= cap:
        assert exc_kb is None and exc_sess is None
    else:
        assert exc_kb is not None and exc_sess is not None
        assert exc_kb.scope == "kb" and exc_sess.scope == "session"


# ============================================================
# 边界单元测试（锚定关键端点，补充属性测试）
# ============================================================


@pytest.mark.parametrize("source_kind", ["kb", "session"])
def test_gate_at_exact_cap_allowed(source_kind):
    """边界：used + incoming == cap 时通过（恰等不拒绝，Req 4.2 / 6.6）。"""
    cap = 100
    exc, _ = _run_async(
        _invoke_gate(source_kind=source_kind, used=40, incoming=60, cap=cap)
    )
    assert exc is None


@pytest.mark.parametrize("source_kind", ["kb", "session"])
def test_gate_at_cap_plus_one_rejected(source_kind):
    """边界：used + incoming == cap + 1 时拒绝（首个超额点必拒）。"""
    cap = 100
    exc, _ = _run_async(
        _invoke_gate(source_kind=source_kind, used=40, incoming=61, cap=cap)
    )
    assert exc is not None
    assert exc.scope == source_kind
    assert exc.cap == cap
    assert exc.used == 40
    assert exc.incoming == 61


@pytest.mark.parametrize("source_kind", ["kb", "session"])
def test_gate_zero_used(source_kind):
    """边界：空库（used=0），incoming==cap 通过、incoming==cap+1 拒绝。"""
    cap = 50
    exc_eq, _ = _run_async(
        _invoke_gate(source_kind=source_kind, used=0, incoming=cap, cap=cap)
    )
    assert exc_eq is None
    exc_over, _ = _run_async(
        _invoke_gate(source_kind=source_kind, used=0, incoming=cap + 1, cap=cap)
    )
    assert exc_over is not None
    assert exc_over.used == 0


@pytest.mark.parametrize("source_kind", ["kb", "session"])
def test_gate_zero_incoming(source_kind):
    """边界：incoming=0，无论 used 多大都通过（不超额）。"""
    cap = 50
    # 注意：used 可以小于 cap、等于 cap、大于 cap，但 incoming=0 时 used+incoming=used，
    # 仅 used > cap 才拒绝；这反映了"已存量超额"在闸门视角不可达（业务上 used 由历史
    # 累积得到，不会自发生超额，这里只断言 incoming=0 在 used <= cap 时必通过）。
    for used in (0, cap // 2, cap):
        exc, _ = _run_async(
            _invoke_gate(source_kind=source_kind, used=used, incoming=0, cap=cap)
        )
        assert exc is None, f"used={used}, incoming=0 应通过"


@pytest.mark.parametrize("source_kind", ["kb", "session"])
def test_gate_large_values(source_kind):
    """边界：大数值（百万级）下决策仍正确。"""
    cap = 1_000_000
    # 通过
    exc_pass, _ = _run_async(
        _invoke_gate(source_kind=source_kind, used=999_990, incoming=10, cap=cap)
    )
    assert exc_pass is None
    # 拒绝
    exc_fail, _ = _run_async(
        _invoke_gate(source_kind=source_kind, used=999_990, incoming=11, cap=cap)
    )
    assert exc_fail is not None
    assert exc_fail.cap == cap


def test_gate_kb_excludes_self_via_incoming_doc_id():
    """KB 上传重处理：incoming_doc_id 排除当前文档自身，避免双计（Req 4.2 注释）。

    场景：库内已有 2 篇文档共 100 chunk，其中 doc_X 占 60 chunk。重处理 doc_X 时传入
    本次 incoming=70（覆盖原 60），incoming_doc_id=doc_X 应使聚合排除 doc_X 的旧 60，
    used=40，used+incoming=110 ≤ cap=120 通过；若不排除，used=100，used+incoming=170 > 120
    会误拒。
    """
    async def _run():
        engine, factory = await _make_factory()
        try:
            kb_id = "kb-001"
            doc_x = "doc-X"
            other = "doc-Y"
            async with factory() as s:
                s.add(KnowledgeBase(id=kb_id, name="kb"))
                s.add(Document(
                    id=doc_x, kb_id=kb_id, filename="x.txt", file_type="txt",
                    status="completed", chunk_count=60,
                ))
                s.add(Document(
                    id=other, kb_id=kb_id, filename="y.txt", file_type="txt",
                    status="completed", chunk_count=40,
                ))
                await s.commit()

            limits = _build_limits(kb_cap=120)
            pipeline, embed_calls = _make_pipeline(factory, incoming=70)
            with patch("app.pipeline.pipeline.get_loader", return_value=_FakeLoader()):
                # 排除自身：used = 40（other），used + 70 = 110 ≤ 120 → 通过
                await pipeline.process_to_vectors(
                    "/fake/x.txt",
                    source_kind="kb",
                    source_id=kb_id,
                    tenant_id=None,
                    limits=limits,
                    incoming_doc_id=doc_x,
                )
                assert embed_calls["count"] > 0, "排除自身后应通过并进入 Embed"

            # 不排除：used = 100，used + 70 = 170 > 120 → 拒绝
            pipeline2, embed_calls2 = _make_pipeline(factory, incoming=70)
            with patch("app.pipeline.pipeline.get_loader", return_value=_FakeLoader()):
                with pytest.raises(UploadCapExceeded) as exc_info:
                    await pipeline2.process_to_vectors(
                        "/fake/x.txt",
                        source_kind="kb",
                        source_id=kb_id,
                        tenant_id=None,
                        limits=limits,
                        incoming_doc_id=None,  # 不排除
                    )
                assert exc_info.value.used == 100
                assert exc_info.value.incoming == 70
                assert embed_calls2["count"] == 0, "拒绝时不应进入 Embed"
        finally:
            await engine.dispose()

    _run_async(_run())
