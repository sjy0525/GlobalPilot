"""Apply a paragraph patch to a trusted snapshot without publishing a version.

These in-memory operations do not check database freshness or user membership.
The future persistence boundary must recheck both before committing a draft.
"""

from uuid import uuid4

from app.modules.marketing.schemas.revision import (
    DraftBlock,
    DraftCandidate,
    DraftPatch,
    DraftValidationResult,
    RevisionTaskInput,
)
from app.modules.marketing.workflow.revision.validation import validate_draft


def apply_patch_to_draft(
    task: RevisionTaskInput,
    patch: DraftPatch,
) -> tuple[DraftCandidate, DraftValidationResult]:
    """Reject invalid patches; return a candidate even if preservation fails.

    Each patch is based on the supplied original snapshot. Candidate creation
    does not increment the source version or mutate the original task.
    """
    # Rebuild rather than trusting mutable model instances and nested lists.
    task = RevisionTaskInput.model_validate(task.model_dump(warnings=False))
    patch = DraftPatch.model_validate(patch.model_dump(warnings=False))
    source = task.draft

    if patch.draft_id != source.draft_id:
        raise ValueError("补丁对应的文稿不正确")
    if patch.base_version != source.version:
        raise ValueError("补丁基于的文稿版本不一致")

    source_blocks = {block.block_id: block for block in source.blocks}
    allowed_ids = set(task.requirements.allowed_block_ids)
    replacements = {}

    # Validate the entire batch before constructing any candidate.
    for change in patch.patches:
        if change.block_id not in source_blocks:
            raise ValueError(f"段落不存在：{change.block_id}")
        if change.block_id not in allowed_ids:
            raise ValueError(f"不允许修改该段落：{change.block_id}")
        if change.expected_text != source_blocks[change.block_id].text:
            raise ValueError(f"补丁预期原文不一致：{change.block_id}")
        replacements[change.block_id] = change.replacement_text

    blocks = []
    for block in source.blocks:
        text = replacements.get(block.block_id, block.text)
        # Copy unchanged paragraphs too; a candidate must not alias the source.
        blocks.append(DraftBlock(block_id=block.block_id, text=text))

    candidate = DraftCandidate(
        candidate_id=uuid4(),
        draft_id=source.draft_id,
        base_version=source.version,
        blocks=blocks,
    )
    return candidate, validate_draft(task, candidate)
