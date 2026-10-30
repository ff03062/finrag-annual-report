from fastapi import APIRouter
users_router = APIRouter()

from users.service import users_service   # 导入 service 模块
from users.entity.users_entity import CheckCodeEntity


@users_router.get(
    path="/sendEmail",
    # 接口标题，显示在 Swagger UI 的接口列表中
    summary="发送邮箱",
    # 接口详细描述，显示在 Swagger UI 的接口详情中
    description="""
        给用户输入的邮箱号发送验证码
        访问路径：http://localhost:8000/users/sendEmail
        请求参数：
            email：字符串类型，用户输入的邮箱号
        返回值：
            {
                "code": 状态码，成功200、失败500，int
                "msg": 提示信息，字符串
                "data": None，数据内容，Object
            }
    """,
)
def send_email(email:str):
    return users_service.send_email(email)

@users_router.post(
    path="/checkCode",
    summary="检查验证码",
    description="""
        检查用户输入的验证码是否正确
        访问路径：http://localhost:8000/users/checkCode
        请求参数：
            CheckCodeEntity：一个对象，2个属性：
                email：邮箱号，用于取出redis中存入的值作为key
                code：验证码，用户输入的
        返回值：
            {
                "code": 状态码，成功200、失败500，int
                "msg": 提示信息，字符串
                "data": None，数据内容，Object
            }
    """,
)
def check_code(checkCodeEntity:CheckCodeEntity):
    return users_service.check_code(checkCodeEntity)

