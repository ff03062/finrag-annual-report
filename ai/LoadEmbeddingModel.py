from functools import lru_cache

from langchain_huggingface import HuggingFaceEmbeddings
import os
from dotenv import load_dotenv
load_dotenv()

# 模型的 sentence_bert_config.json 里 max_seq_length=128，但 config 的位置编码支持 512。
# 128 token 只够 ~168 个汉字，长表格/段落会被直接截断，所以这里按 env 放大到 256。
# create_data/chunking.py 用同一个 env 切块，两边必须一致。
EMBEDDING_MAX_SEQ_LENGTH = int(os.getenv('EMBEDDING_MAX_SEQ_LENGTH', '256'))


def _resolve_device() -> str:
    """优先使用环境变量；未显式配置时按 CUDA 可用性选择，避免在 CPU 机器（如 Docker）上直接崩"""
    device = os.getenv('EMBEDDING_DEVICE')
    if device:
        return device
    try:
        import torch
        return 'cuda' if torch.cuda.is_available() else 'cpu'
    except ImportError:
        return 'cpu'


# 向量化模型只加载一次（模型加载是秒级操作，绝不能放在每次请求里）
@lru_cache(maxsize=1)
def load_embedding_model():
    model = HuggingFaceEmbeddings(
        model_name=os.getenv('EMBEDDING_MODEL_PATH'),
        model_kwargs={
            'device': _resolve_device(),
            'local_files_only': True,
        },
    )
    # HuggingFaceEmbeddings 内部是 SentenceTransformer，直接改它的编码窗口
    client = getattr(model, '_client', None)
    if client is not None and hasattr(client, 'max_seq_length'):
        client.max_seq_length = EMBEDDING_MAX_SEQ_LENGTH
    return model
