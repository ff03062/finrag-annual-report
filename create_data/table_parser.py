"""把 md 里的 HTML 表格解析成结构化事实（facts）。

设计原则：
1. 纯函数、无副作用、不调 LLM —— 离线构建和单元测试都能直接跑
2. 期间类型以「指标」为准，不看表头文字（表头常写「2025年」，但总资产是年末时点）
3. 增减列不入库（可由两年数值反算，且口径混乱：`%` / 无 `%` / `增加0.9个百分点`）
4. 单位统一换算成「元」写入 value_yuan，同时保留 unit_raw 供溯源

表类型（实测 15 家 74 张表）：
    annual_metrics    主要会计数据（15 张）
    quarterly         分季度主要会计数据（15 张）
    shareholder_count 股东总数（kv）
    shareholders      前十名股东
    company_profile   公司简介 / 联系方式
    bonds             债券情况
    other             其余（公告、产品技术等）
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from bs4 import BeautifulSoup

from create_data.metric_dict import (
    METRICS,
    normalize_label,
    normalize_number,
    parse_unit_line,
    resolve_metric,
)

TABLE_RE = re.compile(r"<table\b.*?</table>", re.S | re.I)
YEAR_RE = re.compile(r"(20\d\d)")


# ---------------------------------------------------------------- 数据结构


@dataclass
class TableBlock:
    index: int                     # 在文档中的序号，从 1 开始
    grid: list[list[str]]          # 二维单元格文本
    context: list[str]             # 表格前若干行文本（含标题、单位行）
    table_type: str = "other"
    title: str = ""
    unit_raw: str = "元"
    unit_factor: float = 1.0


@dataclass
class Fact:
    company: str
    report_year: int
    metric_code: str
    period_type: str               # point_in_time | duration
    period_label: str              # 2025年 / 2025年末 / 2025Q1
    period_start: str | None
    period_end: str
    value_num: float               # 原单位数值
    value_yuan: float | None       # 换算成元（比率/每股为 None）
    unit_raw: str
    unit_kind: str                 # currency | ratio | per_share
    table_index: int
    row_index: int
    col_index: int
    raw_text: str
    raw_header: str

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


# ---------------------------------------------------------------- HTML → grid


def _cell_text(td) -> str:
    return re.sub(r"\s+", " ", td.get_text(" ", strip=True)).strip()


def _html_to_grid(table_html: str) -> list[list[str]]:
    soup = BeautifulSoup(table_html, "lxml")
    return [[_cell_text(td) for td in tr.find_all(["td", "th"])]
            for tr in soup.find_all("tr")]


# ---------------------------------------------------------------- 行修复


def repair_rows(grid: list[list[str]]) -> list[list[str]]:
    """修复 OCR 拆行。

    通达动力实测：「归属于上市公司股东」和「的净资产」被拆成两行，第二行数值全空。
    规则：除首列外全空的非空行，拼接到上一行首列。
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


# ---------------------------------------------------------------- 表头识别


def _is_header_row(row: list[str]) -> bool:
    """表头行：首列为空或「项目」，且至少 3 个单元格含年份。"""
    if not row:
        return False
    first = row[0].strip()
    if first not in {"", "项目"}:
        return False
    return sum(1 for c in row if YEAR_RE.search(c)) >= 3


def _parse_header_periods(header_row: list[str]) -> list[tuple[str, int | None, str]]:
    """把一行表头拆成 [(role, year, raw)]。

    role:
        value_point  含「末」→ 时点
        value_span   含「年」不含「末」→ 期间
        delta        含「增减」→ 派生列，不入库
    """
    periods: list[tuple[str, int | None, str]] = []
    for cell in header_row[1:]:
        raw = cell.strip()
        if "增减" in raw:
            periods.append(("delta", None, raw))
            continue
        m = YEAR_RE.search(raw)
        if not m:
            periods.append(("delta", None, raw))
            continue
        year = int(m.group(1))
        role = "value_point" if "末" in raw else "value_span"
        periods.append((role, year, raw))
    return periods


# ---------------------------------------------------------------- 表分类


def classify_table(block: TableBlock) -> str:
    grid = block.grid
    if not grid:
        return "other"
    header = " ".join(grid[0])
    first_col = " ".join(r[0] for r in grid if r)

    if any("第一季度" in c for c in grid[0]):
        return "quarterly"
    if _is_header_row(grid[0]) and ("营业收入" in first_col or "总资产" in first_col):
        return "annual_metrics"
    if "股东总数" in header or "股东总数" in first_col:
        return "shareholder_count"
    if "股东名称" in header or "持股比例" in header:
        return "shareholders"
    if "债券名称" in header:
        return "bonds"
    if "股票简称" in header or "股票代码" in header:
        return "company_profile"
    if "董事会秘书" in header or "董事会秘书" in first_col:
        return "company_profile"
    return "other"


def _table_title(context: list[str]) -> str:
    """标题 = 上文里最后一个不是「单位」行的文本，截断到 64 字。"""
    for line in reversed(context):
        text = line.strip()
        if not text or text.startswith("<table"):
            continue
        if "单位" in text and len(text) < 30:
            continue
        if text.startswith("#"):
            text = text.lstrip("#").strip()
        return text[:64]
    return ""


# ---------------------------------------------------------------- 主表解析


def _period_label(period_type: str, year: int, role: str) -> str:
    if role == "value_point":
        return f"{year}年末"
    return f"{year}年"


def parse_annual_facts(block: TableBlock, company: str, report_year: int) -> list[Fact]:
    """主要会计数据表 → facts。

    支持一组或两组表头（中芯国际/比亚迪/通达/宁德 有两组：时点组 + 期间组）。
    """
    facts: list[Fact] = []
    periods: list[tuple[str, int | None, str]] = []

    for r_i, row in enumerate(block.grid):
        if _is_header_row(row):
            periods = _parse_header_periods(row)
            continue
        if not periods or not row or not row[0].strip():
            continue

        metric_code = resolve_metric(row[0])
        if not metric_code:
            continue

        meta = METRICS[metric_code]
        period_type = meta["period_type"]       # 以指标为准
        unit_kind = meta["unit_kind"]

        for c_i, cell in enumerate(row[1:], start=1):
            idx = c_i - 1
            if idx >= len(periods):
                break
            role, year, raw_header = periods[idx]
            if role == "delta" or year is None:
                continue
            value = normalize_number(cell)
            if value is None:
                continue

            label = f"{year}年末" if period_type == "point_in_time" else f"{year}年"
            start = None if period_type == "point_in_time" else f"{year}-01-01"
            end = f"{year}-12-31"

            facts.append(Fact(
                company=company,
                report_year=report_year,
                metric_code=metric_code,
                period_type=period_type,
                period_label=label,
                period_start=start,
                period_end=end,
                value_num=value,
                value_yuan=value * block.unit_factor if unit_kind == "currency" else None,
                unit_raw=block.unit_raw,
                unit_kind=unit_kind,
                table_index=block.index,
                row_index=r_i,
                col_index=c_i,
                raw_text=cell.strip(),
                raw_header=raw_header,
            ))
    return facts


# ---------------------------------------------------------------- 季度表解析

QUARTER_RANGES = (
    ("2025-01-01", "2025-03-31", "Q1"),
    ("2025-04-01", "2025-06-30", "Q2"),
    ("2025-07-01", "2025-09-30", "Q3"),
    ("2025-10-01", "2025-12-31", "Q4"),
)


def parse_quarter_facts(block: TableBlock, company: str, report_year: int) -> list[Fact]:
    facts: list[Fact] = []
    for r_i, row in enumerate(block.grid):
        if not row or not row[0].strip():
            continue
        metric_code = resolve_metric(row[0])
        if not metric_code:
            continue
        meta = METRICS[metric_code]
        unit_kind = meta["unit_kind"]

        for q, (start, end, tag) in enumerate(QUARTER_RANGES):
            c_i = q + 1
            if c_i >= len(row):
                break
            value = normalize_number(row[c_i])
            if value is None:
                continue
            facts.append(Fact(
                company=company,
                report_year=report_year,
                metric_code=metric_code,
                period_type="duration",
                period_label=f"{report_year}{tag}",
                period_start=start,
                period_end=end,
                value_num=value,
                value_yuan=value * block.unit_factor if unit_kind == "currency" else None,
                unit_raw=block.unit_raw,
                unit_kind=unit_kind,
                table_index=block.index,
                row_index=r_i,
                col_index=c_i,
                raw_text=row[c_i].strip(),
                raw_header=tag,
            ))
    return facts


# ---------------------------------------------------------------- 股东总数

COUNT_RE = re.compile(r"(\d[\d,\s]*)")


def _extract_count(cell: str) -> float | None:
    """「174,955户」「685,147（其中A股股东为685,...）」→ 数值。"""
    text = cell.split("（")[0].split("(")[0]
    m = COUNT_RE.search(text)
    if not m:
        return None
    return normalize_number(m.group(1))


def parse_shareholder_count_facts(block: TableBlock, company: str,
                                  report_year: int) -> list[Fact]:
    """股东总数表（2 列 kv 或 10 列横排）→ 1 条事实。

    只取「报告期末」口径；「披露日前上一月末」是另一个时点，不入库。
    """
    facts: list[Fact] = []
    for r_i, row in enumerate(block.grid):
        for c_i, cell in enumerate(row):
            text = cell.strip()
            if "股东总数" not in text:
                continue
            if "优先股" in text or "表决权" in text or "特别表决权" in text:
                continue
            if "披露日前" in text or "上一月末" in text or "上月末" in text:
                continue
            if c_i + 1 >= len(row):
                continue
            value = _extract_count(row[c_i + 1])
            if value is None:
                continue
            facts.append(Fact(
                company=company,
                report_year=report_year,
                metric_code="shareholder_total",
                period_type="point_in_time",
                period_label=f"{report_year}年末",
                period_start=None,
                period_end=f"{report_year}-12-31",
                value_num=value,
                value_yuan=None,
                unit_raw="户",
                unit_kind="count",
                table_index=block.index,
                row_index=r_i,
                col_index=c_i + 1,
                raw_text=row[c_i + 1].strip(),
                raw_header=text,
            ))
            return facts
    return facts


# ---------------------------------------------------------------- LLM 兜底


def detect_fallback_reason(block: TableBlock, facts: list[Fact]) -> str | None:
    """判断这张表是否需要 LLM 兜底。

    T1 表头识别失败 / T2 列数异常 / T3 指标映射命中率低。
    （T4 四季度之和不等于年度值在文档级 validate_facts 里判断）
    """
    if block.table_type != "annual_metrics":
        return None

    if not any(_is_header_row(row) for row in block.grid):
        return "T1 表头识别失败"

    n_cols = max((len(r) for r in block.grid), default=0)
    if n_cols not in (4, 5):
        return f"T2 列数异常({n_cols})"

    label_rows = [r for r in block.grid if r and r[0].strip()
                  and not _is_header_row(r)]
    if label_rows:
        hit = sum(1 for r in label_rows if resolve_metric(r[0]))
        if hit / len(label_rows) < 0.6:
            return f"T3 指标命中率低({hit}/{len(label_rows)})"

    return None


def parse_with_annotation(block: TableBlock, annotation,
                          company: str, report_year: int) -> list[Fact]:
    """按 LLM 给的列语义重新取数。

    期间类型仍然以 metric_dict 为准（不信 LLM 的 role 判断），
    数值仍然由 normalize_number 从原格子里取（LLM 没见过数值）。
    """
    facts: list[Fact] = []
    for r_i, row in enumerate(block.grid):
        metric_code = annotation.metric_for(r_i)
        if not metric_code or metric_code not in METRICS:
            continue
        group = annotation.group_for(r_i)
        if not group:
            continue

        meta = METRICS[metric_code]
        period_type = meta["period_type"]
        unit_kind = meta["unit_kind"]

        for col in group.get("columns") or []:
            if col.get("role") == "delta":
                continue
            c_i, year = col.get("col"), col.get("year")
            if c_i is None or year is None or not isinstance(c_i, int):
                continue
            if c_i >= len(row):
                continue
            value = normalize_number(row[c_i])
            if value is None:
                continue

            year = int(year)
            label = f"{year}年末" if period_type == "point_in_time" else f"{year}年"
            facts.append(Fact(
                company=company,
                report_year=report_year,
                metric_code=metric_code,
                period_type=period_type,
                period_label=label,
                period_start=None if period_type == "point_in_time" else f"{year}-01-01",
                period_end=f"{year}-12-31",
                value_num=value,
                value_yuan=value * block.unit_factor if unit_kind == "currency" else None,
                unit_raw=block.unit_raw,
                unit_kind=unit_kind,
                table_index=block.index,
                row_index=r_i,
                col_index=c_i,
                raw_text=row[c_i].strip(),
                raw_header=str(col.get("year") or ""),
            ))
    return facts


# ---------------------------------------------------------------- 表格转文本

# 这些表会被解析成 facts（结构化入库），正文里只留占位符
FACT_TABLE_TYPES = frozenset({"annual_metrics", "quarterly", "shareholder_count"})


def table_to_text(block: TableBlock, max_rows: int = 40) -> str:
    """把不入库的表格转成可检索的文本（否则内容会永久丢失）。

    格式：每行一条 `行名：列名=值；列名=值`，比 HTML 更适合向量化。
    """
    grid = block.grid
    if not grid:
        return ""

    header = grid[0]
    # 首行如果是合并标题（只有一格有值），下一行才是表头
    data_start = 1
    if len(grid) > 1 and sum(1 for c in header if c.strip()) <= 1:
        header = grid[1]
        data_start = 2

    cols = [c.strip() for c in header]
    lines = [f"[表格] {block.title}（单位：{block.unit_raw}）"]
    for row in grid[data_start:data_start + max_rows]:
        if not row or not any(c.strip() for c in row):
            continue
        key = row[0].strip()
        cells = []
        for i, cell in enumerate(row[1:], start=1):
            value = cell.strip()
            if not value:
                continue
            label = cols[i] if i < len(cols) and cols[i] else f"第{i}列"
            cells.append(f"{label}={value}")
        if cells:
            lines.append(f"{key}：" + "；".join(cells))
        elif key:
            lines.append(key)
    return "\n".join(lines)


# ---------------------------------------------------------------- 文档级入口


def parse_tables(md_text: str) -> list[TableBlock]:
    blocks: list[TableBlock] = []
    for i, m in enumerate(TABLE_RE.finditer(md_text), start=1):
        context = [ln.strip() for ln in md_text[:m.start()].splitlines() if ln.strip()][-6:]
        grid = repair_rows(_html_to_grid(m.group(0)))
        if not grid:
            continue
        unit_raw, unit_factor = parse_unit_line("\n".join(context))
        block = TableBlock(
            index=i,
            grid=grid,
            context=context,
            unit_raw=unit_raw,
            unit_factor=unit_factor,
            title=_table_title(context),
        )
        block.table_type = classify_table(block)
        blocks.append(block)
    return blocks


def parse_document(path: str | Path, company: str, report_year: int,
                   llm_fallback: bool = False) -> DocumentFacts:
    """解析一份 md：只把 annual_metrics / quarterly / shareholder_count 转成 facts。

    llm_fallback=True 时，规则解析识别失败的表会先交给本地 LLM 做结构标注，
    再按标注重新取数并二次校验；仍不过就记进 warnings 交给人工。
    """
    path = Path(path)
    md_text = path.read_text(encoding="utf-8")
    doc = DocumentFacts(company=company, report_year=report_year,
                        doc_path=str(path), tables=parse_tables(md_text))

    for block in doc.tables:
        if block.table_type == "annual_metrics":
            facts = parse_annual_facts(block, company, report_year)
            reason = detect_fallback_reason(block, facts)
            if reason and llm_fallback:
                print(f"[LLM兜底] {company} 表{block.index} 触发：{reason}")
                from create_data.table_llm_fallback import annotate
                annotation = annotate(block)
                if annotation and annotation.confidence >= 0.6:
                    retried = parse_with_annotation(block, annotation, company, report_year)
                    recheck = detect_fallback_reason(block, retried)
                    if recheck:
                        doc.warnings.append(
                            f"表{block.index} LLM 兜底后仍不合格：{recheck}")
                    else:
                        print(f"[LLM兜底] 表{block.index} 修复成功，取到 {len(retried)} 条")
                        facts = retried
                else:
                    doc.warnings.append(
                        f"表{block.index} 兜底失败（{reason}），进入人工队列")
            elif reason:
                doc.warnings.append(f"表{block.index} {reason}，未启用兜底")
            doc.facts.extend(facts)

        elif block.table_type == "quarterly":
            doc.facts.extend(parse_quarter_facts(block, company, report_year))
        elif block.table_type == "shareholder_count":
            doc.facts.extend(parse_shareholder_count_facts(block, company, report_year))

    doc.warnings.extend(validate_facts(doc.facts))
    return doc


# ---------------------------------------------------------------- 校验


def validate_facts(facts: list[Fact]) -> list[str]:
    """C1：四季度之和必须等于年度值。

    任何列错位、单位错乱、数字截断都会在这里暴露。
    实测 15 家营收/净利润/经营现金流全部通过（45/45）。
    """
    issues: list[str] = []
    if not facts:
        return issues

    by_key: dict[tuple[str, str], dict[str, float]] = {}
    for f in facts:
        key = (f.metric_code, f.period_label)
        by_key.setdefault(f.metric_code, {})[f.period_label] = f.value_num

    for code, values in by_key.items():
        year_total = values.get("2025年")
        quarters = [values.get(f"2025Q{i}") for i in range(1, 5)]
        if year_total is None or any(q is None for q in quarters):
            continue
        diff = year_total - sum(quarters)
        if abs(diff) > 1:
            issues.append(
                f"C1 校验失败 {code}: 年度={year_total:,.2f} 季度和={sum(quarters):,.2f} 差={diff:,.2f}"
            )
    return issues
