"""查询意图 DSL —— 模型做提议者，代码做裁决者。

设计原则
--------
**模型只负责「理解」**：把自然语言变成结构化意图（哪个公司、哪个指标、哪一年）。
**代码负责「裁决」**：意图里的每个字段都必须落在白名单内，越界即降级。

模型全程不产生 SQL、不产生数字、不能指定表名或列名，也不掌握"最终查什么"的决定权。
通过校验的 DSL 会被翻译成 `Route`，下游 `TableQuery` 再据此拼**参数化** SQL。

这样做的收益：
    - 灵活性：口语、省略、指代、复合问题都能被模型理解
    - 安全性：即使模型判断错了，也只能在有限枚举里选错，不会越权查询
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from create_data.metric_dict import METRICS, metric_name, resolve_metric
from chat.utils.datascope import REPORT_YEAR, get_scope
from chat.utils.vocab import RELATIVE_YEAR_OFFSETS, match_metric, resolve_company

# 模型可用的意图枚举。不在这个集合里的一律降级。
VALID_INTENTS = (
    "query_metric",    # 问某公司某指标的数值
    "compare",         # 多家公司对比 / 排名 / 最值
    "growth",          # 同比、增速、增长幅度
    "narrative",       # 业务、原因、行业地位等文字内容
    "chitchat",        # 问候、闲聊
    "out_of_scope",    # 问的东西不在可查范围（应明确说未披露）
)

# 数据覆盖的年份**不在这里写死**：财报摘要本身带往年对比列，
# 库里到底有哪些年由 datascope 从 SQLite 实时读出。

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.S)
_YEAR_RE = re.compile(r"(20\d\d)\s*年?")


@dataclass
class Route:
    """路由结果：下游各通道只认这个结构，不关心它来自模型还是规则。"""

    channel: str                       # sql | growth | rag | both | chat
    company: str | None = None         # 单公司
    companies: list[str] | None = None  # 多公司对比（≥2 家时填这里，company 留空）
    metric: str | None = None
    period_label: str | None = None
    year: int | None = None
    quarter: int | None = None
    intent: str | None = None          # 来自 DSL 的原始意图，仅用于观测
    source: str = "rule"               # rule | llm，标记本次路由由谁产出

    def describe(self) -> str:
        return (f"channel={self.channel} source={self.source} intent={self.intent} "
                f"company={self.company} companies={self.companies} "
                f"metric={self.metric} period={self.period_label}")


@dataclass
class IntentDSL:
    """模型输出的查询意图。字段全部是「提议」，能否生效由 adjudicate 决定。"""

    intent: str
    companies: list[str] = field(default_factory=list)
    metric: str | None = None
    year: int | None = None
    quarter: int | None = None
    aspect: str | None = None      # 叙述关注点，如「增长原因」「行业地位」
    confidence: float = 0.0

    # ------------------------------------------------------------ 解析

    @classmethod
    def parse(cls, raw: str) -> "IntentDSL | None":
        """解析模型输出。容忍代码块包裹、单字段类型漂移。"""
        if not raw:
            return None

        text = raw.strip()
        match = _JSON_BLOCK_RE.search(text)
        if match:
            text = match.group(0)

        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(obj, dict):
            return None

        intent = obj.get("intent") or obj.get("type") or ""
        if not isinstance(intent, str):
            return None

        return cls(
            intent=intent.strip(),
            companies=_as_list(obj.get("companies") or obj.get("company")),
            metric=_as_str(obj.get("metric")),
            year=_as_int(obj.get("year")),
            quarter=_as_quarter(obj.get("quarter")),
            aspect=_as_str(obj.get("aspect")),
            confidence=_as_float(obj.get("confidence")),
        )

    def describe(self) -> str:
        return (f"intent={self.intent} companies={self.companies} metric={self.metric} "
                f"year={self.year} quarter={self.quarter} aspect={self.aspect} "
                f"conf={self.confidence:.2f}")


# ---------------------------------------------------------------- 裁决


def adjudicate(dsl: IntentDSL, question: str = "") -> Route:
    """把模型提议的 DSL 裁决成可执行的路由。

    每一步都可能否决模型的提议：
        公司不在白名单 → 丢弃
        指标不在 13 个 code 内 → 视为不可查，降级为文本检索
        相对时间（去年/前年）→ 由规则换算，不采信模型
        超出数据覆盖的年份 → 用户明确问才保留（下游回答"未披露"），模型编的则回落
        意图与字段不自洽 → 降级为 narrative
    """
    scope = get_scope()
    companies = _clean_companies(dsl.companies)
    if scope.companies and len(companies) >= len(scope.companies):
        # 模型把 15 家全列出来，语义就是「全库排名」，还原成空列表更省查询
        companies = []

    metric = _clean_metric(dsl.metric)
    intent = dsl.intent if dsl.intent in VALID_INTENTS else "narrative"

    # 模型漏字段时用规则补：「挣钱」「家底」这类口语它不一定认得
    if metric is None and intent in ("query_metric", "compare", "growth"):
        metric = match_metric(question, allow_colloquial=True)

    year, quarter = _clean_period(dsl.year, dsl.quarter, metric, question, scope)

    # 意图与字段的一致性检查：字段不足就降级，不允许"半空"路由流到下游
    if intent in ("query_metric", "growth") and not metric:
        intent = "narrative"
    if intent == "query_metric" and not companies:
        # 有指标没公司 → 可能是在问排名，交给 compare
        intent = "compare" if metric else "narrative"
    if intent == "compare" and not metric:
        intent = "narrative"
    if intent == "chitchat":
        return Route(channel="chat", intent=intent, source="llm")
    if intent == "out_of_scope":
        # 指标不在库内，走文本检索让模型基于资料回答"未披露"，而不是报错
        return Route(channel="rag", company=companies[0] if len(companies) == 1 else None,
                     companies=companies or None, year=year,
                     intent=intent, source="llm")

    label = _period_label(metric, year, quarter) if metric else None
    single = companies[0] if len(companies) == 1 else None

    if intent == "query_metric":
        channel = "both" if dsl.aspect else "sql"
        return Route(channel=channel, company=single, companies=companies or None,
                     metric=metric, period_label=label, year=year, quarter=quarter,
                     intent=intent, source="llm")

    if intent == "compare":
        return Route(channel="both", company=single, companies=companies or None,
                     metric=metric, period_label=label, year=year, quarter=quarter,
                     intent=intent, source="llm")

    if intent == "growth":
        return Route(channel="growth", company=single, companies=companies or None,
                     metric=metric, period_label=label, year=year, quarter=quarter,
                     intent=intent, source="llm")

    # narrative：有公司就限定公司检索，没有就全库检索
    return Route(channel="rag", company=single, companies=companies or None,
                 year=year, intent=intent, source="llm")


# ---------------------------------------------------------------- 字段清洗


def _clean_companies(raw: list[str]) -> list[str]:
    """归一化到白名单，保留顺序去重。不认识的直接丢，不做猜测。"""
    out: list[str] = []
    for item in raw:
        standard = resolve_company(item)
        if standard and standard not in out:
            out.append(standard)
    return out


def _clean_metric(raw: str | None) -> str | None:
    """模型可能返回 code，也可能返回中文名；两种都接，不在词典里就返回 None。"""
    if not raw:
        return None
    key = raw.strip()
    if key in METRICS:
        return key
    return resolve_metric(key)


def _clean_period(year: int | None, quarter: int | None, metric: str | None,
                  question: str, scope) -> tuple[int, int | None]:
    """年份的采信优先级：问题里的明确年份 > 相对时间词 > 模型给的值。

    财报摘要里带大量往年对比数据（2023、2024），所以不能把范围压到报告年。
    用户明确问到库里没有的年份时也保留原值 —— 下游会照实回答「该年未披露」，
    这比悄悄改成年报年份、拿一个错年份的数字去答要好。
    """
    explicit = _explicit_year(question)
    relative = _relative_year(question)

    if explicit is not None:
        year = explicit
    elif relative is not None:
        year = relative
    elif year is None or not scope.has(year):
        # 模型凭空给的年份、或压根没给 → 回落默认年
        year = scope.default_year

    if quarter not in (1, 2, 3, 4):
        quarter = None
    if quarter is not None and not scope.has_quarter(metric, year):
        quarter = None
    return year, quarter


def _relative_year(question: str) -> int | None:
    """「去年」「前年」这类相对表达，模型算不准，一律由规则换算。"""
    for word, offset in RELATIVE_YEAR_OFFSETS:
        if word in question:
            return REPORT_YEAR + offset
    return None


def _explicit_year(question: str) -> int | None:
    match = _YEAR_RE.search(question or "")
    return int(match.group(1)) if match else None


def _period_label(metric_code: str, year: int, quarter: int | None) -> str:
    """期间标签：季度用 2025Q1，时点指标用「年末」，其余用「年」。"""
    if quarter:
        return f"{year}Q{quarter}"
    if METRICS[metric_code]["period_type"] == "point_in_time":
        return f"{year}年末"
    return f"{year}年"


# ---------------------------------------------------------------- 类型容错


def _as_str(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def _as_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_quarter(value) -> int | None:
    """容忍 'Q2' / '2' / '第二季度' 三种写法。"""
    if value is None:
        return None
    text = str(value).strip()
    match = re.search(r"[1-4]", text)
    return int(match.group(0)) if match else None


def _as_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------- 提示词素材


def metric_catalog() -> str:
    """给模型看的指标清单：code=中文名。只给枚举，不给任何数值。"""
    return "、".join(f"{code}={metric_name(code)}" for code in METRICS)


def company_catalog() -> str:
    """公司清单以库里实际入库的为准，模型只能从这里面选。"""
    scope = get_scope()
    return "、".join(scope.companies)


def period_catalog() -> str:
    """期间清单同样从数据推导：年度覆盖哪些年、季度覆盖哪些指标。"""
    scope = get_scope()
    years = " / ".join(str(year) for year in scope.years) or "无"
    quarter_years = " / ".join(str(year) for year in scope.quarter_years) or "无"
    if scope.quarter_metrics:
        quarter_metrics = "、".join(metric_name(code) for code in sorted(scope.quarter_metrics))
        quarter_line = f"季度数据：仅 {quarter_years} 年的 {quarter_metrics}"
    else:
        quarter_line = "季度数据：无"
    return f"年度数据：{years}\n{quarter_line}"