from functools import lru_cache

from langchain_openai import ChatOpenAI
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate
from dotenv import load_dotenv
import os

load_dotenv()

# 默认走 DashScope 云端（.env 里的 DASHSCOPE_MODEL=qwen-plus 会覆盖模型）
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen3.8-max-0902"


def _read_windows_env(name):
    """读取 Windows 用户/系统级环境变量

    .env 里放的是占位符（上传 GitHub 用），真实 key 配在本机环境变量里。
    IDE 或服务进程不一定继承后来改动的系统环境变量，所以这里直接读注册表兜一层。
    """
    try:
        import winreg
    except ImportError:  # 非 Windows
        return None

    locations = (
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        (winreg.HKEY_CURRENT_USER, r"Environment"),
    )
    for root, path in locations:
        try:
            with winreg.OpenKey(root, path) as key:
                value, _ = winreg.QueryValueEx(key, name)
                if value:
                    return value
        except OSError:
            continue
    return None


def _api_key():
    """取 API Key：进程环境变量优先，其次本机环境变量，最后才用 .env 里的值"""
    key = os.getenv("DASHSCOPE_API_KEY")
    if key and key.startswith("sk-"):
        return key

    key = _read_windows_env("DASHSCOPE_API_KEY")
    if key:
        os.environ["DASHSCOPE_API_KEY"] = key  # 同步到进程环境，方便其他模块读取
        return key
    return os.getenv("DASHSCOPE_API_KEY")


def llm_config():
    """模型配置：LLM_* 显式覆盖 > DASHSCOPE_* > 默认值"""
    base_url = (
        os.getenv("LLM_BASE_URL")
        or os.getenv("DASHSCOPE_BASE_URL")
        or DEFAULT_BASE_URL
    )
    model = (
        os.getenv("LLM_MODEL")
        or os.getenv("DASHSCOPE_MODEL")
        or os.getenv("LLM_MODEL_NAME")
        or DEFAULT_MODEL
    )
    return base_url, model


@lru_cache(maxsize=1)
def ollama_llm():
    """本地 Ollama 模型（离线，主力）。模型名由 OLLAMA_MODEL_NAME 指定。"""
    from langchain_ollama import ChatOllama

    return ChatOllama(
        model=os.getenv("OLLAMA_MODEL_NAME", "qwen2.5:1.5b"),
        base_url=os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
        streaming=True,
        temperature=float(os.getenv("LLM_TEMPERATURE", "0")),
        num_ctx=int(os.getenv("LLM_NUM_CTX", "16384")),
        client_kwargs={"timeout": int(os.getenv("LLM_TIMEOUT", "180"))},
    )


@lru_cache(maxsize=1)
def chat_llm():
    """LLM 客户端全局复用。
    LLM_PROVIDER=ollama（默认，离线）| dashscope（云端）
    """
    provider = os.getenv("LLM_PROVIDER", "ollama").lower()
    if provider == "ollama":
        return ollama_llm()

    base_url, model = llm_config()
    return ChatOpenAI(
        api_key=_api_key(),
        base_url=base_url,
        model=model,
        streaming=True,
    )


@lru_cache(maxsize=1)
def schema_llm():
    """意图识别专用客户端：非流式、强制 JSON、温度 0、短超时。

    与 chat_llm 共用同一个 Ollama 模型和 num_ctx —— 参数一致才不会让 Ollama
    反复卸载/重载模型（每次约 3.5 秒）。
    """
    from langchain_ollama import ChatOllama

    return ChatOllama(
        model=os.getenv("OLLAMA_MODEL_NAME", "qwen2.5:1.5b"),
        base_url=os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
        streaming=False,
        format="json",
        temperature=0,
        num_ctx=int(os.getenv("LLM_NUM_CTX", "16384")),
        client_kwargs={"timeout": int(os.getenv("INTENT_TIMEOUT", "30"))},
    )


def summarize_table(table_content: str) -> str:
    """
    使用 LLM 生成表格摘要（仅用于规则解析失败的复杂表格）
    """
    llm = chat_llm()
    
    prompt = PromptTemplate.from_template("""
你是一个金融数据分析师。请简要总结以下表格的核心信息。

要求：
- 用1-2句话概括表格主要内容
- 突出关键数据或趋势
- 不要逐行描述

表格内容：
{table_content}

摘要：
""")
    
    chain = prompt | llm | StrOutputParser()
    return chain.invoke({"table_content": table_content})
