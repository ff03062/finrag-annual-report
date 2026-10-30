"""表格结构兜底：规则解析失败时，让本地 LLM 判断「表头怎么分组、每列是什么期间、每行是什么指标」。

边界（很重要）：
- LLM 的输入**不含任何数值**，只有表头文字和行名文字
- LLM 的输出只有结构（列角色 / 行映射），数值仍由代码从原来的格子里取
- 拿到标注后必须重新校验，不通过就进人工队列

这样即使模型判断错了，也只是"取错列"，T4/T5 校验会拦下来，不会把错数字写进库。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from create_data.metric_dict import METRICS

# 输出给模型的指标枚举（只给 code + 中文名，不给别名，避免它乱匹配）
METRIC_ENUM_TEXT = "\n".join(
    f"- {code}: {meta['name_cn']}" for code, meta in METRICS.items()
)

SYSTEM_PROMPT = f"""你是财报表格结构解析器。你的任务是判断一张表格的「表头结构」和「行指标映射」。

你会收到：
1. header_rows：表格前几行，每项含 index（原始行号）和 cells（单元格文字数组）
2. rows：数据行，每项含 index（原始行号）和 label（第一列文字）
3. col_count：列数

**所有 index 都是表格里的原始行号，请原样使用。**

## 你要判断两件事

### A. 表头怎么分组
一张表可能有 1 组或 2 组表头（例如前两行是「2025年末/2024年末/增减/2023年末」，后几行又开始「2025年/2024年/增减/2023年」）。
对每组表头，给出：它是第几行、它管辖哪些数据行、每一列是什么角色。

列角色只能是：
- "value_point"：某年年末的时点值（表头含「末」）
- "value_span"：某年整年的区间值（表头含「年」但不含「末」）
- "delta"：增减百分比或差值列（表头含「增减」「变动」）

### B. 每一行是什么指标
从下面的枚举里选 metric_code，选不出就填 null。

指标枚举：
{METRIC_ENUM_TEXT}

## 输出格式（严格 JSON，不要任何解释文字）

{{"header_groups": [{{"header_row": 0, "applies_to_rows": [1, 2], "columns": [{{"col": 1, "year": 2025, "role": "value_point"}}, {{"col": 3, "role": "delta"}}]}}], "row_metrics": [{{"row": 1, "metric_code": "total_assets"}}], "confidence": 0.9}}

## 规则
1. col 从 0 开始，0 是行名那一列
2. delta 列不需要 year，填 null 或省略
3. applies_to_rows 和 row 都用**原始行号**（和输入里的 index 一致）
4. 表格里的数值一个都没给你，不要猜数值
5. 不确定就填 null / 低 confidence，不要瞎编
"""


@dataclass
class TableAnnotation:
    header_groups: list[dict]
    row_metrics: list[dict]
    confidence: float

    def metric_for(self, row: int) -> str | None:
        for item in self.row_metrics:
            if item.get("row") == row:
                return item.get("metric_code")
        return None

    def group_for(self, row: int) -> dict | None:
        for g in self.header_groups:
            if row in (g.get("applies_to_rows") or []):
                return g
        return None


def _build_payload(block, max_rows: int = 30) -> dict:
    grid = block.grid
    header_rows = [
        {"index": i, "cells": row}
        for i, row in enumerate(grid[:6])
        if any(c.strip() for c in row)
    ]
    rows = [
        {"index": i, "label": row[0].strip()}
        for i, row in enumerate(grid)
        if row and row[0].strip()
    ][:max_rows]
    return {
        "table_title": block.title,
        "unit_hint": block.unit_raw,
        "header_rows": header_rows,
        "rows": rows,
        "col_count": max((len(r) for r in grid), default=0),
    }


def _extract_json(text: str) -> dict | None:
    text = (text or "").strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if match:
        try:
            data = json.loads(match.group(1))
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            pass
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            data = json.loads(match.group(0))
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            pass
    return None


def annotate(block, timeout: int = 120) -> TableAnnotation | None:
    """调用本地 LLM 做结构标注；失败返回 None（由调用方决定是否进人工队列）。"""
    from ai.LoadLLM import chat_llm

    payload = _build_payload(block)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]

    try:
        response = chat_llm().invoke(messages)
    except Exception as e:  # noqa: BLE001
        print(f"[LLM兜底] 调用失败: {type(e).__name__}: {e}")
        return None

    data = _extract_json(getattr(response, "content", ""))
    if not data:
        print("[LLM兜底] 返回不是合法 JSON")
        return None

    groups = data.get("header_groups") or []
    rows = data.get("row_metrics") or []
    if not groups or not rows:
        print(f"[LLM兜底] 标注不完整 groups={len(groups)} rows={len(rows)}")
        return None

    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0

    return TableAnnotation(header_groups=groups, row_metrics=rows, confidence=confidence)
