"""Offline behavior checks for local draft patches and deterministic validation."""

from copy import deepcopy
import json
import unittest
from uuid import UUID

from pydantic import ValidationError

from app.modules.marketing.draft_service import apply_patch_to_draft
from app.modules.marketing.schemas.revision import (
    BlockPatch,
    DraftCandidate,
    DraftPatch,
    DraftValidationResult,
    RevisionTaskInput,
)
from app.modules.marketing.workflow.revision.validation import validate_draft
from tests.test_marketing_revision_schemas import (
    FIRST_BLOCK_ID,
    OUTSIDE_BLOCK_ID,
    SECOND_BLOCK_ID,
    revision_input,
)


REPLACEMENT_TEXT = "  65W GaN 充电器，双 USB-C。  "


def task_input(*, allow_all: bool = False, must_keep: list[str] | None = None):
    payload = revision_input()
    if allow_all:
        payload["requirements"]["allowed_block_ids"] = [FIRST_BLOCK_ID, SECOND_BLOCK_ID]
    if must_keep is not None:
        payload["requirements"]["must_keep"] = must_keep
    return RevisionTaskInput.model_validate(payload)


def patch_input(task: RevisionTaskInput) -> dict:
    return {
        "draft_id": str(task.draft.draft_id),
        "base_version": task.draft.version,
        "patches": [{
            "block_id": FIRST_BLOCK_ID,
            "expected_text": task.draft.blocks[0].text,
            "replacement_text": REPLACEMENT_TEXT,
        }],
    }


def candidate_input(task: RevisionTaskInput) -> dict:
    return {
        "candidate_id": "30000000-0000-4000-8000-000000000001",
        "draft_id": str(task.draft.draft_id),
        "base_version": task.draft.version,
        "blocks": task.draft.model_dump(mode="json")["blocks"],
    }


class RevisionPatchSchemaTests(unittest.TestCase):
    def test_patch_json_round_trip_preserves_paragraph_text(self):
        payload = patch_input(task_input())

        patch = DraftPatch.model_validate_json(json.dumps(payload, ensure_ascii=False))

        self.assertEqual(patch.draft_id, UUID(payload["draft_id"]))
        self.assertEqual(patch.patches[0].block_id, UUID(FIRST_BLOCK_ID))
        self.assertEqual(patch.patches[0].expected_text, payload["patches"][0]["expected_text"])
        self.assertEqual(patch.patches[0].replacement_text, REPLACEMENT_TEXT)
        self.assertEqual(DraftPatch.model_validate_json(patch.model_dump_json()), patch)

    def test_candidate_json_round_trip_preserves_ids_order_and_text(self):
        payload = candidate_input(task_input())

        candidate = DraftCandidate.model_validate_json(json.dumps(payload, ensure_ascii=False))

        self.assertEqual(candidate.candidate_id, UUID(payload["candidate_id"]))
        self.assertEqual(candidate.draft_id, UUID(payload["draft_id"]))
        self.assertEqual(candidate.base_version, payload["base_version"])
        self.assertEqual(
            [(str(block.block_id), block.text) for block in candidate.blocks],
            [(block["block_id"], block["text"]) for block in payload["blocks"]],
        )
        self.assertEqual(DraftCandidate.model_validate_json(candidate.model_dump_json()), candidate)

    def test_patch_requires_nonempty_unique_target_blocks(self):
        payload = patch_input(task_input())
        for patches in ([], payload["patches"] * 2):
            with self.subTest(patches=patches):
                invalid = deepcopy(payload)
                invalid["patches"] = patches
                with self.assertRaises(ValidationError):
                    DraftPatch.model_validate(invalid)

    def test_expected_and_replacement_text_reject_blank_values(self):
        payload = patch_input(task_input())["patches"][0]
        for field in ("expected_text", "replacement_text"):
            for text in ("", " ", "\t\n"):
                with self.subTest(field=field, text=text):
                    invalid = dict(payload, **{field: text})
                    with self.assertRaises(ValidationError) as raised:
                        BlockPatch.model_validate(invalid)
                    self.assertTrue(any(error["loc"] == (field,) for error in raised.exception.errors()))

    def test_candidate_requires_nonempty_unique_blocks(self):
        payload = candidate_input(task_input())
        for blocks in ([], [payload["blocks"][0], deepcopy(payload["blocks"][0])]):
            with self.subTest(blocks=blocks):
                invalid = deepcopy(payload)
                invalid["blocks"] = blocks
                with self.assertRaises(ValidationError):
                    DraftCandidate.model_validate(invalid)

    def test_base_versions_require_strict_positive_integers(self):
        task = task_input()
        for model, payload in ((DraftPatch, patch_input(task)), (DraftCandidate, candidate_input(task))):
            for version in (True, False, "1", 1.0, None, 0, -1):
                with self.subTest(model=model.__name__, version=version):
                    invalid = deepcopy(payload)
                    invalid["base_version"] = version
                    with self.assertRaises(ValidationError):
                        model.model_validate(invalid)

    def test_extra_fields_are_rejected_on_new_models(self):
        task = task_input()
        examples = (
            (BlockPatch, patch_input(task)["patches"][0]),
            (DraftPatch, patch_input(task)),
            (DraftCandidate, candidate_input(task)),
            (DraftValidationResult, {"passed": True, "issues": []}),
        )
        for model, payload in examples:
            with self.subTest(model=model.__name__):
                with self.assertRaises(ValidationError) as raised:
                    model.model_validate(dict(payload, unexpected_field="untrusted"))
                self.assertTrue(any(
                    error["loc"] == ("unexpected_field",) and error["type"] == "extra_forbidden"
                    for error in raised.exception.errors()
                ))

    def test_validation_result_requires_strict_boolean_and_consistent_issues(self):
        for payload in ({"passed": True, "issues": []}, {"passed": False, "issues": ["缺少 65W"]}):
            with self.subTest(valid=payload):
                result = DraftValidationResult.model_validate(payload)
                self.assertEqual(DraftValidationResult.model_validate_json(result.model_dump_json()), result)

        invalid_results = [
            {"passed": True, "issues": ["存在问题"]},
            {"passed": False, "issues": []},
            *({"passed": value, "issues": []} for value in (0, 1, "true", None)),
        ]
        for payload in invalid_results:
            with self.subTest(invalid=payload):
                with self.assertRaises(ValidationError):
                    DraftValidationResult.model_validate(payload)


class ApplyDraftPatchTests(unittest.TestCase):
    def test_local_patch_preserves_source_inputs_order_ids_and_base_version(self):
        task = task_input()
        patch = DraftPatch.model_validate(patch_input(task))
        task_before, patch_before = task.model_dump(), patch.model_dump()

        candidate, result = apply_patch_to_draft(task, patch)

        self.assertTrue(result.passed)
        self.assertEqual(result.issues, [])
        self.assertIsInstance(candidate.candidate_id, UUID)
        self.assertEqual(candidate.draft_id, task.draft.draft_id)
        self.assertEqual(candidate.base_version, task.draft.version)
        self.assertEqual([block.block_id for block in candidate.blocks], [block.block_id for block in task.draft.blocks])
        self.assertEqual(candidate.blocks[0].text, REPLACEMENT_TEXT)
        self.assertEqual(candidate.blocks[1].text, task.draft.blocks[1].text)
        self.assertEqual(task.model_dump(), task_before)
        self.assertEqual(patch.model_dump(), patch_before)

    def test_patch_list_order_does_not_reorder_source_paragraphs(self):
        task = task_input(allow_all=True)
        payload = patch_input(task)
        payload["patches"].insert(0, {
            "block_id": SECOND_BLOCK_ID,
            "expected_text": task.draft.blocks[1].text,
            "replacement_text": "支持 30 天退货。欢迎咨询。",
        })

        candidate, result = apply_patch_to_draft(task, DraftPatch.model_validate(payload))

        self.assertTrue(result.passed)
        self.assertEqual([block.block_id for block in candidate.blocks], [UUID(FIRST_BLOCK_ID), UUID(SECOND_BLOCK_ID)])
        self.assertEqual(candidate.blocks[0].text, REPLACEMENT_TEXT)
        self.assertEqual(candidate.blocks[1].text, "支持 30 天退货。欢迎咨询。")

    def test_wrong_draft_or_version_is_rejected_without_changing_inputs(self):
        for field, value in (("draft_id", OUTSIDE_BLOCK_ID), ("base_version", 2)):
            with self.subTest(field=field):
                task = task_input()
                payload = patch_input(task)
                payload[field] = value
                patch = DraftPatch.model_validate(payload)
                task_before, patch_before = task.model_dump(), patch.model_dump()
                with self.assertRaises(ValueError):
                    apply_patch_to_draft(task, patch)
                self.assertEqual(task.model_dump(), task_before)
                self.assertEqual(patch.model_dump(), patch_before)

    def test_unknown_or_read_only_block_is_rejected(self):
        for block_id in (OUTSIDE_BLOCK_ID, SECOND_BLOCK_ID):
            with self.subTest(block_id=block_id):
                task = task_input()
                payload = patch_input(task)
                payload["patches"][0]["block_id"] = block_id
                if block_id == SECOND_BLOCK_ID:
                    payload["patches"][0]["expected_text"] = task.draft.blocks[1].text
                with self.assertRaises(ValueError):
                    apply_patch_to_draft(task, DraftPatch.model_validate(payload))

    def test_expected_text_requires_exact_match_including_whitespace(self):
        for expected_text in ("另一份旧稿", task_input().draft.blocks[0].text.strip()):
            with self.subTest(expected_text=expected_text):
                task = task_input()
                payload = patch_input(task)
                payload["patches"][0]["expected_text"] = expected_text
                with self.assertRaises(ValueError):
                    apply_patch_to_draft(task, DraftPatch.model_validate(payload))

    def test_invalid_later_patch_does_not_partially_mutate_source(self):
        task = task_input(allow_all=True)
        payload = patch_input(task)
        payload["patches"].append({
            "block_id": SECOND_BLOCK_ID,
            "expected_text": "不是第二段原文",
            "replacement_text": "支持 30 天退货。",
        })
        patch = DraftPatch.model_validate(payload)
        task_before, patch_before = task.model_dump(), patch.model_dump()

        with self.assertRaises(ValueError):
            apply_patch_to_draft(task, patch)

        self.assertEqual(task.model_dump(), task_before)
        self.assertEqual(patch.model_dump(), patch_before)

    def test_candidate_paragraphs_do_not_alias_source_objects(self):
        task = task_input()
        patch = DraftPatch.model_validate(patch_input(task))
        task_before, patch_before = task.model_dump(), patch.model_dump()

        candidate, _ = apply_patch_to_draft(task, patch)

        self.assertIsNot(candidate.blocks, task.draft.blocks)
        for candidate_block, source_block in zip(candidate.blocks, task.draft.blocks):
            self.assertIsNot(candidate_block, source_block)
        candidate.blocks[0].text = "后续修改候选第一段"
        candidate.blocks[1].text = "后续修改候选只读段"
        candidate.blocks.reverse()
        self.assertEqual(task.model_dump(), task_before)
        self.assertEqual(patch.model_dump(), patch_before)

    def test_missing_preserved_text_returns_candidate_and_specific_failure(self):
        task = task_input()
        payload = patch_input(task)
        payload["patches"][0]["replacement_text"] = "双 USB-C 充电器。"

        candidate, result = apply_patch_to_draft(task, DraftPatch.model_validate(payload))

        self.assertFalse(result.passed)
        self.assertTrue(any("65W" in issue for issue in result.issues))
        self.assertEqual(candidate.blocks[0].text, "双 USB-C 充电器。")
        self.assertEqual(candidate.base_version, task.draft.version)
        self.assertIn("65W", task.draft.blocks[0].text)

    def test_patch_mutated_after_construction_is_revalidated(self):
        for mutation in ("duplicate", "empty", "blank_replacement", "bad_version"):
            with self.subTest(mutation=mutation):
                task = task_input()
                patch = DraftPatch.model_validate(patch_input(task))
                if mutation == "duplicate":
                    patch.patches.append(patch.patches[0])
                elif mutation == "empty":
                    patch.patches.clear()
                elif mutation == "blank_replacement":
                    patch.patches[0].replacement_text = " \n"
                else:
                    patch.base_version = 0
                task_before = task.model_dump()
                with self.assertRaises(ValueError):
                    apply_patch_to_draft(task, patch)
                self.assertEqual(task.model_dump(), task_before)

    def test_task_mutated_after_construction_is_revalidated(self):
        task = task_input()
        patch = DraftPatch.model_validate(patch_input(task))
        candidate = DraftCandidate.model_validate(candidate_input(task))
        task.requirements.allowed_block_ids.append(UUID(OUTSIDE_BLOCK_ID))

        with self.assertRaises(ValueError):
            apply_patch_to_draft(task, patch)
        with self.assertRaises(ValueError):
            validate_draft(task, candidate)


class ValidateDraftCandidateTests(unittest.TestCase):
    def test_valid_candidate_passes_without_mutating_inputs(self):
        task = task_input()
        candidate = DraftCandidate.model_validate(candidate_input(task))
        task_before, candidate_before = task.model_dump(), candidate.model_dump()

        result = validate_draft(task, candidate)

        self.assertTrue(result.passed)
        self.assertEqual(result.issues, [])
        self.assertEqual(task.model_dump(), task_before)
        self.assertEqual(candidate.model_dump(), candidate_before)

    def test_candidate_for_wrong_draft_or_base_version_fails(self):
        task = task_input(must_keep=[])
        for field, value in (("draft_id", OUTSIDE_BLOCK_ID), ("base_version", 2)):
            with self.subTest(field=field):
                payload = candidate_input(task)
                payload[field] = value
                result = validate_draft(task, DraftCandidate.model_validate(payload))
                self.assertFalse(result.passed)
                self.assertTrue(result.issues)

    def test_deleted_added_replaced_or_reordered_block_ids_fail(self):
        task = task_input(allow_all=True, must_keep=[])
        for change in ("deleted", "added", "replaced", "reordered"):
            with self.subTest(change=change):
                payload = candidate_input(task)
                if change == "deleted":
                    payload["blocks"].pop()
                elif change == "added":
                    payload["blocks"].append({"block_id": OUTSIDE_BLOCK_ID, "text": "新增段落"})
                elif change == "replaced":
                    payload["blocks"][0]["block_id"] = OUTSIDE_BLOCK_ID
                else:
                    payload["blocks"].reverse()
                result = validate_draft(task, DraftCandidate.model_validate(payload))
                self.assertFalse(result.passed)
                self.assertTrue(result.issues)

    def test_read_only_paragraph_must_remain_exactly_unchanged(self):
        task = task_input(must_keep=[])
        for replacement in ("被偷偷修改的第二段", task.draft.blocks[1].text.strip()):
            with self.subTest(replacement=replacement):
                payload = candidate_input(task)
                payload["blocks"][1]["text"] = replacement
                result = validate_draft(task, DraftCandidate.model_validate(payload))
                self.assertFalse(result.passed)
                self.assertTrue(result.issues)

    def test_preserved_fragment_must_remain_exact_inside_one_paragraph(self):
        task = task_input(allow_all=True, must_keep=["30 天退货"])
        for texts in (("其他正文", "支持 30天退货。"), ("支持 30 天", "退货。")):
            with self.subTest(texts=texts):
                payload = candidate_input(task)
                for block, text in zip(payload["blocks"], texts):
                    block["text"] = text
                result = validate_draft(task, DraftCandidate.model_validate(payload))
                self.assertFalse(result.passed)
                self.assertTrue(any("30 天退货" in issue for issue in result.issues))

    def test_candidate_mutated_after_construction_is_revalidated(self):
        task = task_input()
        for mutation in ("duplicate", "empty", "blank_text", "bad_version"):
            with self.subTest(mutation=mutation):
                candidate = DraftCandidate.model_validate(candidate_input(task))
                if mutation == "duplicate":
                    candidate.blocks.append(candidate.blocks[0])
                elif mutation == "empty":
                    candidate.blocks.clear()
                elif mutation == "blank_text":
                    candidate.blocks[0].text = " \n"
                else:
                    candidate.base_version = 0
                with self.assertRaises(ValueError):
                    validate_draft(task, candidate)


if __name__ == "__main__":
    unittest.main()
