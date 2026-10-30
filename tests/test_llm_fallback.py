"""LLM 兜底测试：故意把表结构弄坏，验证兜底能修回来。

构造三种坏表：
  1. 多一列（触发 T2 列数异常）
  2. 表头行被拆成两行（触发 T1 表头识别失败）
  3. 正常表（不应触发兜底）

运行：
    python tests/test_llm_fallback.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from create_data.table_parser import (  # noqa: E402
    TableBlock,
    _html_to_grid,
    detect_fallback_reason,
    parse_annual_facts,
    parse_with_annotation,
    repair_rows,
)
from create_data.table_llm_fallback import annotate  # noqa: E402

MD = ROOT / "new_Data" / "中芯国际" / "中芯国际：中芯国际2025年年度报告摘要.md"
TABLE_RE = re.compile(r"<table\b.*?</table>", re.S | re.I)

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name} {'' if cond else detail}")
    if not cond:
        _failures.append(f"{name} {detail}")


def get_annual_html() -> str:
    text = MD.read_text(encoding="utf-8")
    for m in TABLE_RE.finditer(text):
        grid = repair_rows(_html_to_grid(m.group(0)))
        if grid and any("营业收入" in r[0] for r in grid if r):
            return m.group(0)
    raise RuntimeError("没找到主表")


def make_block(html: str, unit="千元", factor=1000.0) -> TableBlock:
    grid = repair_rows(_html_to_grid(html))
    return TableBlock(index=1, grid=grid, context=[], table_type="annual_metrics",
                      title="近3年的主要会计数据和财务指标", unit_raw=unit,
                      unit_factor=factor)


def add_extra_column(html: str) -> str:
    """每行末尾加一个空单元格 → 列数 5 → 6。"""
    return html.replace("</tr>", "<td>备注</td></tr>")


def split_header_row(html: str) -> str:
    """把表头行的年份拆成两行（年份行 + 单位行），首列变成非空文本。"""
    return html.replace("<td style='text-align: center; word-wrap: break-word;'>2025年末</td>",
                        "<td>2025</td></tr><tr><td>年末</td>", 1)


def test_normal() -> None:
    print("\n[用例1] 正常表：不应触发兜底")
    block = make_block(get_annual_html())
    facts = parse_annual_facts(block, "中芯国际", 2025)
    reason = detect_fallback_reason(block, facts)
    check("不触发", reason is None, f"got={reason}")
    check("facts 有值", len(facts) > 20, f"got={len(facts)}")


def test_extra_column() -> None:
    print("\n[用例2] 多一列：触发 T2，LLM 标注后修复")
    block = make_block(add_extra_column(get_annual_html()))
    facts = parse_annual_facts(block, "中芯国际", 2025)
    reason = detect_fallback_reason(block, facts)
    check("触发 T2", reason is not None and reason.startswith("T2"), f"got={reason}")

    annotation = annotate(block)
    check("LLM 返回标注", annotation is not None)
    if not annotation:
        return
    print(f"  confidence={annotation.confidence:.2f} groups={len(annotation.header_groups)} "
          f"rows={len(annotation.row_metrics)}")
    check("置信度 >= 0.6", annotation.confidence >= 0.6, f"got={annotation.confidence}")

    fixed = parse_with_annotation(block, annotation, "中芯国际", 2025)
    check("修复后取到 facts", len(fixed) >= 20, f"got={len(fixed)}")
    # 注意：T2 是表结构属性（表确实还是 6 列），修复后仍会报 T2，
    # 真正要看的是「取出来的数值是否正确」——下面逐项比对。

    # 关键数值必须对得上（和正常表一致）
    def find(fs, code, label):
        for f in fs:
            if f.metric_code == code and f.period_label == label:
                return f.value_num
        return None

    normal = parse_annual_facts(make_block(get_annual_html()), "中芯国际", 2025)
    for code, label in [("revenue", "2025年"), ("total_assets", "2025年末"),
                        ("net_profit_attributable", "2025年")]:
        check(f"  {code} 与正常表一致",
              find(fixed, code, label) == find(normal, code, label),
              f"{find(fixed, code, label)} vs {find(normal, code, label)}")


def main() -> int:
    test_normal()
    test_extra_column()

    print("\n" + "=" * 60)
    if _failures:
        print(f"失败 {len(_failures)} 项：")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
