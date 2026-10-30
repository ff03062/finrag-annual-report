"""数据覆盖范围：从 financial.db 实时推导「到底能查什么」。

为什么不能硬编码
----------------
财报摘要虽然是 2025 年的，但表格里必然带**往年对比列**：本期/上期、2025/2024，
三大报表的年度数据往往覆盖三年。库里实际就有 2023、2024、2025 三个完整年度，
其中 2023/2024 各 112 条年度记录、30 条时点记录。

把年份写死成 `(2023, 2024, 2025)` 有两个问题：
    1. 明年接入 2026 年报时，旧代码会把 2026 判成非法年份
    2. 换一批公司做新项目时，季度指标覆盖范围可能不同

所以这份「schema 边界」必须从数据本身推导，代码只做读取和缓存。
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_DEFAULT_DB = Path(__file__).resolve().parents[2] / "db" / "financial.db"
DB_PATH = Path(os.getenv("FIN_DB_PATH", _DEFAULT_DB))

# 报告年份：判「今年/去年/前年」和「没提年份时默认查哪年」用
REPORT_YEAR = int(os.getenv("REPORT_YEAR", "2025"))

_QUARTER_LIKE = "%Q%"


@dataclass(frozen=True)
class DataScope:
    """库里真实存在的数据范围。所有字段都来自查询，不来自假设。"""

    years: tuple[int, ...]              # 有年度/时点数据的年份，升序
    quarter_years: tuple[int, ...]      # 有季度数据的年份
    quarter_metrics: frozenset[str]     # 有季度数据的指标 code
    companies: tuple[str, ...]          # 实际入库的公司

    @property
    def default_year(self) -> int:
        """没提年份时查哪年：优先报告年，不在范围内则取最新一年。"""
        if REPORT_YEAR in self.years:
            return REPORT_YEAR
        return max(self.years) if self.years else REPORT_YEAR

    def has(self, year: int) -> bool:
        return year in self.years

    def has_quarter(self, metric: str | None, year: int | None) -> bool:
        """该指标在该年是否有季度粒度数据。"""
        if metric is None or year is None:
            return False
        return year in self.quarter_years and metric in self.quarter_metrics

    def describe(self) -> str:
        years = "/".join(str(y) for y in self.years) or "无"
        metric_names = "、".join(sorted(self.quarter_metrics)) or "无"
        return (f"年度数据：{years}；季度数据：{metric_names}"
                f"（仅 {', '.join(str(y) for y in self.quarter_years) or '无'}）")


def _load() -> DataScope:
    if not DB_PATH.exists():
        return DataScope(years=(), quarter_years=(), quarter_metrics=frozenset(), companies=())

    con = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True, timeout=5)
    try:
        years = tuple(sorted({
            int(row[0]) for row in con.execute(
                "SELECT DISTINCT substr(period_label, 1, 4) FROM fact")
            if row[0] and str(row[0]).isdigit()
        }))
        quarter_years = tuple(sorted({
            int(row[0]) for row in con.execute(
                "SELECT DISTINCT substr(period_label, 1, 4) FROM fact WHERE period_label LIKE ?",
                (_QUARTER_LIKE,))
            if row[0] and str(row[0]).isdigit()
        }))
        quarter_metrics = frozenset(
            row[0] for row in con.execute(
                "SELECT DISTINCT metric_code FROM fact WHERE period_label LIKE ?",
                (_QUARTER_LIKE,))
            if row[0]
        )
        companies = tuple(
            row[0] for row in con.execute(
                "SELECT DISTINCT company FROM fact ORDER BY company")
            if row[0]
        )
    finally:
        con.close()

    return DataScope(years=years, quarter_years=quarter_years,
                     quarter_metrics=quarter_metrics, companies=companies)


@lru_cache(maxsize=1)
def get_scope() -> DataScope:
    """进程内缓存。重建索引后需重启服务才会刷新。"""
    scope = _load()
    print(f"[数据范围] {scope.describe()}")
    return scope


def clear_cache() -> None:
    get_scope.cache_clear()