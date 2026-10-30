from pydantic import BaseModel, Field


class CheckCodeEntity(BaseModel):
    email: str = Field(..., title="邮箱号")
    code: str = Field(..., title="验证码")