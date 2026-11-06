"""Workflow adapters for targeted-revision business operations."""

import json
import logging
from uuid import uuid4

from app.modules.marketing.schemas.revision import (
    DraftCandidate,
    RevisionRepairOutput,
    RevisionReviewContext,
    RevisionTaskInput,
    SemanticValidationResult,
)
from app.modules.marketing.workflow.revision.agent import (
    RevisionReviser,
    invoke_revision_reviser,
    invoke_semantic_reviewer,
)
from app.modules.marketing.workflow.revision.prompts import REVISION_REPAIR_PROMPT
from app.modules.marketing.workflow.revision.state import RevisionState
from app.modules.marketing.workflow.revision import tools
from app.modules.marketing.workflow.revision.validation import (
    SemanticReviewer,
    semantic_input_hash,
    validate_semantics,
)

logger = logging.getLogger(__name__)

#取出state中的人物、候选稿、审核资料，用Pydantic检查并转化成数据对象，调用写好的语义化检查校验器，最后生成报告
def validate_candidate_node(
    state: RevisionState,
    *,
    reviewer: SemanticReviewer = invoke_semantic_reviewer,
) -> dict:
    """Validate serialized inputs and replace the previous review report."""
    task = RevisionTaskInput.model_validate(state["task"])
    candidate = DraftCandidate.model_validate(state["candidate"])
    context = RevisionReviewContext.model_validate(state["review_context"])
    result = validate_semantics(task, candidate, context, reviewer=reviewer)
    return {"semantic_validation": result.model_dump(mode="json")}


def _initial_metadata(state: RevisionState, task: RevisionTaskInput) -> dict:
    return {
        "original_requirements": state.get(
            "original_requirements", task.requirements.model_dump(mode="json")
        ),
        "revision_attempts": state.get("revision_attempts", 0),
        "generation_attempts": state.get("generation_attempts", 0),
        "attempt_history": state.get("attempt_history", []),
        "clarification_history": state.get("clarification_history", []),
        "status": "running",
        "stop_reason": None,
        "execution_issues": [],
    }


def read_document_node(state: RevisionState, *, reader: tools.DocumentReader) -> dict:
    task = tools.read_document(reader)
    return {
        **_initial_metadata(state, task),
        "task": task.model_dump(mode="json"),
        "phase": "read_document",
        "attempt_history": state.get("attempt_history", []) + [{
            "kind": "read_document", "draft_id": str(task.draft.draft_id),
            "version": task.draft.version,
        }],
    }


def load_evidence_node(
    state: RevisionState,
    *,
    searcher: tools.EvidenceSearcher,
) -> dict:
    task = RevisionTaskInput.model_validate(state["task"])
    allowed = set(task.requirements.allowed_block_ids)
    query = "\n".join([
        task.requirements.goal,
        *(block.text for block in task.draft.blocks if block.block_id in allowed),
    ])
    context = tools.search_brand_evidence(searcher, query)
    update = {
        "review_context": context.model_dump(mode="json"),
        "phase": "search_brand_evidence",
        "attempt_history": state.get("attempt_history", []) + [{
            "kind": "search_brand_evidence",
            "query": query,
            "context": context.model_dump(mode="json"),
        }],
    }
    if not context.evidence:
        update.update(
            status="needs_clarification",
            stop_reason="missing_evidence",
            execution_issues=["缺少可核查的品牌资料，请补充证据后继续"],
        )
    return update


def initialize_supplied_node(state: RevisionState) -> dict:
    task = RevisionTaskInput.model_validate(state["task"])
    RevisionReviewContext.model_validate(state["review_context"])
    return {**_initial_metadata(state, task), "phase": "initialize"}


def _source_candidate(task: RevisionTaskInput) -> DraftCandidate:
    return DraftCandidate(
        candidate_id=uuid4(),
        draft_id=task.draft.draft_id,
        base_version=task.draft.version,
        blocks=[block.model_copy(deep=True) for block in task.draft.blocks],
    )


def _messages(task, candidate, context, issues, phase):
    source = {block.block_id: block.text for block in task.draft.blocks}
    current = {block.block_id: block.text for block in candidate.blocks}
    payload = {
        "phase": phase,
        "requirements": task.requirements.model_dump(mode="json"),
        "context": context.model_dump(mode="json"),
        "issues": issues,
        "blocks": [
            {"block_id": str(bid), "original_text": source[bid], "candidate_text": current[bid]}
            for bid in task.requirements.allowed_block_ids
        ],
    }
    return [
        (
            "system",
            REVISION_REPAIR_PROMPT + "\nJSON Schema:\n"
            + json.dumps(RevisionRepairOutput.model_json_schema(), ensure_ascii=False),
        ),
        ("human", json.dumps(payload, ensure_ascii=False)),
    ]


def generate_candidate_node(
    state: RevisionState,
    *,
    reviser: RevisionReviser = invoke_revision_reviser,
) -> dict:
    task = RevisionTaskInput.model_validate(state["task"])
    context = RevisionReviewContext.model_validate(state["review_context"])
    current = _source_candidate(task)
    count = state.get("generation_attempts", 0) + 1
    try:
        output = reviser(_messages(task, current, context, [], "initial"))
        output = RevisionRepairOutput.model_validate(output.model_dump(warnings=False))
        if output.clarification_questions:
            return {
                "status": "needs_clarification", "phase": "generate",
                "stop_reason": "clarification_requested", "generation_attempts": count,
                "execution_issues": output.clarification_questions,
                "attempt_history": state.get("attempt_history", []) + [{
                    "kind": "initial", "clarification_questions": output.clarification_questions,
                }],
            }
        candidate, program = tools.apply_patch_to_draft(task, output)
    except Exception as exc:
        logger.exception("Initial targeted revision failed")
        return {
            "status": "error", "phase": "generate", "stop_reason": "generation_error",
            "generation_attempts": count,
            "execution_issues": [f"首次改稿未完成（{type(exc).__name__}）"],
        }
    return {
        "candidate": candidate.model_dump(mode="json"),
        "semantic_validation": None,
        "generation_attempts": count,
        "status": "running", "phase": "generate", "stop_reason": None,
        "execution_issues": [],
        "attempt_history": state.get("attempt_history", []) + [{
            "kind": "initial", "candidate": candidate.model_dump(mode="json"),
            "program_validation": program.model_dump(mode="json"),
        }],
    }


def check_revision_node(state: RevisionState, *, reviewer: SemanticReviewer) -> dict:
    update = validate_candidate_node(state, reviewer=reviewer)
    report = update["semantic_validation"]
    status = {
        "passed": "awaiting_review", "needs_revision": "running",
        "needs_clarification": "needs_clarification", "error": "error",
    }[report["status"]]
    return {
        **update, "status": status, "phase": "validate",
        "stop_reason": "review_error" if status == "error" else None,
        "execution_issues": report["issues"] if status == "error" else [],
        "attempt_history": state.get("attempt_history", []) + [{
            "kind": "validate", "report": report,
        }],
    }


def revise_candidate_node(
    state: RevisionState,
    *,
    reviser: RevisionReviser = invoke_revision_reviser,
    max_revisions: int = 3,
) -> dict:
    count = state.get("revision_attempts", 0)
    if count >= max_revisions:
        return exhaust_budget_node(state)
    task = RevisionTaskInput.model_validate(state["task"])
    candidate = DraftCandidate.model_validate(state["candidate"])
    context = RevisionReviewContext.model_validate(state["review_context"])
    report = SemanticValidationResult.model_validate(state["semantic_validation"])
    if (
        report.status != "needs_revision"
        or report.candidate_id != candidate.candidate_id
        or report.input_hash != semantic_input_hash(task, candidate, context)
    ):
        return {
            "status": "error", "stop_reason": "stale_report",
            "execution_issues": ["检查报告已过期，请重新检查"],
        }
    source = {block.block_id: block for block in task.draft.blocks}
    current = {block.block_id: block for block in candidate.blocks}
    allowed = set(task.requirements.allowed_block_ids)
    if (
        candidate.draft_id != task.draft.draft_id
        or candidate.base_version != task.draft.version
        or [block.block_id for block in candidate.blocks] != list(source)
        or any(current[bid].text != block.text for bid, block in source.items() if bid not in allowed)
    ):
        return {
            "status": "error", "stop_reason": "source_conflict",
            "execution_issues": ["候选来源、版本或只读范围异常，请重新读取原稿"],
        }
    attempt = count + 1
    generations = state.get("generation_attempts", 0) + 1
    try:
        output = reviser(_messages(task, candidate, context, report.issues, "repair"))
        output = RevisionRepairOutput.model_validate(output.model_dump(warnings=False))
        if output.clarification_questions:
            return {
                "status": "needs_clarification", "phase": "repair",
                "stop_reason": "clarification_requested",
                "revision_attempts": attempt, "generation_attempts": generations,
                "execution_issues": output.clarification_questions,
                "attempt_history": state.get("attempt_history", []) + [{
                    "kind": "repair", "attempt": attempt,
                    "clarification_questions": output.clarification_questions,
                }],
            }
        revised, program = tools.apply_patch_to_draft(task, output)
    except Exception as exc:
        logger.exception("Targeted revision repair failed")
        return {
            "status": "error", "phase": "repair", "stop_reason": "repair_error",
            "revision_attempts": attempt, "generation_attempts": generations,
            "execution_issues": [f"修正未完成（{type(exc).__name__}）"],
            "attempt_history": state.get("attempt_history", []) + [{
                "kind": "repair", "attempt": attempt, "error": type(exc).__name__,
            }],
        }
    if revised.blocks == candidate.blocks:
        return {
            "status": "error", "phase": "repair", "stop_reason": "no_change",
            "revision_attempts": attempt, "generation_attempts": generations,
            "execution_issues": ["修正结果没有变化，已停止重复调用"],
            "attempt_history": state.get("attempt_history", []) + [{
                "kind": "repair", "attempt": attempt, "stop_reason": "no_change",
                "candidate": revised.model_dump(mode="json"),
                "program_validation": program.model_dump(mode="json"),
            }],
        }
    return {
        "candidate": revised.model_dump(mode="json"), "semantic_validation": None,
        "revision_attempts": attempt, "generation_attempts": generations,
        "status": "running", "phase": "repair", "stop_reason": None, "execution_issues": [],
        "attempt_history": state.get("attempt_history", []) + [{
            "kind": "repair", "attempt": attempt,
            "candidate": revised.model_dump(mode="json"),
            "program_validation": program.model_dump(mode="json"),
        }],
    }


def exhaust_budget_node(state: RevisionState) -> dict:
    return {"status": "budget_exhausted", "stop_reason": "budget_exhausted", "phase": "finished"}


def clarification_node(state: RevisionState) -> dict:
    task = RevisionTaskInput.model_validate(state["task"])
    report = state.get("semantic_validation") or {}
    answer = tools.request_clarification({
        "issues": list(dict.fromkeys(state.get("execution_issues", []) + report.get("issues", []))),
        "revision_attempts": state.get("revision_attempts", 0),
    })
    updated_task = RevisionTaskInput.model_validate(answer["task"])
    context = RevisionReviewContext.model_validate(answer["review_context"])
    if (
        updated_task.draft != task.draft
        or set(updated_task.requirements.allowed_block_ids) != set(task.requirements.allowed_block_ids)
    ):
        raise ValueError("补充资料不能更换原稿或修改允许范围")
    history = state.get("clarification_history", []) + [{
        "feedback": str(answer.get("feedback") or ""),
        "requirements": updated_task.requirements.model_dump(mode="json"),
        "context": context.model_dump(mode="json"),
    }]
    return {
        "task": updated_task.model_dump(mode="json"),
        "review_context": context.model_dump(mode="json"),
        "semantic_validation": None,
        "clarification_history": history,
        "status": "running" if context.evidence else "needs_clarification",
        "stop_reason": None if context.evidence else "missing_evidence",
        "phase": "clarification",
        "execution_issues": [] if context.evidence else ["仍缺少可核查的品牌证据"],
    }


def human_review_node(state: RevisionState) -> dict:
    task = RevisionTaskInput.model_validate(state["task"])
    candidate = DraftCandidate.model_validate(state["candidate"])
    context = RevisionReviewContext.model_validate(state["review_context"])
    report = SemanticValidationResult.model_validate(state["semantic_validation"])
    answer = tools.submit_for_review(task, candidate, context, report)
    decision = answer.get("decision", answer.get("action"))
    feedback = str(answer.get("feedback") or "")
    if decision == "rejected":
        return {
            "status": "rejected", "stop_reason": "rejected", "phase": "finished",
            "review_decision": decision, "review_feedback": feedback,
            "attempt_history": state.get("attempt_history", []) + [{
                "kind": "human_review", "decision": decision, "feedback": feedback,
            }],
        }
    if decision != "approved":
        raise ValueError("审核决定只能是 approved 或 rejected")
    try:
        final_candidate = DraftCandidate.model_validate(answer.get("candidate") or candidate.model_dump(mode="json"))
        final_report = SemanticValidationResult.model_validate(answer.get("semantic_validation") or report.model_dump(mode="json"))
        tools.require_reviewable(task, final_candidate, context, final_report)
    except ValueError:
        return {
            "status": "error", "stop_reason": "approval_validation_error",
            "execution_issues": ["人工最终稿未通过范围、版本或检查报告校验"],
        }
    return {
        "candidate": final_candidate.model_dump(mode="json"),
        "semantic_validation": final_report.model_dump(mode="json"),
        "status": "accepted", "stop_reason": "accepted", "phase": "finished",
        "review_decision": decision, "review_feedback": feedback,
        "attempt_history": state.get("attempt_history", []) + [{
            "kind": "human_review", "decision": decision, "feedback": feedback,
            "candidate": final_candidate.model_dump(mode="json"),
            "report": final_report.model_dump(mode="json"),
        }],
    }
