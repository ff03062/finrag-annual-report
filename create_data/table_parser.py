"""HTML 表格 → 结构化事实（OCR 路）。

数据源是 PaddleOCR-VL 转出的 md，表格保留成 HTML。只解析三类表：主要会计数据、
分季度主要会计数据、股东总数；其余表转成文本供检索，否则内容永久丢失。

两条反直觉的约定：

- 期间类型以 metric_dict 为准，不看表头文字。总资产那行的表头写「2025年」，但它
  是年末时点值，按表头判断会生成一条错误的「2025年」记录。
- 增减列不入库。口径混乱（`%` / 无 `%` / 「增加 0.9 个百分点」），且可由两年数值反算。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from bs4 import BeautifulSoup

from create_data.metric_dict import (
    METRICS,
    normalize_number,
    parse_unit_line,
    resolve_metric,
)

TABLE_RE = re.compile(r"<table\b.*?</table>", re.S | re.I)
YEAR_RE = re.compile(r"(20\d\d)")
COUNT_RE = re.compile(r"(\d[\d,\s]*)")

# 已结构化入库的表类型；在正文里只留占位符，避免和 SQL 通道重复
FACT_TABLE_TYPES = frozenset({"annual_metrics", "quarterly", "shareholder_count"})

QUARTER_RANGES = (
    ("2025-01-01", "2025-03-31", "Q1"),
    ("2025-04-01", "2025-06-30", "Q2"),
    ("2025-07-01", "2025-09-30", "Q3"),
    ("2025-10-01", "2025-12-31", "Q4"),
)


@dataclass
class TableBlock:
    index: int
    grid: list[list[str]]
    context: list[str]
    table_type: str = "other"
    title: str = ""
    unit_raw: str = "元"
    unit_factor: float = 1.0


@dataclass
class Fact:
    company: str
    report_year: int
    metric_code: str
    period_type: str
    period_label: str
    period_start: str | None
    period_end: str
    value_num: float
    value_yuan: float | None
    unit_raw: str
    unit_kind: str
    table_index: int | None
    row_index: int | None
    col_index: int | None
    raw_text: str
    raw_header: str
    source: str = "ocr"
    source_page: int | None = None

    def as_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class DocumentFacts:
    company: str
    report_year: int
    doc_path: str
    facts: list[Fact] = field(default_factory=list)
    tables: list[TableBlock] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _cell_text(td) -> str:
    return re.sub(r"\s+", " ", td.get_text(" ", strip=True)).strip()


def html_to_grid(table_html: str) -> list[list[str]]:
    soup = BeautifulSoup(table_html, "lxml")
    return [[_cell_text(td) for td in tr.find_all(["td", "th"])]
            for tr in soup.find_all("tr")]


def repair_rows(grid: list[list[str]]) -> list[list[str]]:
    """把 OCR 拆断的一行拼回去。

    实测通达动力：「归属于上市公司股东」和「的净资产」被拆成两行，第二行数值全空。
    判据是「首列有字、其余列全空」，这种行只可能是上一行的续行。
    """
    out: list[list[str]] = []
    for row in grid:
        if not row:
            continue
        head, rest = row[0].strip(), row[1:]
        if all(not c.strip() for c in rest):
            if not head:
                continue
            if out and len(out[-1]) >= 2:
                out[-1][0] = (out[-1][0] + head).strip()
            continue
        out.append(list(row))
    return out


def is_header_row(row: list[str]) -> bool:
    if not row:
        return False
    if row[0].strip() not in {"", "项目"}:
        return False
    return sum(1 for c in row if YEAR_RE.search(c)) >= 3


def parse_header_periods(header_row: list[str]) -> list[tuple[str, int | None, str]]:
    """表头行 → [(role, year, raw)]。

    role: value_point（含「末」）/ value_span（含「年」）/ delta（增减列）
    """
    periods: list[tuple[str, int | None, str]] = []
    for cell in header_row[1:]:
        raw = cell.strip()
        match = YEAR_RE.search(raw)
        if "增减" in raw or not match:
            periods.append(("delta", None, raw))
            continue
        year = int(match.group(1))
        periods.append(("value_point" if "末" in raw else "value_span", year, raw))
    return periods


def classify_table(block: TableBlock) -> str:
    grid = block.grid
    if not grid:
        return "other"
    header = " ".join(grid[0])
    first_col = " ".join(row[0] for row in grid if row)

    if any("第一季度" in c for c in grid[0]):
        return "quarterly"
    if is_header_row(grid[0]) and ("营业收入" in first_col or "总资产" in first_col):
        return "annual_metrics"
    if "股东总数" in header or "股东总数" in first_col:
        return "shareholder_count"
    if "股东名称" in header or "持股比例" in header:
        return "shareholders"
    if "债券名称" in header:
        return "bonds"
    if any(k in header or k in first_col for k in ("股票简称", "股票代码", "董事会秘书")):
        return "company_profile"
    return "other"


def table_title(context: list[str]) -> str:
    for line in reversed(context):
        text = line.strip()
        if not text or text.startswith("<table"):
            continue
        if "单位" in text and len(text) < 30:
            continue
        return text.lstrip("#").strip()[:64]
    return ""


def _new_fact(block: TableBlock, company: str, report_year: int, metric_code: str,
              row_index: int, col_index: int, value: float, raw_text: str,
              raw_header: str, period_label: str, period_start: str | None,
              period_end: str) -> Fact:
    """填一个 Fact。单位换算和指标元数据查表都在这里，期间由调用方给出。"""
    meta = METRICS[metric_code]
    return Fact(
        company=company,
        report_year=report_year,
        metric_code=metric_code,
        period_type=meta["period_type"],
        period_label=period_label,
        period_start=period_start,
        period_end=period_end,
        value_num=value,
        value_yuan=value * block.unit_factor if meta["unit_kind"] == "currency" else None,
        unit_raw=block.unit_raw,
        unit_kind=meta["unit_kind"],
        table_index=block.index,
        row_index=row_index,
        col_index=col_index,
        raw_text=raw_text,
        raw_header=raw_header,
    )


def parse_annual_facts(block: TableBlock, company: str, report_year: int) -> list[Fact]:
    """主要会计数据表 → facts。

    有些公司（中芯国际 / 比亚迪 / 通达 / 宁德）在一张表里放了时点组和期间组两组表头，
    所以 periods 要跟着最新识别到的表头滚动替换。
    """
    facts: list[Fact] = []
    periods: list[tuple[str, int | None, str]] = []

    for r_i, row in enumerate(block.grid):
        if is_header_row(row):
            periods = parse_header_periods(row)
            continue
        if not row or not row[0].strip():
            continue
        metric_code = resolve_metric(row[0])
        if not metric_code:
            continue

        point_in_time = METRICS[metric_code]["period_type"] == "point_in_time"
        for c_i, cell in enumerate(row[1:], start=1):
            if c_i - 1 >= len(periods):
                break
            role, year, raw_header = periods[c_i - 1]
            if role == "delta" or year is None:
                continue
            value = normalize_number(cell)
            if value is None:
                continue
            facts.append(_new_fact(
                block, company, report_year, metric_code, r_i, c_i, value,
                cell.strip(), raw_header,
                f"{year}年末" if point_in_time else f"{year}年",
                None if point_in_time else f"{year}-01-01",
                f"{year}-12-31",
            ))
    return facts


def parse_quarter_facts(block: TableBlock, company: str, report_year: int) -> list[Fact]:
    facts: list[Fact] = []
    for r_i, row in enumerate(block.grid):
        if not row or not row[0].strip():
            continue
        metric_code = resolve_metric(row[0])
        if not metric_code:
            continue
        for q, (start, end, tag) in enumerate(QUARTER_RANGES):
            c_i = q + 1
            if c_i >= len(row):
                break
            value = normalize_number(row[c_i])
            if value is None:
                continue
            facts.append(_new_fact(
                block, company, report_year, metric_code, r_i, c_i, value,
                row[c_i].strip(), tag, f"{report_year}{tag}", start, end,
            ))
    return facts


def _extract_count(cell: str) -> float | None:
    """「174,955户」「685,147（其中 A 股股东为 685,...）」→ 数值。"""
    match = COUNT_RE.search(cell.split("（")[0].split("(")[0])
    return normalize_number(match.group(1)) if match else None


def parse_shareholder_count_facts(block: TableBlock, company: str,
                                  report_year: int) -> list[Fact]:
    """股东总数表 → 1 条事实。

    只取「报告期末」口径。「披露日前上一月末」是另一个时点，混进来会让查询出现两个值。
    """
    for r_i, row in enumerate(block.grid):
        for c_i, cell in enumerate(row):
            text = cell.strip()
            if "股东总数" not in text:
                continue
            if any(k in text for k in ("优先股", "表决权", "披露日前", "上一月末", "上月末")):
                continue
            if c_i + 1 >= len(row):
                continue
            value = _extract_count(row[c_i + 1])
            if value is None:
                continue
            return [Fact(
                company=company, report_year=report_year,
                metric_code="shareholder_total", period_type="point_in_time",
                period_label=f"{report_year}年末", period_start=None,
                period_end=f"{report_year}-12-31", value_num=value, value_yuan=None,
                unit_raw="户", unit_kind="count", table_index=block.index,
                row_index=r_i, col_index=c_i + 1,
                raw_text=row[c_i + 1].strip(), raw_header=text,
            )]
    return []


def table_to_text(block: TableBlock, max_rows: int = 40) -> str:
    """不入库的表格转成文本，格式是 `行名：列名=值；列名=值`，比 HTML 更适合向量化。"""
    grid = block.grid
    if not grid:
        return ""

    header, data_start = grid[0], 1
    if len(grid) > 1 and sum(1 for c in header if c.strip()) <= 1:
        header, data_start = grid[1], 2

    cols = [c.strip() for c in header]
    lines = [f"[表格] {block.title}（单位：{block.unit_raw}）"]
    for row in grid[data_start:data_start + max_rows]:
        if not row or not any(c.strip() for c in row):
            continue
        cells = [f"{cols[i] if i < len(cols) and cols[i] else f'第{i}列'}={cell.strip()}"
                 for i, cell in enumerate(row[1:], start=1) if cell.strip()]
        if cells:
            lines.append(f"{row[0].strip()}：" + "；".join(cells))
        elif row[0].strip():
            lines.append(row[0].strip())
    return "\n".join(lines)


def parse_tables(md_text: str) -> list[TableBlock]:
    blocks: list[TableBlock] = []
    for i, match in enumerate(TABLE_RE.finditer(md_text), start=1):
        context = [ln.strip() for ln in md_text[:match.start()].splitlines() if ln.strip()][-6:]
        grid = repair_rows(html_to_grid(match.group(0)))
        if not grid:
            continue
        unit_raw, unit_factor = parse_unit_line("\n".join(context))
        block = TableBlock(index=i, grid=grid, context=context,
                           unit_raw=unit_raw, unit_factor=unit_factor,
                           title=table_title(context))
        block.table_type = classify_table(block)
        blocks.append(block)
    return blocks


PARSERS = {
    "annual_metrics": parse_annual_facts,
    "quarterly": parse_quarter_facts,
    "shareholder_count": parse_shareholder_count_facts,
}


def parse_document(path: str | Path, company: str, report_year: int) -> DocumentFacts:
    path = Path(path)
    doc = DocumentFacts(company=company, report_year=report_year, doc_path=str(path),
                        tables=parse_tables(path.read_text(encoding="utf-8")))
    for block in doc.tables:
        parser = PARSERS.get(block.table_type)
        if parser:
            doc.facts.extend(parser(block, company, report_year))
    doc.warnings.extend(validate_facts(doc.facts, report_year))
    return doc


def validate_facts(facts: list[Fact], report_year: int) -> list[str]:
    """C1：四个季度之和必须等于年度值。

    列错位、单位错乱、数字截断都会在这里暴露。实测 15 家营收/净利润/经营现金流 45/45 通过。
    """
    values: dict[str, dict[str, float]] = {}
    for fact in facts:
        values.setdefault(fact.metric_code, {})[fact.period_label] = fact.value_num

    issues = []
    for code, by_period in values.items():
        year_total = by_period.get(f"{report_year}年")
        quarters = [by_period.get(f"{report_year}Q{i}") for i in range(1, 5)]
        if year_total is None or any(q is None for q in quarters):
            continue
        if abs(year_total - sum(quarters)) > 1:
            issues.append(f"C1 校验失败 {code}: 年度={year_total:,.2f} "
                          f"季度和={sum(quarters):,.2f}")
    return issues
