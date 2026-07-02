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

import asyncio
import logging
import time
from datetime import datetime

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    _BEARER_PREFIX,
    get_db_session,
    require_authenticated,
    resolve_identity_from_credentials,
)
from app.api.document import _ALLOWED_EXTENSIONS
from app.api.errors import (
    AppError,
    CrossTenantError,
    MissingExternalUserIdError,
    UnauthenticatedError,
)
from app.api.validators import NameValidationError, validate_filename
from app.auth.constants import HEADER_EXTERNAL_USER_ID, HEADER_TENANT_ID
from app.auth.identity import IdentityContext
from app.schema.db import ChatSession
from app.session_upload.events import get_event_hub
from app.session_upload.limits import get_upload_limit_resolver
from app.session_upload.service import (
    SessionFileVO,
    get_session_upload_service,
)

logger = logging.getLogger(__name__)

# WebSocket close codes（4xxx 应用自定义区，见 design C6）
WS_CLOSE_UNAUTHENTICATED = 4401  # 未认证 / token 无效或过期
WS_CLOSE_MISSING_EXTERNAL_USER = 4400  # external_agent 缺 external_user_id
WS_CLOSE_MUST_CHANGE_PASSWORD = 4403  # 强制改密未完成
WS_CLOSE_SESSION_FORBIDDEN = 4404  # 会话不存在 / 非本人（存在性非泄露）
WS_CLOSE_TOO_MANY_CONNECTIONS = 4429  # 单会话连接数超限

# 服务端心跳间隔兜底默认值（秒）。配置项 session_upload_ws_ping_interval
# 由任务 9.1 落地；此处 getattr 防御式读取，避免硬依赖尚未落地的配置。
_DEFAULT_WS_PING_INTERVAL = 30

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
    status: str = Field(..., description="文件状态：queued / processing / completed / failed")
    progress: int = Field(0, description="建索引进度（0-100），轮询兜底与 WS 断线重连对账入口（REQ-8）")
    progress_message: str | None = Field(None, description="当前阶段人类可读描述（如「正在解析与切分」）")
    error_message: str | None = Field(None, description="失败原因（status=failed 时填充，其余为 null）")
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
            progress=vo.progress,
            progress_message=vo.progress_message,
            error_message=vo.error_message,
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
    status_code=202,
    summary="上传会话文件（秒回，后台异步建索引）",
    description=(
        "在指定会话内上传单个文件：系统在请求内完成 会话归属校验（仅本人）→ 文件名 /"
        " 扩展名校验 → 文件大小闸门（租户级 ``Upload_File_Size_Limit``）→ 存 MinIO 原件"
        " → 建 ``SessionFile(status=queued)`` 行 → 入队后台建索引，随后**立即返回**"
        " ``202 Accepted``（不等待解析 / 切分 / 向量化）。响应体含 ``file_id / session_id"
        " / filename / status=queued``；后续建索引进展经 WebSocket"
        " ``.../files/events`` 推送、或 ``GET .../files`` 列表轮询兜底。文件大小超限即时"
        " 返回 413（不入队、不留 MinIO 残留）；Redis 队列 / MinIO 不可用返回 503（快速"
        "失败，不建 DB 行）。"
    ),
)
async def upload_session_file(
    session_id: str,
    file: UploadFile = File(..., description="待上传文件（multipart/form-data）"),
    identity: IdentityContext = Depends(require_authenticated()),
    db: AsyncSession = Depends(get_db_session),
) -> SessionFileResponse:
    """上传会话文件（秒回 + 后台异步建索引，REQ-1）。

    归属校验 → 文件名 / 扩展名校验 → 生效限制快照 → 委托 ``SessionUploadService.enqueue_upload``
    （其内部按快照判定文件大小闸门 → 存 MinIO → 建 ``SessionFile(queued)`` 行 → 入队），
    立即返回 ``status=queued`` 的 VO，不在请求内建索引。

    错误映射：

    - ``FileTooLargeError`` → 413（经 ``register_exception_handlers`` 全局处理，不在此捕获）。
    - ``RuntimeError``（Redis 队列 / MinIO 不可用 / 入队失败）→ 503（快速失败，不留残留）。
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

    # 读取文件内容（限制求解后由 service 按字节长度精确判定大小闸门）
    content = await file.read()

    # 生效限制快照：单次校验全程复用（Req 9.3），避免半途配置变更导致前后口径不一致
    limits = await get_upload_limit_resolver().resolve(identity.tenant_id)

    # 委托给 SessionUploadService.enqueue_upload：大小闸门 → 存 MinIO → 建 queued 行 → 入队 → 秒回
    service = get_session_upload_service()
    try:
        vo = await service.enqueue_upload(
            session_id=session_id,
            tenant_id=identity.tenant_id,
            owner_user_id=identity.acting_subject_id,
            filename=filename,
            content=content,
            limits=limits,
        )
    except RuntimeError:
        # 队列 / 对象存储不可用 / 入队失败：快速失败，不留残留（FileTooLargeError 不在此捕获，
        # 交由全局 413 处理器）。
        raise HTTPException(
            status_code=503, detail="服务暂时不可用，请稍后重试"
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


@router.get(
    "/{file_id}/raw",
    summary="获取会话文件原件",
    description=(
        "返回会话内某文件的原始内容（用于原件在线预览/下载）。源文件存于对象存储，"
        "流式透传。仅本人可访问。"
    ),
)
async def get_session_file_raw(
    session_id: str,
    file_id: str,
    identity: IdentityContext = Depends(require_authenticated()),
    db: AsyncSession = Depends(get_db_session),
) -> Response:
    """返回会话文件原件（用于原件在线预览/下载）。"""
    await _verify_session_owner(db, session_id, identity)
    service = get_session_upload_service()
    try:
        data, filename, ext = await service.get_file_raw(
            session_id=session_id, file_id=file_id
        )
    except ValueError:
        # 不存在 / 不属于该会话 → 404（存在性非泄露）
        raise CrossTenantError()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="原始文件不存在")
    except RuntimeError:
        raise HTTPException(status_code=503, detail="对象存储不可用")

    from urllib.parse import quote

    media_type_map = {
        "pdf": "application/pdf",
        "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
        "txt": "text/plain; charset=utf-8", "md": "text/markdown; charset=utf-8",
        "csv": "text/csv; charset=utf-8",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }
    media_type = media_type_map.get(ext, "application/octet-stream")
    disposition = f"inline; filename*=UTF-8''{quote(filename)}"
    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": disposition},
    )


# ============================================================
# WebSocket 路由：会话上传状态实时推送
# ============================================================


@router.websocket("/events")
async def session_files_events(websocket: WebSocket, session_id: str) -> None:
    """会话上传状态 WebSocket 推送（REQ-4 / REQ-5 / REQ-7 / REQ-8）。

    浏览器 WebSocket 无法自定义 ``Authorization`` 头，故凭据优先从 query
    ``access_token`` 取，回退到 ``Authorization: Bearer`` 头（大小写不敏感，与
    ``deps._extract_bearer`` 同款）。external_agent 代理场景经 query
    ``external_user_id`` / ``tenant_id`` 注入对应请求头供
    ``resolve_identity_from_credentials`` 解析。

    鉴权与归属校验全部发生在 ``accept()`` **之前**：任一不满足即以 4xxx 应用自定义
    close code 关闭握手（见 design C6），前端据 code 区分「重登录 / 缺 external_user_id
    / 先改密 / 无权 / 连接过多」。校验通过后 register→accept→推 snapshot→进入心跳循环，
    ``finally`` 中 unregister（幂等），确保连接生命周期内 EventHub 映射不泄漏。

    close code 矩阵：
        4401 未认证 / token 无效或过期（缺 token 亦归此）
        4400 external_agent 缺 external_user_id
        4403 强制改密未完成
        4404 会话不存在 / 非本人（存在性非泄露）
        4429 单会话连接数超限
    """
    from app.config import get_settings
    from app.storage.database import async_session

    # 1) 提取 token：query access_token 优先，回退 Authorization: Bearer 头
    token = websocket.query_params.get("access_token")
    if not token:
        auth = websocket.headers.get("Authorization")
        if auth and auth[: len(_BEARER_PREFIX)].lower() == _BEARER_PREFIX:
            token = auth[len(_BEARER_PREFIX):].strip()
    if not token:
        # 无任何凭据：未认证（accept 前关闭）
        await websocket.close(code=WS_CLOSE_UNAUTHENTICATED)
        return

    # 2) 组装凭据解析用 headers：以握手头为底，按 query 注入 external_user_id / tenant_id
    #    （浏览器无法自定义头，这些身份要素只能经 query 传入，用精确的 HEADER_* 键注入）。
    headers = dict(websocket.headers)
    external_user_id = websocket.query_params.get("external_user_id")
    if external_user_id:
        headers[HEADER_EXTERNAL_USER_ID] = external_user_id
    tenant_id = websocket.query_params.get("tenant_id") or websocket.query_params.get(
        "x_tenant_id"
    )
    if tenant_id:
        headers[HEADER_TENANT_ID] = tenant_id

    hub = get_event_hub()
    registered = False
    try:
        async with async_session() as session:
            # 3) 凭据解析：区分 4400（缺 external_user_id）与 4401（其余认证失败）
            try:
                identity, must_change = await resolve_identity_from_credentials(
                    token, headers, session
                )
            except MissingExternalUserIdError:
                await websocket.close(code=WS_CLOSE_MISSING_EXTERNAL_USER)
                return
            except (UnauthenticatedError, AppError):
                await websocket.close(code=WS_CLOSE_UNAUTHENTICATED)
                return

            # 4) 强制改密闸门（与 HTTP 侧 _must_change_gate 同语义）
            if must_change:
                await websocket.close(code=WS_CLOSE_MUST_CHANGE_PASSWORD)
                return

            # 5) 会话归属校验（复用 HTTP 侧同款；非本人统一 4404，存在性非泄露）
            try:
                await _verify_session_owner(session, session_id, identity)
            except CrossTenantError:
                await websocket.close(code=WS_CLOSE_SESSION_FORBIDDEN)
                return

        # 6) 连接注册（accept 之前）：超限直接 4429，避免完成握手后再驱逐
        if hub is not None:
            registered = await hub.register(session_id, websocket)
            if not registered:
                await websocket.close(code=WS_CLOSE_TOO_MANY_CONNECTIONS)
                return

        # 7) 握手完成
        await websocket.accept()

        # 8) 推 snapshot：断线重连对账入口（REQ-8），一次性下发当前全量文件状态
        service = get_session_upload_service()
        files = await service.list_files(session_id)
        snapshot = {
            "type": "snapshot",
            "session_id": session_id,
            "files": [
                {
                    "file_id": f.id,
                    "filename": f.filename,
                    "status": f.status,
                    "progress": f.progress,
                    "progress_message": f.progress_message,
                    "error_message": f.error_message,
                    "chunk_count": f.chunk_count,
                }
                for f in files
            ],
        }
        await websocket.send_json(snapshot)

        # 9) 心跳循环：空闲超过 ping_interval 则下发服务端 keepalive；客户端消息（含
        #    心跳应答）读取后忽略。断开或写失败即退出循环，交 finally 注销。
        ping_interval = getattr(
            get_settings(), "session_upload_ws_ping_interval", _DEFAULT_WS_PING_INTERVAL
        )
        while True:
            try:
                # 忽略客户端消息内容：仅用于探测连接存活 / 触发超时下的 keepalive
                await asyncio.wait_for(
                    websocket.receive_text(), timeout=ping_interval
                )
            except asyncio.TimeoutError:
                # 空闲：下发服务端 keepalive，探测连接可写性
                try:
                    await websocket.send_json({"type": "ping", "ts": time.time()})
                except Exception:
                    break
            except WebSocketDisconnect:
                break
            except Exception:
                # 其它读写异常：连接已不可用，退出循环由 finally 注销
                break
    finally:
        # 10) 幂等注销：仅当成功注册过才注销（未注册/降级路径无副作用）
        if hub is not None and registered:
            await hub.unregister(session_id, websocket)
