"""把 md 切成检索用的 chunk（叙述通道）。

设计：
- 按标题切 section（不强制 H3：实测有的公司全用 H2，有的用 H3，按层级切更鲁棒）
- section 内按 400 字切块，句边界对齐，块间重叠 60 字
- HTML 表格不进正文，替换成一行占位 `[表：<标题>]`（表格数据已结构化为 facts）
- 图片以 `img/` 目录的文件名为图注（人工命名，质量高于 OCR 标题），单独成块

不依赖 langchain，输出纯 dataclass，方便单测。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from create_data.table_parser import (
    FACT_TABLE_TYPES,
    TableBlock,
    parse_tables,
    table_to_text,
)

TARGET_CHARS = 400      # 目标块长
OVERLAP_CHARS = 60      # 相邻块重叠
MAX_CHARS = 600         # 硬上限
MIN_SECTION_CHARS = 40  # 短于此的 section 不单独成块，向后合并

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
SENT_SPLIT_RE = re.compile(r"(?<=[。！？；!?;])|\n")
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}


@dataclass
class Chunk:
    id: str
    company: str
    company_code: str
    report_year: int
    section: str                 # 标题路径，用 " > " 连接
    chunk_type: str              # narrative | table_summary | figure
    text: str
    doc_path: str
    img_path: str | None = None
    table_ref: str | None = None

    def as_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class Section:
    path: tuple[str, ...]        # 标题栈
    lines: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)   # 该 section 内表格占位标题


# ---------------------------------------------------------------- 解析 md


def split_sections(md_text: str) -> list[Section]:
    """按标题切段；表格替换为占位符。"""
    sections: list[Section] = []
    stack: list[tuple[int, str]] = []
    current = Section(path=())
    sections.append(current)

    lines = md_text.splitlines()
    i = 0
    table_idx = 0
    table_blocks = {b.index: b for b in parse_tables(md_text)}

    while i < len(lines):
        line = lines[i]
        m = HEADING_RE.match(line)
        if m:
            level, title = len(m.group(1)), m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            current = Section(path=tuple(t for _, t in stack))
            sections.append(current)
            i += 1
            continue

        if "<table" in line.lower():
            # 整张表可能跨多行：跳到 </table>
            while i < len(lines) and "</table>" not in lines[i].lower():
                i += 1
            table_idx += 1
            block = table_blocks.get(table_idx)
            title = (block.title if block else "") or f"表{table_idx}"
            current.tables.append(title)
            if block and block.table_type in FACT_TABLE_TYPES:
                # 已结构化入库的表，正文里只留占位符，避免和 SQL 通道重复
                current.lines.append(f"[[TABLE:{table_idx}:{title}]]")
            elif block:
                # 不入库的表（产品技术、债券、公告…）必须转文本保留，否则内容会永久丢失
                current.lines.append(table_to_text(block))
            i += 1
            continue

        current.lines.append(line)
        i += 1

    return [s for s in sections if s.lines or s.tables]


def _table_titles(md_text: str) -> dict[int, str]:
    """表序号 → 标题（取表格上文最后一条非单位行）。"""
    titles: dict[int, str] = {}
    for i, block in enumerate(parse_tables(md_text), start=1):
        titles[i] = block.title or f"表{i}"
    return titles


def clean_lines(lines: list[str]) -> str:
    """把 section 的行拼成正文：去掉空行、合并断行。"""
    out: list[str] = []
    for line in lines:
        text = line.strip()
        if not text:
            continue
        out.append(text)
    return "\n".join(out)


# ---------------------------------------------------------------- 切块


def split_by_sentence(text: str) -> list[str]:
    parts = SENT_SPLIT_RE.split(text)
    return [p for p in (x.strip() for x in parts) if p]


def split_text(text: str, target=TARGET_CHARS, overlap=OVERLAP_CHARS,
               hard_max=MAX_CHARS) -> list[str]:
    """句边界切块，块间重叠约 overlap 字（取尾部句子）。"""
    if len(text) <= hard_max:
        return [text] if text else []

    sents = split_by_sentence(text)
    chunks: list[str] = []
    buf: list[str] = []
    size = 0

    for sent in sents:
        if size + len(sent) > target and buf:
            chunks.append("".join(buf).strip())
            # 尾部句子作为下一块的开头，形成重叠
            tail: list[str] = []
            tail_len = 0
            for s in reversed(buf):
                if tail_len >= overlap:
                    break
                tail.insert(0, s)
                tail_len += len(s)
            buf = tail
            size = tail_len
        buf.append(sent)
        size += len(sent)

        # 单个句子超长：按硬上限强切
        if size > hard_max:
            chunks.append("".join(buf).strip())
            buf, size = [], 0

    if buf:
        chunks.append("".join(buf).strip())

    return [c for c in chunks if c]


# ---------------------------------------------------------------- 入口


def _chunk_id(company: str, section: str, index: int) -> str:
    raw = f"{company}|{section}|{index}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


def list_figures(img_dir: Path) -> list[str]:
    """图片文件名即图注（人工命名）。"""
    if not img_dir.is_dir():
        return []
    return sorted(p.stem for p in img_dir.iterdir() if p.suffix.lower() in IMG_EXT)


def _with_heading(path_text: str, body: str) -> str:
    """把标题（只取最后一级）拼到正文前面。

    否则「1. 盈利模式」这种短小节被合并后，标题只留在 section 字段里，
    向量化只看 text，检索永远命中不了这个词。
    """
    leaf = path_text.split(" > ")[-1].strip() if path_text else ""
    if leaf and not body.lstrip().startswith(leaf):
        return f"{leaf}\n{body}"
    return body


def build_chunks(md_path: str | Path, company: str, company_code: str,
                 report_year: int) -> list[Chunk]:
    md_path = Path(md_path)
    md_text = md_path.read_text(encoding="utf-8")
    sections = split_sections(md_text)
    doc_path = str(md_path.relative_to(Path(md_path).parents[1])) \
        if md_path.parents[1] else str(md_path)

    chunks: list[Chunk] = []
    pending: list[Chunk] = []      # 短 section 收集器

    def flush() -> None:
        nonlocal pending
        if pending:
            chunks.extend(pending)
            pending = []

    for sec in sections:
        body = clean_lines(sec.lines)
        if not body:
            continue
        path_text = " > ".join(sec.path)

        # 表格占位单独成块，让"这张表讲什么"也能被检索到
        for t_idx, title in _iter_table_placeholders(body):
            chunks.append(Chunk(
                id=_chunk_id(company, path_text, len(chunks) + t_idx),
                company=company, company_code=company_code,
                report_year=report_year, section=path_text,
                chunk_type="table_summary",
                text=f"[表] {title}",
                doc_path=doc_path,
                table_ref=f"table#{t_idx}",
            ))

        plain = _strip_table_placeholders(body)
        if len(plain) < MIN_SECTION_CHARS:
            # 太短的 section 合并到下一个，避免碎块
            titled = _with_heading(path_text, plain)
            if pending:
                prev = pending[-1]
                prev.text = prev.text + "\n" + titled
            elif plain:
                pending.append(Chunk(
                    id=_chunk_id(company, path_text, 0),
                    company=company, company_code=company_code,
                    report_year=report_year, section=path_text,
                    chunk_type="narrative", text=titled, doc_path=doc_path,
                ))
            continue

        flush()
        for idx, piece in enumerate(split_text(plain, hard_max=MAX_CHARS - 40)):
            chunks.append(Chunk(
                id=_chunk_id(company, path_text, idx),
                company=company, company_code=company_code,
                report_year=report_year, section=path_text,
                chunk_type="narrative",
                text=_with_heading(path_text, piece), doc_path=doc_path,
            ))

    flush()

    # 图注块
    img_dir = md_path.parent / "img"
    for name in list_figures(img_dir):
        chunks.append(Chunk(
            id=_chunk_id(company, "图表", hash(name) & 0xFFFF),
            company=company, company_code=company_code,
            report_year=report_year, section="图表",
            chunk_type="figure",
            text=f"[图] {name}",
            doc_path=doc_path,
            img_path=f"img/{name}",
        ))

    chunks = merge_short_narrative(chunks)
    return [c for c in chunks if c.text.strip()]


MIN_NARRATIVE_CHARS = 50   # 短于此的叙述块会被合并，避免碎块拖低检索质量


def merge_short_narrative(chunks: list[Chunk],
                          min_len: int = MIN_NARRATIVE_CHARS,
                          max_len: int = MAX_CHARS) -> list[Chunk]:
    """把过短的叙述块并入相邻块（优先同 section、向后合并）。"""
    out: list[Chunk] = []
    for chunk in chunks:
        if (chunk.chunk_type != "narrative" or len(chunk.text) >= min_len or not out):
            out.append(chunk)
            continue
        prev = out[-1]
        if (prev.chunk_type == "narrative"
                and len(prev.text) + len(chunk.text) + 1 <= max_len):
            prev.text = f"{prev.text}\n{chunk.text}"
        else:
            out.append(chunk)
    return out


def _iter_table_placeholders(body: str):
    for m in re.finditer(r"\[\[TABLE:(\d+):(.+?)\]\]", body):
        yield int(m.group(1)), m.group(2)


def _strip_table_placeholders(body: str) -> str:
    return re.sub(r"\n?\[\[TABLE:\d+:.+?\]\]\n?", "\n", body).strip()
