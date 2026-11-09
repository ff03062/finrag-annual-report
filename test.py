import os
from openai import OpenAI

# 建议把 Key 放到环境变量里，不要硬编码在代码中
# 临时测试也可以直接写：api_key="sk-你的APIKey"
api_key = os.getenv("DASHSCOPE_API_KEY")

client = OpenAI(
    api_key=api_key,
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
)

completion = client.chat.completions.create(
    model="qwen3.8-max-0902",  # 选一个你已开通/有免费额度的模型
    messages=[{"role": "user", "content": "你好，请回复“测试成功”"}],
)

print(completion.choices[0].message.content)