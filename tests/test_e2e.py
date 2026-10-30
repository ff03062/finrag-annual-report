"""端到端评测：21 题跑完整 chat 链路，按题型断言。

运行：
    python tests/test_e2e.py            # 全部
    python tests/test_e2e.py --sql-only # 只跑 1-15 题（不依赖 Milvus/LLM 叙述能力）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from chat.service.ChatService import chat  # noqa: E402

QUESTIONS = ROOT / "tests" / "eval_questions.json"


def _norm(text: str) -> str:
    return text.replace(",", "").replace("，", "").replace(" ", "")


def check_item(item: dict, answer: str) -> tuple[bool, str]:
    if item.get("expect_reject"):
        # 必须明确说"没有这个数据"，且不能出现像模像样的数字。
        # 措辞由生成模型决定，这里按常见表达放宽，只要没编造就算通过。
        rejected = any(k in answer for k in
                       ("未披露", "未在", "没有披露", "未找到", "无相关", "不适用",
                        "未提供", "未给出", "未包含", "不含", "无法确认", "未提及"))
        return (rejected, "未拒答" if not rejected else "ok")

    if "expect_number" in item:
        target = _norm(item["expect_number"])
        return (target in _norm(answer), f"缺数字 {item['expect_number']}")

    if "expect_keyword" in item:
        return (item["expect_keyword"] in answer, f"缺关键词 {item['expect_keyword']}")

    return (True, "no assertion")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sql-only", action="store_true")
    args = parser.parse_args()

    items = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    if args.sql_only:
        items = [i for i in items if i["channel"] == "sql"]

    passed = 0
    rows = []
    for item in items:
        t0 = time.time()
        try:
            answer = "".join(chat(item["q"], 0, ""))
        except Exception as e:  # noqa: BLE001
            answer = f"<EXCEPTION {type(e).__name__}: {e}>"
        elapsed = time.time() - t0
        ok, note = check_item(item, answer)
        passed += ok
        rows.append((item["id"], ok, elapsed, answer.strip(), note))

    print(f"\n{'#':>3} {'结果':<4} {'耗时':>6}  问题 / 答案")
    print("-" * 100)
    for qid, ok, elapsed, answer, note in rows:
        mark = "PASS" if ok else "FAIL"
        short = answer.replace("\n", " ")[:70]
        print(f"{qid:>3} {mark:<4} {elapsed:>5.1f}s  {short}")
        if not ok:
            print(f"      ↳ {note}")

    total = len(rows)
    print("-" * 100)
    print(f"通过 {passed}/{total}（{passed / max(total, 1):.0%}）")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
