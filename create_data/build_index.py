"""离线构建索引：facts → SQLite，chunks → Milvus。

用法：
    python create_data/build_index.py --sqlite          # 只建 SQLite（不依赖 Milvus）
    python create_data/build_index.py --milvus          # 只建 Milvus
    python create_data/build_index.py --all             # 两个都建
    python create_data/build_index.py --dry-run         # 只统计，不写库

每份财报走两路取数再对账：
- OCR 路（table_parser）：HTML 表格 + 规则
- VLM 路（vlm_extract）：页面图 + 视觉模型，没有页面图或没有 VLM 配置时返回空
两路差异进 fact_conflict 表，不入 fact 表。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from create_data import reconcile, vlm_extract  # noqa: E402
from create_data.chunker import Chunk, build_chunks  # noqa: E402
from create_data.metric_dict import METRICS  # noqa: E402
from create_data.table_parser import (  # noqa: E402
    DocumentFacts,
    Fact,
    TableBlock,
    parse_document,
)

load_dotenv()

DATA_DIR = Path(os.getenv("NEW_DATA_DIR", ROOT / "new_Data"))
DB_PATH = Path(os.getenv("FIN_DB_PATH", ROOT / "db" / "financial.db"))
REPORT_YEAR = int(os.getenv("REPORT_YEAR", "2025"))
CODE_RE = re.compile(r"(?:证券|公司)代码\s*[：:]\s*(\d{6})")
COLLECTION = os.getenv("MILVUS_COLLECTION", "fin_report_chunks")
DENSE_DIM = int(os.getenv("EMBEDDING_DIM", "768"))


@dataclass
class Document:
    company: str
    code: str
    doc_path: str
    facts: list[Fact] = field(default_factory=list)
    tables: list[TableBlock] = field(default_factory=list)
    chunks: list[Chunk] = field(default_factory=list)
    conflicts: list[reconcile.Conflict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def extract_company_code(md_text: str) -> str:
    match = CODE_RE.search(md_text)
    return match.group(1) if match else ""


def load_documents() -> list[Document]:
    docs: list[Document] = []
    for co_dir in sorted(p for p in DATA_DIR.iterdir() if p.is_dir()):
        md = next(co_dir.glob("*.md"), None)
        if md is None:
            continue

        parsed: DocumentFacts = parse_document(md, co_dir.name, REPORT_YEAR)
        vlm_facts = vlm_extract.extract_facts(co_dir, co_dir.name, REPORT_YEAR)
        facts, conflicts = reconcile.merge(parsed.facts, vlm_facts, parsed.doc_path)
        code = extract_company_code(md.read_text(encoding="utf-8"))

        docs.append(Document(
            company=co_dir.name, code=code, doc_path=parsed.doc_path,
            facts=facts, tables=parsed.tables, conflicts=conflicts,
            warnings=parsed.warnings,
            chunks=build_chunks(md, co_dir.name, code, REPORT_YEAR, parsed.tables),
        ))
    return docs


SCHEMA = """
DROP TABLE IF EXISTS fact;
DROP TABLE IF EXISTS metric;
DROP TABLE IF EXISTS table_meta;
DROP TABLE IF EXISTS fact_conflict;

CREATE TABLE metric (
  code        TEXT PRIMARY KEY,
  name_cn     TEXT NOT NULL,
  aliases     TEXT NOT NULL,
  period_type TEXT NOT NULL,
  unit_kind   TEXT NOT NULL
);

CREATE TABLE fact (
  id            INTEGER PRIMARY KEY,
  company       TEXT NOT NULL,
  company_code  TEXT,
  report_year   INTEGER NOT NULL,
  metric_code   TEXT NOT NULL REFERENCES metric(code),
  period_type   TEXT NOT NULL,
  period_label  TEXT NOT NULL,
  period_start  TEXT,
  period_end    TEXT NOT NULL,
  value_num     REAL NOT NULL,
  value_yuan    REAL,
  unit_raw      TEXT,
  unit_kind     TEXT NOT NULL,
  source        TEXT NOT NULL,
  source_doc    TEXT,
  source_page   INTEGER,
  source_table  INTEGER,
  source_row    INTEGER,
  source_col    INTEGER,
  raw_text      TEXT,
  raw_header    TEXT,
  UNIQUE(company, metric_code, period_label)
);
CREATE INDEX idx_fact_lookup ON fact(metric_code, period_end);
CREATE INDEX idx_fact_company ON fact(company, metric_code);

CREATE TABLE table_meta (
  id INTEGER PRIMARY KEY,
  company TEXT NOT NULL,
  source_doc TEXT,
  table_index INTEGER,
  title TEXT,
  table_type TEXT,
  unit TEXT,
  n_rows INTEGER
);

CREATE TABLE fact_conflict (
  id           INTEGER PRIMARY KEY,
  company      TEXT NOT NULL,
  metric_code  TEXT NOT NULL,
  period_label TEXT NOT NULL,
  ocr_value    REAL,
  vlm_value    REAL,
  source_doc   TEXT
);
"""


def build_sqlite(docs: list[Document]) -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    DB_PATH.unlink(missing_ok=True)

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.executescript(SCHEMA)
    cur.executemany(
        "INSERT INTO metric VALUES (?,?,?,?,?)",
        [(code, meta["name_cn"], json.dumps(meta["aliases"], ensure_ascii=False),
          meta["period_type"], meta["unit_kind"]) for code, meta in METRICS.items()],
    )

    fact_rows, meta_rows, conflict_rows = [], [], []
    for doc in docs:
        for f in doc.facts:
            fact_rows.append((
                doc.company, doc.code, f.report_year, f.metric_code, f.period_type,
                f.period_label, f.period_start, f.period_end, f.value_num,
                f.value_yuan, f.unit_raw, f.unit_kind, f.source, doc.doc_path,
                f.source_page, f.table_index, f.row_index, f.col_index,
                f.raw_text, f.raw_header,
            ))
        for t in doc.tables:
            meta_rows.append((doc.company, doc.doc_path, t.index, t.title,
                              t.table_type, t.unit_raw, len(t.grid)))
        for c in doc.conflicts:
            conflict_rows.append((c.company, c.metric_code, c.period_label,
                                  c.ocr_value, c.vlm_value, c.doc_path))

    cur.executemany(
        "INSERT INTO fact (company, company_code, report_year, metric_code, period_type,"
        " period_label, period_start, period_end, value_num, value_yuan, unit_raw,"
        " unit_kind, source, source_doc, source_page, source_table, source_row,"
        " source_col, raw_text, raw_header) VALUES (" + ",".join("?" * 20) + ")",
        fact_rows,
    )
    cur.executemany(
        "INSERT INTO table_meta (company, source_doc, table_index, title, table_type,"
        " unit, n_rows) VALUES (?,?,?,?,?,?,?)",
        meta_rows,
    )
    cur.executemany(
        "INSERT INTO fact_conflict (company, metric_code, period_label, ocr_value,"
        " vlm_value, source_doc) VALUES (?,?,?,?,?,?)",
        conflict_rows,
    )
    con.commit()

    counts = {name: cur.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
              for name in ("fact", "table_meta", "fact_conflict")}
    con.close()
    print(f"[SQLite] {DB_PATH}  fact={counts['fact']}  "
          f"table_meta={counts['table_meta']}  conflict={counts['fact_conflict']}")


def _milvus_client():
    from pymilvus import MilvusClient
    return MilvusClient(uri=os.getenv("MILVUS_ENDPOINT", "http://127.0.0.1:19530"))


def _analyzer_params_for(uri: str) -> dict:
    """分词器跟着后端版本走。Lite 只认 jieba，standalone v2.5 只认 chinese。"""
    return {"type": "chinese" if uri.startswith("http") else "jieba",
            "filter": [{"type": "lowercase"}]}


def _create_collection(client, analyzer: dict) -> None:
    from pymilvus import DataType, Function, FunctionType

    if client.has_collection(COLLECTION):
        client.drop_collection(COLLECTION)

    schema = client.create_schema(auto_id=True, enable_dynamic_field=False)
    schema.add_field("id", DataType.INT64, is_primary=True)
    schema.add_field("company", DataType.VARCHAR, max_length=64)
    schema.add_field("company_code", DataType.VARCHAR, max_length=16)
    schema.add_field("report_year", DataType.INT32)
    schema.add_field("section", DataType.VARCHAR, max_length=1024)
    schema.add_field("chunk_type", DataType.VARCHAR, max_length=32)
    schema.add_field("doc_path", DataType.VARCHAR, max_length=512)
    schema.add_field("img_path", DataType.VARCHAR, max_length=512)
    schema.add_field("table_ref", DataType.VARCHAR, max_length=64)
    schema.add_field("text", DataType.VARCHAR, max_length=4096,
                     enable_analyzer=True, analyzer_params=analyzer)
    schema.add_field("dense", DataType.FLOAT_VECTOR, dim=DENSE_DIM)
    schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)
    schema.add_function(Function(
        name="text_bm25", function_type=FunctionType.BM25,
        input_field_names=["text"], output_field_names=["sparse"],
    ))

    index_params = client.prepare_index_params()
    index_params.add_index("dense", index_type="FLAT", metric_type="IP")
    # BM25 function 产出的稀疏向量，索引 metric 必须是 BM25
    index_params.add_index("sparse", index_type="SPARSE_INVERTED_INDEX", metric_type="BM25")
    client.create_collection(COLLECTION, schema=schema, index_params=index_params)


def build_milvus(docs: list[Document]) -> None:
    from ai.LoadEmbeddingModel import load_embedding_model

    client = _milvus_client()
    uri = os.getenv("MILVUS_ENDPOINT", "")
    analyzer = _analyzer_params_for(uri)
    print(f"[Milvus] endpoint={uri or '(lite default)'} analyzer={analyzer}")
    _create_collection(client, analyzer)

    rows = [{
        "company": c.company,
        "company_code": c.company_code,
        "report_year": c.report_year,
        "section": c.section[:1000],
        "chunk_type": c.chunk_type,
        "doc_path": c.doc_path[:500],
        "img_path": c.img_path or "",
        "table_ref": c.table_ref or "",
        "text": c.text[:4000],
    } for doc in docs for c in doc.chunks]

    print(f"[Milvus] 生成向量 {len(rows)} 条 ...")
    vectors = load_embedding_model().embed_documents([r["text"] for r in rows])
    for row, vector in zip(rows, vectors):
        row["dense"] = vector

    client.insert(COLLECTION, rows)
    client.flush(COLLECTION)
    print(f"[Milvus] 完成，实体数 = {client.get_collection_stats(COLLECTION).get('row_count')}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite", action="store_true")
    parser.add_argument("--milvus", action="store_true")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not (args.sqlite or args.milvus or args.all or args.dry_run):
        parser.print_help()
        return 1

    docs = load_documents()
    total = {name: sum(len(getattr(d, name)) for d in docs)
             for name in ("facts", "chunks", "conflicts", "warnings")}
    by_source = {src: sum(1 for d in docs for f in d.facts if f.source == src)
                 for src in ("ocr", "vlm", "both")}
    print(f"文档 {len(docs)} 份 | facts {total['facts']} | chunks {total['chunks']} | "
          f"冲突 {total['conflicts']} | 校验警告 {total['warnings']}")
    print(f"取数来源 ocr={by_source['ocr']} vlm={by_source['vlm']} both={by_source['both']}")

    for doc in docs:
        line = (f"  {doc.company:<6} code={doc.code or '-':<6} facts={len(doc.facts):>3} "
                f"chunks={len(doc.chunks):>3} 冲突={len(doc.conflicts)}")
        if doc.warnings:
            line += f"  ⚠ {doc.warnings}"
        print(line)

    if args.dry_run:
        return 0
    if args.sqlite or args.all:
        build_sqlite(docs)
    if args.milvus or args.all:
        build_milvus(docs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
