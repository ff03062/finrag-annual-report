from functools import lru_cache

from FlagEmbedding import FlagReranker
import os
from dotenv import load_dotenv

load_dotenv()


def _use_fp16() -> bool:
    """CPU 上不能开 fp16，按实际设备决定（Docker 里通常没有 GPU）"""
    env = os.getenv("RERANKER_USE_FP16")
    if env is not None:
        return env.lower() == "true"
    try:
        import torch
        return torch.cuda.is_available()
    except ImportError:
        return False


# Reranker 是 1G+ 的大模型，必须全局只加载一次
@lru_cache(maxsize=1)
def load_rerank_model():
    return FlagReranker(
        model_name_or_path=os.getenv("RERANKER_MODEL_PATH"),
        use_fp16=_use_fp16()
    )
