"""聊天主链路：路由 → 双通道取数 → 组装上下文 → LLM 流式回答。

两条通道：
    A. 结构化（SQL）  财务数字，精确、可溯源
    B. 非结构化（RAG） Milvus 双路召回 + RRF + 精排，覆盖业务/行业/风险等叙述

路由走「小模型提议 + 代码裁决」（QueryRouter.resolve）：
模型只把问题理解成受限枚举的 DSL，是否采纳、查什么、查哪年由代码裁决，
输出不合法时回退规则路由。
"""
from __future__ import annotations

import re

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnableLambda, RunnableParallel

from ai import LoadLLM
from chat.dao import ChatDao
from chat.service import HistoryService
from chat.utils import QueryRouter, TableQuery

MAX_HISTORY_TURNS = 3
RAG_TOP_K = 8   # 检索喂给模型几条（too few 会让开放式问题只拿到边角料）

# LLM 必须知道的财报合规标记语义（这些词错了，答案就会把"不适用"说成"没披露"）
COMPLIANCE_RULES = """参考资料里会出现「适用 / 不适用 / 是 / 否 / 无 / -」这类合规标记，含义固定：
- 「不适用」：该公司按规则不需要披露此项，**不是数据缺失**，回答时不要写成"未披露"
- 「否」：对前面「是否…」问句的否定回答。例如「公司上市时未盈利且尚未实现盈利：否」表示公司已实现盈利
- 「无」「-」「--」：该项单元格为空，**不是数值 0**
以上标记都不能当成数字参与计算，也不能解读成"数据缺失"。"""

PROMPT_TEMPLATE = """你是一名严谨的财报分析助手。请只根据下面提供的资料回答用户问题。

""" + COMPLIANCE_RULES + """

回答规则：
1. 数字必须来自「结构化数据」或「文本资料」，不要凭空编造。
2. 「结构化数据」与「文本资料」冲突时，以结构化数据为准（正文数字常有四舍五入）。
3. 金额换算成「亿元」时保留 2 位小数，并注明原始单位。
4. 资料只覆盖问题的一部分时，先回答能回答的部分，再说明缺什么，不要整体拒答。
5. 只有当资料与问题完全无关时，才说明「摘要未披露」。
6. 可以基于资料做合理的简单推算或对比分析，但推算要写明口径和局限，不能伪装成披露值。
7. **输出纯文本**：不要用 Markdown 表格、不要用 `|---|` 分隔线；多个对象用「1. 2. 3.」或分号列出。
8. 回答用中文，先给结论，再给依据；不要输出「来源1」「参考资料」这类字样。

历史对话：
{history}

参考资料：
{context}

用户问题：{question}
{brief_hint}
回答："""


def _clean_history(messages, max_turns: int = MAX_HISTORY_TURNS):
    cleaned = [m for m in messages if m.get("content")]
    return cleaned[-(max_turns * 2):]


def _format_history(messages) -> str:
    if not messages:
        return "（无）"
    return "\n".join(
        f"{'用户' if m.get('role') == 'user' else '助手'}: {m['content']}"
        for m in messages
    )


# ---------------------------------------------------------------- 通道执行


def _sql_result(route: QueryRouter.Route) -> dict:
    """结构化通道取数：单公司查指标、多公司对比、或全库排名。"""
    if not route.metric:
        return {"status": "no_metric", "rows": []}
    if route.company:
        result = TableQuery.query_metric(route.company, route.metric, route.period_label)
        if result["status"] == "not_disclosed":
            # 退一步取该指标所有期间，至少让模型看到已有口径
            result = TableQuery.query_trend(route.company, route.metric)
        return result
    return TableQuery.query_compare(route.metric, route.period_label,
                                    companies=route.companies)


def _sql_channel(route: QueryRouter.Route) -> str:
    """给 LLM 看的结构化上下文（both 通道用）。"""
    return TableQuery.format_rows(_sql_result(route))


def _rag_channel(route: QueryRouter.Route, question: str,
                 sink: list | None = None) -> str:
    """非结构化通道：Milvus 双路召回 + RRF + 精排。

    不按 route.year 过滤：库里只有 2025 年那一份摘要，
    而问题里的年份（"2024 至 2030 年的预测"）往往是内容而不是报告年份，
    用它过滤会把本来能召回的内容全部滤掉。

    sink 非空时，把命中的 Document 收集起来，供答案末尾标注出处。
    """
    from chat.utils.MilvusRetriever import format_docs, search

    try:
        docs = search(question, company=route.company, year=None, k=RAG_TOP_K)
    except Exception as e:  # Milvus 未启动时不让整条链路失败
        print(f"[RAG] 检索不可用: {type(e).__name__}: {e}")
        return ""
    if sink is not None:
        sink.extend(docs)
    return format_docs(docs)


def _sources_text(docs: list, limit: int = 3, prefix: str = "来源：") -> str:
    """把命中片段的出处整理成一行附在答案末尾。

    出处由代码从检索结果的 metadata 里取，不交给生成模型 ——
    模型会编造出不存在的「来源 1」，或者把编号抄到错误的句子上。

    both 通道前面已经用「来源：」标过数值表，这里换成「参考资料：」避免两条来源混淆。
    """
    items: list[str] = []
    for doc in docs or []:
        meta = getattr(doc, "metadata", None) or {}
        name = TableQuery.doc_label(str(meta.get("doc_path") or ""),
                                    str(meta.get("company") or ""))
        if not name:
            continue
        section = (meta.get("section") or "").strip()
        label = f"{name} §{section}" if section else name
        if label not in items:
            items.append(label)
        if len(items) >= limit:
            break
    if not items:
        return ""
    return f"\n\n{prefix}" + "；".join(f"{i}. {x}" for i, x in enumerate(items, 1))


def _collect(route: QueryRouter.Route, question: str,
             rag_docs: list | None = None) -> dict:
    """按路由并行取两类资料。"""
    sql_context = (_sql_channel(route)
                   if route.channel in ("sql", "both", "rag_brief") else "")
    rag_context = (_rag_channel(route, question, rag_docs)
                   if route.channel in ("rag", "both", "rag_brief") else "")
    return {"sql_context": sql_context, "rag_context": rag_context}


def _compose_context(data: dict) -> str:
    """只拼接有内容的通道。

    不能把空通道写成「（无）」：模型会把「文本资料：（无）」理解成"没有资料"，
    即使结构化数据那一行明明有值，也会回答"未披露"。
    """
    blocks = []
    if data["sql_context"]:
        blocks.append("【结构化数据｜表格解析所得，数值精确】\n" + data["sql_context"])
    if data["rag_context"]:
        blocks.append("【文本资料｜年报正文检索结果】\n" + data["rag_context"])
    return "\n\n".join(blocks) if blocks else "（本次未检索到相关资料）"


# ---------------------------------------------------------------- 主函数


def chat(question: str, historyId, username: str = ""):
    """流式回答。historyId=0 表示新会话。"""
    history = []
    if historyId != 0:
        history = _clean_history(HistoryService.conversation_log(historyId, username)["data"])
    history_text = _format_history(history)

    llm = LoadLLM.chat_llm()
    route = QueryRouter.resolve(question, history_text=history_text)
    print(f"[路由] {route.describe()}")
    rag_docs: list = []   # 检索命中的片段，答完在末尾标注出处

    if route.channel == "chat":
        messages = history + [{"role": "user", "content": question}]
        for chunk in llm.stream(messages):
            if chunk.content:
                yield chunk.content
        return

    # 纯数值问题：答案由 SQL 结果确定性渲染，不经过生成模型
    # （实测 1.5B/1.7B 会把 322,045 户写成 32,204,500 户，数字不能交给小模型转述）
    if route.channel == "sql":
        result = _sql_result(route)
        answer = TableQuery.render_answer(result)
        if answer:
            print(f"[SQL] 命中 {len(result['rows'])} 行，确定性作答")
            yield answer
            return
        print(f"[SQL] 未命中（status={result.get('status')}），转文本通道")
        route.channel = "rag"

    # 增长率：需要两年数据，同样确定性计算
    if route.channel == "growth":
        year = route.year or 2025
        result = TableQuery.query_growth(
            route.metric, f"{year - 1}年", f"{year}年", company=route.company)
        answer = TableQuery.render_growth(result)
        if answer:
            print(f"[SQL] 同比计算命中 {len(result['rows'])} 家")
            yield answer
            return
        route.channel = "rag"

    # both：SQL 已经能给出确定答案时，先把它说完，再让模型只补充资料里的定性信息。
    # 不让模型复述数字——实测它会把「推算值」说成「直接披露」，也会改写数字。
    if route.channel == "both":
        result = _sql_result(route)
        answer = TableQuery.render_answer(result)
        if answer:
            print(f"[both] SQL 先答 {len(result['rows'])} 行，再补叙述")
            yield answer
            yield "\n\n"
            route.channel = "rag_brief"

    prompt = PromptTemplate(
        template=PROMPT_TEMPLATE,
        input_variables=["history", "context", "question", "brief_hint"],
    )

    BRIEF_HINT = ("\n注意：上面的结构化答案已经输出给用户了。你**只补充资料里关于该指标的定性信息**"
                  "（构成、变化原因、风险提示等），不要再复述任何数字，也不要重复结论。"
                  "如果资料里确实没有相关内容，用一句话说明「年报摘要只披露主要会计数据，"
                  "不含该指标的明细科目」即可，**不要编造任何项目名称或数字**。")
    COMPARE_HINT = ("\n注意：上面的排名数据已经输出给用户了。请基于这些数据做**简述对比分析**"
                    "（差距倍数、体量差异、行业特征等），不要重复罗列数字，也不要编造未给出的数据。")

    def _prepare(data: dict) -> dict:
        hint = ""
        if route.channel == "rag_brief":
            hint = COMPARE_HINT if (route.companies and len(route.companies) > 1) else BRIEF_HINT
        return {
            "history": history_text,
            "context": _compose_context(data),
            "question": question,
            "brief_hint": hint,
        }

    chain = (
        RunnableParallel({
            "data": RunnableLambda(lambda _: _collect(route, question, rag_docs)),
        })
        | RunnableLambda(lambda x: _prepare(x["data"]))
        | prompt
        | llm
        | StrOutputParser()
    )

    for chunk in chain.stream(question):
        yield chunk

    tail = _sources_text(
        rag_docs, prefix="参考资料：" if route.channel == "rag_brief" else "来源：")
    if tail:
        yield tail


def save_conversation_result(conversation_result_entity):
    question = conversation_result_entity.question
    username = conversation_result_entity.username
    parent_id = conversation_result_entity.parentId
    answer = conversation_result_entity.answer

    if parent_id and not ChatDao.parent_belongs_to(parent_id, username):
        return {"code": 403, "message": "父会话不存在或不属于当前用户", "data": None}

    history_id = ChatDao.save_conversation_result(question, username, parent_id, answer)
    if history_id != 0:
        return {"code": 200, "message": "保存成功", "data": history_id}
    return {"code": 500, "message": "保存失败", "data": None}
