import json
import logging

from fastapi import Depends, Request, HTTPException, status
from fastapi import APIRouter
from fastapi.security import HTTPAuthorizationCredentials
from starlette.responses import StreamingResponse

from chat.entity.ConversationResultEntity import ConversationResultEntity
from chat.service import ChatService
from users.utils.JWTDecode import bearer_scheme, get_current_user
from users.utils.JWTUtil import verify_token

logger = logging.getLogger(__name__)

chat_router = APIRouter()

# SSE 必须关掉各级缓冲，否则 nginx / 代理会把流攒成一次性响应
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

def get_current_user_optional(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> dict:
    # 1. 先从 Authorization header 取（Swagger 的 Authorize 按钮）
    token = credentials.credentials if credentials else None
    # 2. 没有就从 query 取（浏览器 EventSource 不能设置 header）
    if not token:
        token = request.query_params.get("token")
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")
    # verify_token 校验失败时会自己抛 401，不会返回 None
    return verify_token(token)


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@chat_router.get(
    path="/chat",
    summary='聊天路由',
    description='''
        聊天
        访问路径：http://localhost:8000/chat/chat
        请求参数：
            question：用户问题
            historyId：可选，历史对话 id
            token：可选，SSE 模式专用（因为浏览器原生 EventSource 无法设置 header）
        返回值：
            流式输出 sse
    '''
)
def chat(
    question: str,
    historyId: int = 0,
    current_user: dict = Depends(get_current_user_optional),
):
    username = current_user.get("username")

    def generator():
        try:
            # 原始 question 传给检索，username 单独传进 ChatService 用于 prompt 拼接
            for chunk in ChatService.chat(question, historyId, username):
                yield _sse({'content': chunk})
        except Exception as e:
            # 响应头已经发出去了，只能在流里补一个错误事件，
            # 否则前端 EventSource 会一直等不到 [DONE]，还可能自动重连把问题再问一遍
            logger.exception("聊天生成失败")
            yield _sse({'content': f'[错误] {type(e).__name__}: {e}', 'error': 'chat_failed'})
        yield _sse({'content': '[DONE]'})

    return StreamingResponse(
        content=generator(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@chat_router.get(
    path="/chunks",
    summary="查看检索库内容（调试）",
    description="""
        直接查看 Milvus 里存了哪些 chunk（不走向量检索）。
        访问路径：http://localhost:8000/chat/chunks?company=恩捷股份&keyword=生产技术&limit=5
    """
)
def chunks(company: str | None = None, keyword: str | None = None, limit: int = 10):
    from chat.utils.MilvusRetriever import list_chunks
    return {"code": 200, "data": list_chunks(company, keyword, limit)}


@chat_router.post(
    path="/saveConversationResult",
    summary="保存会话结果",
    description="""
        保存会话结果
        访问路径：http://localhost:8000/chat/saveConversationResult
    """
)
def save_conversation_result(
    conversationResultEntity: ConversationResultEntity,
    current_user: dict = Depends(get_current_user),
):
    conversationResultEntity.username = current_user.get("username")
    return ChatService.save_conversation_result(conversationResultEntity)
