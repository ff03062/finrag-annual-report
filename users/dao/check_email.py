from common import mysqlConn


def check_email(email):
    conn = mysqlConn.get_mysql_conn()
    cur = conn.cursor()
    sql = "select * from users where email=%s"
    cur.execute(sql, email)
    result = cur.fetchall()
    mysqlConn.close_connection(cur, conn)
    return result


if __name__ == '__main__':
    pass
    # email = '2069985674@qq.com'
    # result = check_email(email)
    # print(result)
# 打印
# [{'user_id': 1, 'username': 'cc', 'email': '2069985674@qq.com', 'create_time': datetime.datetime(2026, 7, 30, 18, 33, 11)}]
# 这是一个由列表包裹的字典
