"""切块测试：15 份摘要 → chunks。

运行：
    python create_data/test_chunker.py
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

DATA_DIR = ROOT / "new_Data"
REPORT_YEAR = 2025
CODE_RE = re.compile(r"公司代码\s*[：:]\s*(\d+)")

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        _failures.append(f"{name} {detail}")


def test_split_text() -> None:
    print("\n[单元] 句边界切块")
    text = "。".join(f"这是第{i}句话，用来测试切块逻辑是否稳定" for i in range(60)) + "。"
    pieces = split_text(text)
    check("块数 > 1", len(pieces) > 1, f"got={len(pieces)}")
    check("每块 <= 硬上限", all(len(p) <= MAX_CHARS for p in pieces),
          f"max={max(len(p) for p in pieces)}")
    check("平均块长接近目标",
          abs(sum(len(p) for p in pieces) / len(pieces) - TARGET_CHARS) < 150,
          f"avg={sum(len(p) for p in pieces) / len(pieces):.0f}")


def test_split_sections() -> None:
    print("\n[单元] 标题切段")
    md = "# 标题\n\n## 第一节\n\n正文一\n\n### 小节\n\n正文二\n"
    secs = split_sections(md)
    paths = [" > ".join(s.path) for s in secs]
    check("段数 == 3", len(secs) == 3, f"got={paths}")
    check("有多级路径", any(">" in p for p in paths), f"got={paths}")


def test_documents() -> None:
    print("\n[集成] 15 份摘要切块")
    total = 0
    for co_dir in sorted(p for p in DATA_DIR.iterdir() if p.is_dir()):
        md = next(co_dir.glob("*.md"), None)
        if md is None:
            continue
        text = md.read_text(encoding="utf-8")
        m = CODE_RE.search(text)
        code = m.group(1) if m else ""

        chunks = build_chunks(md, co_dir.name, code, REPORT_YEAR)
        total += len(chunks)

        kinds: dict[str, int] = {}
        for c in chunks:
            kinds[c.chunk_type] = kinds.get(c.chunk_type, 0) + 1

        imgs = len(list((co_dir / "img").glob("*.jpg"))) if (co_dir / "img").is_dir() else 0
        print(f"  {co_dir.name:<6} chunks={len(chunks):>3} "
              f"narrative={kinds.get('narrative', 0):>2} "
              f"table={kinds.get('table_summary', 0):>2} "
              f"figure={kinds.get('figure', 0):>2}  图片={imgs}")

        check(f"{co_dir.name} 无 HTML 残留", not any("<table" in c.text.lower() for c in chunks))
        check(f"{co_dir.name} 无占位符残留", not any("[[TABLE" in c.text for c in chunks))
        check(f"{co_dir.name} 块长不超上限", all(len(c.text) <= MAX_CHARS for c in chunks),
              f"max={max(len(c.text) for c in chunks)}")
        check(f"{co_dir.name} figure 块 == 图片数", kinds.get("figure", 0) == imgs,
              f"{kinds.get('figure', 0)} vs {imgs}")
        check(f"{co_dir.name} 元数据完整",
              all(c.company and c.chunk_type and c.doc_path for c in chunks))

    print(f"\n  总块数 {total}（实测 477：narrative 331 + table 74 + figure 72）")
    check("总块数在 380~600", 380 <= total <= 600, f"got={total}")


def main() -> int:
    test_split_text()
    test_split_sections()
    test_documents()

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
