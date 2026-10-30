"""离线构建索引：facts → SQLite，chunks → Milvus。

用法：
    python create_data/build_index.py --sqlite          # 只建 SQLite（不依赖 Milvus）
    python create_data/build_index.py --milvus          # 只建 Milvus
    python create_data/build_index.py --all             # 两个都建
    python create_data/build_index.py --all --dry-run   # 只统计，不写库
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from create_data.chunker import build_chunks  # noqa: E402
from create_data.metric_dict import METRICS  # noqa: E402
from create_data.table_parser import parse_document  # noqa: E402

load_dotenv()

DATA_DIR = Path(os.getenv("NEW_DATA_DIR", ROOT / "new_Data"))
DB_PATH = Path(os.getenv("FIN_DB_PATH", ROOT / "db" / "financial.db"))
REPORT_YEAR = int(os.getenv("REPORT_YEAR", "2025"))
CODE_RE = re.compile(r"公司代码\s*[：:]\s*(\d+)")


# ---------------------------------------------------------------- 文档层


def extract_company_code(md_text: str, tables) -> str:
    """公司代码：优取文首「公司代码：」，其次取公司简介表里的股票代码。"""
    m = CODE_RE.search(md_text)
    if m:
        return m.group(1)
    for t in tables:
        if t.table_type != "company_profile":
            continue
        for row in t.grid:
            for i, cell in enumerate(row):
                if "股票代码" in cell and i + 1 < len(row):
                    # 比亚迪是「002594、01211、81211」，取第一个 6 位代码
                    found = re.findall(r"\d{6}", row[i + 1])
                    if found:
                        return found[0]
    return ""


def load_documents(llm_fallback: bool = False) -> list[tuple[str, str, object, list]]:
    """返回 [(company, code, DocumentFacts, chunks)]"""
    result = []
    for co_dir in sorted(p for p in DATA_DIR.iterdir() if p.is_dir()):
        md = next(co_dir.glob("*.md"), None)
        if md is None:
            continue
        text = md.read_text(encoding="utf-8")
        doc = parse_document(md, co_dir.name, REPORT_YEAR, llm_fallback=llm_fallback)
        code = extract_company_code(text, doc.tables)
        chunks = build_chunks(md, co_dir.name, code, REPORT_YEAR)
        result.append((co_dir.name, code, doc, chunks))
    return result


# ---------------------------------------------------------------- SQLite

SCHEMA = """
DROP TABLE IF EXISTS fact;
DROP TABLE IF EXISTS metric;
DROP TABLE IF EXISTS table_meta;

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
  source_doc    TEXT,
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
"""


def build_sqlite(docs) -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.executescript(SCHEMA)

    cur.executemany(
        "INSERT INTO metric VALUES (?,?,?,?,?)",
        [(code, m["name_cn"], json.dumps(m["aliases"], ensure_ascii=False),
          m["period_type"], m["unit_kind"]) for code, m in METRICS.items()],
    )

    fact_rows, meta_rows = [], []
    for company, code, doc, _ in docs:
        for f in doc.facts:
            fact_rows.append((
                company, code, f.report_year, f.metric_code, f.period_type,
                f.period_label, f.period_start, f.period_end, f.value_num,
                f.value_yuan, f.unit_raw, f.unit_kind, doc.doc_path,
                f.table_index, f.row_index, f.col_index, f.raw_text, f.raw_header,
            ))
        for t in doc.tables:
            meta_rows.append((company, doc.doc_path, t.index, t.title,
                              t.table_type, t.unit_raw, len(t.grid)))

    cur.executemany(
        "INSERT INTO fact (company, company_code, report_year, metric_code, period_type,"
        " period_label, period_start, period_end, value_num, value_yuan, unit_raw,"
        " unit_kind, source_doc, source_table, source_row, source_col, raw_text, raw_header)"
        " VALUES (" + ",".join("?" * 18) + ")",
        fact_rows,
    )
    cur.executemany(
        "INSERT INTO table_meta (company, source_doc, table_index, title, table_type, unit, n_rows)"
        " VALUES (?,?,?,?,?,?,?)",
        meta_rows,
    )
    con.commit()

    n_fact = cur.execute("SELECT COUNT(*) FROM fact").fetchone()[0]
    n_meta = cur.execute("SELECT COUNT(*) FROM table_meta").fetchone()[0]
    con.close()
    print(f"[SQLite] {DB_PATH}  fact={n_fact}  table_meta={n_meta}")


# ---------------------------------------------------------------- Milvus

COLLECTION = os.getenv("MILVUS_COLLECTION", "fin_report_chunks")
DENSE_DIM = int(os.getenv("EMBEDDING_DIM", "768"))


def _milvus_client():
    from pymilvus import MilvusClient
    uri = os.getenv("MILVUS_ENDPOINT", "http://127.0.0.1:19530")
    return MilvusClient(uri=uri)


def _analyzer_params_for(uri: str) -> dict:
    """分词器跟着后端版本走，并统一做小写归一。

    - Milvus Lite（本地路径，3.x）：只认 jieba
    - Milvus standalone v2.5.x（http）：只认 chinese
    - lowercase filter：让 "mocvd" 能命中 "MOCVD"（设备名大小写不统一，很常见）
    """
    base = "chinese" if uri.startswith("http") else "jieba"
    return {"type": base, "filter": [{"type": "lowercase"}]}


def _create_collection(client, drop: bool = True, analyzer: dict | None = None) -> None:
    from pymilvus import DataType, Function, FunctionType

    if drop and client.has_collection(COLLECTION):
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
                     enable_analyzer=True,
                     analyzer_params=analyzer or {"type": "jieba"})
    schema.add_field("dense", DataType.FLOAT_VECTOR, dim=DENSE_DIM)
    schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)
    schema.add_function(Function(
        name="text_bm25", function_type=FunctionType.BM25,
        input_field_names=["text"], output_field_names=["sparse"],
    ))

    index_params = client.prepare_index_params()
    index_params.add_index("dense", index_type="FLAT", metric_type="IP")
    # BM25 function 产出的稀疏向量，索引 metric 必须是 BM25（Milvus 3.x 强制）
    index_params.add_index("sparse", index_type="SPARSE_INVERTED_INDEX", metric_type="BM25")
    client.create_collection(COLLECTION, schema=schema, index_params=index_params)


def build_milvus(docs) -> None:
    from ai.LoadEmbeddingModel import load_embedding_model

    embedder = load_embedding_model()
    client = _milvus_client()
    uri = os.getenv("MILVUS_ENDPOINT", "")
    analyzer = _analyzer_params_for(uri)
    print(f"[Milvus] endpoint={uri or '(lite default)'} analyzer={analyzer}")
    _create_collection(client, analyzer=analyzer)

    rows = []
    for company, code, doc, chunks in docs:
        for c in chunks:
            rows.append({
                "company": c.company,
                "company_code": c.company_code,
                "report_year": c.report_year,
                "section": c.section[:1000],
                "chunk_type": c.chunk_type,
                "doc_path": c.doc_path[:500],
                "img_path": c.img_path or "",
                "table_ref": c.table_ref or "",
                "text": c.text[:4000],
            })

    texts = [r["text"] for r in rows]
    print(f"[Milvus] 生成向量 {len(texts)} 条 ...")
    vectors = embedder.embed_documents(texts)
    for row, vec in zip(rows, vectors):
        row["dense"] = vec

    print(f"[Milvus] 写入 collection={COLLECTION} ...")
    client.insert(COLLECTION, rows)
    client.flush(COLLECTION)

    stats = client.get_collection_stats(COLLECTION)
    print(f"[Milvus] 完成，实体数 = {stats.get('row_count')}")


# ---------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite", action="store_true")
    parser.add_argument("--milvus", action="store_true")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--llm-fallback", action="store_true",
                        help="表格结构识别失败时调用本地 LLM 兜底（较慢，默认关）")
    args = parser.parse_args()

    if not (args.sqlite or args.milvus or args.all or args.dry_run):
        parser.print_help()
        return 1

    docs = load_documents(llm_fallback=args.llm_fallback)
    total_facts = sum(len(d.facts) for _, _, d, _ in docs)
    total_chunks = sum(len(c) for _, _, _, c in docs)
    total_warn = sum(len(d.warnings) for _, _, d, _ in docs)
    print(f"文档 {len(docs)} 份 | facts {total_facts} | chunks {total_chunks} | 校验警告 {total_warn}")
    for company, code, doc, chunks in docs:
        print(f"  {company:<6} code={code or '-':<6} facts={len(doc.facts):>3} chunks={len(chunks):>3}"
              + (f"  ⚠ {doc.warnings}" if doc.warnings else ""))

    if args.dry_run:
        return 0

    if args.sqlite or args.all:
        build_sqlite(docs)
    if args.milvus or args.all:
        build_milvus(docs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
