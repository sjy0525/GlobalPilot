"""Program checks and explicit model-assisted checks against a task snapshot.

Passing these checks does not establish model quality, factual support, user
permissions, or the current database version. must_keep is document-scoped:
each fragment must occur continuously within at least one candidate paragraph.
Length limits apply to editable paragraphs and explicitly select code-point or
whitespace-delimited counting, without modifying or normalizing candidate text.
validate_draft stays deterministic; only validate_semantics invokes a reviewer.
Literal references do not establish semantic entailment or exhaustive claims.
"""

import hashlib
import json
import logging
from collections.abc import Callable, Mapping
from typing import Any

from app.modules.marketing.schemas.revision import (
    CriterionReview,
    DraftCandidate,
    DraftValidationResult,
    ModelSemanticReview,
    RevisionReviewContext,
    RevisionTaskInput,
    SemanticValidationResult,
)
from app.modules.marketing.workflow.revision.prompts import (
    SEMANTIC_REVIEW_PROMPT,
)


logger = logging.getLogger(__name__)

# Bump when reviewer model/configuration or checking policy changes. The prompt
# and output schema are additionally bound into each input hash automatically.
SEMANTIC_REVIEW_VERSION = "revision-semantic-v1"

SemanticReviewer = Callable[[list[tuple[str, str]]], Any]


def count_text(text: str, unit: str) -> int:
    """Count Unicode code points or whitespace-separated items exactly as stored."""
    if unit == "unicode_chars":
        return len(text)
    if unit == "whitespace_words":
        return len(text.split())
    raise ValueError(f"不支持的长度计数单位：{unit}")


def validate_draft(
    task: RevisionTaskInput,
    candidate: DraftCandidate,
) -> DraftValidationResult:
    """Revalidate inputs, then return all detectable candidate problems."""
    task = RevisionTaskInput.model_validate(task.model_dump(warnings=False))
    candidate = DraftCandidate.model_validate(candidate.model_dump(warnings=False))
    source = task.draft
    issues = []

    if candidate.draft_id != source.draft_id:
        issues.append("候选对应的文稿不正确")
    if candidate.base_version != source.version:
        issues.append("候选基于的文稿版本不一致")

    source_ids = [block.block_id for block in source.blocks]
    candidate_ids = [block.block_id for block in candidate.blocks]
    if candidate_ids != source_ids:
        issues.append("候选的段落数量、ID 或顺序与原稿不一致")

    candidate_blocks = {block.block_id: block for block in candidate.blocks}
    allowed_ids = set(task.requirements.allowed_block_ids)
    for block in source.blocks:
        if block.block_id not in allowed_ids:
            current = candidate_blocks.get(block.block_id)
            if current is None or current.text != block.text:
                issues.append(f"允许范围外的段落被修改或丢失：{block.block_id}")

    for text in task.requirements.must_keep:
        if not any(text in block.text for block in candidate.blocks):
            issues.append(f"缺少必须保留的原文片段：{text}")

    source_positions = {
        block_id: position for position, block_id in enumerate(source_ids, start=1)
    }
    for limit in task.requirements.length_limits:
        block = candidate_blocks.get(limit.block_id)
        if block is None:
            # The structure check above already reports the missing paragraph.
            continue
        actual = count_text(block.text, limit.unit)
        if actual > limit.maximum:
            label = "Unicode 码点" if limit.unit == "unicode_chars" else "按空白分隔的词"
            position = source_positions[limit.block_id]
            issues.append(
                f"第 {position} 段（{limit.block_id}）有 {actual} 个{label}，"
                f"超过上限 {limit.maximum}（{limit.unit}）"
            )

    return DraftValidationResult(passed=not issues, issues=issues)


def semantic_input_hash(
    task: RevisionTaskInput,
    candidate: DraftCandidate,
    context: RevisionReviewContext,
) -> str:
    """Bind a report to all task inputs, including read-only paragraphs.

    This digest does not query the database or prove current source versions.
    A future submission boundary must reload those versions before using it.
    """
    payload = {
        "review_version": SEMANTIC_REVIEW_VERSION,
        "task": task.model_dump(mode="json"),
        "candidate": candidate.model_dump(mode="json"),
        "context": context.model_dump(mode="json"),
        "prompt": SEMANTIC_REVIEW_PROMPT,
        "output_schema": ModelSemanticReview.model_json_schema(),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _check_semantic_report(
    task: RevisionTaskInput,
    candidate: DraftCandidate,
    context: RevisionReviewContext,
    report: ModelSemanticReview,
) -> list[str]:
    """Check report coverage and reference integrity, not factual entailment."""
    errors: list[str] = []

    allowed_ids = set(task.requirements.allowed_block_ids)
    reviewed_ids = [block.block_id for block in report.blocks]
    if (
        len(reviewed_ids) != len(allowed_ids)
        or set(reviewed_ids) != allowed_ids
    ):
        errors.append("模型报告遗漏、重复或加入了不允许检查的段落")

    expected_rules = set(range(len(context.brand_rules)))
    reviewed_rules = [rule.rule_index for rule in report.rules]
    if (
        len(reviewed_rules) != len(expected_rules)
        or set(reviewed_rules) != expected_rules
    ):
        errors.append("模型报告遗漏、重复或加入了不存在的品牌规则")

    candidate_blocks = {
        block.block_id: block.text for block in candidate.blocks
    }
    evidence_by_id = {
        item.evidence_id: item for item in context.evidence
    }

    for block in report.blocks:
        if block.block_id not in allowed_ids:
            continue
        text = candidate_blocks[block.block_id]

        for fact in block.facts:
            if fact.claim not in text:
                errors.append(
                    f"段落 {block.block_id} 的事实片段不是候选原文：{fact.claim}"
                )

            if (
                fact.verdict in {"supported", "contradicted"}
                and not fact.references
            ):
                errors.append(f"事实“{fact.claim}”的判断缺少证据引用")

            for reference in fact.references:
                evidence = evidence_by_id.get(reference.evidence_id)
                if evidence is None:
                    errors.append(
                        f"事实“{fact.claim}”引用了不存在的证据："
                        f"{reference.evidence_id}"
                    )
                    continue
                if reference.quote not in evidence.quote:
                    errors.append(
                        f"事实“{fact.claim}”的引用不是证据"
                        f" {reference.evidence_id} 中的连续原文"
                    )

    return errors


def validate_semantics(
    task: RevisionTaskInput,
    candidate: DraftCandidate,
    context: RevisionReviewContext,
    *,
    reviewer: SemanticReviewer,
) -> SemanticValidationResult:
    """Run one gated review and derive status from a verified model report.

    Invalid caller inputs raise schema errors before any model call. Provider
    failures and malformed model reports produce an error result; they must not
    be treated as copy defects or consume content-revision attempts implicitly.
    """
    task = RevisionTaskInput.model_validate(task.model_dump(warnings=False))
    candidate = DraftCandidate.model_validate(candidate.model_dump(warnings=False))
    context = RevisionReviewContext.model_validate(context.model_dump(warnings=False))
    input_hash = semantic_input_hash(task, candidate, context)

    def finish(
        status: str,
        issues: list[str],
        report: ModelSemanticReview | None = None,
    ) -> SemanticValidationResult:
        return SemanticValidationResult(
            status=status,
            candidate_id=candidate.candidate_id,
            input_hash=input_hash,
            review_version=SEMANTIC_REVIEW_VERSION,
            issues=issues,
            model_review=report,
        )

    program_result = validate_draft(task, candidate)
    if not program_result.passed:
        return finish("needs_revision", program_result.issues)

    source_blocks = {block.block_id: block for block in task.draft.blocks}
    candidate_blocks = {block.block_id: block for block in candidate.blocks}
    review_input = {
        "platform": task.draft.platform,
        "goal": task.requirements.goal,
        "brand_rules": [
            {"rule_index": index, "text": text}
            for index, text in enumerate(context.brand_rules)
        ],
        "blocks": [
            {
                "block_id": str(block_id),
                "original_text": source_blocks[block_id].text,
                "candidate_text": candidate_blocks[block_id].text,
            }
            for block_id in task.requirements.allowed_block_ids
        ],
        "evidence": [item.model_dump(mode="json") for item in context.evidence],
    }
    messages = [
        (
            "system",
            SEMANTIC_REVIEW_PROMPT
            + "\nJSON Schema:\n"
            + json.dumps(ModelSemanticReview.model_json_schema(), ensure_ascii=False),
        ),
        ("human", json.dumps(review_input, ensure_ascii=False)),
    ]

    try:
        response = reviewer(messages)
    except Exception as exc:
        logger.exception("Revision semantic model call failed")
        return finish(
            "error",
            [f"语义检查调用失败（{type(exc).__name__}），请检查服务或重试"],
        )

    # Review adapters, including test doubles, must expose a complete DeepSeek
    # response. Reject truncation even when the returned JSON happens to parse.
    metadata = getattr(response, "response_metadata", None)
    if not isinstance(metadata, Mapping):
        return finish("error", ["语义检查响应缺少有效的结束状态"])
    finish_reason = metadata.get("finish_reason")
    if finish_reason != "stop":
        return finish(
            "error",
            ["语义检查响应未正常结束："
             f"{finish_reason or '缺少 finish_reason'}"],
        )

    content = getattr(response, "content", None)
    if not isinstance(content, str) or not content.strip():
        return finish("error", ["语义检查没有返回有效的文本报告"])

    try:
        report = ModelSemanticReview.model_validate_json(content)
    except ValueError:
        logger.exception("Revision semantic report parsing failed")
        return finish("error", ["模型报告不符合约定的 JSON 数据结构"])

    report_errors = _check_semantic_report(task, candidate, context, report)
    if report_errors:
        return finish("error", report_errors, report)

    revision_issues: list[str] = []
    clarification_issues: list[str] = []

    def collect(label: str, assessment: CriterionReview) -> None:
        if assessment.verdict == "unmet":
            revision_issues.append(f"{label}：{assessment.note}")
        elif assessment.verdict == "uncertain":
            clarification_issues.append(f"{label}：{assessment.note}")

    collect("修改目标", report.goal)
    for rule in report.rules:
        collect(f"品牌规则 {rule.rule_index + 1}", rule)

    source_positions = {
        block.block_id: position
        for position, block in enumerate(task.draft.blocks, start=1)
    }
    for block in report.blocks:
        label = f"第 {source_positions[block.block_id]} 段"
        collect(f"{label}的表达与语气", block.tone)
        for fact in block.facts:
            issue = f"{label}“{fact.claim}”：{fact.note}"
            if fact.verdict in {"unsupported", "contradicted"}:
                revision_issues.append(issue)
            elif fact.verdict == "uncertain":
                clarification_issues.append(issue)

    clarification_issues.extend(
        f"待补充信息：{item}" for item in report.uncertainties
    )
    issues = revision_issues + clarification_issues
    if clarification_issues:
        return finish("needs_clarification", issues, report)
    if revision_issues:
        return finish("needs_revision", issues, report)
    return finish("passed", [], report)
