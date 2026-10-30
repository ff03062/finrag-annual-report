import redis
import os
from dotenv import load_dotenv

load_dotenv()

def get_redis_conn():
    kwargs = {
        'host': os.getenv('REDIS_HOST'),
        'port': int(os.getenv('REDIS_PORT')),
        'password': os.getenv('REDIS_PASSWORD'),
        'db': int(os.getenv('REDIS_DB')),
        'decode_responses': True
    }
    return redis.Redis(**kwargs)

def close_redis_connection(conn):
    conn.close()