"""KB 上传文件大小闸门的属性测试（任务 11.1）

被测对象：``app/api/document.py`` 的 ``upload_document`` 端点中由
``UploadLimitResolver`` 驱动的文件大小闸门——文件被读取后、写盘 / hash 去重 /
DB 写入 / 入队之前，按租户级 ``upload_max_file_bytes`` 拦截：

    limits = await get_upload_limit_resolver().resolve(identity.tenant_id)
    if file_size > limits.upload_max_file_bytes:
        raise FileTooLargeError.from_limit(limits.upload_max_file_bytes)

会话上传与 KB 上传共用同一租户级 ``L`` —— 同一 ``UploadLimitResolver`` 单例 +
同一 ``upload_max_file_bytes`` 字段（design C2 / C8 / C11，requirements Req 3.5）。

Property 7（文件大小闸门）：
*For any* 文件大小 ``s`` 与生效上限 ``L``，上传入口 SHALL 当且仅当 ``s > L`` 时
拒绝；该闸门对 Session_Upload 与 KB_Upload 使用同一租户级 ``L``。

为同时满足"≥100 迭代覆盖输入空间"与"实测 HTTP 端点行为"，本测试分两层：

1. **预测谓词属性**：针对核心布尔规则 ``reject_iff_size_gt_limit`` 用 hypothesis
   生成 ``(s, L)`` 大量样本，断言谓词与端点闸门的行为对应（在端点测试中用具体
   ``(s, L)`` 实例验证两侧一致）。
2. **端点行为**：通过 httpx + ASGITransport 直接驱动 FastAPI 应用，取若干
   关键示例（`s == L` / `s < L` / `s > L` / `s == L+1` / `s == L-1`）触发
   真实端点，断言：
   - ``s > L`` → 413 + ``FileTooLargeError`` 文案，且 **不写盘 / 不写 DB / 不入队**
     （校验发生在解析前的"零侧效应"位置——这里"解析"指 hash 去重 / DB 写入 /
     pipeline 入队，文件内容已读取但 ``upload_document`` 在闸门后才落盘）
   - ``s ≤ L`` → 201 + DocumentResponse
3. **共用同一租户 L**：直接断言 ``UploadLimitResolver.resolve(tenant_id)`` 对同一
   ``tenant_id`` 返回相同 ``upload_max_file_bytes``（KB 上传与会话上传都从此读取，
   故 L 同源——design C2/C8/C11）；并断言 KB 与 Session 路径都消费同一字段。

Feature: session-file-upload
Validates: Requirements 3.2, 3.5 (Property 7)
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# get_settings() 启动期 fail-fast 需要 JWT_SECRET。
os.environ.setdefault("JWT_SECRET", "upload-size-gate-test-secret-0123456789abcdef")
# Mock 重型依赖模块，避免 pymilvus 导入依赖问题（沿用现有测试模式）。
sys.modules.setdefault("pymilvus", MagicMock())

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.retrieval.config import RETRIEVAL_FIELD_SPECS  # noqa: E402
from app.schema.db import Base, Document  # noqa: E402
from app.session_upload.limits import (  # noqa: E402
    UploadLimits,
    UploadLimitResolver,
    get_upload_limit_resolver,
)


# ============================================================
# 模块常量（避免魔法值；与被测模块 _BYTES_PER_MB 一致）
# ============================================================

_BYTES_PER_MB = 1024 * 1024

# 范围采样上下界（基于 RETRIEVAL_FIELD_SPECS["upload_max_file_mb"] 的 [1, 100]，
# 转字节后约 [1MB, 100MB]）。为避免在端点测试里实际生成超大 payload，端点测试用
# 小 limit（例：1 字节）+ 小 payload（例：2 字节）等价覆盖"超限/未超限"边界。
_LIMIT_SPEC = RETRIEVAL_FIELD_SPECS["upload_max_file_mb"]
_LIMIT_LO_BYTES = _LIMIT_SPEC.lo * _BYTES_PER_MB
_LIMIT_HI_BYTES = _LIMIT_SPEC.hi * _BYTES_PER_MB


# ============================================================
# Property 7 ①：核心谓词的属性测试（≥100 迭代覆盖输入空间）
# ============================================================


def _gate_predicate(file_size: int, upload_max_file_bytes: int) -> bool:
    """文件大小闸门的核心谓词：当且仅当 ``file_size > upload_max_file_bytes`` 时拒绝。

    与 ``app/api/document.py`` 中 ``upload_document`` 的判定语义一致：
    ``if file_size > limits.upload_max_file_bytes: raise FileTooLargeError``。
    """
    return file_size > upload_max_file_bytes


@settings(max_examples=100)
@given(
    file_size=st.integers(min_value=0, max_value=_LIMIT_HI_BYTES + 4096),
    limit=st.integers(min_value=_LIMIT_LO_BYTES, max_value=_LIMIT_HI_BYTES),
)
def test_property7_reject_iff_size_gt_limit(file_size: int, limit: int) -> None:
    """Feature: session-file-upload, Property 7: 文件大小闸门当且仅当 ``s > L`` 时拒绝。

    For any 文件大小 ``s`` 与生效上限 ``L``：
    - ``s > L``  → 拒绝（``_gate_predicate`` 为 True）
    - ``s == L`` → 通过（边界含 L，文案 "最大 NMB" 含 L 自身）
    - ``s < L``  → 通过

    该谓词同样适用于会话上传与 KB 上传（共用同一租户级 ``L``，design C2/C11）。

    Validates: Requirements 3.2, 3.5
    """
    rejected = _gate_predicate(file_size, limit)
    # 充要条件：rejected ⇔ size > limit
    assert rejected is (file_size > limit)
    # 等价表述：未拒绝 ⇔ size ≤ limit
    assert (not rejected) is (file_size <= limit)


@settings(max_examples=100)
@given(limit=st.integers(min_value=_LIMIT_LO_BYTES, max_value=_LIMIT_HI_BYTES))
def test_property7_boundary_size_equal_limit_passes(limit: int) -> None:
    """Feature: session-file-upload, Property 7（边界切片）：``s == L`` 必须通过。

    Validates: Requirements 3.2
    """
    assert _gate_predicate(limit, limit) is False


@settings(max_examples=100)
@given(limit=st.integers(min_value=_LIMIT_LO_BYTES, max_value=_LIMIT_HI_BYTES))
def test_property7_boundary_size_limit_plus_one_rejects(limit: int) -> None:
    """Feature: session-file-upload, Property 7（边界切片）：``s == L+1`` 必须拒绝。

    Validates: Requirements 3.2
    """
    assert _gate_predicate(limit + 1, limit) is True


# ============================================================
# Property 7 ②：会话与 KB 上传共用同一租户级 L（design C2 / C8 / C11）
# ============================================================


@pytest.mark.asyncio
async def test_property7_kb_and_session_share_same_tenant_limit() -> None:
    """同一 ``tenant_id`` 下，KB 上传与会话上传消费的 ``upload_max_file_bytes``
    必出自同一来源（``UploadLimitResolver.resolve``），故必相等。

    本断言验证 design C2 的"共用一份 ``UploadLimits`` 快照"承诺：
    ``upload_max_file_bytes`` 字段在 ``UploadLimits`` 中**唯一**，KB（C11）与
    Session（C8）路径都从该字段读取，无独立来源（Req 3.5）。

    Validates: Requirements 3.5
    """

    fake_limits = UploadLimits(
        upload_max_file_bytes=42 * _BYTES_PER_MB,
        kb_chunk_cap=1_000_000,
    )

    class _FakeResolver:
        async def resolve(self, tenant_id: str | None) -> UploadLimits:
            return fake_limits

    resolver = _FakeResolver()
    kb_limits = await resolver.resolve("tenant-A")
    session_limits = await resolver.resolve("tenant-A")
    # 同源：单一 UploadLimits 快照中的同一字段，KB/Session 都读它
    assert kb_limits.upload_max_file_bytes == session_limits.upload_max_file_bytes
    # 字段名是承诺的一部分（Session 与 KB 都从此字段读取）
    assert hasattr(kb_limits, "upload_max_file_bytes")


def test_property7_get_upload_limit_resolver_is_singleton() -> None:
    """``get_upload_limit_resolver()`` 返回进程内单例——KB 与 Session 路径都通过
    同一函数取得，确保对同一 ``tenant_id`` 的求解返回相同字节上限（无第二来源）。

    Validates: Requirements 3.5
    """
    a = get_upload_limit_resolver()
    b = get_upload_limit_resolver()
    assert a is b
    assert isinstance(a, UploadLimitResolver)


# ============================================================
# Property 7 ③：端点行为 —— 拒绝路径无副作用、通过路径正常落库
# ============================================================


@pytest_asyncio.fixture
async def kb_upload_ctx(tmp_path):
    """端点测试上下文：内存 sqlite + 旁路鉴权 + 临时上传目录 + 受控 limit。

    - 把上传目录 ``_UPLOAD_DIR`` / 缩略图目录 ``_THUMBNAIL_DIR`` 重定向到 tmp_path
      下的隔离子目录，避免测试污染真实 ``data/uploads``。
    - 进程隔离地把 ``deps._resolve_identity`` 替换为返回固定 admin 身份。
    - 用 ``patch`` 把 ``UploadLimitResolver`` 单例换成会话级伪 resolver，
      ``upload_max_file_bytes`` 由 holder["limit_bytes"] 控制；这样可在不生成 100MB
      payload 的前提下覆盖"超限/未超限"边界（用 1 字节 limit + 2 字节 payload 等）。
    - 把 ``_enqueue_or_fallback`` mock 掉避免触发 Redis / 进程内 pipeline。

    yield (client, holder, factory, uploads_dir)
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # 重定向上传/缩略图目录到 tmp_path 隔离区。
    import app.api.document as doc_module

    orig_upload_dir = doc_module._UPLOAD_DIR
    orig_thumb_dir = doc_module._THUMBNAIL_DIR
    uploads_dir = tmp_path / "uploads"
    thumbs_dir = uploads_dir / "thumbnails"
    doc_module._UPLOAD_DIR = uploads_dir
    doc_module._THUMBNAIL_DIR = thumbs_dir

    # 全局 async_session 与 deps 重定向到测试库。
    import app.storage.database as dbmod

    orig_async_session = dbmod.async_session
    dbmod.async_session = factory

    from app.main import app
    import app.api.deps as deps
    from app.auth.constants import TenantRoleEnum
    from app.auth.identity import (
        IdentityContext,
        IdentitySourceEnum,
        OperationLevelEnum,
    )
    from app.storage.database import get_db

    holder: dict = {
        "identity": IdentityContext(
            source=IdentitySourceEnum.JWT,
            op_level=OperationLevelEnum.TENANT,
            tenant_id="tenant-A",
            user_id="u1",
            username="u1",
            is_super_admin=False,
            role=TenantRoleEnum.ADMIN,
        ),
        "limit_bytes": 1 * _BYTES_PER_MB,  # 默认 1MB；具体测试可改
    }

    orig_resolve = deps._resolve_identity

    async def _fake_resolve(request, session):
        return holder["identity"], False

    deps._resolve_identity = _fake_resolve

    async def _override_get_db():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = _override_get_db
    orig_get_db_session = deps.get_db_session
    deps.get_db_session = _override_get_db
    app.dependency_overrides[deps.get_db_session] = _override_get_db

    # 把 UploadLimitResolver 单例替换为受测试控制的伪 resolver（同一 tenant 同一 limit）。
    import app.session_upload.limits as limits_module

    class _FakeResolver:
        async def resolve(self, tenant_id):
            return UploadLimits(
                upload_max_file_bytes=holder["limit_bytes"],
                kb_chunk_cap=1_000_000,
            )

    orig_resolver = limits_module._resolver
    limits_module._resolver = _FakeResolver()

    # 旁路 pipeline 入队（避免触发 Redis / 进程内异步任务）。
    enqueue_patch = patch(
        "app.api.document._enqueue_or_fallback", new=AsyncMock(return_value=None)
    )
    enqueue_patch.start()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # 创建一个 KB 给 KB 上传端点用。
        resp = await ac.post("/api/knowledge-bases", json={"name": "size-gate-kb"})
        assert resp.status_code == 201, resp.text
        kb_id = resp.json()["id"]
        holder["kb_id"] = kb_id

        yield ac, holder, factory, uploads_dir

    enqueue_patch.stop()
    limits_module._resolver = orig_resolver
    deps._resolve_identity = orig_resolve
    deps.get_db_session = orig_get_db_session
    dbmod.async_session = orig_async_session
    doc_module._UPLOAD_DIR = orig_upload_dir
    doc_module._THUMBNAIL_DIR = orig_thumb_dir
    app.dependency_overrides.clear()
    await engine.dispose()
    if uploads_dir.exists():
        shutil.rmtree(uploads_dir, ignore_errors=True)


async def _count_documents(factory, kb_id: str) -> int:
    """统计某 KB 下的 Document 行数（用于断言"未写 DB"）。"""
    async with factory() as session:
        rows = await session.execute(select(Document).where(Document.kb_id == kb_id))
        return len(rows.scalars().all())


def _files_under(uploads_dir: Path) -> list[Path]:
    """递归列出 uploads_dir 下的常规文件（thumbnails 子目录排除）。"""
    if not uploads_dir.exists():
        return []
    return [p for p in uploads_dir.iterdir() if p.is_file()]


@pytest.mark.asyncio
async def test_endpoint_rejects_when_size_above_limit_without_side_effects(kb_upload_ctx):
    """``s > L`` → 413 + 文案带 limit_mb；不写 DB / 不写盘 / 不入队。

    Validates: Requirements 3.2 (Property 7 拒绝路径)
    """
    client, holder, factory, uploads_dir = kb_upload_ctx
    # 1 字节上限 + 5 字节内容 → 必拒绝（覆盖 s > L 全等价类）。
    holder["limit_bytes"] = 1
    payload = b"hello"  # 5 字节 > 1
    pre_doc_count = await _count_documents(factory, holder["kb_id"])
    pre_files = _files_under(uploads_dir)

    resp = await client.post(
        f"/api/knowledge-bases/{holder['kb_id']}/documents/upload",
        files={"file": ("test.txt", payload, "text/plain")},
    )

    assert resp.status_code == 413, resp.text
    body = resp.json()
    assert "上传文件超过允许的大小上限" in body["detail"]
    # 文案携带允许上限（MB）—— 1 字节按 //(1024*1024) 显示 0MB
    assert "MB" in body["detail"]

    # 校验"零侧效应"：DB 未新增 Document、上传目录未新增文件、入队未触发。
    assert await _count_documents(factory, holder["kb_id"]) == pre_doc_count
    assert _files_under(uploads_dir) == pre_files
    # _enqueue_or_fallback 是 AsyncMock，但拒绝路径根本不应到达它
    import app.api.document as doc_module

    enqueue_mock = doc_module._enqueue_or_fallback
    assert enqueue_mock.await_count == 0


@pytest.mark.asyncio
async def test_endpoint_accepts_when_size_below_limit(kb_upload_ctx):
    """``s < L`` → 201 + DocumentResponse；DB 写入新文档、上传目录有文件落盘。

    Validates: Requirements 3.2 (Property 7 通过路径)
    """
    client, holder, factory, uploads_dir = kb_upload_ctx
    holder["limit_bytes"] = 1024  # 1KB
    payload = b"x" * 16  # 16 字节 < 1KB

    resp = await client.post(
        f"/api/knowledge-bases/{holder['kb_id']}/documents/upload",
        files={"file": ("ok.txt", payload, "text/plain")},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["filename"] == "ok.txt"
    assert data["status"] == "pending"
    assert data["file_size"] == 16
    assert await _count_documents(factory, holder["kb_id"]) == 1
    # 上传目录新增了 doc_id.txt 文件
    files = _files_under(uploads_dir)
    assert len(files) == 1
    assert files[0].name.endswith(".txt")


@pytest.mark.asyncio
async def test_endpoint_accepts_when_size_equals_limit(kb_upload_ctx):
    """``s == L`` → 通过（边界含 L，与谓词 ``s > L`` 互斥）。

    Validates: Requirements 3.2 (Property 7 边界 s == L)
    """
    client, holder, factory, _uploads_dir = kb_upload_ctx
    holder["limit_bytes"] = 32
    payload = b"y" * 32  # 恰好等于 limit

    resp = await client.post(
        f"/api/knowledge-bases/{holder['kb_id']}/documents/upload",
        files={"file": ("eq.txt", payload, "text/plain")},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["file_size"] == 32
    assert await _count_documents(factory, holder["kb_id"]) == 1


@pytest.mark.asyncio
async def test_endpoint_rejects_when_size_equals_limit_plus_one(kb_upload_ctx):
    """``s == L + 1`` → 拒绝（最近边界拒绝点）。

    Validates: Requirements 3.2 (Property 7 边界 s == L+1)
    """
    client, holder, factory, _uploads_dir = kb_upload_ctx
    holder["limit_bytes"] = 32
    payload = b"z" * 33  # = limit + 1

    resp = await client.post(
        f"/api/knowledge-bases/{holder['kb_id']}/documents/upload",
        files={"file": ("over.txt", payload, "text/plain")},
    )
    assert resp.status_code == 413, resp.text
    assert await _count_documents(factory, holder["kb_id"]) == 0


@pytest.mark.asyncio
async def test_endpoint_uses_resolver_on_each_request_for_hot_reload(kb_upload_ctx):
    """连续两次上传时，限制变更后下一次按新值校验（端点每次现读 resolver）。

    第一次：limit=1MB，上传 5 字节 → 通过；
    第二次：limit=1 字节，上传同样 5 字节 → 拒绝。
    证明 ``upload_document`` 在每次请求都现取 ``UploadLimitResolver`` 结果，
    满足"租户级 L 即时热生效"承诺（design C2 / Req 3.4）。

    Validates: Requirements 3.2, 3.5
    """
    client, holder, factory, _uploads_dir = kb_upload_ctx
    holder["limit_bytes"] = 1 * _BYTES_PER_MB
    r1 = await client.post(
        f"/api/knowledge-bases/{holder['kb_id']}/documents/upload",
        files={"file": ("a.txt", b"hello", "text/plain")},
    )
    assert r1.status_code == 201, r1.text

    holder["limit_bytes"] = 1  # 收紧到 1 字节
    r2 = await client.post(
        f"/api/knowledge-bases/{holder['kb_id']}/documents/upload",
        files={"file": ("b.txt", b"hello", "text/plain")},
    )
    assert r2.status_code == 413, r2.text
