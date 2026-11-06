"""Model invocation capabilities for the targeted-revision workflow."""

from collections.abc import Callable, Mapping
from typing import Any

from app.modules.marketing.schemas.revision import RevisionRepairOutput


RevisionReviser = Callable[[list[tuple[str, str]]], RevisionRepairOutput]


def invoke_semantic_reviewer(messages: list[tuple[str, str]]) -> Any:
    """Execute one review request without importing tools or workflow nodes."""
    # Import lazily so deterministic validation and fake-reviewer tests do not
    # initialize provider clients or require model credentials.
    from app.infra.ai.llm import llm

    reviewer = llm.bind(
        timeout=60.0,
        max_tokens=4096,
        response_format={"type": "json_object"},
    )
    return reviewer.invoke(messages)


def invoke_revision_reviser(messages: list[tuple[str, str]]) -> RevisionRepairOutput:
    """Generate editable blocks; the backend constructs patch metadata."""
    from app.infra.ai.llm import llm

    response = llm.bind(
        timeout=60.0,
        max_tokens=4096,
        response_format={"type": "json_object"},
    ).invoke(messages)
    metadata = getattr(response, "response_metadata", None)
    if not isinstance(metadata, Mapping) or metadata.get("finish_reason") != "stop":
        raise ValueError("改稿模型响应未正常结束")
    content = getattr(response, "content", None)
    if not isinstance(content, str) or not content.strip():
        raise ValueError("改稿模型返回空内容")
    return RevisionRepairOutput.model_validate_json(content)
