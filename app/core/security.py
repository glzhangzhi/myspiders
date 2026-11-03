from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

from app.core.settings import settings


api_key_scheme = APIKeyHeader(name="x-token", auto_error=False, description="管理员令牌")


def require_admin_token(api_key: str | None = Security(api_key_scheme)) -> str:
    """简单的 Header 校验，保护后台接口。"""

    if not settings.admin_token:
        raise HTTPException(status_code=500, detail="管理员令牌未配置")
    if not api_key or api_key != settings.admin_token:
        raise HTTPException(status_code=401, detail="invalid admin token")
    return api_key
