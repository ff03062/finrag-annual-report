"""意图 DSL 裁决器单测（不依赖模型，纯逻辑）。

裁决器的职责：把模型可能给出的任何输出，收敛成有限且安全的 Route。
这里逐条验证「越界即降级」的规则。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from chat.utils import IntentDSL
from chat.utils.IntentDSL import IntentDSL as DSL, adjudicate


def _dsl(**kwargs) -> DSL:
    base = dict(intent="query_metric", companies=[], metric=None,
                year=2025, quarter=None, aspect=None, confidence=0.9)
    base.update(kwargs)
    return DSL(**base)


CASES = [
    # (说明, DSL, 期望 channel, 期望字段)
    ("标准数值题",
     _dsl(companies=["中芯国际"], metric="revenue"),
     "sql", {"company": "中芯国际", "metric": "revenue", "period_label": "2025年"}),

    ("口语别名归一",
     _dsl(companies=["中芯"], metric="营业收入"),
     "sql", {"company": "中芯国际", "metric": "revenue"}),

    ("不在白名单的公司被丢弃",
     _dsl(companies=["苹果"], metric="revenue"),
     "both", {"company": None, "metric": "revenue"}),

    ("不在词典的指标→降级文本检索",
     _dsl(companies=["比亚迪"], metric="毛利率"),
     "rag", {"company": "比亚迪"}),

    ("多家公司→对比",
     _dsl(intent="compare", companies=["比亚迪", "宁德时代"], metric="revenue"),
     "both", {"metric": "revenue"}),

    ("增速→growth",
     _dsl(intent="growth", companies=["德福科技"], metric="revenue"),
     "growth", {"metric": "revenue"}),

    ("数值+叙述关注点→both",
     _dsl(companies=["比亚迪"], metric="revenue", aspect="增长原因"),
     "both", {"aspect_ignored": True}),

    ("季度数据只对 4 个指标有效",
     _dsl(companies=["中芯国际"], metric="total_assets", quarter=2),
     "sql", {"period_label": "2025年末", "quarter": None}),

    ("季度对 revenue 有效",
     _dsl(companies=["中芯国际"], metric="revenue", quarter=1),
     "sql", {"period_label": "2025Q1", "quarter": 1}),

    ("超出范围的年份回落到 2025",
     _dsl(companies=["比亚迪"], metric="revenue", year=2019),
     "sql", {"year": 2025}),

    ("指代消解后的叙述题",
     _dsl(intent="narrative", companies=["锐科激光"]),
     "rag", {"company": "锐科激光"}),

    ("越界指标声明→文本检索",
     _dsl(intent="out_of_scope", companies=["宁德时代"], metric=None),
     "rag", {"company": "宁德时代"}),

    ("闲聊",
     _dsl(intent="chitchat"),
     "chat", {}),

    ("意图非法→降级",
     _dsl(intent="随便写", companies=["比亚迪"], metric="revenue"),
     "rag", {}),

    ("query_metric 缺指标→降级为文本题",
     _dsl(companies=["比亚迪"], metric=None),
     "rag", {}),

    ("compare 缺指标→降级为文本题",
     _dsl(intent="compare", companies=["比亚迪", "宁德时代"], metric=None),
     "rag", {}),

    # 财报摘要带往年对比列，库里实际有 2023/2024 数据，不能被压成 2025
    ("往年数据可直接查（2023）",
     _dsl(companies=["中芯国际"], metric="revenue", year=2023),
     "sql", {"year": 2023, "period_label": "2023年"}),

    ("往年时点数据（2024 年末）",
     _dsl(companies=["宁德时代"], metric="total_assets", year=2024),
     "sql", {"year": 2024, "period_label": "2024年末"}),

    ("往年季度数据不存在→只保留年度",
     _dsl(companies=["中芯国际"], metric="revenue", year=2024, quarter=1),
     "sql", {"year": 2024, "quarter": None, "period_label": "2024年"}),
]

# 需要 question 文本参与裁决的用例（相对时间、明确年份）
QUESTION_CASES = [
    ("去年 → 2024，不采信模型算的值",
     _dsl(companies=["比亚迪"], metric="net_profit_attributable", year=2023),
     "比亚迪去年赚了多少", {"year": 2024, "period_label": "2024年"}),

    ("前年 → 2023",
     _dsl(companies=["比亚迪"], metric="revenue", year=2025),
     "比亚迪前年营收多少", {"year": 2023}),

    ("问句里明确写 2023 时以问句为准",
     _dsl(companies=["中芯国际"], metric="revenue", year=2025),
     "中芯国际2023年营业收入是多少", {"year": 2023, "period_label": "2023年"}),

    ("模型漏指标时用规则补口语",
     _dsl(intent="compare", companies=["比亚迪", "宁德时代"], metric=None),
     "这两家谁更能挣钱", {"metric": "net_profit_attributable"}),

    ("用户明确问库里没有的年份→保留原值，让下游答未披露",
     _dsl(companies=["比亚迪"], metric="revenue", year=2020),
     "比亚迪2020年营业收入多少", {"year": 2020}),
]


def test_parse_tolerance():
    """模型输出常见脏格式都要能解析。"""
    samples = [
        '{"intent":"query_metric","companies":["比亚迪"],"metric":"revenue","year":2025}',
        '```json\n{"intent":"query_metric","companies":"比亚迪","metric":"revenue","year":"2025"}\n```',
        '好的，结果是：{"intent":"compare","companies":["比亚迪","宁德时代"],"metric":"revenue","quarter":"Q2"}',
    ]
    for raw in samples:
        dsl = DSL.parse(raw)
        assert dsl is not None, f"解析失败: {raw[:60]}"

    assert DSL.parse('```json\n{"intent":"query_metric","companies":"比亚迪","metric":"revenue","year":"2025"}\n```').companies == ["比亚迪"]
    assert DSL.parse('{"intent":"compare","companies":["比亚迪"],"metric":"revenue","quarter":"Q2"}').quarter == 2
    assert DSL.parse("完全不是 JSON") is None
    assert DSL.parse("") is None
    print("parse 容错 通过")


def main():
    test_parse_tolerance()

    failed = 0
    for name, dsl, expect_channel, expect in CASES:
        route = adjudicate(dsl, "")
        ok = route.channel == expect_channel
        for key, value in expect.items():
            if key == "aspect_ignored":
                continue
            if getattr(route, key, None) != value:
                ok = False
        status = "OK  " if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"  {status} {name:28} -> channel={route.channel:6} "
              f"company={route.company} metric={route.metric} period={route.period_label}")

    for name, dsl, question, expect in QUESTION_CASES:
        route = adjudicate(dsl, question)
        ok = all(getattr(route, key, None) == value for key, value in expect.items())
        status = "OK  " if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"  {status} {name:28} -> channel={route.channel:6} "
              f"metric={route.metric} period={route.period_label}")

    total = len(CASES) + len(QUESTION_CASES)
    print(f"\n{total - failed}/{total} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())