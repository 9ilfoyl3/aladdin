"""API Key 认证中间件

拦截 /v1/ 路径下的请求，验证 Authorization: Bearer sk-xxx 头。
- /v1/ 路径：需要有效 API Key
- /api/ 路径：管理接口，无需认证
- / 根路径：健康检查，无需认证
"""

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.auth import verify_key
from app.storage.database import async_session


class ApiKeyAuthMiddleware(BaseHTTPMiddleware):
    """API Key 认证中间件

    仅对 /v1/ 前缀的端点进行认证检查。
    验证通过后自动递增 call_count 并更新 last_used_at。
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # 仅对 /v1/ 路径进行认证
        if not path.startswith("/v1/"):
            return await call_next(request)

        # 提取 Authorization 头
        auth_header = request.headers.get("Authorization")
        if not auth_header:
            return JSONResponse(
                status_code=401,
                content={"detail": "缺少 Authorization 头"},
            )

        # 解析 Bearer token
        parts = auth_header.split(" ", 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return JSONResponse(
                status_code=401,
                content={"detail": "Authorization 格式错误，应为 Bearer <key>"},
            )

        token = parts[1].strip()
        if not token.startswith("sk-"):
            return JSONResponse(
                status_code=401,
                content={"detail": "无效的 API Key 格式"},
            )

        # 验证 Key
        async with async_session() as session:
            api_key = await verify_key(token, session)

        if api_key is None:
            return JSONResponse(
                status_code=401,
                content={"detail": "API Key 无效或已被撤销"},
            )

        # 验证通过，继续处理请求
        return await call_next(request)
