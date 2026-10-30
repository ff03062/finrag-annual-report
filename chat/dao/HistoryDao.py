from common import mysqlConn


# 查询历史记录菜单栏
def query_history_menu(username):
    conn = mysqlConn.get_mysql_connection()
    cur = conn.cursor()
    sql = "SELECT history_id, question, create_time FROM history WHERE username=%s AND parent_id=0;"
    cur.execute(sql, [username])
    results = cur.fetchall()
    mysqlConn.close_connection(cur, conn)
    return results


# 查询某一条详细对话记录
def conversation_log(historyId,username):
    conn = mysqlConn.get_mysql_connection()
    cur = conn.cursor()
    sql = "SELECT question, answer FROM history WHERE username=%s AND (history_id=%s OR parent_id=%s) ORDER BY history_id ASC;"
    cur.execute(sql, [username,historyId, historyId])
    results = cur.fetchall()
    mysqlConn.close_connection(cur, conn)
    return results


if __name__ == '__main__':
    print(conversation_log(1, 'cy'))
