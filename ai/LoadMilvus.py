"""Milvus 连接单例。"""
from __future__ import annotations

import os
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


@lru_cache(maxsize=1)
def load_milvus_client():
    from pymilvus import MilvusClient
    uri = os.getenv("MILVUS_ENDPOINT", "http://127.0.0.1:19530")
    return MilvusClient(uri=uri)
