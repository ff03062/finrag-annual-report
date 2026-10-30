"""问题路由：模型提议意图，代码裁决，规则兜底。

三段式
------
```
SchemaLinker.link()  →  IntentDSL  →  IntentDSL.adjudicate()  →  Route
     ↑ 模型提议             ↑ 结构化                ↑ 代码裁决
```

- **模型**只把自然语言理解成「哪个公司、哪个指标、哪一年」，输出受限枚举的 JSON
- **代码**校验每个字段是否落在白名单内，越界即降级，最终翻译成参数化查询
- **规则**（本文件的 `route()`）不参与决策，只做兜底：模型未启动、超时、输出不合法时接管

规则本身仍然有用 —— 它是离线可跑、零延迟的基线，也是裁决失手时的安全网。
"""
from __future__ import annotations

import re

from create_data.metric_dict import METRICS
from chat.utils import IntentDSL, SchemaLinker
from chat.utils.IntentDSL import Route
from chat.utils.vocab import (
    CHITCHAT_HINTS,
    COMPANY_ALIASES,
    GROWTH_HINTS,
    NARRATIVE_HINTS,
    match_companies_in,
    match_metric,
)

__all__ = [
    "Route",
    "COMPANY_ALIASES",
    "match_companies",
    "match_company",
    "match_metric",
    "parse_period",
    "period_label",
    "route",
    "resolve",
]

QUARTER_CN = {"一": 1, "二": 2, "三": 3, "四": 4, "1": 1, "2": 2, "3": 3, "4": 4}

# 兼容旧调用点
match_companies = match_companies_in


def match_company(question: str) -> str | None:
    companies = match_companies_in(question)
    return companies[0] if companies else None


def parse_period(question: str, default_year: int = 2025) -> tuple[int, int | None]:
    """返回 (year, quarter)。季度为 None 表示年度。"""
    year = default_year
    m = re.search(r"(20\d\d)\s*年?", question)
    if m:
        year = int(m.group(1))
    elif "去年" in question:
        year = default_year - 1
    elif "前年" in question:
        year = default_year - 2

    quarter = None
    m = re.search(r"[Qq]([1-4])", question)
    if m:
        quarter = int(m.group(1))
    else:
        m = re.search(r"第?([一二三四1234])\s*季度", question)
        if m:
            quarter = QUARTER_CN[m.group(1)]
    return year, quarter


def period_label(metric_code: str, year: int, quarter: int | None) -> str:
    if quarter:
        return f"{year}Q{quarter}"
    return f"{year}年末" if METRICS[metric_code]["period_type"] == "point_in_time" else f"{year}年"


# ---------------------------------------------------------------- 统一入口


def resolve(question: str, default_year: int = 2025, history_text: str = "") -> Route:
    """路由主入口：模型提议 → 代码裁决 → 规则兜底。

    闲聊和空问题不进模型：一次小模型调用约 0.5 秒，对「你好」没有收益。
    """
    q = (question or "").strip()
    if not q:
        return Route(channel="rag", year=default_year, intent="narrative", source="rule")
    if any(hint in q for hint in CHITCHAT_HINTS):
        return Route(channel="chat", intent="chitchat", source="rule")

    if not SchemaLinker.is_enabled():
        return route(question, default_year=default_year, history_text=history_text)

    dsl = SchemaLinker.link(question, history_text)
    if dsl is None:
        print("[裁决] 模型未给出可用意图，回退规则路由")
        return route(question, default_year=default_year, history_text=history_text)

    decided = IntentDSL.adjudicate(dsl, question)
    print(f"[裁决] {decided.describe()}")
    return decided


# ---------------------------------------------------------------- 规则兜底


def route(question: str, default_year: int = 2025, history_text: str = "") -> Route:
    """纯规则路由：不调模型，作为兜底基线和离线能力。

    只在 `resolve()` 无法拿到模型意图时使用。
    """
    q = question.strip()
    if not q:
        return Route(channel="rag", year=default_year, intent="narrative", source="rule")
    if any(h in q for h in CHITCHAT_HINTS):
        return Route(channel="chat", intent="chitchat", source="rule")

    companies = match_companies_in(q)
    has_narrative = any(h in q for h in NARRATIVE_HINTS)
    # 有叙述意图时（"盈利模式""负债情况"）只看正式别名，口语映射会让「盈利」误伤成净利润
    metric = match_metric(q, allow_colloquial=not has_narrative)

    # 追问继承：「2024 年的呢」本身没有公司和指标，从最近几轮对话里补上。
    # 只取尾部，避免把很早之前的话题带进来。
    if history_text and (not companies or not metric):
        tail = history_text[-400:]
        if not companies:
            companies = match_companies_in(tail)
        if not metric:
            metric = match_metric(tail, allow_colloquial=False)

    single = companies[0] if len(companies) == 1 else None
    year, quarter = parse_period(q, default_year)

    if metric:
        label = period_label(metric, year, quarter)
        if any(h in q for h in GROWTH_HINTS):
            return Route(channel="growth", company=single, companies=companies or None,
                         metric=metric, period_label=label, year=year, quarter=quarter,
                         intent="growth", source="rule")
        # 多公司对比要模型做分析（"谁更赚钱"不是给两个数字就完事），走 both
        multi = len(companies) > 1
        channel = "both" if (has_narrative or multi) else "sql"
        return Route(channel=channel, company=single, companies=companies or None,
                     metric=metric, period_label=label, year=year, quarter=quarter,
                     intent="query_metric" if not multi else "compare", source="rule")

    if companies:
        return Route(channel="rag", company=single, companies=companies, year=year,
                     intent="narrative", source="rule")

    # 没有具体公司（"哪家营收最高""行业里提到固态电池的有哪些"）→ 全库检索
    return Route(channel="rag", year=year, intent="narrative", source="rule")