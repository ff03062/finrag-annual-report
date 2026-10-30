from fastapi import APIRouter
from fastapi import Depends
from users.utils.JWTDecode import get_current_user

from chat.service import HistoryService

history_router = APIRouter()


@history_router.get(
    path="/queryHistoryMenu",
    summary="查询历史记录菜单栏",
)
def query_history_menu(current_user: dict = Depends(get_current_user)):
    username = current_user.get("username")
    return HistoryService.query_history_menu(username)


@history_router.get(
    path="/conversationLog",
    summary="查询某一条详细对话记录",
)
def conversation_log(historyId: int,current_user: dict = Depends(get_current_user)):
    username = current_user.get("username")
    return HistoryService.conversation_log(historyId,username)



