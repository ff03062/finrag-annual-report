# 上市公司年报摘要 RAG 问答系统

面向 15 家 A 股半导体 / 新能源产业链公司的年报摘要问答系统。
**数值走结构化查询（可校验），叙述走混合检索（可引用）**，数值题不经过生成模型。

> 表格解析 → 结构化入库 → 意图识别 → 双链路检索 → 答案生成，全流程可复现。

## 核心特性

| 能力 | 实现 |
|---|---|
| **数值零幻觉** | 数值题由参数化 SQL + 模板渲染产出，不经过生成模型；实测小模型会把 `322,045 户` 改写成 `32,204,500 户` |
| **双链路混合检索** | Milvus 内稠密（bge-base-zh-v1.5）+ 稀疏（BM25）双索引，RRF 融合后 cross-encoder 精排 |
| **意图：模型提议** | 本地 4B 小模型做 Schema Linking，只看枚举清单，输出受限 DSL，不碰 SQL 与数值 |
| **路由：代码裁决** | 白名单校验、相对时间换算、字段一致性降级，模型不可用时自动回退规则 |
| **答案可溯源** | 每个答案标注「哪份文件、第几张表」或「哪一节」，由代码追加而非模型生成 |
| **表格三重校验** | 「四季度之和 = 年度值」硬约束，45/45 通过 |

## 架构

```
                       用户问题
                          │
                  ┌───────▼────────┐
                  │  SchemaLinker  │  本地 4B 小模型
                  │  输出意图 DSL   │  只看 15 家公司 + 13 个指标枚举
                  └───────┬────────┘
                  ┌───────▼────────┐
                  │  裁决器（代码） │  白名单校验 / 时间换算 / 降级
                  └───────┬────────┘
                          │  Route
      ┌───────────┬───────┴───────┬────────────┐
      ▼           ▼               ▼            ▼
   sql/growth    rag            both          chat
   SQLite     Milvus 双链路   SQL 先答      直接回答
   模板渲染   +RRF+精排      + 模型补叙述
   零幻觉     top-8
```

## 快速开始

### 1. 环境

```bash
pip install -r requirements.txt
```

需要 Python 3.10+。首次运行会自动下载 `bge-base-zh-v1.5`（约 400MB）和 `bge-reranker-large`（约 1.3GB）。

### 2. 配置

复制 `.env.example` 为 `.env`，填入：

```ini
# 生成模型（云端）
LLM_PROVIDER = "dashscope"
DASHSCOPE_API_KEY = "sk-你的key"
DASHSCOPE_MODEL = "qwen3.8-max-0902"

# 向量库：先用 Milvus Lite，零依赖
MILVUS_ENDPOINT = "./db/milvus.db"
MILVUS_COLLECTION = "fin_report_chunks"

# 意图识别（可选，关掉则走纯规则路由）
INTENT_MODE = "rule"
```

### 3. 构建索引

```bash
# 结构化事实 → SQLite（681 条）
python create_data/build_index.py --sqlite

# 文本与表格 → Milvus（512 chunks）
python create_data/build_index.py --milvus
```

### 4. 启动

```bash
python main.py     # http://127.0.0.1:8000
```

打开 http://127.0.0.1:8000/docs 可直接试接口。

### 5. 验证

```bash
python tests/test_intent_dsl.py           # 意图裁决器 24 条（不需模型，最快）
python create_data/test_table_parser.py   # 表格解析
python create_data/test_chunker.py        # 切块
python tests/test_retrieval.py            # 检索 Recall / MRR（需 Milvus）
python tests/test_e2e.py                  # 端到端 21 题（需 Milvus + LLM）
```

## 实测数据

| 指标 | 结果 |
|---|---|
| 表格解析硬校验（四季度之和 = 年度） | **45/45** |
| 结构化事实 | **681 条**（年度/时点 441 + 季度 240） |
| 切块 | **512 块** |
| 检索 Recall@5 / MRR@10 | **100% / 0.944** |
| 意图裁决单测 | **24/24** |
| 端到端 21 题（含 3 道越界拒答） | **21/21** |
| 意图识别耗时 | 中位 **1.17s** |
| SQL 通道端到端 | **1.1–1.3s** |
| RAG 通道端到端 | **10–27s** |

## 数据覆盖

- **公司**：15 家（半导体 / 锂电 / 光伏产业链）
- **年度**：2023 / 2024 / 2025（含财报表格里的往年对比列）
- **季度**：仅 2025 年，且只有营收、归母净利润、扣非净利润、经营现金流 4 个指标
- **指标**：13 个（含 1 个推算指标「负债合计」）

数据范围由 `chat/utils/datascope.py` 从 SQLite 实时推导，不写死在代码里。

## 项目结构

```
chat/
  controller/     HTTP 入口、SSE、JWT
  service/        主链路编排
  utils/
    QueryRouter   路由总入口：模型提议 → 代码裁决 → 规则兜底
    SchemaLinker  调本地小模型做意图识别
    IntentDSL     DSL 定义 + 裁决器
    TableQuery    参数化 SQL + 确定性渲染 + 溯源
    MilvusRetriever 双链路检索 + RRF + 精排
    datascope     数据覆盖范围推导
create_data/      离线构建：表格解析、切块、建索引
ai/               embedding / reranker / LLM / Milvus 客户端
tests/            评测集与测试脚本
new_Data/         15 份年报摘要（md + 图片）
docs/             设计文档
```

## 文档

- [架构详解](docs/交付物/02-框架设计详解.md) — 调用链、裁决规则、代码地图
- [速览](docs/PROJECT.md) — 核心设计与实测结果
- [本地模型部署](docs/finqa4b-deploy.md) — 4B 小模型的下载、量化、导入

## 向量库切换

| 模式 | 配置 | 适用 |
|---|---|---|
| Milvus Lite | `MILVUS_ENDPOINT = "./db/milvus.db"` | 本地开发，零依赖 |
| Milvus standalone | `MILVUS_ENDPOINT = "http://127.0.0.1:19530"` | 多客户端、可视化 |

```bash
docker compose -f docker-compose.milvus.yml up -d
```

两种模式的**数据不共享**，切换后需重跑 `build_index.py --milvus`。
分词器随 URI 自动切换（Lite → jieba，standalone → chinese）。

## License

MIT