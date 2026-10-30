from common import mysqlConn

"""
    查询操作不需要做事务管理
    增删改操作需要事务管理
    操作成功 --- commit 提交事务：执行当前操作
    操作失败 --- rollback 回滚事务：不执行当前操作
"""


def parent_belongs_to(parent_id, username):
    """检查 parentId 是否是当前用户名下的会话"""
    conn = mysqlConn.get_mysql_connection()
    cur = conn.cursor()
    try:
        sql = "select 1 as ok from `history` where history_id=%s and username=%s limit 1"
        cur.execute(sql, [parent_id, username])
        return cur.fetchone() is not None
    finally:
        mysqlConn.close_connection(cur, conn)


def save_conversation_result(question, username, parent_id, answer):
        conn = mysqlConn.get_mysql_connection()
        cur = conn.cursor()
        try:
            sql = "insert into `history` values(null, %s, %s, %s, %s, now())"
            cur.execute(sql, [question, username, parent_id, answer])
            conn.commit()  # 提交事务
            # 返回当前新增数据的主键自增ID -- history_id
            return cur.lastrowid
        except Exception as e:
            print(f"新增会话结果失败：{e}")
            conn.rollback()  # 回滚事务
            return 0
        finally:
            mysqlConn.close_connection(cur, conn)
