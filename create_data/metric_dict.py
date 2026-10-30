"""指标词典：把财报表格里的行名（中文长句）收敛成稳定的 metric_code。

这是全项目唯一的指标定义源：
- create_data/table_parser.py 用它把行名映射成 code
- chat/utils/TableQuery.py 用它校验用户问的指标是否在枚举内

只依赖标准库，纯数据 + 纯函数。
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------- 指标定义

# code -> (中文名, 别名列表, 期间类型, 单位类型)
# period_type: point_in_time（存量，年末时点） / duration（流量，年度区间）
# unit_kind:   currency（金额） / ratio（比率） / per_share（每股）
METRICS: dict[str, dict] = {
    "total_assets": {
        "name_cn": "总资产",
        "aliases": ["总资产", "资产总计", "资产合计"],
        "period_type": "point_in_time",
        "unit_kind": "currency",
    },
    "net_assets_attributable": {
        "name_cn": "归属于上市公司股东的净资产",
        "aliases": ["归属于上市公司股东的净资产", "归母净资产", "净资产", "股东权益"],
        "period_type": "point_in_time",
        "unit_kind": "currency",
    },
    "revenue": {
        "name_cn": "营业收入",
        "aliases": ["营业收入", "营收", "总营收", "主营业务收入", "收入"],
        "period_type": "duration",
        "unit_kind": "currency",
    },
    "total_profit": {
        "name_cn": "利润总额",
        "aliases": ["利润总额", "税前利润"],
        "period_type": "duration",
        "unit_kind": "currency",
    },
    "net_profit_attributable": {
        "name_cn": "归属于上市公司股东的净利润",
        "aliases": ["归属于上市公司股东的净利润", "归母净利润", "归母净利", "净利润"],
        "period_type": "duration",
        "unit_kind": "currency",
    },
    "net_profit_deducted": {
        "name_cn": "归属于上市公司股东的扣除非经常性损益的净利润",
        "aliases": [
            "归属于上市公司股东的扣除非经常性损益的净利润",
            "扣非净利润", "扣非净利", "扣非",
        ],
        "period_type": "duration",
        "unit_kind": "currency",
    },
    "ocf_net": {
        "name_cn": "经营活动产生的现金流量净额",
        "aliases": ["经营活动产生的现金流量净额", "经营活动现金流量净额", "经营现金流"],
        "period_type": "duration",
        "unit_kind": "currency",
    },
    "roe_weighted": {
        "name_cn": "加权平均净资产收益率",
        "aliases": ["加权平均净资产收益率", "净资产收益率", "ROE"],
        "period_type": "duration",
        "unit_kind": "ratio",
    },
    "eps_basic": {
        "name_cn": "基本每股收益",
        "aliases": ["基本每股收益", "每股收益", "EPS"],
        "period_type": "duration",
        "unit_kind": "per_share",
    },
    "eps_diluted": {
        "name_cn": "稀释每股收益",
        "aliases": ["稀释每股收益"],
        "period_type": "duration",
        "unit_kind": "per_share",
    },
    "rd_revenue_ratio": {
        "name_cn": "研发投入占营业收入的比例",
        "aliases": ["研发投入占营业收入的比例", "研发投入占比", "研发占比"],
        "period_type": "duration",
        "unit_kind": "ratio",
    },
    "shareholder_total": {
        "name_cn": "普通股股东总数",
        "aliases": ["普通股股东总数", "股东总数", "股东户数"],
        "period_type": "point_in_time",
        "unit_kind": "count",
    },
    # 摘要不直接披露负债合计，但可以按「总资产 − 归母净资产」推算量级。
    # 标 derived_from 后，查询时会先找真实值，找不到才走推算（见 TableQuery）。
    "total_liabilities": {
        "name_cn": "负债合计",
        "aliases": ["负债合计", "负债总额", "总负债", "负债"],
        "period_type": "point_in_time",
        "unit_kind": "currency",
        "derived_from": ["total_assets", "net_assets_attributable"],
    },
}

DERIVED_CODES: frozenset[str] = frozenset(
    code for code, meta in METRICS.items() if meta.get("derived_from")
)

METRIC_CODES: frozenset[str] = frozenset(METRICS)

# 报表行名里常见、但不入事实表的行（表头列名 / 分类小标题）
NON_METRIC_LABELS: frozenset[str] = frozenset({"项目", "指标", "科目"})

# ---------------------------------------------------------------- 归一化

# 去掉行名尾部的口径后缀：「（元/股）」「（%）」「(元)」等
_SUFFIX_RE = re.compile(r"[（(][^）)]{0,12}[）)]\s*$")
_WS_RE = re.compile(r"\s+")


def normalize_label(text: str) -> str:
    """行名归一：去空白、去口径后缀、统一扣非写法。"""
    if not text:
        return ""
    s = _WS_RE.sub("", text)
    s = _SUFFIX_RE.sub("", s)
    # 不同公司写法：扣非…后的净利润 / 扣非…的净利润
    s = s.replace("扣除非经常性损益后的净利润", "扣除非经常性损益的净利润")
    return s


# 别名 → code 的反查表（每次 import 构建一次）
_ALIAS_TO_CODE: dict[str, str] = {}
for _code, _meta in METRICS.items():
    for _alias in _meta["aliases"]:
        _ALIAS_TO_CODE[normalize_label(_alias)] = _code


def resolve_metric(label: str) -> str | None:
    """行名 / 用户口语 → metric_code；不命中返回 None。"""
    key = normalize_label(label)
    if not key or key in NON_METRIC_LABELS:
        return None
    return _ALIAS_TO_CODE.get(key)


def metric_period_type(code: str) -> str:
    return METRICS[code]["period_type"]


def metric_unit_kind(code: str) -> str:
    return METRICS[code]["unit_kind"]


def metric_name(code: str) -> str:
    return METRICS.get(code, {}).get("name_cn", code)


# ---------------------------------------------------------------- 数字清洗

_NULL_TOKENS = frozenset({"-", "--", "—", "——", "/", "－", "", "不适用", "无"})


def normalize_number(cell: str) -> float | None:
    """单元格 → float。

    实测脏数据：`367, 718, 196`、`15, 213, 372, 100. 28`、`0. 6857`、`34. 22%`、`-1, 171, 520`。
    规则：去所有空白 → 去千分位逗号 → 去百分号 → 空标记返回 None。
    """
    if cell is None:
        return None
    s = _WS_RE.sub("", str(cell))
    s = s.replace(",", "").replace("，", "").replace("%", "").replace("％", "")
    s = re.sub(r"^\+", "", s)
    if s in _NULL_TOKENS:
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ---------------------------------------------------------------- 单位

_UNIT_FACTORS = {"元": 1.0, "千元": 1_000.0, "万元": 10_000.0, "亿元": 100_000_000.0}
UNIT_LINE_RE = re.compile(r"单位\s*[：:]\s*([^\n]{0,24})")


def parse_unit_line(text: str) -> tuple[str, float]:
    """从「单位：千元 币种：人民币」里取出单位与换算系数，未声明时按元。"""
    if not text:
        return "元", 1.0
    m = UNIT_LINE_RE.search(text)
    if not m:
        return "元", 1.0
    raw = m.group(1)
    # 必须按长度降序匹配：否则「千元」会先命中「元」
    for unit in ("亿元", "万元", "千元", "元"):
        if unit in raw:
            return unit, _UNIT_FACTORS[unit]
    return "元", 1.0
