from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from users.utils.JWTUtil import create_access_token, verify_token

# Swagger 的 Authorize 按钮会显示这个 HTTPBearer 方案：
# 直接把登录返回的 token 粘进去即可（不用手动加 "Bearer " 前缀）
bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> dict:
    """解析 Authorization: Bearer <token>，返回 JWT payload"""
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")
    return verify_token(credentials.credentials)


if __name__ == '__main__':
    token = create_access_token({"user_id": 1, 'username': 'cy'})
    print(token)
    print(verify_token(token))
    print(get_current_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)))
