"""表格解析测试：15 份摘要 → facts → 校验。

运行：
    python create_data/test_table_parser.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from create_data.metric_dict import parse_unit_line, resolve_metric  # noqa: E402
from create_data.table_parser import (  # noqa: E402
    classify_table,
    normalize_number,
    parse_document,
    parse_tables,
    repair_rows,
)

DATA_DIR = ROOT / "new_Data"
REPORT_YEAR = 2025

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        _failures.append(f"{name} {detail}")


# ---------------------------------------------------------------- 单元测试


def test_normalize_number() -> None:
    print("\n[单元] 数字清洗")
    cases = {
        "367, 718, 196": 367718196.0,
        "15, 213, 372, 100. 28": 15213372100.28,
        "0. 6857": 0.6857,
        "34. 22%": 34.22,
        "-1, 171, 520": -1171520.0,
        "3.4": 3.4,
    }
    for raw, expect in cases.items():
        got = normalize_number(raw)
        check(f"{raw!r} -> {expect}", got == expect, f"got={got}")
    for null in ("-", "--", "", "不适用"):
        check(f"{null!r} -> None", normalize_number(null) is None)


def test_resolve_metric() -> None:
    print("\n[单元] 行名 → metric_code")
    cases = {
        "营业收入": "revenue",
        "总资产": "total_assets",
        "归属于上市公司股东的净资产": "net_assets_attributable",
        "归属于上市公司股东的扣除非经常性损益后的净利润": "net_profit_deducted",
        "基本每股收益（元/股）": "eps_basic",
        "基本每股收益（元 / 股）": "eps_basic",
        "加权平均净资产收益率（%）": "roe_weighted",
        "研发投入占营业收入的比例（%）": "rd_revenue_ratio",
        "项目": None,
        "商誉": None,
    }
    for label, expect in cases.items():
        got = resolve_metric(label)
        check(f"{label} -> {expect}", got == expect, f"got={got}")


def test_repair_rows() -> None:
    print("\n[单元] OCR 拆行修复（通达动力场景）")
    grid = [
        ["总资产", "2,061,459,810.17", "2,092,988,515.37"],
        ["归属于上市公司股东", "1,326,159,903.76", "1,257,259,922.24"],
        ["的净资产", "", ""],
    ]
    got = repair_rows(grid)
    check("行数 3 -> 2", len(got) == 2, f"got={len(got)}")
    check("行名已拼接", got[1][0] == "归属于上市公司股东的净资产", f"got={got[1][0]}")


def test_unit() -> None:
    print("\n[单元] 单位解析")
    cases = {
        "单位：千元 币种：人民币": ("千元", 1000.0),
        "单位：元": ("元", 1.0),
        "单位：万元": ("万元", 10000.0),
        "无单位": ("元", 1.0),
    }
    for text, (unit, factor) in cases.items():
        got = parse_unit_line(text)
        check(f"{text} -> {unit}", got == (unit, factor), f"got={got}")


# ---------------------------------------------------------------- 真实数据


def test_all_documents() -> list:
    print("\n[集成] 15 份摘要解析")
    docs = []
    total_facts = 0
    for co_dir in sorted(p for p in DATA_DIR.iterdir() if p.is_dir()):
        md = next(co_dir.glob("*.md"), None)
        if md is None:
            continue
        doc = parse_document(md, co_dir.name, REPORT_YEAR)
        docs.append((co_dir.name, doc))
        total_facts += len(doc.facts)

        annual = sum(1 for f in doc.facts if "Q" not in f.period_label)
        quarter = sum(1 for f in doc.facts if "Q" in f.period_label)
        print(f"  {co_dir.name:<6} 表={len(doc.tables)} facts={len(doc.facts):>3} "
              f"(年度 {annual} / 季度 {quarter})  单位={doc.tables[0].unit_raw if doc.tables else '-'}")

        check(f"{co_dir.name} 无校验失败", not doc.warnings, str(doc.warnings))
        check(f"{co_dir.name} 年度 facts 在 24~38", 24 <= annual <= 38, f"got={annual}")
        check(f"{co_dir.name} 季度 facts == 16", quarter == 16, f"got={quarter}")
        check(f"{co_dir.name} 有股东总数",
              any(f.metric_code == "shareholder_total" for f in doc.facts))

    check("facts 总数 == 681", total_facts == 681, f"got={total_facts}")
    return docs


def test_spot_values(docs: list) -> None:
    print("\n[集成] 关键数值抽查")
    by_co = {name: doc for name, doc in docs}

    def get(co: str, code: str, label: str):
        for f in by_co[co].facts:
            if f.metric_code == code and f.period_label == label:
                return f
        return None

    cases = [
        ("中芯国际", "revenue", "2025年", 67323192.0, "千元"),
        ("比亚迪", "revenue", "2025年", 803964958000.0, "元"),
        ("宁德时代", "total_assets", "2025年末", 974827544.0, "千元"),
        ("中芯国际", "revenue", "2025Q1", 16301085.0, "千元"),
        ("中微公司", "rd_revenue_ratio", "2025年", 30.23, "元"),
        ("中芯国际", "shareholder_total", "2025年末", 322045.0, "户"),
    ]
    for co, code, label, expect, unit in cases:
        f = get(co, code, label)
        check(f"{co} {code} {label} 存在", f is not None)
        if f:
            check(f"  数值 {expect:,.2f}", f.value_num == expect, f"got={f.value_num:,.2f}")
            check(f"  单位 {unit}", f.unit_raw == unit, f"got={f.unit_raw}")


def test_table_classification(docs: list) -> None:
    print("\n[集成] 表分类统计")
    stat: dict[str, int] = {}
    for _, doc in docs:
        for t in doc.tables:
            stat[t.table_type] = stat.get(t.table_type, 0) + 1
    for k, v in sorted(stat.items(), key=lambda x: -x[1]):
        print(f"  {k:<18} {v}")
    check("annual_metrics 有 15 张", stat.get("annual_metrics") == 15, f"got={stat.get('annual_metrics')}")
    check("quarterly 有 15 张", stat.get("quarterly") == 15, f"got={stat.get('quarterly')}")


def main() -> int:
    test_normalize_number()
    test_resolve_metric()
    test_repair_rows()
    test_unit()

    docs = test_all_documents()
    test_spot_values(docs)
    test_table_classification(docs)

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
