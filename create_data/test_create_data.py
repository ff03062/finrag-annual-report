"""create_data 测试：表格解析 + 切块。

运行：
    python create_data/test_create_data.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from create_data.chunker import (  # noqa: E402
    MAX_CHARS,
    TARGET_CHARS,
    build_chunks,
    split_sections,
    split_text,
)
from create_data.metric_dict import normalize_number, parse_unit_line, resolve_metric  # noqa: E402
from create_data.table_parser import parse_document, repair_rows  # noqa: E402
from create_data.vlm_extract import _is_continuation  # noqa: E402

DATA_DIR = ROOT / "new_Data"
REPORT_YEAR = 2025
CODE_RE = re.compile(r"(?:证券|公司)代码\s*[：:]\s*(\d{6})")
IMG_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}

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


def test_continuation() -> None:
    print("\n[单元] 跨页续行判定")
    check("完整行名 → 续行", _is_continuation([["营业收入", "1"]]))
    check("断掉的半截行名 → 续行", _is_continuation([["的净资产", "", ""]]))
    check("表头行（首列空）→ 不是", not _is_continuation([["", "2025年末", "2024年末"]]))
    check("分季度表头 → 不是", not _is_continuation([["第一季度", "1"]]))


def test_split_text() -> None:
    print("\n[单元] 句边界切块")
    text = "。".join(f"这是第{i}句话，用来测试切块逻辑是否稳定" for i in range(60)) + "。"
    pieces = split_text(text)
    check("块数 > 1", len(pieces) > 1, f"got={len(pieces)}")
    check("每块 <= 硬上限", all(len(p) <= MAX_CHARS for p in pieces),
          f"max={max(len(p) for p in pieces)}")
    average = sum(len(p) for p in pieces) / len(pieces)
    check("平均块长接近目标", abs(average - TARGET_CHARS) < 150, f"avg={average:.0f}")


def test_split_sections() -> None:
    print("\n[单元] 标题切段")
    md = "# 标题\n\n## 第一节\n\n正文一\n\n### 小节\n\n正文二\n"
    sections = split_sections(md, [])
    paths = [" > ".join(s.path) for s in sections]
    # 「标题」那段没有正文被过滤，正文各自归到最近一级标题下
    check("段数 == 2", len(sections) == 2, f"got={paths}")
    check("有多级路径", any(">" in p for p in paths), f"got={paths}")


# ---------------------------------------------------------------- 真实数据


def test_documents() -> list:
    print("\n[集成] 15 份摘要 → facts + chunks")
    docs = []
    total_facts = total_chunks = 0

    for co_dir in sorted(p for p in DATA_DIR.iterdir() if p.is_dir()):
        md = next(co_dir.glob("*.md"), None)
        if md is None:
            continue

        match = CODE_RE.search(md.read_text(encoding="utf-8"))
        code = match.group(1) if match else ""
        doc = parse_document(md, co_dir.name, REPORT_YEAR)
        chunks = build_chunks(md, co_dir.name, code, REPORT_YEAR, doc.tables)
        docs.append((co_dir.name, doc, chunks))
        total_facts += len(doc.facts)
        total_chunks += len(chunks)

        annual = sum(1 for f in doc.facts if "Q" not in f.period_label)
        quarter = sum(1 for f in doc.facts if "Q" in f.period_label)
        images = [p for p in (co_dir / "img").iterdir() if p.suffix.lower() in IMG_SUFFIXES] \
            if (co_dir / "img").is_dir() else []
        figures = sum(1 for c in chunks if c.chunk_type == "figure")

        print(f"  {co_dir.name:<6} facts={len(doc.facts):>3} (年度 {annual:>2}/季度 {quarter:>2}) "
              f"chunks={len(chunks):>3} (narrative {sum(1 for c in chunks if c.chunk_type == 'narrative'):>2} "
              f"table {sum(1 for c in chunks if c.chunk_type == 'table_summary'):>2} figure {figures:>2})")

        check(f"{co_dir.name} 无校验失败", not doc.warnings, str(doc.warnings))
        check(f"{co_dir.name} 年度 facts 在 24~38", 24 <= annual <= 38, f"got={annual}")
        check(f"{co_dir.name} 季度 facts == 16", quarter == 16, f"got={quarter}")
        check(f"{co_dir.name} 有股东总数",
              any(f.metric_code == "shareholder_total" for f in doc.facts))

        check(f"{co_dir.name} 无 HTML 残留", not any("<table" in c.text.lower() for c in chunks))
        check(f"{co_dir.name} 块长不超上限",
              all(len(c.text) <= MAX_CHARS for c in chunks),
              f"max={max(len(c.text) for c in chunks)}")
        check(f"{co_dir.name} figure 块 == 图片数", figures == len(images),
              f"{figures} vs {len(images)}")
        check(f"{co_dir.name} 元数据完整",
              all(c.company and c.chunk_type and c.doc_path for c in chunks))

    check("facts 总数 == 681", total_facts == 681, f"got={total_facts}")
    check("chunks 总数 == 514", total_chunks == 514, f"got={total_chunks}")
    return docs


def test_spot_values(docs: list) -> None:
    print("\n[集成] 关键数值抽查")
    by_company = {name: doc for name, doc, _ in docs}

    def get(company: str, code: str, label: str):
        for fact in by_company[company].facts:
            if fact.metric_code == code and fact.period_label == label:
                return fact
        return None

    cases = [
        ("中芯国际", "revenue", "2025年", 67323192.0, "千元"),
        ("比亚迪", "revenue", "2025年", 803964958000.0, "元"),
        ("宁德时代", "total_assets", "2025年末", 974827544.0, "千元"),
        ("中芯国际", "revenue", "2025Q1", 16301085.0, "千元"),
        ("中微公司", "rd_revenue_ratio", "2025年", 30.23, "元"),
        ("中芯国际", "shareholder_total", "2025年末", 322045.0, "户"),
    ]
    for company, code, label, expect, unit in cases:
        fact = get(company, code, label)
        check(f"{company} {code} {label} 存在", fact is not None)
        if fact:
            check(f"  数值 {expect:,.2f}", fact.value_num == expect, f"got={fact.value_num:,.2f}")
            check(f"  单位 {unit}", fact.unit_raw == unit, f"got={fact.unit_raw}")


def test_table_classification(docs: list) -> None:
    print("\n[集成] 表分类统计")
    stat: dict[str, int] = {}
    for _, doc, _ in docs:
        for table in doc.tables:
            stat[table.table_type] = stat.get(table.table_type, 0) + 1
    for kind, count in sorted(stat.items(), key=lambda item: -item[1]):
        print(f"  {kind:<18} {count}")
    check("annual_metrics 有 15 张", stat.get("annual_metrics") == 15,
          f"got={stat.get('annual_metrics')}")
    check("quarterly 有 15 张", stat.get("quarterly") == 15,
          f"got={stat.get('quarterly')}")


def main() -> int:
    test_normalize_number()
    test_resolve_metric()
    test_repair_rows()
    test_unit()
    test_continuation()
    test_split_text()
    test_split_sections()

    docs = test_documents()
    test_spot_values(docs)
    test_table_classification(docs)

    print("\n" + "=" * 60)
    if _failures:
        print(f"失败 {len(_failures)} 项：")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
