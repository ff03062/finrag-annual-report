from pymilvus import MilvusClient

c = MilvusClient('db/milvus.db')
c.load_collection('fin_report_chunks')

rows = c.query(
    'fin_report_chunks',
    filter="company == '中芯国际'",
    output_fields=['section', 'chunk_type', 'text'],
    limit=10,
)

for r in rows:
    print(r['chunk_type'], '|', r['section'][:40], '|', r['text'][:60])