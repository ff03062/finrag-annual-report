import pymysql
import os
from dotenv import load_dotenv

load_dotenv()


def get_mysql_conn():
    return pymysql.connect(
        host=os.getenv('MYSQL_HOST'),
        port=int(os.getenv('MYSQL_PORT')),
        user=os.getenv('MYSQL_USER'),
        password=os.getenv('MYSQL_PASSWORD'),
        database=os.getenv('MYSQL_DATABASE'),
        charset=os.getenv('MYSQL_CHARSET'),
        cursorclass=pymysql.cursors.DictCursor
    )


def get_mysql_connection():
    return get_mysql_conn()


def close_connection(cur, conn):
    cur.close()
    conn.close()

