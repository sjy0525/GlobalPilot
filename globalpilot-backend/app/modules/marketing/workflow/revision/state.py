"""Serializable business state for validation and durable targeted revisions."""

from typing import NotRequired, TypedDict


class RevisionState(TypedDict):
    task: NotRequired[dict]  # 原稿和修改要求，由授权读取节点加载
    candidate: NotRequired[dict | None]  # 当前候选稿，首次改稿前可以不存在
    review_context: NotRequired[dict]  # 品牌规则和证据
    semantic_validation: NotRequired[dict | None]  # 检查报告，新候选须重新检查
    original_requirements: NotRequired[dict]
    revision_attempts: NotRequired[int]
    generation_attempts: NotRequired[int]
    status: NotRequired[str]
    phase: NotRequired[str]
    stop_reason: NotRequired[str | None]
    execution_issues: NotRequired[list[str]]
    attempt_history: NotRequired[list[dict]]
    clarification_history: NotRequired[list[dict]]
    review_decision: NotRequired[str | None]
    review_feedback: NotRequired[str | None]
