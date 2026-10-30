"""Milvus 双链路检索：稠密 ANN + 稀疏 BM25，库内 RRF 融合，再 cross-encoder 精排。

为什么两路：
- 稠密链路负责语义（"赚钱能力" ↔ "净利润"）
- 稀疏链路负责字面（"UNSPSC"、"注 1"、"第一大股东" 这类 embedding 表达不出来的词）

为什么 RRF：
- 两路分数量纲不可比（IP 相似度 0~1 vs BM25 0~20+），转成排名后相加即可

为什么还要精排：
- 双塔把 query 和 doc 分别编码，只能判断"语义接近"；cross-encoder 拼起来读，能判断"是否回答了问题"
"""
from __future__ import annotations

import os

from langchain_core.documents import Document

from ai.LoadEmbeddingModel import load_embedding_model
from ai.LoadMilvus import load_milvus_client
from ai.LoadRerankerModel import load_rerank_model

COLLECTION = os.getenv("MILVUS_COLLECTION", "fin_report_chunks")
RECALL_K = int(os.getenv("MILVUS_RECALL_K", "50"))   # 每路召回条数
FUSE_K = int(os.getenv("MILVUS_FUSE_K", "20"))       # RRF 融合后进入精排的条数
RRF_K = int(os.getenv("MILVUS_RRF_K", "60"))         # RRF 平滑常数

OUTPUT_FIELDS = ["company", "company_code", "report_year", "section",
                 "chunk_type", "doc_path", "img_path", "table_ref", "text"]

_LOADED = False


def _ensure_loaded(client) -> None:
    """Milvus 进程重启后 collection 会回到 released 状态，检索前必须 load。"""
    global _LOADED
    if _LOADED:
        return
    client.load_collection(COLLECTION)
    _LOADED = True


def _build_expr(company: str | None, year: int | None,
                chunk_types: list[str] | None) -> str | None:
    clauses = []
    if company:
        clauses.append(f'company == "{company}"')
    if year:
        clauses.append(f"report_year == {int(year)}")
    if chunk_types:
        quoted = ", ".join(f'"{t}"' for t in chunk_types)
        clauses.append(f"chunk_type in [{quoted}]")
    return " and ".join(clauses) if clauses else None


def search(query: str,
           company: str | None = None,
           year: int | None = None,
           chunk_types: list[str] | None = None,
           k: int = 5,
           use_rerank: bool = True) -> list[Document]:
    """双链路召回 + RRF + 精排，返回 top-k Document。"""
    from pymilvus import AnnSearchRequest, RRFRanker

    client = load_milvus_client()
    _ensure_loaded(client)
    embedder = load_embedding_model()
    expr = _build_expr(company, year, chunk_types)

    dense_req = AnnSearchRequest(
        data=[embedder.embed_query(query)],
        anns_field="dense",
        param={"metric_type": "IP", "params": {"ef": 64}},
        limit=RECALL_K,
        expr=expr,
    )
    # 稀疏链路直接吃原文，由 Milvus 内的 BM25 function 解析成稀疏向量
    sparse_req = AnnSearchRequest(
        data=[query],
        anns_field="sparse",
        param={"metric_type": "BM25"},
        limit=RECALL_K,
        expr=expr,
    )

    hits = client.hybrid_search(
        collection_name=COLLECTION,
        reqs=[dense_req, sparse_req],
        ranker=RRFRanker(RRF_K),
        limit=FUSE_K,
        output_fields=OUTPUT_FIELDS,
    )
    if not hits or not hits[0]:
        return []

    docs = [Document(page_content=hit["entity"]["text"],
                     metadata={k2: v for k2, v in hit["entity"].items() if k2 != "text"})
            for hit in hits[0]]

    if not use_rerank or len(docs) <= k:
        return docs[:k]

    reranker = load_rerank_model()
    scores = reranker.compute_score([(query, d.page_content) for d in docs])
    ranked = sorted(zip(docs, scores), key=lambda x: x[1], reverse=True)
    out = []
    for doc, score in ranked[:k]:
        doc.metadata["rerank_score"] = float(score)
        out.append(doc)
    return out


def format_docs(docs: list[Document]) -> str:
    """把检索结果拼成参考资料（带来源，便于模型引用与前端展示图片）。"""
    if not docs:
        return ""
    blocks = []
    for i, doc in enumerate(docs, start=1):
        meta = doc.metadata
        head = f"[来源{i}] {meta.get('company', '')}｜{meta.get('section', '')}"
        if meta.get("img_path"):
            head += f"｜图片：{meta['img_path']}"
        blocks.append(f"{head}\n{doc.page_content}")
    return "\n\n".join(blocks)


def list_chunks(company: str | None = None, keyword: str | None = None,
                limit: int = 10) -> list[dict]:
    """调试用：直接看库里存了什么（不走向量检索）。"""
    client = load_milvus_client()
    _ensure_loaded(client)

    clauses = []
    if company:
        clauses.append(f'company == "{company}"')
    if keyword:
        clauses.append(f'text like "%{keyword}%"')
    expr = " and ".join(clauses) if clauses else "report_year == 2025"

    rows = client.query(
        COLLECTION,
        filter=expr,
        output_fields=["company", "section", "chunk_type", "text", "img_path", "table_ref"],
        limit=limit,
    )
    return rows
