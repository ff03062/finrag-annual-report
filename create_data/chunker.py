"""md → 检索块。

md 是三样东西的混合：标题、正文、HTML 表格。切块前先按标题栈切成 section，
每个 section 里正文和表格按出现顺序排好，再决定每块怎么进库：

- 已被 table_parser 结构化入库的表 → 只留一句「[表] 标题」的摘要块，
  免得正文检索和 SQL 通道重复回答同一个问题
- 其余表（产品矩阵、债券、股东名单…）→ 转成文本混进正文，否则内容永久丢失
- 图片 → 按文件名建 figure 块，文件名本身就是图注
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from create_data.table_parser import (
    FACT_TABLE_TYPES,
    TABLE_RE,
    TableBlock,
    parse_tables,
    table_to_text,
)

TARGET_CHARS = 400
OVERLAP_CHARS = 60
MAX_CHARS = 600
MIN_NARRATIVE_CHARS = 50

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
SENT_SPLIT_RE = re.compile(r"(?<=[。！？；!?;])|\n")
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}


@dataclass
class Chunk:
    id: str
    company: str
    company_code: str
    report_year: int
    section: str
    chunk_type: str
    text: str
    doc_path: str
    img_path: str | None = None
    table_ref: str | None = None


@dataclass
class Section:
    path: tuple[str, ...]
    blocks: list[str | TableBlock] = field(default_factory=list)


def _tokens(md_text: str, tables: list[TableBlock]) -> list[tuple[str, object]]:
    """把 md 流式拆成 (行 | 表格) 序列，表格按 parse_tables 的序号配对。"""
    by_index = {t.index: t for t in tables}
    tokens: list[tuple[str, object]] = []
    cursor = 0
    for i, match in enumerate(TABLE_RE.finditer(md_text), start=1):
        tokens.extend(("line", ln) for ln in md_text[cursor:match.start()].splitlines())
        block = by_index.get(i)
        if block is not None:
            tokens.append(("table", block))
        cursor = match.end()
    tokens.extend(("line", ln) for ln in md_text[cursor:].splitlines())
    return tokens


def split_sections(md_text: str, tables: list[TableBlock]) -> list[Section]:
    sections: list[Section] = []
    stack: list[tuple[int, str]] = []
    current = Section(path=())
    sections.append(current)

    for kind, item in _tokens(md_text, tables):
        if kind == "table":
            current.blocks.append(item)
            continue

        heading = HEADING_RE.match(item)
        if heading:
            level = len(heading.group(1))
            stack = [s for s in stack if s[0] < level] + [(level, heading.group(2).strip())]
            current = Section(path=tuple(title for _, title in stack))
            sections.append(current)
            continue

        current.blocks.append(item)

    return [s for s in sections
            if any(isinstance(b, TableBlock) or b.strip() for b in s.blocks)]


def split_text(text: str, target: int = TARGET_CHARS,
               overlap: int = OVERLAP_CHARS, hard_max: int = MAX_CHARS) -> list[str]:
    """按句边界切块，块间重叠约 overlap 字（取尾部句子）。"""
    if len(text) <= hard_max:
        return [text] if text else []

    chunks: list[str] = []
    buf: list[str] = []
    size = 0

    for sentence in (s.strip() for s in SENT_SPLIT_RE.split(text)):
        if not sentence:
            continue
        if size + len(sentence) > target and buf:
            chunks.append("".join(buf).strip())
            tail, tail_len = [], 0
            for s in reversed(buf):
                if tail_len >= overlap:
                    break
                tail.insert(0, s)
                tail_len += len(s)
            buf, size = tail, tail_len

        buf.append(sentence)
        size += len(sentence)
        if size > hard_max:
            chunks.append("".join(buf).strip())
            buf, size = [], 0

    if buf:
        chunks.append("".join(buf).strip())
    return [c for c in chunks if c]


def chunk_id(*parts: object) -> str:
    return hashlib.md5("|".join(map(str, parts)).encode("utf-8")).hexdigest()[:16]


def _with_heading(path_text: str, body: str) -> str:
    """把最末一级标题拼到正文前。

    向量化只看 text 字段，「1. 盈利模式」这类短小节合并后标题只剩在 section 里，
    不拼进来就永远检索不到这个词。
    """
    leaf = path_text.split(" > ")[-1].strip() if path_text else ""
    if leaf and not body.lstrip().startswith(leaf):
        return f"{leaf}\n{body}"
    return body


def _section_text(blocks: list[str | TableBlock]) -> str:
    parts = [block if isinstance(block, str) else table_to_text(block)
             for block in blocks]
    return "\n".join(line.strip() for line in parts if line.strip())


def merge_short_narrative(chunks: list[Chunk]) -> list[Chunk]:
    """过短的叙述块并入前一块，避免碎块拖低召回。"""
    out: list[Chunk] = []
    for chunk in chunks:
        if (chunk.chunk_type != "narrative" or len(chunk.text) >= MIN_NARRATIVE_CHARS
                or not out):
            out.append(chunk)
            continue
        prev = out[-1]
        if prev.chunk_type == "narrative" and len(prev.text) + len(chunk.text) + 1 <= MAX_CHARS:
            prev.text = f"{prev.text}\n{chunk.text}"
        else:
            out.append(chunk)
    return out


def _figure_chunks(md_path: Path, company: str, company_code: str,
                   report_year: int, doc_path: str) -> list[Chunk]:
    img_dir = md_path.parent / "img"
    if not img_dir.is_dir():
        return []
    return [
        Chunk(id=chunk_id(company, "figure", image.stem), company=company,
              company_code=company_code, report_year=report_year, section="图表",
              chunk_type="figure", text=f"[图] {image.stem}", doc_path=doc_path,
              img_path=f"img/{image.name}")
        for image in sorted(img_dir.iterdir()) if image.suffix.lower() in IMG_EXT
    ]


def build_chunks(md_path: str | Path, company: str, company_code: str,
                 report_year: int, tables: list[TableBlock] | None = None) -> list[Chunk]:
    md_path = Path(md_path)
    md_text = md_path.read_text(encoding="utf-8")
    tables = parse_tables(md_text) if tables is None else tables
    doc_path = str(md_path.relative_to(md_path.parents[1]))

    chunks: list[Chunk] = []
    for section in split_sections(md_text, tables):
        path_text = " > ".join(section.path)

        body_blocks = []
        for block in section.blocks:
            if isinstance(block, TableBlock) and block.table_type in FACT_TABLE_TYPES:
                chunks.append(Chunk(
                    id=chunk_id(company, path_text, block.index), company=company,
                    company_code=company_code, report_year=report_year,
                    section=path_text, chunk_type="table_summary",
                    text=f"[表] {block.title}", doc_path=doc_path,
                    table_ref=f"table#{block.index}",
                ))
            else:
                body_blocks.append(block)

        body = _section_text(body_blocks)
        if not body:
            continue
        for index, piece in enumerate(split_text(body, hard_max=MAX_CHARS - 40)):
            chunks.append(Chunk(
                id=chunk_id(company, path_text, index), company=company,
                company_code=company_code, report_year=report_year,
                section=path_text, chunk_type="narrative",
                text=_with_heading(path_text, piece), doc_path=doc_path,
            ))

    chunks.extend(_figure_chunks(md_path, company, company_code, report_year, doc_path))
    return [c for c in merge_short_narrative(chunks) if c.text.strip()]
