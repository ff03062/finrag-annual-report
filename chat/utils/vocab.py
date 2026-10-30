"""路由与意图识别共用的词汇表。

单独成文件是为了打破循环依赖：QueryRouter 与 SchemaLinker 都要用公司别名和
意图关键词，但两者不应互相 import（QueryRouter 需要调 SchemaLinker 做提议，
SchemaLinker 又需要公司清单来构造 schema）。

这里只放常量和纯函数，不 import 路由/检索等业务模块（唯一依赖是 metric_dict 的指标定义）。
"""
from __future__ import annotations

from create_data.metric_dict import METRICS

# ---------------------------------------------------------------- 公司

# 各种写法 → 标准简称（与 financial.db 的 company 字段一致）
COMPANY_ALIASES: dict[str, str] = {
    "中芯": "中芯国际", "中芯国际": "中芯国际",
    "比亚迪": "比亚迪", "BYD": "比亚迪",
    "宁德": "宁德时代", "宁德时代": "宁德时代", "CATL": "宁德时代",
    "汇川": "汇川科技", "汇川技术": "汇川科技", "汇川科技": "汇川科技",
    "长电": "长电科技", "长电科技": "长电科技",
    "裕能": "湖南裕能", "湖南裕能": "湖南裕能",
    "天赐": "天赐材料", "天赐材料": "天赐材料",
    "恩捷": "恩捷股份", "恩捷股份": "恩捷股份",
    "德福": "德福科技", "德福科技": "德福科技",
    "中微": "中微公司", "中微公司": "中微公司",
    "中科电气": "中科电气",
    "华海": "华海清科", "华海清科": "华海清科",
    "和胜": "和胜股份", "和胜股份": "和胜股份",
    "锐科": "锐科激光", "锐科激光": "锐科激光",
    "通达": "通达动力", "通达动力": "通达动力",
}

# 白名单：库里实际存在的 15 家。模型返回任何不在此列的公司都会被丢弃。
COMPANIES: tuple[str, ...] = (
    "比亚迪", "宁德时代", "中芯国际", "汇川科技", "长电科技",
    "湖南裕能", "天赐材料", "恩捷股份", "德福科技", "中微公司",
    "中科电气", "华海清科", "和胜股份", "锐科激光", "通达动力",
)

# 长度降序，保证「中芯国际」优先于「中芯」
COMPANY_ALIAS_INDEX: tuple[str, ...] = tuple(sorted(COMPANY_ALIASES, key=len, reverse=True))


def resolve_company(name: str) -> str | None:
    """任意写法 → 白名单标准简称；无法确定身份时返回 None。

    先查别名表，再判断是否是白名单全名（防止模型自造公司）。
    """
    if not name:
        return None
    key = name.strip()
    if key in COMPANY_ALIASES:
        return COMPANY_ALIASES[key]

    # 「云南恩捷新材料股份有限公司」这类全称：包含白名单公司名即认
    for standard in COMPANIES:
        if standard and standard in key:
            return standard
    return None


def match_companies_in(text: str) -> list[str]:
    """从自然语言里找出所有提到的公司（最长匹配优先，命中后抹掉避免重复计）。"""
    rest = text
    found: list[str] = []
    for alias in COMPANY_ALIAS_INDEX:
        if alias in rest:
            company = COMPANY_ALIASES[alias]
            if company not in found:
                found.append(company)
            rest = rest.replace(alias, "\x00")
    return found


# ---------------------------------------------------------------- 意图关键词

# 叙述类词：出现说明用户要的不只是数字
NARRATIVE_HINTS: tuple[str, ...] = (
    "为什么", "原因", "如何", "怎么", "怎么样", "介绍", "业务", "行业", "风险",
    "优势", "策略", "前景", "发展", "竞争", "地位", "模式", "产品", "技术",
    "规划", "布局", "影响", "分析", "评价", "情况",
)

# 增长率类词：需要两年数据算同比
GROWTH_HINTS: tuple[str, ...] = ("增速", "增长", "同比", "增幅", "涨幅", "跌幅")

# 闲聊
CHITCHAT_HINTS: tuple[str, ...] = ("你好", "您好", "谢谢", "再见", "你是谁", "介绍一下你自己")

# 口语说法 → 指标（不进 metric_dict，避免「赚」这种短词污染表格行名映射）
COLLOQUIAL_METRICS: tuple[tuple[str, str], ...] = (
    ("赚了多少钱", "net_profit_attributable"),
    ("赚了多少", "net_profit_attributable"),
    ("赚了", "net_profit_attributable"),
    ("赚钱", "net_profit_attributable"),
    ("挣钱", "net_profit_attributable"),
    ("更赚", "net_profit_attributable"),
    ("盈利", "net_profit_attributable"),
    ("营业额", "revenue"),
    ("销售收入", "revenue"),
    ("家底", "total_assets"),
)

# 相对时间词 → 相对报告年的偏移。模型不擅长这类换算，一律以规则为准。
RELATIVE_YEAR_OFFSETS: tuple[tuple[str, int], ...] = (
    ("前年", -2),
    ("去年", -1),
    ("今年", 0),
)


# ---------------------------------------------------------------- 指标匹配

# 指标别名按长度降序，保证「总营收」优先于「营收」、「扣非净利润」优先于「净利润」
_ALIAS_INDEX: tuple[tuple[str, str], ...] = tuple(sorted(
    ((alias, code) for code, meta in METRICS.items() for alias in meta["aliases"]),
    key=lambda item: -len(item[0]),
))

# 这些别名太通用，只在没有更具体命中时才用
_GENERIC_ALIASES = frozenset({"收入", "净利润", "净资产", "股东权益", "每股收益"})


def match_metric(text: str, allow_colloquial: bool = True) -> str | None:
    """自由文本 → metric_code。

    allow_colloquial=False 时不看口语映射：「盈利模式」里的「盈利」不能被当成净利润。
    """
    if not text:
        return None

    hits = [(alias, code) for alias, code in _ALIAS_INDEX if alias in text]
    if hits:
        specific = [hit for hit in hits if hit[0] not in _GENERIC_ALIASES]
        return (specific or hits)[0][1]

    if not allow_colloquial:
        return None
    for phrase, code in COLLOQUIAL_METRICS:
        if phrase in text:
            return code
    return None