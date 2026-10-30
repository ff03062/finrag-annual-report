"""Schema Linking：把小模型约束在「已定义的 schema 内」做意图识别。

核心约束
--------
模型看到的只有**枚举清单**（15 家公司、13 个指标的 code 与中文名、年份范围），
看不到数据库、看不到 SQL、看不到任何数值。

模型输出的 JSON 必须落在这些枚举里，落不进去的字段会被 `IntentDSL.adjudicate`
丢弃或降级 —— 这就是「模型提议、代码裁决」的分界线。

本模块只负责「调用 + 解析」，不负责裁决。
"""
from __future__ import annotations

import os

from chat.utils import IntentDSL
from chat.utils.datascope import get_scope
from ai import LoadLLM

# 追问时需要的上下文长度（只取尾部，避免把很早的话题带进来）
HISTORY_TAIL_CHARS = 300

PROMPT = """你是财报问答系统的意图解析器。把用户问题解析成一个 JSON 查询意图。

可查询公司（companies 只能填下面的简称）：
{companies}

可查询指标（metric 只能填 code）：
{metrics}

期间范围（数据库实际覆盖的范围）：
{periods}

期间规则：
- 问题里提到年份就填那一年；没提就填 {default_year}
- quarter 只能是 1 / 2 / 3 / 4 或 null，且只有上面列出的指标有季度值
- 指标是「总资产」「净资产」「股东总数」这类时点值时，不需要填 quarter

intent 只能取以下六个值：
- query_metric：问某家公司某个指标的具体数值
- compare：涉及多家公司对比、排名、谁最高/谁最低/哪家最多
- growth：问同比、增速、增长了百分之多少
- narrative：问业务、原因、行业地位、竞争优势、风险、未来发展等文字内容
- chitchat：问候、闲聊、与财报无关的寒暄
- out_of_scope：问的指标不在上面的指标清单里（例如毛利率、存货、研发费用金额、员工人数）

输出字段：
- intent：上面六个值之一
- companies：公司简称数组，没有提到就填 []
- metric：指标 code，没有提到或不可查就填 null
- year：整数
- quarter：1-4 或 null
- aspect：如果是文字类问题，填关注的方面（如「增长原因」「行业地位」），否则 null
- confidence：0 到 1 之间的置信度

只输出 JSON 对象，不要任何解释文字。

{history}用户问题：{question}

JSON："""


def build_prompt(question: str, history_text: str = "") -> str:
    history = ""
    if history_text and history_text != "（无）":
        tail = history_text[-HISTORY_TAIL_CHARS:].strip()
        if tail:
            history = f"最近对话（用于理解「它」「那家」「去年呢」这类指代）：\n{tail}\n\n"
    return PROMPT.format(
        companies=IntentDSL.company_catalog(),
        metrics=IntentDSL.metric_catalog(),
        periods=IntentDSL.period_catalog(),
        default_year=get_scope().default_year,
        history=history,
        question=question.strip(),
    )


def link(question: str, history_text: str = "") -> IntentDSL | None:
    """调本地小模型做 schema linking。

    任何失败（模型未启动、超时、输出不是合法 JSON）都返回 None，
    由上层回退到规则路由 —— 意图识别不能成为整条链路的单点故障。
    """
    if not question or not question.strip():
        return None

    try:
        llm = LoadLLM.schema_llm()
        response = llm.invoke(build_prompt(question, history_text))
        raw = getattr(response, "content", "") or ""
    except Exception as exc:  # noqa: BLE001 - 降级路径，任何异常都不该冒泡
        print(f"[意图] 模型不可用，回退规则：{type(exc).__name__}: {exc}")
        return None

    dsl = IntentDSL.IntentDSL.parse(raw)
    if dsl is None:
        preview = raw.strip().replace("\n", " ")[:120]
        print(f"[意图] 输出无法解析，回退规则：{preview!r}")
        return None

    print(f"[意图] 模型提议 {dsl.describe()}")
    return dsl


def is_enabled() -> bool:
    """是否启用模型意图识别。INTENT_MODE=rule 可整体关掉（省钱/省时）。"""
    return os.getenv("INTENT_MODE", "llm").lower() not in ("rule", "off", "false", "0")