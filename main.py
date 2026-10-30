import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI

load_dotenv()


@asynccontextmanager
async def start_end_run(app: FastAPI):
    """启动时预热模型：否则第一个问题要现加载 embedding + reranker（实测约 6 秒）"""
    print('项目启动，开始预热...')

    from ai.LoadEmbeddingModel import load_embedding_model
    from ai.LoadMilvus import load_milvus_client
    from ai.LoadRerankerModel import load_rerank_model
    from ai.LoadLLM import chat_llm
    from chat.utils.TableQuery import list_companies

    warmups = (
        ("向量化模型", load_embedding_model),
        ("Milvus 连接", load_milvus_client),
        ("Reranker 精排模型", load_rerank_model),
        ("LLM 客户端", chat_llm),
        ("结构化库", list_companies),
    )
    for name, loader in warmups:
        try:
            loader()
            print(f'  [{name}] 预热完成')
        except Exception as e:
            # 预热失败不阻塞启动，首次请求会再尝试加载一次
            print(f'  [{name}] 预热失败: {type(e).__name__}: {e}')

    yield
    print('项目关闭')


app = FastAPI(lifespan=start_end_run)

# 跨域配置：允许的前端地址用逗号分隔（默认 Vue CLI 的 8080）
from fastapi.middleware.cors import CORSMiddleware
cors_origins = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "http://localhost:8080").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from users.controller.users_router import users_router
app.include_router(
    router=users_router,
    prefix="/users",
    tags=["users"],
)

from chat.controller.ChatController import chat_router
app.include_router(
    router=chat_router,
    prefix='/chat',
    tags=['chat'],
)

from chat.controller.HistoryController import history_router
app.include_router(
    router=history_router,
    prefix="/history",
    tags=['history'],
)


if __name__ == '__main__':
    import uvicorn as uv

    uv.run(
        app="main:app",
        host="localhost",
        port=8000,
        reload=False,
    )
