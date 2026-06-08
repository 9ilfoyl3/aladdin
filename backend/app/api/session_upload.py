"""会话级文件上传 API（design C8 / Task 8）

会话内文件上传（同步建索引）/ 文件列表 / 移除单文件三件套，承载 ``session-file-upload``
特性"传一个合同立刻问它"的对外入口。所有端点均在 ``/api/sessions/{session_id}/files``
前缀下，鉴权由 ``require_authenticated`` 完成、会话归属由 ``_verify_session_owner``
收敛（仅会话所有者本人可上传 / 列出 / 移除，会话是个人对话历史，``super_admin`` 不
在此处特殊放行——Req 1.11）。

路由层遵守团队规范"Controller 层禁止 try-catch"：

- 上传超限（文件大小 / 累计 chunk）由 ``FileTooLargeError`` / ``UploadCapExceeded``
  抛出，经 ``register_exception_handlers`` 统一转 413。
- 会话不存在 / 非本人 → ``CrossTenantError``（404，存在性非泄露，与 ``app/api/session.py``
  的 ``_get_owned_session`` 同语义）。
- 文件名 / 扩展名校验复用 ``validate_filename`` 与 ``app/api/document.py`` 的
  ``_ALLOWED_EXTENSIONS`` 同款策略，KB 与会话上传接受同一集合。

文件上传 / 同步建索引的核心逻辑（落盘 → pipeline 解耦单元 → Pre_Embed_Gate → 写
共享 collection + 关系表 → 失效广播）由 ``SessionUploadService.upload`` 承担，
本模块只做参数校验、归属校验、限制求解快照（一次性，全程复用）与 VO 转换。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db_session, require_authenticated
from app.api.document import _ALLOWED_EXTENSIONS
from app.api.errors import CrossTenantError
from app.api.validators import NameValidationError, validate_filename
from app.auth.identity import IdentityContext
from app.schema.db import ChatSession
from app.session_upload.limits import get_upload_limit_resolver
from app.session_upload.service import (
    SessionFileVO,
    get_session_upload_service,
)

router = APIRouter(
    prefix="/api/sessions/{session_id}/files",
    tags=["SessionUpload"],
)


# ============================================================
# 响应模型（VO，与 SessionFileVO 字段对齐；保留独立模型方便后续不破坏内部 dataclass）
# ============================================================


class SessionFileResponse(BaseModel):
    """会话上传文件响应（VO）。"""

    model_config = {"from_attributes": True}

    id: str = Field(..., description="文件 ID（亦为 Milvus doc_id）")
    session_id: str = Field(..., description="所属会话 ID")
    filename: str = Field(..., description="原始文件名")
    file_type: str | None = Field(None, description="文件类型扩展名（小写，无点）")
    file_size: int | None = Field(None, description="文件字节数")
    chunk_count: int = Field(..., description="该文件 child chunk 数（计入会话累计配额）")
    status: str = Field(..., description="文件状态：processing / completed / failed")
    created_at: datetime = Field(..., description="上传完成时间")

    @classmethod
    def from_vo(cls, vo: SessionFileVO) -> "SessionFileResponse":
        """``SessionFileVO`` -> 响应模型。dataclass 不被 ``from_attributes`` 自动展开，
        显式映射避免 pydantic 配置坑。"""
        return cls(
            id=vo.id,
            session_id=vo.session_id,
            filename=vo.filename,
            file_type=vo.file_type,
            file_size=vo.file_size,
            chunk_count=vo.chunk_count,
            status=vo.status,
            created_at=vo.created_at,
        )


# ============================================================
# 内部辅助
# ============================================================


async def _verify_session_owner(
    db: AsyncSession, session_id: str, identity: IdentityContext
) -> ChatSession:
    """校验当前身份对该会话的所有权。

    与 ``app/api/session.py`` 的 ``_get_owned_session`` 同语义（按
    ``owner_user_id == identity.acting_subject_id`` 收敛），但参数顺序贴合本模块
    依赖装配（路由层先取 db / session_id / identity，再校验）。

    会话是个人对话历史（Req 1.11），归属校验**不放行 super_admin**：

    - 会话不存在 / 跨租户 / 非本人 → 一律 ``CrossTenantError``（404，与"资源不存在"
      不可区分，存在性非泄露）。
    - ``acting_subject_id`` 为 None 的 tenant_level 机器身份不绑定自然人，无任何
      个人会话归属，全部视为 404。

    Args:
        db: 请求级 DB 会话（``get_db_session`` 注入）。
        session_id: 路径参数中的会话 ID。
        identity: 鉴权通过后的身份上下文。

    Returns:
        归属校验通过的 ``ChatSession`` ORM 对象。

    Raises:
        CrossTenantError: 会话不存在 / 非本人。
    """
    result = await db.execute(
        select(ChatSession).where(ChatSession.id == session_id)
    )
    cs = result.scalar_one_or_none()
    if cs is None:
        raise CrossTenantError()
    subject = identity.acting_subject_id
    if subject is None or cs.owner_user_id != subject:
        raise CrossTenantError()
    return cs


def _validate_extension(filename: str) -> str:
    """提取并校验文件扩展名（小写、无点）。

    会话上传与 KB 上传共用 ``_ALLOWED_EXTENSIONS``（``app/api/document.py``），保证
    用户对"哪些类型可上传"在两条入口上认知一致，避免出现"KB 能传但会话不能"。
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in _ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"不支持的文件类型: {ext}，"
                f"支持: {', '.join(sorted(_ALLOWED_EXTENSIONS))}"
            ),
        )
    return ext


# ============================================================
# 路由实现
# ============================================================


@router.post(
    "",
    response_model=SessionFileResponse,
    status_code=201,
    summary="上传会话文件并同步建索引",
    description=(
        "在指定会话内上传单个文件，系统同步执行 解析 → 切分 → 向量化 → 写入共享"
        " ``kb_session_files`` collection。上传前依次校验：会话归属（仅本人）、"
        "文件名 / 扩展名、文件大小（租户级 ``Upload_File_Size_Limit``）；Chunk 后由"
        " Pre_Embed_Gate 用精确 child chunk 数判定累计是否超 ``kb_chunk_cap``（临时文件与"
        "知识库共用同一上限）。任一闸门触发即返回明确提示，不写入向量。"
    ),
)
async def upload_session_file(
    session_id: str,
    file: UploadFile = File(..., description="待上传文件（multipart/form-data）"),
    identity: IdentityContext = Depends(require_authenticated()),
    db: AsyncSession = Depends(get_db_session),
) -> SessionFileResponse:
    """上传会话文件（同步建索引）。

    Req 1.1 / 1.8 / 1.9 / 1.10 / 1.11 一站式入口：归属校验 → 文件名 / 扩展名校验 →
    生效限制快照 → 委托 ``SessionUploadService.upload``（其内部按快照判定文件大小 /
    累计文件数 / Pre_Embed_Gate 累计 chunk）。
    """
    # 归属校验：会话存在 + 仅本人；非本人统一 404（存在性非泄露，Req 1.11）
    await _verify_session_owner(db, session_id, identity)

    # 文件名校验（去首尾空格 / 长度 / 禁止字符 / 保留名）
    raw_name = file.filename or "unknown"
    try:
        filename = validate_filename(raw_name)
    except NameValidationError as e:
        raise HTTPException(status_code=422, detail=e.message)

    # 扩展名校验（与 KB 上传共用 _ALLOWED_EXTENSIONS）
    _validate_extension(filename)

    # 读取文件内容（同步上传路径不分块流式，限制求解后由 service 按字节长度精确判定大小）
    content = await file.read()

    # 生效限制快照：单次校验全程复用（Req 9.3），避免半途配置变更导致前后口径不一致
    limits = await get_upload_limit_resolver().resolve(identity.tenant_id)

    # 委托给 SessionUploadService：文件大小 / 累计文件数 / Pre_Embed_Gate / 写库 / 失效广播
    service = get_session_upload_service()
    vo = await service.upload(
        session_id=session_id,
        tenant_id=identity.tenant_id,
        owner_user_id=identity.acting_subject_id,
        filename=filename,
        content=content,
        limits=limits,
    )
    return SessionFileResponse.from_vo(vo)


@router.get(
    "",
    response_model=list[SessionFileResponse],
    summary="列出会话已上传文件",
    description="返回当前会话已上传文件列表（仅本人可见），按上传时间倒序（最新在前）。",
)
async def list_session_files(
    session_id: str,
    identity: IdentityContext = Depends(require_authenticated()),
    db: AsyncSession = Depends(get_db_session),
) -> list[SessionFileResponse]:
    """列出会话已上传文件（Req 1.8）。"""
    await _verify_session_owner(db, session_id, identity)
    service = get_session_upload_service()
    files = await service.list_files(session_id)
    return [SessionFileResponse.from_vo(f) for f in files]


@router.delete(
    "/{file_id}",
    status_code=204,
    summary="移除会话内单个文件",
    description=(
        "按文件 ID 移除当前会话内单个上传文件：删 Milvus 向量（其余文件不受影响）+ 删"
        " ``session_files`` / ``session_chunks`` 行（释放文件数 / chunk 配额）+ publish"
        " 失效广播。仅本人可移除。"
    ),
)
async def remove_session_file(
    session_id: str,
    file_id: str,
    identity: IdentityContext = Depends(require_authenticated()),
    db: AsyncSession = Depends(get_db_session),
) -> None:
    """移除会话内单个文件（Req 1.8 / 6.7）。

    会话归属校验先行；``service.remove_file`` 内部再校验 ``file_id`` 属于该会话
    （防止跨会话误删，对未知 file_id 报错亦保持存在性非泄露语义——以 404 返回）。
    """
    await _verify_session_owner(db, session_id, identity)
    service = get_session_upload_service()
    try:
        await service.remove_file(session_id=session_id, file_id=file_id)
    except ValueError:
        # service 对"文件不存在 / 不属于该会话"统一抛 ValueError；归属语义对外按
        # 404（存在性非泄露，与会话不存在同文案，复用 CrossTenantError 全局映射）。
        raise CrossTenantError()
    return None
