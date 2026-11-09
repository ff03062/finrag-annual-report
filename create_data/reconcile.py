"""两路对账：OCR 路和 VLM 路按 (指标, 期间) 配对。

四种结果：

- 两路都有且数值一致 → source=both，保留 OCR 的行列定位入库
- 只有 OCR 有     → source=ocr
- 只有 VLM 有     → source=vlm
- 两路都有但对不上 → 不入库，进冲突表等人工看

冲突不入库是刻意的。财报数字错一条比缺一条更糟：缺的那条查不到会显式说「未披露」，
错的那条会被当成事实讲出去。

一致的判定看换算成元之后的值，不看原单位下的 value_num：两路声明的单位可能不同
（OCR 从单位行读到千元、VLM 从表头读到万元），比原值会得出假冲突。
"""
from __future__ import annotations

from dataclasses import dataclass

from create_data.table_parser import Fact


@dataclass
class Conflict:
    company: str
    metric_code: str
    period_label: str
    ocr_value: float
    vlm_value: float
    doc_path: str


def _comparable(fact: Fact) -> float:
    return fact.value_yuan if fact.value_yuan is not None else fact.value_num


def _same(a: float, b: float) -> bool:
    """相对误差 1e-6。两路读的是同一页，单位换算会带出浮点尾差，不能用 ==。"""
    return abs(a - b) <= max(abs(a), abs(b), 1.0) * 1e-6


def merge(ocr_facts: list[Fact], vlm_facts: list[Fact],
          doc_path: str) -> tuple[list[Fact], list[Conflict]]:
    vlm_by_key = {(f.metric_code, f.period_label): f for f in vlm_facts}
    merged: list[Fact] = []
    conflicts: list[Conflict] = []
    matched: set[tuple[str, str]] = set()

    for fact in ocr_facts:
        key = (fact.metric_code, fact.period_label)
        other = vlm_by_key.get(key)
        if other is None:
            merged.append(fact)
            continue

        matched.add(key)
        if _same(_comparable(fact), _comparable(other)):
            fact.source = "both"
            # 页码和表序必须来自同一体系：md 的表序号和 PDF 的页码表序不能混用，
            # 否则会拼出「第 6 页第 3 张表」这种并不存在的位置。
            fact.source_page = other.source_page
            fact.table_index = other.table_index
            merged.append(fact)
        else:
            conflicts.append(Conflict(
                company=fact.company, metric_code=fact.metric_code,
                period_label=fact.period_label, ocr_value=fact.value_num,
                vlm_value=other.value_num, doc_path=doc_path,
            ))

    merged.extend(fact for key, fact in vlm_by_key.items() if key not in matched)
    return merged, conflicts
