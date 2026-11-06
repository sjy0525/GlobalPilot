"""Task-scoped business tools backed by authorized service read capabilities."""

from collections.abc import Callable, Mapping
from typing import Any

from langgraph.types import interrupt

from app.modules.marketing.draft_service import apply_patch_to_draft as apply_patch
from app.modules.marketing.schemas.revision import (
    BlockPatch,
    DraftCandidate,
    DraftPatch,
    DraftValidationResult,
    RevisionRepairOutput,
    RevisionReviewContext,
    RevisionTaskInput,
    SemanticValidationResult,
)
from app.modules.marketing.workflow.revision.validation import (
    SemanticReviewer,
    semantic_input_hash,
    validate_draft as validate_program,
    validate_semantics,
)

DocumentReader = Callable[[], RevisionTaskInput]
EvidenceSearcher = Callable[[str], RevisionReviewContext]


def read_document(reader: DocumentReader) -> RevisionTaskInput:
    """The service-bound reader checks actor, team, source and frozen version."""
    value = reader()
    return RevisionTaskInput.model_validate(value.model_dump(warnings=False))


def search_brand_evidence(
    searcher: EvidenceSearcher,
    query: str,
) -> RevisionReviewContext:
    if not isinstance(query, str) or not query.strip():
        raise ValueError("证据查询不能为空")
    value = searcher(query)
    return RevisionReviewContext.model_validate(value.model_dump(warnings=False))


def apply_patch_to_draft(
    task: RevisionTaskInput,
    output: RevisionRepairOutput,
) -> tuple[DraftCandidate, DraftValidationResult]:
    """Model supplies final editable text; backend supplies immutable metadata."""
    task = RevisionTaskInput.model_validate(task.model_dump(warnings=False))
    output = RevisionRepairOutput.model_validate(output.model_dump(warnings=False))
    ids = [block.block_id for block in output.blocks]
    allowed = set(task.requirements.allowed_block_ids)
    if len(ids) != len(allowed) or set(ids) != allowed:
        raise ValueError("模型必须完整返回全部可改段落，不能重复或修改范围外段落")
    source = {block.block_id: block for block in task.draft.blocks}
    replacements = {block.block_id: block.text for block in output.blocks}
    patch = DraftPatch(
        draft_id=task.draft.draft_id,
        base_version=task.draft.version,
        patches=[
            BlockPatch(
                block_id=block_id,
                expected_text=source[block_id].text,
                replacement_text=replacements[block_id],
            )
            for block_id in task.requirements.allowed_block_ids
        ],
    )
    return apply_patch(task, patch)


def validate_draft(
    task: RevisionTaskInput,
    candidate: DraftCandidate,
    context: RevisionReviewContext,
    *,
    reviewer: SemanticReviewer,
) -> SemanticValidationResult:
    return validate_semantics(task, candidate, context, reviewer=reviewer)


def request_clarification(payload: dict[str, Any]) -> dict:
    answer = interrupt({"kind": "clarification", **payload})
    if not isinstance(answer, Mapping):
        raise ValueError("补充信息必须是对象")
    return dict(answer)


def require_reviewable(
    task: RevisionTaskInput,
    candidate: DraftCandidate,
    context: RevisionReviewContext,
    report: SemanticValidationResult,
) -> None:
    """Submission guards are program decisions, never model completion text."""
    if not validate_program(task, candidate).passed:
        raise ValueError("候选未通过程序检查")
    if (
        report.status != "passed"
        or report.candidate_id != candidate.candidate_id
        or report.input_hash != semantic_input_hash(task, candidate, context)
    ):
        raise ValueError("语义检查未通过或检查报告已过期")


def submit_for_review(
    task: RevisionTaskInput,
    candidate: DraftCandidate,
    context: RevisionReviewContext,
    report: SemanticValidationResult,
) -> dict:
    require_reviewable(task, candidate, context, report)
    answer = interrupt({
        "kind": "review",
        "candidate": candidate.model_dump(mode="json"),
        "semantic_validation": report.model_dump(mode="json"),
    })
    if not isinstance(answer, Mapping):
        raise ValueError("审核决定必须是对象")
    return dict(answer)
