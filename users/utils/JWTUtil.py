from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from jose import JWTError, jwt
import os
from dotenv import load_dotenv

load_dotenv()


# 生成token --- data：用户需要封装进入payload的数据内容，字典格式{k:v}
def create_access_token(data: dict) -> str:
    # 拷贝一份data
    copy_data = data.copy()
    # 设置token到什么时间过期 = 当前时间 + 设置的过期时间
    expire = datetime.now(timezone.utc) + timedelta(minutes=int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES")))
    # 修改copy_data数据，添加过期时间 --- exp: 过期时间, iat: token生成时间
    copy_data.update({
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "username": data.get("username"),
        "user_id": data.get("user_id"),
        "role_name": data.get("role_name"),
    })
    """
        本质上调这个方法的时候token生成后入库 --- 退出登录的时候应该删除掉token
    """
    # 生成token
    return jwt.encode(
        claims=copy_data,  # 数据
        key=os.getenv("SECRET_KEY"),  # 密钥
        algorithm=os.getenv("ALGORITHM"),  # 算法
    )


# 验证token
def verify_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, key=os.getenv("SECRET_KEY"), algorithms=[os.getenv("ALGORITHM")])
        return payload
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="token已过期或错误")


if __name__ == '__main__':
    token = create_access_token({"user_id": 2, "username": "zs", "role_name": "user"})
    # token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VyX2lkIjoxLCJleHAiOjE3ODYzMjg5ODAsImlhdCI6MTc4NjMyODkyMH0.sle5rJ06iBODSD60D7qXcn6SdWML0qJL2OaXd2HwAyc"
    print(token)
    print(verify_token(token))