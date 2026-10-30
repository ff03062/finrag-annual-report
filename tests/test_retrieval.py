"""检索层评测：双路召回 + RRF + 精排的 Recall@5 / MRR@10。

运行：
    python tests/test_retrieval.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from chat.utils.MilvusRetriever import search  # noqa: E402

# (问题, 公司过滤, 命中关键词) —— 关键词出现在 page_content 里即视为相关
CASES = [
    ("锐科激光的主营业务是什么？", "锐科激光", "激光器"),
    ("中芯国际的行业地位如何？", "中芯国际", "晶圆代工"),
    ("中芯国际采用什么经营模式？", "中芯国际", "模式"),
    ("中芯国际面临哪些风险？", "中芯国际", "风险"),
    ("宁德时代的主要产品是什么？", "宁德时代", "电池"),
    ("比亚迪的业务范围有哪些？", "比亚迪", "新能源"),
    ("功率GaN器件市场规模预测", None, "GaN"),
    ("汇川科技有哪些工业机器人产品？", "汇川科技", "机器人"),
    ("长电科技的核心封装技术有哪些？", "长电科技", "封装"),
    ("中微公司的主要设备产品是什么？", "中微公司", "刻蚀"),
    ("半导体行业的发展趋势", None, "半导体"),
    ("恩捷股份的隔膜业务情况", "恩捷股份", "隔膜"),
]

TOP_K = 5
MRR_K = 10


def main() -> int:
    hits5 = 0
    mrr_sum = 0.0
    rows = []

    for q, company, keyword in CASES:
        t0 = time.time()
        try:
            docs = search(q, company=company, k=MRR_K)
        except Exception as e:  # noqa: BLE001
            rows.append((q, 0, 0.0, f"ERROR {type(e).__name__}: {e}"))
            continue

        rank = 0
        for i, d in enumerate(docs, start=1):
            if keyword in d.page_content:
                rank = i
                break
        mrr_sum += 1.0 / rank if rank else 0.0
        if 0 < rank <= TOP_K:
            hits5 += 1
        rows.append((q, rank, time.time() - t0, keyword))

    print(f"\n{'#':>2} {'rank':>4} {'耗时':>6}  问题")
    print("-" * 90)
    for i, (q, rank, elapsed, kw) in enumerate(rows, start=1):
        mark = "PASS" if 0 < rank <= TOP_K else ("LATE" if rank else "MISS")
        print(f"{i:>2} {mark:<4} {rank:>3} {elapsed:>5.2f}s  {q}   (kw={kw})")

    n = len(rows)
    print("-" * 90)
    print(f"Recall@{TOP_K} = {hits5}/{n} = {hits5 / n:.1%}")
    print(f"MRR@{MRR_K}    = {mrr_sum / n:.3f}")
    return 0 if hits5 == n else 1


if __name__ == "__main__":
    sys.exit(main())
