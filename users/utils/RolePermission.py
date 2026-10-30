# 验证用户是否有访问这个接口的权限
from fastapi import Header, HTTPException
from users.utils.JWTDecode import get_current_user

# allowed_rols：允许访问的用户角色列表
def has_roles(*allowed_roles: list):
    print(allowed_roles)

    # 判定当前用户是否有访问权限
    def user_permission(authorization: str = Header(None)):
        # 获取请求头中的Authorization内容
        token = authorization.split(" ")[1]
        # 获取当前用户信息
        current_user = get_current_user(token)
        print(token)
        print(current_user)
        # 判定
        if current_user['role_name'] in allowed_roles[0]:
            return current_user
        else:
            raise HTTPException(status_code=403, detail="无访问权限")

    return user_permission
