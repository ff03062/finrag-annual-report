"""VLM 路：把 PDF 里的表格裁成图片，交给视觉模型读成 facts。

**只裁表格**。PDF 是电子版、有文本层，`find_tables()` 能直接给出每张表的 bbox；
按 bbox 渲染出来的图里只有表格本身，图片、图表、正文段落都不在框内。
所以不需要在 prompt 里叮嘱模型"忽略图表"——它根本看不到。

**跨页表要拼起来**。财报的主要会计数据表经常跨页，下半页没有表头，
单独喂给模型它就不知道该读哪一列。判据是三条同时成立：本页表底边贴住页面
内容区下沿、下一页顶部还有表、两页表列数相同（同一张表跨页，列数必然一致）。

裁图和 manifest 缓存在 `new_Data/<公司>/tables/`，重复构建直接复用。
和 OCR 路的关系：两路独立读数，由 reconcile 对账，缺的互补、错的互斥。
单独一路的结果不直接信任——VLM 也会编数字。

三种数据来源，按优先级：
1. `vlm_facts.json`    离线结果，不需要 token
2. `tables/*.png` + VLM_MODEL + key    真的调视觉模型
3. 都没有 → 返回空，构建退化成 OCR 单路

离线结果文件格式：
    {"facts": [{"metric": "revenue", "period": "2025年", "value": 362012554000, "unit": "元"}]}
"""
from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path

from create_data.metric_dict import (
    METRICS,
    normalize_number,
    parse_unit_line,
    resolve_metric,
)
from create_data.table_parser import YEAR_RE, Fact

TABLE_DIR = "tables"
MANIFEST_NAME = "manifest.json"
CACHE_NAME = "vlm_facts.json"
TABLE_DPI = 200

# 跨页判据。md 里没有页码页脚，PDF 里表底边到的是内容区下沿而不是纸边，
# 所以这两个比例要放宽；放宽的代价只是多拼一张，漏拼的代价是整块数据读错。
BOTTOM_RATIO = 0.82
TOP_RATIO = 0.28

_UNIT_FACTORS = {"元": 1.0, "千元": 1_000.0, "万元": 10_000.0, "亿元": 100_000_000.0}
_CN_DIGIT = {"一": 1, "二": 2, "三": 3, "四": 4}
_QUARTER_END = {1: ("01-01", "03-31"), 2: ("04-01", "06-30"),
                3: ("07-01", "09-30"), 4: ("10-01", "12-31")}

VLM_PROMPT = """你是财报表格读数器。读图里的表格，只输出 JSON，不要解释：
{{"facts": [{{"metric": "revenue", "period": "2025年", "value": 1760583930.12, "unit": "元"}}]}}

metric 只能取：{metrics}
本文档统一单位：{unit}，unit 一律填它。

1. 一个单元格一条记录；period 抄表头原文（如 "2024年末"、"2023年"、"2025Q1"）。
   表头里有几个年份就输出几个年份，不要只取当年
2. 「增减」「%」这类派生列跳过；空格、「-」「不适用」跳过
3. value 只填数字，去掉千分位和百分号，被换行拆开的数字拼回去
4. 图可能上下拼了两页，下半页列含义同上
5. 表里没有的不要编
"""


def _metric_tables(page) -> list:
    """本页含指标名的表，按纵向位置排序。"""
    tables = [t for t in page.find_tables().tables
              if any(resolve_metric(str(row[0])) for row in t.extract() if row and row[0])]
    return sorted(tables, key=lambda t: t.bbox[1])


def _is_continuation(rows: list) -> bool:
    """下一页首行是否是上一张表的续行。

    续行直接从行名开始 —— 可能是被页边切断的半截（通达动力的「的净资产」）；
    另一张表的首行是表头，第一列必然是空的（恩捷股份的分季度表、比亚迪的第 3 页）。
    所以判据只看一条：首列非空，且不是表头文字（年份、「第 N 季度」）。
    """
    first = str((rows[0] or [""])[0] or "").strip() if rows else ""
    if not first:
        return False
    return not YEAR_RE.search(first) and "季度" not in first


def _spans_next(page, table, next_tables: list) -> bool:
    """本页这张表是否延续到下一页。

    四条一起判：贴住内容区下沿、下一页顶部还有表、列数相同、下一页首行是续行。
    前三条只能说明"像"，最后一条才能区分"同一张表的下半页"和"紧邻的另一张表"。
    """
    if not next_tables:
        return False
    if table.bbox[3] / page.rect.height < BOTTOM_RATIO:
        return False

    following = next_tables[0]
    if following.bbox[1] / page.rect.height > TOP_RATIO:
        return False
    if table.col_count != following.col_count:
        return False
    return _is_continuation(following.extract())


def _stitch(pixmaps: list, path: Path) -> None:
    from PIL import Image

    images = [Image.frombytes("RGB", (p.width, p.height), p.samples) for p in pixmaps]
    canvas = Image.new("RGB", (max(i.width for i in images), sum(i.height for i in images)),
                       "white")
    offset = 0
    for image in images:
        canvas.paste(image, (0, offset))
        offset += image.height
    canvas.save(path)


def render_table_images(pdf_path: str | Path, out_dir: str | Path,
                        dpi: int = TABLE_DPI) -> list[dict]:
    """把 PDF 里含财务指标的表格裁成图片，跨页的拼成一张。

    返回 [{"file", "page", "table"}]，同时写进 tables/manifest.json 供复用和溯源。
    """
    import pymupdf

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    doc = pymupdf.open(pdf_path)
    pages = [_metric_tables(page) for page in doc]
    manifest: dict[str, dict] = {}

    consumed: set[tuple[int, int]] = set()
    for page_no in range(len(doc)):
        for order, table in enumerate(pages[page_no], start=1):
            if (page_no, order) in consumed:
                continue

            next_tables = pages[page_no + 1] if page_no + 1 < len(pages) else []
            parts = [doc[page_no].get_pixmap(clip=table.bbox, dpi=dpi)]
            last_page = page_no

            if _spans_next(doc[page_no], table, next_tables):
                parts.append(doc[page_no + 1].get_pixmap(clip=next_tables[0].bbox, dpi=dpi))
                consumed.add((page_no + 1, 1))
                last_page = page_no + 1

            name = (f"p{page_no + 1:02d}_t{order}.png" if last_page == page_no
                    else f"p{page_no + 1:02d}-p{last_page + 1:02d}_t{order}.png")
            path = out_dir / name
            if len(parts) > 1:
                _stitch(parts, path)
            else:
                parts[0].save(path)
            manifest[name] = {"page": page_no + 1, "table": order}

    doc.close()
    (out_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return [{"file": name, **meta} for name, meta in manifest.items()]


def _table_images(doc_dir: Path) -> list[dict]:
    table_dir = doc_dir / TABLE_DIR
    manifest = table_dir / MANIFEST_NAME
    if manifest.is_file():
        # 复用缓存。改了 render_table_images 的裁剪逻辑后必须删掉 tables/，
        # 否则这里会一直拿旧图去调模型。
        return [{"path": table_dir / name, **meta}
                for name, meta in json.loads(manifest.read_text(encoding="utf-8")).items()]

    pdf = next(doc_dir.glob("*.pdf"), None)
    if pdf is None:
        return []
    return [{**item, "path": table_dir / item["file"]}
            for item in render_table_images(pdf, table_dir)]


def _document_unit(doc_dir: Path) -> str:
    """整份摘要的货币单位。

    财报摘要通常全文只声明一次单位，所以取 md 里第一次出现的「单位：X」。
    大多数公司的单位行离表格 200pt 以上（甚至在上页），裁图够不到，只能这样给模型。
    """
    md = next(doc_dir.glob("*.md"), None)
    if md is None:
        return "元"
    return parse_unit_line(md.read_text(encoding="utf-8"))[0]


def _period(text: str, report_year: int) -> tuple[str, str | None, str] | None:
    """期间文字 → (period_label, period_start, period_end)。"""
    text = (text or "").strip()
    match = YEAR_RE.search(text)
    year = int(match.group(1)) if match else report_year

    quarter = re.search(r"Q([1-4])", text, re.I) or re.search(r"第([一二三四])季度", text)
    if quarter:
        raw = quarter.group(1)
        number = int(raw) if raw.isdigit() else _CN_DIGIT[raw]
        start, end = _QUARTER_END[number]
        return f"{year}Q{number}", f"{year}-{start}", f"{year}-{end}"

    if "末" in text:
        return f"{year}年末", None, f"{year}-12-31"
    return f"{year}年", f"{year}-01-01", f"{year}-12-31"


def _to_fact(item: dict, company: str, report_year: int,
             page: int | None = None, table: int | None = None,
             default_unit: str = "元") -> Fact | None:
    """一条 VLM 输出 → Fact。这是外部数据的入口，校验只在这里做一次。"""
    metric_code = resolve_metric(str(item.get("metric") or ""))
    period = _period(str(item.get("period") or ""), report_year)
    value = normalize_number(item.get("value"))
    if not metric_code or not period or value is None:
        return None

    label, start, end = period
    meta = METRICS[metric_code]
    # 时点 / 期间由指标定义决定，不采信模型给的 period。
    # 跨页表的下半页没有表头，模型很容易把「2025年末」读成「2025年」，
    # 那样会多出一条并不存在的年度记录，和 OCR 路同指标不同期间、配不上对。
    if meta["period_type"] == "point_in_time" and not label.endswith("年末"):
        label, start, end = f"{label[:4]}年末", None, f"{label[:4]}-12-31"

    unit = str(item.get("unit") or default_unit).strip()
    currency = meta["unit_kind"] == "currency"
    return Fact(
        company=company, report_year=report_year, metric_code=metric_code,
        period_type=meta["period_type"], period_label=label,
        period_start=start, period_end=end, value_num=value,
        value_yuan=value * _UNIT_FACTORS.get(unit, 1.0) if currency else None,
        unit_raw=unit, unit_kind=meta["unit_kind"],
        table_index=table, row_index=None, col_index=None,
        raw_text=str(item.get("value")), raw_header=str(item.get("period")),
        source="vlm", source_page=page,
    )


def _parse(payload: dict, company: str, report_year: int,
           default_unit: str = "元") -> list[Fact]:
    """JSON → Fact 列表。page / table 跟着每条记录走，缓存回读时也能复原溯源。"""
    facts = []
    for item in payload.get("facts") or []:
        fact = _to_fact(item, company, report_year,
                        item.get("page"), item.get("table"), default_unit)
        if fact:
            facts.append(fact)
    return facts


def _call_api(images: list[dict], company: str, report_year: int,
              unit: str) -> tuple[list[Fact], list[dict]]:
    """调视觉模型。没配模型名、没 key、没图都返回空 —— VLM 路是可选路径。"""
    from langchain_core.messages import HumanMessage
    from langchain_openai import ChatOpenAI

    from ai.LoadLLM import _api_key, llm_config

    model = os.getenv("VLM_MODEL", "")
    api_key = _api_key()
    if not model or not api_key or not images:
        return [], []

    base_url, _ = llm_config()
    metrics = ", ".join(f"{code}（{meta['name_cn']}）"
                        for code, meta in METRICS.items() if not meta.get("derived_from"))
    prompt = VLM_PROMPT.format(metrics=metrics, unit=unit)

    facts: list[Fact] = []
    payloads: list[dict] = []
    llm = ChatOpenAI(api_key=api_key, base_url=base_url, model=model, temperature=0)
    for image in images:
        encoded = base64.b64encode(image["path"].read_bytes()).decode()
        message = HumanMessage(content=[
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
        ])
        raw = llm.invoke([message]).content
        data = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
        for item in data.get("facts") or []:
            item["page"] = image.get("page")
            item["table"] = image.get("table")
        payloads.append(data)
        facts.extend(_parse(data, company, report_year, unit))
    return facts, payloads


def extract_facts(doc_dir: str | Path, company: str, report_year: int) -> list[Fact]:
    doc_dir = Path(doc_dir)
    unit = _document_unit(doc_dir)

    cache = doc_dir / CACHE_NAME
    if cache.is_file():
        return _parse(json.loads(cache.read_text(encoding="utf-8")),
                      company, report_year, unit)

    facts, payloads = _call_api(_table_images(doc_dir), company, report_year, unit)
    if payloads:
        combined = {"facts": [item for payload in payloads
                              for item in payload.get("facts") or []]}
        cache.write_text(json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8")
    return facts


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: python -m create_data.vlm_extract <公司目录>")
        sys.exit(1)

    folder = Path(sys.argv[1])
    pdf = next(folder.glob("*.pdf"))
    items = render_table_images(pdf, folder / TABLE_DIR)
    print(f"{folder.name}：{pdf.name} → {len(items)} 张（含跨页拼接）")
    for item in items:
        print(f"  {item['file']:<18} 第 {item['page']} 页 第 {item['table']} 张表")
