# 财报摘要 RAG 系统
# 构建：docker build -t finrag:latest .
# 运行：docker run -p 8000:8000 --env-file .env finrag:latest
#       若要连宿主机的 Milvus standalone，用 --network host 或把 MILVUS_ENDPOINT 指向宿主 IP

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

# 系统依赖：
#   build-essential  部分包需要编译
#   libgomp1         torch 的 OpenMP 运行时
#   curl             healthcheck 用
#   libglib2.0-0     Pillow / 图像库的运行时依赖
#   fonts-noto-cjk   中文字体
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        libgomp1 \
        libglib2.0-0 \
        fonts-noto-cjk \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 国内构建加速（不需要可删）
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /app

# 先装 CPU 版 torch：体积约为 CUDA 版的 1/10，容器里也用不到 GPU。
# requirements.txt 里已把 torch 注释掉，避免重复安装。
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 应用代码（db/、.env、模型缓存由 .dockerignore 排除，运行时挂载或环境变量注入）
COPY . .

EXPOSE 8000

# 首次启动要加载 embedding + reranker，给 90 秒预热窗口
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD curl -fsS http://localhost:8000/docs > /dev/null || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
