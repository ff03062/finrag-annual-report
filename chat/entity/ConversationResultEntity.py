from pydantic import BaseModel, Field


class ConversationResultEntity(BaseModel):
    question: str = Field(..., description="问题")
    # username 由后端从 JWT 里取，不再强制前端传（前端传了也会被覆盖）
    username: str = Field("", description="用户名（后端会用 token 中的用户名覆盖）")
    parentId: int = Field(..., description="历史会话id")
    answer: str = Field(..., description="答案")
