# 重构大纲（新版代码，基于 new_Data）

> 目标：保留原有 LangChain Runnable 写法，把数据源换成 15 份摘要，新增「结构化 + RAG」双通道。
> 原则：低耦合、每层只做一件事、不做无用兜底。

---

## 0. 目录与职责

```
create_data/                     ← 离线构建（不含 LLM 调用）
  metric_dict.py      新增  13 个指标词典 + 行名归一
  table_parser.py     新增  HTML 表 → facts（纯函数，只依赖 bs4）
  chunker.py          新增  md → chunks（H3 切分 / 表格占位 / 图注合并）
  build_index.py      新增  CLI：facts → SQLite，chunks → Milvus
  chunking.py         保留  旧全量链路，不再作为主路径
  WriteChroma.py      保留  同上

chat/
  service/ChatService.py   改   Router + 双通道并行 + 原有 Runnable 链
  utils/
    QueryRouter.py    新增  规则优先的问题路由（不改 intention_recognition）
    TableQuery.py     新增  facts 参数化查询 + lineage
    MilvusRetriever.py 新增 Milvus 双路 hybrid_search（RRF 用库内 ranker）

ai/
  LoadMilvus.py       新增  Milvus client 单例
  LoadLLM.py          改   支持 rLLM-FinQA-4B（保留 qwen 回退）

db/financial.db               新增  SQLite，facts + metric + table_meta
tests/                        新增  解析/检索/端到端验证
```

**依赖方向**（不允许反向）：

```
table_parser ──▶ metric_dict
     │
     ▼
build_index ──▶ db/financial.db
     │
chunker ──▶ Milvus

ChatService ──▶ QueryRouter ──▶ TableQuery ──▶ sqlite
            └─▶ MilvusRetriever ──▶ Milvus
            └─▶ LLM
```

---

## 1. 数据层：`metric_dict.py`

- `METRICS`：13 个主指标 + 5 个季度指标，字段 `code / name_cn / aliases / period_type / unit_kind`
- `normalize_label(s)`：去空白、去「（元/股）」「（%）」后缀、统一「扣非…后的净利润」写法
- `resolve_metric(label) -> str | None`：行名 → metric_code
- 这张表同时被 `table_parser`（行映射）和 `TableQuery`（SQL 枚举校验）引用，是唯一的指标定义源

## 2. 解析层：`table_parser.py`

纯函数，无 LLM、无 IO 副作用：

| 函数 | 职责 |
|---|---|
| `parse_tables(md_text)` | 抽出所有 `<table>`，返回 `[(index, grid, context_lines)]` |
| `repair_rows(grid)` | OCR 拆行合并（除首列全空 → 拼上一行） |
| `detect_header_groups(grid)` | 识别表头行，返回 `[{row_idx, periods:[(col, year, type, role)]}]` |
| `parse_annual_facts(...)` | 主表 → facts |
| `parse_quarter_facts(...)` | 季度表 → facts |
| `extract_unit(context_lines)` | 「单位：千元」→ (`千元`, 1000) |
| `normalize_number(cell)` | 去空格/逗号/百分号，`-`→None |
| `validate(facts)` | C1：季度和 == 年度；返回校验结果 |

关键规则（已实测）：
- 表头判定：首列为空或 `项目`，且 ≥3 格含 `20\d\d`
- 期间类型**以指标为准**，不以表头为准
- 增减列（含「增减」）不入库
- 单位统一换算为「元」，保留 `unit_raw`

## 3. 切块层：`chunker.py`

| 参数 | 值 |
|---|---|
| 切分单位 | H3；H3 过长再按段落 |
| 块长 | 350–450 字 |
| overlap | 60 字 |
| 硬上限 | 600 字 |

- 表格位置替换为占位 `[表：近三年主要会计数据]`（`table_summary` 类型块）
- 图片：以 `img/*.jpg` 文件名为图注，与后续段落合并成 `chart` 块
- 元数据：`company / company_code / report_year / section / chunk_type / doc_path / img_path`

## 4. 索引层：`build_index.py`

- `--sqlite`：`facts.json` → `db/financial.db`（建表 + 灌数 + 校验）
- `--milvus`：`chunks.jsonl` → Milvus（dense 768 + sparse BM25 内置）
- `--dry-run`：只统计不进库

## 5. 路由层：`QueryRouter.py`

规则优先，不调 LLM：

```
问题 → 公司匹配（KB_COMPANIES 全称/简称）
     → 指标匹配（metric aliases）
     → 期间解析（2025 / 2025Q1 / 去年）
     → 输出 {channel, company, year, metric, period}
```

`channel`：
- `sql`：公司+指标+期间都命中 → 走 facts
- `rag`：无指标命中（业务/行业/风险）
- `both`：命中指标但同时有叙述性词（"增长原因"）

只在规则无法判断时才落到原 `intention_recognition`（LLM），保持它不动。

## 6. 查询层：`TableQuery.py`

```python
def query(company, metric, period) -> dict
```
- 参数化 SQL，`PRAGMA query_only=ON`
- 返回 `{rows, lineage, unit}`，`lineage` 含 doc/table/row
- 查不到返回空，由上层决定如何表述

## 7. 检索层：`MilvusRetriever.py`

```python
def search(query, company=None, year=None, k=5) -> list[Document]
```
- `AnnSearchRequest(dense, limit=50)` + `AnnSearchRequest(sparse, limit=50)`
- `hybrid_search(ranker=RRFRanker(60), limit=20)`
- `bge-reranker-large` 精排 → top k
- 标量过滤下推到 ANN 前

## 8. 编排层：`ChatService.py`

保持原 Runnable 结构，只替换内部：

```python
qa_chain = (
    RunnableParallel({
        "sql":  RunnableLambda(_sql_channel),
        "docs": RunnableLambda(_rag_channel),
    })
    | RunnableLambda(_compose_context)   # 合并通道结果
    | prompt
    | llm
    | StrOutputParser()
)
```

### 提示词必须加入的「适用/不适用」语义

财报里固定出现的标记，必须写进 system prompt：

| 原文 | 含义 | LLM 该怎么理解 |
|---|---|---|
| `不适用` | 该披露项对这家公司不成立 | **不是缺失**，是「此项无内容」 |
| `否` | 针对「是否…」类问句的否定 | 例如「未盈利且尚未实现盈利：否」= 公司已盈利 |
| `无` | 该项为空/不存在 | 不是 0 |
| `-` / `--` | 表格单元格无数据 | 不是 0 |

提示词原文：

```
参考资料中可能出现「适用 / 不适用 / 是 / 否 / 无 / -」这类合规标记，含义如下：
- 「不适用」表示该公司在报表上无需披露此项，**不是因为数据缺失**
- 「否」是对前面「是否…」问句的否定回答，例如「公司上市时未盈利且尚未实现盈利：否」表示公司已实现盈利
- 「无」「-」「--」表示该项单元格为空，**数值为 0 也是空**
回答时不要把这些标记当成数字，也不要说成"数据缺失"。
```

## 9. 验证集：`tests/`

| 文件 | 内容 |
|---|---|
| `test_table_parser.py` | 15 家解析 + C1 校验 45/45 |
| `test_chunker.py` | 块数区间、元数据完整性、表格占位不残留 HTML |
| `test_retrieval.py` | Recall@5 / MRR@10，用 `docs/eval-set-draft.md` 的检索子集 |
| `test_e2e.py` | 21 题端到端，输出通过率 |

评测集标注文件：`tests/eval_questions.json`（问题 + 期望通道 + 期望值 + 相关 chunk）

---

## 10. 实施顺序

1. `metric_dict.py` + `table_parser.py` + `test_table_parser.py`（先把 666 facts 跑通）
2. `chunker.py` + `test_chunker.py`
3. `build_index.py`（SQLite 先，Milvus 后）
4. `TableQuery.py` + `QueryRouter.py` + 单测
5. `MilvusRetriever.py`（Milvus 起容器后）
6. `ChatService.py` 改造 + 提示词
7. 端到端 21 题

每步独立可测，前 4 步不依赖 Milvus 和 LLM。
