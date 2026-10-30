from chat.dao import HistoryDao

# 查询历史记录菜单栏
def query_history_menu(username):
    # 获取结果
    results = HistoryDao.query_history_menu(username)
    # 包装结果
    data_list = []
    for item in results:
        data_list.append({
            "historyId": item['history_id'],
            "title": item['question'],
            "time": item['create_time'].strftime("%Y-%m-%d %H:%M:%S"),
        })
    return {
        "code": 200,
        "msg": "查询成功",
        "data": data_list
    }


# 查询某一条详细对话记录
def conversation_log(historyId:int,username:str):
    results = HistoryDao.conversation_log(historyId,username)
    data_list = []
    for item in results:
        data_list.append({
            "role": "user",
            "content": item['question'],
        })
        data_list.append({
            "role": "assistant",
            "content": item['answer'],
        })
    return {
        "code": 200,
        "msg": "查询成功",
        "data": data_list
    }


if __name__ == '__main__':
    print(conversation_log(1,'cy'))
