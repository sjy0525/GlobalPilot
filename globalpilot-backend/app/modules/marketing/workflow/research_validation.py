"""Validate research receipts and an independent review without calling a model.

URL provenance and verbatim quotes are checked in code. Whether a quote actually
supports a claim is assessed by the supplied reviewer; substring matching alone
does not establish that semantic relationship.
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
import json
import re
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.modules.marketing.schemas.research_report import ResearchAudit, ResearchReport


class ResearchReportError(RuntimeError):
    """A research run did not produce a usable, validated report."""


class ResearchReportMissingError(ResearchReportError):
    """The agent ended without a structured research report."""


class ResearchReportValidationError(ResearchReportError):
    """A report or its independent review failed validation."""


RESEARCH_REVIEW_SCOPE = {
    "node": "research_agent",
    "deliverables": ["有出处的调研事实", "已有品牌事实", "信息缺口和适用范围"],
    "excluded_deliverables": ["完整营销策略", "五部分策略", "内容排期", "渠道执行计划", "最终文案"],
    "completion_rule": "只判断本次调研所需信息是否已查清，不判断后续策略或文案是否已交付。",
}

INSUFFICIENT_SUMMARY = "现有资料不足以形成可核对的调研结论。"


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block if isinstance(block, str) else block.get("text", "")
            for block in content
            if isinstance(block, str)
            or (isinstance(block, dict) and isinstance(block.get("text"), str))
        )
    return ""


def _normalized(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _contains_quote(quote: str, original: str) -> bool:
    normalized_quote = _normalized(quote)
    return bool(normalized_quote) and normalized_quote in _normalized(original)


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item.strip() for item in items if item.strip()))


def _sentences(summary: str) -> list[tuple[str, str]]:
    """Keep the original separators so accepted text retains its punctuation."""
    parts = re.split(r"([。！？!?；;\r\n]+)", summary)
    sentences: list[tuple[str, str]] = []
    for index in range(0, len(parts), 2):
        text = parts[index].strip()
        if not text:
            continue
        separator = parts[index + 1] if index + 1 < len(parts) else ""
        sentences.append((text, text + separator))
    return sentences


def _collect_evidence(messages: list[Any]) -> tuple[str, dict[str, dict[str, Any]], dict]:
    task = "\n\n".join(
        _text(message.content) for message in messages if isinstance(message, HumanMessage)
    )
    queries_by_id = {
        call["id"]: call.get("args", {}).get("query", "")
        for message in messages
        if isinstance(message, AIMessage)
        for call in message.tool_calls
        if call.get("name") == "search_web" and call.get("id")
    }
    sources: dict[str, dict[str, Any]] = {}
    queries: list[str] = []
    errors: list[str] = []
    search_count = 0

    for message in messages:
        if not isinstance(message, ToolMessage) or message.name != "search_web":
            continue
        search_count += 1
        raw_content = _text(message.content)
        query = queries_by_id.get(message.tool_call_id, "")
        try:
            payload = json.loads(raw_content)
        except (ValueError, TypeError):
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("query"), str):
            query = payload["query"]
        if isinstance(query, str) and query.strip():
            queries.append(query.strip())

        if message.status == "error":
            errors.append(f"第{search_count}次搜索失败：{raw_content or '工具返回错误'}")
            continue
        if not isinstance(payload, dict):
            errors.append(f"第{search_count}次搜索未返回有效的JSON对象。")
            continue
        if payload.get("ok") is False or payload.get("error"):
            errors.append(f"第{search_count}次搜索失败：{payload.get('error') or '工具标记失败'}")
            continue
        results = payload.get("results")
        if not isinstance(results, list):
            errors.append(f"第{search_count}次搜索未返回有效的结果列表。")
            continue

        for item in results:
            if not isinstance(item, dict):
                continue
            url, content = item.get("url"), item.get("content")
            if not isinstance(url, str) or not url.strip():
                continue
            if not isinstance(content, str) or not content.strip():
                continue
            url, content = url.strip(), content.strip()
            title = item.get("title")
            title = title.strip() if isinstance(title, str) else ""
            date = item.get("date")
            date = date if isinstance(date, str) else ""
            if url not in sources:
                sources[url] = {"url": url, "title": title, "content": content, "date": date}
            else:
                if content not in sources[url]["content"]:
                    sources[url]["content"] += "\n\n" + content
                if date and not sources[url]["date"]:
                    sources[url]["date"] = date
                elif date and date != sources[url]["date"]:
                    dates = sources[url].setdefault("date_values", [])
                    if date not in dates:
                        dates.append(date)

    process = {
        "search_count": search_count,
        "queries": queries,
        "tool_errors": errors,
        "returned_source_count": len(sources),
        "returned_urls": list(sources),
        "description": (
            f"已收到{search_count}次search_web搜索返回，其中{len(errors)}次发生错误；"
            f"成功结果中有{len(sources)}个同时具备链接和正文的来源。"
        ),
    }
    return task, sources, process


def validate_research_report(
    state: dict,
    report: ResearchReport,
    reviewer: Callable[[dict], ResearchAudit | dict],
) -> tuple[ResearchReport, dict]:
    """Return only reviewed conclusions, or raise when review cannot be trusted."""
    if not isinstance(report, ResearchReport):
        raise ResearchReportValidationError("调研报告不是ResearchReport对象。")

    task, sources, process = _collect_evidence(state.get("messages", []))
    if any(not url.strip() for url in report.source_urls):
        raise ResearchReportValidationError("报告包含空的来源链接。")
    cited_urls = _unique(report.source_urls)
    unknown_urls = [url for url in cited_urls if url not in sources]
    if unknown_urls:
        raise ResearchReportValidationError("报告引用了搜索工具未成功返回正文的来源链接。")

    sentence_units = _sentences(report.summary)
    sentences = [
        {"index": index, "text": text}
        for index, (text, _) in enumerate(sentence_units)
    ]
    review_input = {
        "scope": RESEARCH_REVIEW_SCOPE,
        "task": task,
        "sentences": sentences,
        "sources": [dict(sources[url]) for url in cited_urls],
        "process": process,
        "report": report.model_dump(),
    }
    process_original = json.dumps(process, ensure_ascii=False, sort_keys=True)
    try:
        audit = ResearchAudit.model_validate(reviewer(deepcopy(review_input)))
    except Exception as exc:
        raise ResearchReportValidationError("独立核对未返回有效的ResearchAudit。") from exc

    checked_indices = [check.index for check in audit.checks]
    expected_indices = set(range(len(sentence_units)))
    if len(checked_indices) != len(expected_indices) or set(checked_indices) != expected_indices:
        raise ResearchReportValidationError("独立核对必须对每个句段返回唯一且完整的检查结果。")
    explicit_rejections = set(audit.rejected_indices)
    if len(explicit_rejections) != len(audit.rejected_indices) or not explicit_rejections <= expected_indices:
        raise ResearchReportValidationError("显式拒绝的句段index必须唯一且属于本次检查。")

    checks_by_index = {check.index: check for check in audit.checks}
    kept_sentences: list[str] = []
    supported_urls: list[str] = []
    rejected_indices: list[int] = []
    issues: list[str] = []
    for index, (text, rendered_text) in enumerate(sentence_units):
        check = checks_by_index[index]
        valid = False
        reason = check.reason.strip()
        if index in explicit_rejections:
            reason = reason or "独立核对明确拒绝该句段。"
        elif check.verdict == "supported":
            if check.source == "task":
                valid = _contains_quote(check.quote, task)
            elif check.source == "process":
                valid = _contains_quote(check.quote, process_original)
            elif check.source == "web" and check.source_url in cited_urls:
                valid = _contains_quote(check.quote, sources[check.source_url]["content"])
                if valid:
                    supported_urls.append(check.source_url)
            elif check.source == "web_metadata" and check.source_url in cited_urls:
                source = sources[check.source_url]
                date_values = [source["date"], *source.get("date_values", [])]
                # Metadata dates are not body text or proof of current service.
                # A fixed descriptive sentence prevents this evidence type from
                # carrying a separate policy/current-status assertion.
                valid = (
                    bool(check.quote)
                    and check.quote in date_values
                    and _normalized(text) == _normalized(f"检索结果date字段标注日期为{check.quote}")
                )
                if valid:
                    supported_urls.append(check.source_url)
            if not valid:
                reason = (
                    "检索元数据必须完整匹配date原值，且只能描述该字段，不能证明政策或当前服务状态。"
                    if check.source == "web_metadata"
                    else "核对引用未出现在对应的原始资料中。"
                )
        elif check.verdict == "inference":
            valid = check.source == "inference" and ("推断" in text or "假设" in text)
            if not valid:
                reason = "推断未在句段中明确标注为推断或假设。"

        if valid:
            kept_sentences.append(rendered_text)
        else:
            rejected_indices.append(index)
            issues.append(f"未保留结论“{text}”：{reason or '缺少支持该结论的依据。'}")

    global_issues = _unique(audit.issues)
    if global_issues:
        # A material problem without a trustworthy sentence location cannot be
        # repaired by merely downgrading status while retaining the old claims.
        kept_sentences = []
        supported_urls = []
        rejected_indices = sorted(expected_indices)
        issues.extend(global_issues)
        issues.append("独立核对存在无法定位的实质问题，整份摘要未予保留。")

    if not kept_sentences:
        summary = INSUFFICIENT_SUMMARY
        issues.append("没有可保留的、有依据的调研结论。")
    else:
        summary = "".join(kept_sentences).strip()
    if not audit.task_answered:
        issues.append("现有结论未完整回答本次调研任务。")
    if process["tool_errors"]:
        issues.extend(process["tool_errors"])
    if process["search_count"] and report.status == "not_needed":
        issues.append("实际发生过搜索，不能将报告标记为无需外部调研。")
    if report.status == "completed" and not supported_urls:
        issues.append("报告缺少经过逐段核对的网页依据，不能标记为调研完成。")
    issues = _unique(issues)
    limitations = _unique(audit.limitations)

    validated = ResearchReport.model_validate(
        {
            **report.model_dump(),
            "status": "insufficient" if issues else report.status,
            "summary": summary,
            "source_urls": _unique(supported_urls),
            "uncertainties": _unique([*report.uncertainties, *limitations, *issues]),
        }
    )
    metadata = {
        # `passed` means the report can be used for this node's intended
        # delivery. A correctly reviewed insufficient report is still not a
        # successful fact-finding result; review_completed distinguishes these.
        "passed": not issues and audit.task_answered and validated.status in {"completed", "not_needed"},
        "review_completed": True,
        "task_answered": audit.task_answered,
        "report_status": validated.status,
        "issues": issues,
        "limitations": limitations,
        "rejected_indices": rejected_indices,
        "globally_blocked": bool(global_issues),
        "checked_sentences": len(sentences),
        "sources_checked": len(cited_urls),
        "review_model_calls": 1,
    }
    return validated, metadata
