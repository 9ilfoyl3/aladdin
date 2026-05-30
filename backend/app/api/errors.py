"""统一异常与全局异常处理（tenant-auth）。

对齐团队规范"Controller 层禁止 try-catch，由 GlobalExceptionHandler 统一处理"的精神：
鉴权相关异常由 Guard / 授权判定函数抛出，路由层不再各自 try-catch 兜鉴权。

状态码语义（安全设计的一部分，三处收敛点必须一致）：
- 401 未认证：无凭据 / 凭据无效 / JWT 过期或签名错 / Key 不存在或被撤销
- 404 跨租户 / 不在可读范围：**存在性非泄露**，与"资源不存在"不可区分
- 403 已认证但权限不足 / 租户或用户停用 / must_change_password 闸门 / api_key 触达管理或平台操作
- 400 输入非法：非法 grantee_type 预留值 / 代理 Key 缺 X-External-User-Id
"""

from __future__ import annotations

from enum import Enum

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class ErrorCodeEnum(str, Enum):
    """业务错误码（禁止硬编码字符串/数字散落各处）。"""

    UNAUTHENTICATED = "unauthenticated"
    CROSS_TENANT = "cross_tenant"          # 对外表现为 404
    NOT_FOUND = "not_found"
    PERMISSION_DENIED = "permission_denied"
    TENANT_DISABLED = "tenant_disabled"
    USER_DISABLED = "user_disabled"
    MUST_CHANGE_PASSWORD = "must_change_password"
    INVALID_GRANTEE_TYPE = "invalid_grantee_type"
    MISSING_EXTERNAL_USER_ID = "missing_external_user_id"
    BAD_REQUEST = "bad_request"


class AppError(Exception):
    """业务异常基类。携带错误码与 HTTP 状态码，由全局 handler 统一转响应。"""

    code: ErrorCodeEnum = ErrorCodeEnum.BAD_REQUEST
    http_status: int = 400
    # 默认对外文案。跨租户/不存在必须使用**相同**文案以保证存在性非泄露。
    default_detail: str = "请求无法处理"

    def __init__(self, detail: str | None = None):
        self.detail = detail or self.default_detail
        super().__init__(self.detail)


class UnauthenticatedError(AppError):
    code = ErrorCodeEnum.UNAUTHENTICATED
    http_status = 401
    default_detail = "未认证或凭据无效"


class CrossTenantError(AppError):
    """跨租户访问 / 资源不存在 / 不在可读范围——统一 404 且文案一致（存在性非泄露）。"""

    code = ErrorCodeEnum.CROSS_TENANT
    http_status = 404
    default_detail = "资源不存在"


class NotFoundError(AppError):
    """普通不存在。文案与 CrossTenantError 保持一致，攻击者无法区分。"""

    code = ErrorCodeEnum.NOT_FOUND
    http_status = 404
    default_detail = "资源不存在"


class PermissionDeniedError(AppError):
    code = ErrorCodeEnum.PERMISSION_DENIED
    http_status = 403
    default_detail = "权限不足"


class TenantDisabledError(AppError):
    code = ErrorCodeEnum.TENANT_DISABLED
    http_status = 403
    default_detail = "租户已停用"


class UserDisabledError(AppError):
    code = ErrorCodeEnum.USER_DISABLED
    http_status = 403
    default_detail = "账号已停用"


class MustChangePasswordError(AppError):
    code = ErrorCodeEnum.MUST_CHANGE_PASSWORD
    http_status = 403
    default_detail = "请先修改初始口令后再操作"


class InvalidGranteeTypeError(AppError):
    code = ErrorCodeEnum.INVALID_GRANTEE_TYPE
    http_status = 400
    default_detail = "不支持的被授权主体类型"


class MissingExternalUserIdError(AppError):
    code = ErrorCodeEnum.MISSING_EXTERNAL_USER_ID
    http_status = 400
    default_detail = "缺少 X-External-User-Id 请求头"


def register_exception_handlers(app: FastAPI) -> None:
    """注册全局异常处理：把 AppError 统一转为 {"detail": ...} 响应。"""

    @app.exception_handler(AppError)
    async def _handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"detail": exc.detail})
