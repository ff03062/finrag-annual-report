# rLLM-FinQA-4B 部署手册

本地小模型，在项目中承担**意图识别与槽位抽取**：判断问题走 SQL 还是向量检索，抽取公司、指标、年份。
答案生成由云端大模型完成。

| 项 | 值 |
|---|---|
| 模型实例 | `rllm-finqa-4b:q4` |
| 参数量 | 4.4B |
| 磁盘占用 | 2.7 GB |
| 显存占用 | 5.1 GB（num_ctx=16384） |
| 运行后端 | Ollama 0.32.5 |
| 验证环境 | Windows 11 + RTX 4060 Laptop 8GB |

---

## 1. 环境要求

| 项 | 要求 |
|---|---|
| 操作系统 | Windows / Linux |
| GPU 显存 | ≥ 8 GB |
| Ollama | ≥ 0.32 |
| 磁盘 | 3 GB（模型） + 9 GB（转换过程临时占用） |
| Python | 3.10+，无需第三方依赖 |

---

## 2. 部署流程

### 2.1 下载

从 HuggingFace 拉取原始权重（仓库 `rLLM/rLLM-FinQA-4B`，apache-2.0，无需授权）。

```powershell
New-Item -ItemType Directory -Force -Path D:\models\rLLM-FinQA-4B | Out-Null

curl.exe -L -C - -o D:\models\rLLM-FinQA-4B\model-00001-of-00002.safetensors `
  https://huggingface.co/rLLM/rLLM-FinQA-4B/resolve/main/model-00001-of-00002.safetensors

curl.exe -L -C - -o D:\models\rLLM-FinQA-4B\model-00002-of-00002.safetensors `
  https://huggingface.co/rLLM/rLLM-FinQA-4B/resolve/main/model-00002-of-00002.safetensors
```

还需要同目录下的 `config.json`、`tokenizer.json`、`tokenizer_config.json`、`special_tokens_map.json`、
`vocab.json`、`merges.txt`、`added_tokens.json`、`chat_template.jinja`、`model.safetensors.index.json`：

```powershell
$base = "https://huggingface.co/rLLM/rLLM-FinQA-4B/resolve/main"
foreach ($f in @("config.json","tokenizer.json","tokenizer_config.json","special_tokens_map.json",
                 "vocab.json","merges.txt","added_tokens.json","chat_template.jinja",
                 "model.safetensors.index.json")) {
  curl.exe -L -C - -o "D:\models\rLLM-FinQA-4B\$f" "$base/$f"
}
```

下载完成后 `D:\models\rLLM-FinQA-4B\` 应为 8.82 GB。

### 2.2 转换

用 llama.cpp 的转换脚本把权重转成 f16 中间文件。

```powershell
git clone --depth 1 https://github.com/ggml-org/llama.cpp D:\models\lcpp\llama.cpp-master
python -m pip install -r D:\models\lcpp\llama.cpp-master\requirements.txt

python D:\models\lcpp\llama.cpp-master\convert_hf_to_gguf.py `
  D:\models\rLLM-FinQA-4B `
  --outtype f16 `
  --outfile D:\models\finqa-gguf\rllm-finqa-4b-f16.gguf
```

产出：`rllm-finqa-4b-f16.gguf`，8.22 GB。

### 2.3 量化

下载 llama.cpp 预编译工具，执行 Q4_K_M 量化。

```powershell
# 下载 win-cpu-x64 版本，解压到 D:\models\llama-cpu\
# 地址：https://github.com/ggml-org/llama.cpp/releases

D:\models\llama-cpu\llama-quantize.exe `
  D:\models\finqa-gguf\rllm-finqa-4b-f16.gguf `
  D:\models\finqa-gguf\rllm-finqa-4b-q4_K_M.gguf `
  q4_K_M
```

产出：`rllm-finqa-4b-q4_K_M.gguf`，2.53 GB。

### 2.4 导入

新建 `D:\models\Modelfile.q4km`：

```
FROM D:/models/finqa-gguf/rllm-finqa-4b-q4_K_M.gguf

PARAMETER num_ctx 16384
PARAMETER temperature 0
PARAMETER top_p 0.9
PARAMETER repeat_penalty 1.05
```

注册到 Ollama：

```powershell
ollama create rllm-finqa-4b:q4 -f D:\models\Modelfile.q4km
```

### 2.5 验证

```powershell
ollama list
# NAME                ID              SIZE
# rllm-finqa-4b:q4    db28abf6a1b6    2.7 GB

ollama show rllm-finqa-4b:q4
```

期望输出：

```
  Model
    architecture        qwen3
    parameters          4.4B
    context length      262144
    quantization        Q4_K_M
  Capabilities
    tools
    completion
  Parameters
    num_ctx            16384
    repeat_penalty     1.05
    temperature        0
    top_p              0.9
```

跑一次推理，确认加载位置：

```powershell
ollama run rllm-finqa-4b:q4 "计算 12345 乘以 67"
# -> 827115

ollama ps
# NAME                SIZE     PROCESSOR   CONTEXT
# rllm-finqa-4b:q4    5.1 GB   100% GPU    16384
```

`PROCESSOR` 为 `100% GPU` 即部署成功。

### 2.6 项目接入

`.env` 增加三项：

```ini
OLLAMA_BASE_URL   = "http://127.0.0.1:11434"
OLLAMA_MODEL_NAME = "rllm-finqa-4b:q4"
LLM_PROVIDER      = "dashscope"
```

| 变量 | 作用 |
|---|---|
| `OLLAMA_BASE_URL` | 本地推理服务地址 |
| `OLLAMA_MODEL_NAME` | 意图识别使用的模型 |
| `LLM_PROVIDER` | `dashscope`=云端生成答案；`ollama`=全程本地 |

对应代码：`ai/LoadLLM.py`
- `ollama_llm()` → 读取前两项，返回本地客户端
- `chat_llm()` → 读取 `LLM_PROVIDER`，返回云端客户端

---

## 3. 参数配置

`num_ctx` 与显存、速度的关系（RTX 4060 Laptop 8GB 实测）：

| num_ctx | 显存占用 | 可用性 |
|---:|---:|---|
| 8192 | ~2.4 GB | 可用 |
| 12288 | ~2.4 GB | 可用 |
| **16384** | **5.1 GB** | **推荐，100% GPU 满速** |
| 32768 | — | 可加载，生成速度 65 → 25 tok/s |

5.1 GB ≈ 2.59 GB 权重 + 2.5 GB KV cache。

意图识别的输入是「用户问题 + 候选枚举」，长度远小于 16384，固定用 16384 即可。

其余参数：

| 参数 | 值 | 原因 |
|---|---|---|
| `temperature` | 0 | 结构化判断需要稳定输出 |
| `top_p` | 0.9 | 默认 |
| `repeat_penalty` | 1.05 | 避免重复片段 |
| `keep_alive` | `"10m"` | 保持常驻，避免每次冷加载 3.5 s |

---

## 4. 性能基线

| 场景 | 加载 | 首 token | 生成速度 |
|---|---|---|---|
| 冷启动 | 3.56 s | ≈3.7 s | 76 tok/s |
| 常驻 | 0.27 s | 0.3–0.4 s | 44–77 tok/s |
| 长文本 4631 tokens | — | ≈1.6 s | 65.6 tok/s |

分场景：

| 场景 | prompt tokens | 生成 tokens | 生成速度 |
|---|---:|---:|---:|
| 英文表格题 | 76 | 19 | 44.6 tok/s |
| 中文表格题 | 76 | 15 | 26.3 tok/s |
| 中文算术 | 35 | 239 | 77.4 tok/s |
| 工具调用 | 224 | 36 | 44.7 tok/s |

---

## 5. 调用示例

### 5.1 基础对话

```python
import json
import time
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
MODEL = "rllm-finqa-4b:q4"
OPTIONS = {"num_ctx": 16384, "temperature": 0, "top_p": 0.9, "repeat_penalty": 1.05}


def chat(prompt: str, system: str | None = None) -> dict:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": MODEL,
        "messages": messages,
        "stream": False,
        "options": OPTIONS,
        "keep_alive": "10m",
    }
    req = urllib.request.Request(
        f"{OLLAMA}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    data["_wall_s"] = round(time.time() - t0, 2)
    return data


if __name__ == "__main__":
    r = chat("营业收入 67,323,192 千元，折合多少亿元？")
    print(r["message"]["content"])
    print(f"{r['_wall_s']}s, {r['eval_count'] / (r['eval_duration'] / 1e9):.1f} tok/s")
```

### 5.2 工具调用

```python
tools = [{
    "type": "function",
    "function": {
        "name": "route_query",
        "description": "决定问题走哪个检索通道",
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "enum": ["sql", "rag", "both", "chat"]},
            },
            "required": ["channel"],
        },
    },
}]

payload = {
    "model": "rllm-finqa-4b:q4",
    "messages": [{"role": "user", "content": "比亚迪 2025 年营收涨这么多，是什么原因？"}],
    "tools": tools,
    "stream": False,
    "options": {"num_ctx": 16384, "temperature": 0},
}

req = urllib.request.Request(
    "http://127.0.0.1:11434/api/chat",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(req, timeout=600) as r:
    resp = json.loads(r.read().decode())

for c in resp["message"].get("tool_calls", []):
    print(c["function"]["name"], c["function"]["arguments"])
# -> route_query {'channel': 'both'}
```

工具调用时 `message.content` 为空、`message.tool_calls` 有值。

---

## 6. 文件清单

| 路径 | 大小 | 说明 |
|---|---|---|
| `D:\models\finqa-gguf\rllm-finqa-4b-q4_K_M.gguf` | 2.53 GB | 部署必需 |
| `D:\models\Modelfile.q4km` | 213 B | 部署必需 |
| `D:\models\llama-cpu\` | ~60 MB | 量化工具，重新生成时需要 |
| `D:\models\lcpp\llama.cpp-master\` | ~100 MB | 转换脚本，重新生成时需要 |
| `D:\models\rLLM-FinQA-4B\` | 8.82 GB | 原始权重，可删 |
| `D:\models\finqa-gguf\rllm-finqa-4b-f16.gguf` | 8.22 GB | 转换中间产物，可删 |

---

## 7. 模型信息

| 项 | 值 |
|---|---|
| 仓库 | `rLLM/rLLM-FinQA-4B` |
| 基座 | `Qwen/Qwen3-4B-Instruct-2507` |
| 训练方式 | GRPO 强化学习 |
| 参数规模 | 4.4B（36 层，hidden 2560，32 heads / 8 KV heads） |
| 原生上下文 | 262144 |
| 原生工具 | 表格查询、SQL 执行、计算器 |
| 训练语言 | 英文；中文实测可用 |
| 许可 | apache-2.0 |