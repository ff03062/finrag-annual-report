"""结构化通道：查 financial.db 里的 facts。

只做参数化查询，不拼 SQL、不接受自由文本条件。
上层（QueryRouter）负责把问题解析成 (company, metric_code, period)。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from dotenv import load_dotenv

from create_data.metric_dict import METRICS, metric_name, metric_unit_kind

load_dotenv()

DB_PATH = Path(os.getenv("FIN_DB_PATH", Path(__file__).resolve().parents[1] / "db" / "financial.db"))


@contextmanager
def _connect():
    """只读连接：任何写操作都会被 SQLite 拒绝。"""
    uri = f"file:{DB_PATH.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        yield con
    finally:
        con.close()


def is_available() -> bool:
    return DB_PATH.exists()


def list_companies() -> list[str]:
    if not is_available():
        return []
    with _connect() as con:
        return [r[0] for r in con.execute("SELECT DISTINCT company FROM fact ORDER BY company")]


def resolve_company(name: str, candidates: list[str] | None = None) -> str | None:
    """公司名归一：全称/简称/带“股份有限公司”都能对上。"""
    if not name:
        return None
    name = name.strip()
    pool = candidates if candidates is not None else list_companies()
    if name in pool:
        return name
    for c in pool:
        if c in name or name in c:
            return c
    return None


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "company": row["company"],
        "period_label": row["period_label"],
        "value_num": row["value_num"],
        "value_yuan": row["value_yuan"],
        "unit_raw": row["unit_raw"],
        "unit_kind": row["unit_kind"],
        "raw_text": row["raw_text"],
        "source_doc": row["source_doc"],
        "source_table": row["source_table"],
    }


def query_metric(company: str, metric_code: str, period_label: str | None = None) -> dict:
    """单公司单指标。period_label 为空时返回该指标所有期间（按时间升序）。

    派生指标（如负债合计）先找真实披露值，找不到才按公式推算。
    """
    if metric_code not in METRICS:
        return {"status": "unsupported_metric", "metric_code": metric_code, "rows": []}
    if not is_available():
        return {"status": "db_missing", "rows": []}

    sql = ("SELECT * FROM fact WHERE company = ? AND metric_code = ?")
    params: list = [company, metric_code]
    if period_label:
        sql += " AND period_label = ?"
        params.append(period_label)
    sql += " ORDER BY period_label"

    with _connect() as con:
        rows = [_row_to_dict(r) for r in con.execute(sql, params)]

    if not rows and METRICS[metric_code].get("derived_from"):
        return _derive(company, metric_code, period_label)

    if not rows:
        return {"status": "not_disclosed", "company": company,
                "metric_code": metric_code, "period": period_label, "rows": []}
    return {
        "status": "ok",
        "company": company,
        "metric_code": metric_code,
        "metric_name": metric_name(metric_code),
        "unit_kind": metric_unit_kind(metric_code),
        "unit_raw": rows[0]["unit_raw"],
        "rows": rows,
    }


def _derive(company: str, metric_code: str, period_label: str | None) -> dict:
    """按 derived_from 里的两个基础指标做差（只支持两值相减）。"""
    bases = METRICS[metric_code]["derived_from"]
    if len(bases) != 2:
        return {"status": "not_disclosed", "rows": []}

    a = query_metric(company, bases[0], period_label)
    b = query_metric(company, bases[1], period_label)
    if a.get("status") != "ok" or b.get("status") != "ok":
        return {"status": "not_disclosed", "company": company,
                "metric_code": metric_code, "rows": []}

    ra, rb = a["rows"][0], b["rows"][0]
    value = ra["value_num"] - rb["value_num"]
    return {
        "status": "ok",
        "company": company,
        "metric_code": metric_code,
        "metric_name": metric_name(metric_code),
        "unit_kind": "currency",
        "unit_raw": ra["unit_raw"],
        "derived": True,
        "derived_note": (f"推算值 = {metric_name(bases[0])} − {metric_name(bases[1])}；"
                         f"该口径把少数股东权益也计入负债，仅供量级参考"),
        "rows": [{
            "company": company,
            "period_label": ra["period_label"],
            "value_num": value,
            "value_yuan": value * (ra["value_yuan"] / ra["value_num"]) if ra["value_num"] else None,
            "unit_raw": ra["unit_raw"],
            "unit_kind": "currency",
            "raw_text": f"{ra['value_num']:,.0f} - {rb['value_num']:,.0f}",
            "source_doc": ra["source_doc"],
            "source_table": ra["source_table"],
        }],
    }


def query_compare(metric_code: str, period_label: str,
                  companies: list[str] | None = None) -> dict:
    """多公司同一指标对比（金额按元、比率/股数按原值降序）。"""
    if metric_code not in METRICS:
        return {"status": "unsupported_metric", "rows": []}
    if not is_available():
        return {"status": "db_missing", "rows": []}

    # 比率 / 每股 / 户数的 value_yuan 是 NULL，排序必须用 value_num，否则全部并列
    sort_col = "value_yuan" if metric_unit_kind(metric_code) == "currency" else "value_num"

    sql = f"SELECT * FROM fact WHERE metric_code = ? AND period_label = ?"
    params: list = [metric_code, period_label]
    if companies:
        sql += f" AND company IN ({','.join('?' * len(companies))})"
        params.extend(companies)
    sql += f" ORDER BY {sort_col} DESC"

    with _connect() as con:
        rows = [_row_to_dict(r) for r in con.execute(sql, params)]

    if not rows:
        return {"status": "not_disclosed", "metric_code": metric_code, "rows": []}
    return {
        "status": "ok",
        "metric_code": metric_code,
        "metric_name": metric_name(metric_code),
        "unit_kind": metric_unit_kind(metric_code),
        "unit_raw": rows[0]["unit_raw"],
        "rows": rows,
    }


def query_trend(company: str, metric_code: str) -> dict:
    """单公司多期趋势（只取年度/年末，不含季度）。"""
    result = query_metric(company, metric_code)
    if result["status"] != "ok":
        return result
    rows = [r for r in result["rows"] if "Q" not in r["period_label"]]
    return {**result, "rows": rows}


def _fmt_value(row: dict) -> str:
    """按单位类型格式化一个值（不经过 LLM，避免数字被改写）。"""
    value, unit = row["value_num"], row["unit_raw"]
    if row["unit_kind"] == "currency":
        return f"{row['value_yuan'] / 1e8:,.2f} 亿元（原文 {value:,.0f} {unit}）"
    if row["unit_kind"] == "count":
        return f"{value:,.0f} {unit}"
    if row["unit_kind"] == "ratio":
        return f"{value:.2f}%"
    if row["unit_kind"] == "per_share":
        return f"{value:.2f} 元/股"
    return f"{value:,.2f} {unit}"


def _period_text(label: str) -> str:
    if label.endswith("年末"):
        return f"{label[:-2]} 年末"
    if "Q" in label:
        return label.replace("Q", " 年第 ") + " 季度"
    return f"{label}"


def render_answer(result: dict) -> str:
    """把查询结果渲染成确定性的中文答案。

    数值型问题的答案不需要生成模型：模板拼出来 100% 准确、毫秒级返回，
    这也是"数字零容错"最直接的落实。
    """
    if result.get("status") != "ok" or not result.get("rows"):
        return ""

    name = result["metric_name"]
    rows = result["rows"]

    if len(rows) == 1:
        r = rows[0]
        if result.get("derived"):
            # 推算值没有「原文」，不能写成「原文 XXX」
            yuan = r["value_yuan"] / 1e8 if r["value_yuan"] else r["value_num"] / 1e8
            return (f"{r['company']} {_period_text(r['period_label'])}{name}"
                    f"约 {yuan:,.2f} 亿元（**推算值**）。\n"
                    f"（{result['derived_note']}）")
        return _with_lineage(
            f"{r['company']} {_period_text(r['period_label'])}{name}为 {_fmt_value(r)}。", result)

    # 多行：跨公司对比 / 多期趋势
    head = f"{name}（{_period_text(rows[0]['period_label'])}）排名："
    if len({r["company"] for r in rows}) == 1:
        head = f"{rows[0]['company']} 的{name}："
    lines = [head]
    for i, r in enumerate(rows[:10], start=1):
        prefix = f"{r['company']} " if len({x['company'] for x in rows}) > 1 else f"{_period_text(r['period_label'])} "
        lines.append(f"{i}. {prefix}{_fmt_value(r)}")
    if len(rows) > 10:
        lines.append(f"（共 {len(rows)} 条，仅列前 10）")
    return _with_lineage("\n".join(lines), result)


def doc_label(raw_path: str, company: str = "") -> str:
    """把 source_doc 路径渲染成适合展示的文件名。

    文件命名是「公司名：原始文件名.md」，而原始文件名本身可能已经含公司名
    （中芯国际：中芯国际2025年年度报告摘要.md），直接展示就成了「中芯国际：中芯国际…」。
    去前缀后若把公司名也去没了就补回去；OCR 转换后缀一并抹掉。
    """
    if not raw_path:
        return ""
    name = Path(raw_path).name
    _head, sep, tail = name.partition("：")
    if sep and tail:
        name = tail
    if ".pdf_by_" in name:
        name = name.split(".pdf_by_")[0] + ".md"
    if company and company not in name:
        name = f"{company}：{name}"
    return name


def lineage_of(result: dict) -> str:
    """溯源信息：文档名 + 表序号。

    跨公司对比时各行可能来自不同文件，去重后最多列 3 个，其余归并为「等 N 份」。
    """
    items: list[str] = []
    for r in result.get("rows") or []:
        doc = doc_label(r.get("source_doc") or "", r.get("company") or "")
        if not doc:
            continue
        table = r.get("source_table")
        item = f"{doc} 第 {table} 张表" if table else doc
        if item not in items:
            items.append(item)
    if not items:
        return ""
    if len(items) > 3:
        return "、".join(items[:3]) + f" 等 {len(items)} 份"
    return "、".join(items)


def _with_lineage(text: str, result: dict) -> str:
    """在确定性答案末尾补一行溯源。

    来源由代码从查询结果里取，不交给生成模型 —— 模型编造「来源 1」比不给来源更糟。
    """
    src = lineage_of(result)
    return f"{text}\n来源：{src}" if src else text


def query_growth(metric_code: str, base_label: str, curr_label: str,
                 company: str | None = None) -> dict:
    """计算同比增速：(curr - base) / |base| × 100，按增速降序。"""
    if metric_code not in METRICS:
        return {"status": "unsupported_metric", "rows": []}
    if metric_unit_kind(metric_code) != "currency":
        return {"status": "unsupported_metric", "metric_code": metric_code, "rows": []}

    sql = """
        SELECT company,
               MAX(CASE WHEN period_label = ? THEN value_yuan END) AS base_val,
               MAX(CASE WHEN period_label = ? THEN value_yuan END) AS curr_val,
               MAX(CASE WHEN period_label = ? THEN source_doc END) AS source_doc,
               MAX(CASE WHEN period_label = ? THEN source_table END) AS source_table
        FROM fact
        WHERE metric_code = ? AND period_label IN (?, ?)
    """
    params: list = [base_label, curr_label, curr_label, curr_label,
                    metric_code, base_label, curr_label]
    if company:
        sql += " AND company = ?"
        params.append(company)
    sql += " GROUP BY company"

    with _connect() as con:
        rows = []
        for r in con.execute(sql, params):
            base, curr = r["base_val"], r["curr_val"]
            if base is None or curr is None or base == 0:
                continue
            rows.append({
                "company": r["company"],
                "base_label": base_label,
                "curr_label": curr_label,
                "base_yuan": base,
                "curr_yuan": curr,
                "growth_pct": (curr - base) / abs(base) * 100,
                "source_doc": r["source_doc"],
                "source_table": r["source_table"],
            })

    if not rows:
        return {"status": "not_disclosed", "metric_code": metric_code, "rows": []}
    rows.sort(key=lambda x: -x["growth_pct"])
    return {"status": "ok", "metric_code": metric_code,
            "metric_name": metric_name(metric_code), "unit_kind": "currency", "rows": rows}


def render_growth(result: dict) -> str:
    if result.get("status") != "ok" or not result.get("rows"):
        return ""
    rows = result["rows"]
    name = result["metric_name"]
    if len(rows) == 1:
        r = rows[0]
        return _with_lineage(
                f"{r['company']} {name}同比{'增长' if r['growth_pct'] >= 0 else '下降'} "
                f"{abs(r['growth_pct']):.2f}%"
                f"（{r['base_label']} {r['base_yuan'] / 1e8:,.2f} 亿元 → "
                f"{r['curr_label']} {r['curr_yuan'] / 1e8:,.2f} 亿元）。", result)
    lines = [f"{name}同比增速排名（{rows[0]['base_label']} → {rows[0]['curr_label']}）："]
    for i, r in enumerate(rows[:10], start=1):
        sign = "+" if r["growth_pct"] >= 0 else ""
        lines.append(f"{i}. {r['company']} {sign}{r['growth_pct']:.2f}%"
                     f"（{r['base_yuan'] / 1e8:,.2f} → {r['curr_yuan'] / 1e8:,.2f} 亿元）")
    return _with_lineage("\n".join(lines), result)


def format_rows(result: dict) -> str:
    """把查询结果整理成给 LLM 看的参考文本。

    注意：跨公司对比时各家单位可能不同（元 / 千元），每一行必须用自己的 unit_raw，
    不能统一用第一行的单位，否则 67,323,192 千元会被写成 67,323,192 元。
    """
    if result.get("status") != "ok":
        return ""
    name = result.get("metric_name") or result.get("metric_code")
    units = {r["unit_raw"] for r in result["rows"]}
    header = f"[结构化数据] {name}"
    header += f"（原始单位：{units.pop()}）" if len(units) == 1 else "（各公司原始单位不同，见每行）"
    if result.get("derived"):
        header += f"｜⚠ 这是推算值，不是披露值：{result['derived_note']}"

    lines = [header]
    for r in result["rows"]:
        line = f"  {r['company']} {r['period_label']}: {r['value_num']:,.2f} {r['unit_raw']}"
        if r["unit_kind"] == "currency" and r["value_yuan"] is not None:
            line += f"（约 {r['value_yuan'] / 1e8:,.2f} 亿元）"
        line += f"  [来源: {Path(r['source_doc']).name} 表{r['source_table']}]"
        lines.append(line)
    return "\n".join(lines)
