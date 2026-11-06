"""Offline checks for explicit paragraph length limits and counting rules."""

import json
import unittest
from uuid import UUID

from pydantic import ValidationError

from app.modules.marketing.draft_service import apply_patch_to_draft
from app.modules.marketing.schemas.revision import (
    BlockLengthLimit,
    DraftCandidate,
    DraftPatch,
    RevisionRequirements,
    RevisionTaskInput,
)
from app.modules.marketing.workflow.revision.validation import count_text, validate_draft
from tests.test_marketing_revision_schemas import (
    FIRST_BLOCK_ID,
    OUTSIDE_BLOCK_ID,
    SECOND_BLOCK_ID,
    revision_input,
)


def limit_input(*, unit: str = "unicode_chars", maximum: int = 10) -> dict:
    return {"block_id": FIRST_BLOCK_ID, "unit": unit, "maximum": maximum}


def limited_task_input(*, unit: str = "unicode_chars", maximum: int = 10) -> dict:
    payload = revision_input()
    payload["requirements"]["must_keep"] = []
    payload["requirements"]["length_limits"] = [limit_input(unit=unit, maximum=maximum)]
    return payload


def candidate_with_text(task: RevisionTaskInput, text: str) -> DraftCandidate:
    blocks = task.draft.model_dump(mode="json")["blocks"]
    blocks[0]["text"] = text
    return DraftCandidate.model_validate({
        "candidate_id": "30000000-0000-4000-8000-000000000001",
        "draft_id": task.draft.draft_id,
        "base_version": task.draft.version,
        "blocks": blocks,
    })


def patch_with_text(task: RevisionTaskInput, text: str) -> DraftPatch:
    return DraftPatch.model_validate({
        "draft_id": task.draft.draft_id,
        "base_version": task.draft.version,
        "patches": [{
            "block_id": FIRST_BLOCK_ID,
            "expected_text": task.draft.blocks[0].text,
            "replacement_text": text,
        }],
    })


class RevisionLengthSchemaTests(unittest.TestCase):
    def test_absent_limits_default_to_independent_empty_lists(self):
        values = revision_input()["requirements"]
        first = RevisionRequirements.model_validate(values)
        second = RevisionRequirements.model_validate(values)

        self.assertEqual(first.length_limits, [])
        first.length_limits.append(BlockLengthLimit.model_validate(limit_input()))
        self.assertEqual(second.length_limits, [])

    def test_limit_json_round_trip_preserves_ids_units_and_order(self):
        payload = limited_task_input()
        payload["requirements"]["length_limits"].append(limit_input(unit="whitespace_words", maximum=3))

        task = RevisionTaskInput.model_validate_json(json.dumps(payload, ensure_ascii=False))

        self.assertEqual(
            [(limit.block_id, limit.unit, limit.maximum) for limit in task.requirements.length_limits],
            [(UUID(FIRST_BLOCK_ID), "unicode_chars", 10), (UUID(FIRST_BLOCK_ID), "whitespace_words", 3)],
        )
        self.assertEqual(RevisionTaskInput.model_validate_json(task.model_dump_json()), task)

    def test_maximum_requires_a_strict_positive_integer(self):
        for value in (True, False, "1", 1.0, None, 0, -1):
            with self.subTest(value=value):
                payload = limit_input()
                payload["maximum"] = value
                with self.assertRaises(ValidationError) as raised:
                    BlockLengthLimit.model_validate(payload)
                self.assertTrue(any(error["loc"] == ("maximum",) for error in raised.exception.errors()))

    def test_unknown_units_and_invalid_uuids_are_rejected(self):
        for field, values in (("unit", ("words", "tokens", "UNICODE_CHARS", "", None)), ("block_id", ("invalid", None))):
            for value in values:
                with self.subTest(field=field, value=value):
                    payload = limit_input()
                    payload[field] = value
                    with self.assertRaises(ValidationError) as raised:
                        BlockLengthLimit.model_validate(payload)
                    self.assertTrue(any(error["loc"] == (field,) for error in raised.exception.errors()))

    def test_limit_rejects_missing_and_extra_fields(self):
        for field in ("block_id", "unit", "maximum"):
            with self.subTest(missing=field):
                payload = limit_input()
                del payload[field]
                with self.assertRaises(ValidationError) as raised:
                    BlockLengthLimit.model_validate(payload)
                self.assertTrue(any(
                    error["loc"] == (field,) and error["type"] == "missing"
                    for error in raised.exception.errors()
                ))
        with self.assertRaises(ValidationError) as raised:
            BlockLengthLimit.model_validate(dict(limit_input(), unexpected="untrusted"))
        self.assertTrue(any(
            error["loc"] == ("unexpected",) and error["type"] == "extra_forbidden"
            for error in raised.exception.errors()
        ))

    def test_duplicate_block_and_unit_is_rejected_even_with_different_maximum(self):
        for maximum in (10, 20):
            with self.subTest(maximum=maximum):
                payload = limited_task_input()["requirements"]
                payload["length_limits"].append(limit_input(maximum=maximum))
                with self.assertRaises(ValidationError):
                    RevisionRequirements.model_validate(payload)

    def test_limits_cannot_target_unknown_or_read_only_blocks(self):
        for block_id in (OUTSIDE_BLOCK_ID, SECOND_BLOCK_ID):
            with self.subTest(block_id=block_id):
                payload = limited_task_input()
                payload["requirements"]["length_limits"][0]["block_id"] = block_id
                with self.assertRaises(ValidationError):
                    RevisionTaskInput.model_validate(payload)

    def test_over_limit_original_is_allowed_as_a_task_to_be_repaired(self):
        task = RevisionTaskInput.model_validate(limited_task_input(maximum=1))

        self.assertGreater(len(task.draft.blocks[0].text), 1)
        self.assertEqual(task.requirements.length_limits[0].maximum, 1)


class TextCountingTests(unittest.TestCase):
    def test_unicode_count_includes_whitespace_punctuation_and_newlines(self):
        for text, expected in (("", 0), (" A，\n", 4), ("中文。", 3), ("a\r\nb", 4)):
            with self.subTest(text=text):
                self.assertEqual(count_text(text, "unicode_chars"), expected)

    def test_unicode_count_uses_code_points_without_grapheme_normalization(self):
        for text, expected in (("👩‍💻", 3), ("e\u0301", 2), ("é", 1), (" A，\n👩‍💻e\u0301", 9)):
            with self.subTest(text=text):
                self.assertEqual(count_text(text, "unicode_chars"), expected)

    def test_words_are_whitespace_separated_items_without_language_tokenization(self):
        cases = (
            ("", 0),
            (" \t\n", 0),
            ("  don't stop co-operate  ", 3),
            ("one\ttwo\nthree\u00a0four", 4),
            ("中文没有空格", 1),
            ("中文 一段", 2),
            ("hello,world!", 1),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(count_text(text, "whitespace_words"), expected)

    def test_unknown_counting_unit_is_explicitly_rejected(self):
        for unit in ("tokens", "words", "UNICODE_CHARS"):
            with self.subTest(unit=unit):
                with self.assertRaises(ValueError):
                    count_text("some text", unit)


class RevisionLengthValidationTests(unittest.TestCase):
    def test_exact_limit_passes_and_one_over_limit_fails(self):
        cases = (
            ("unicode_chars", 5, "abcde", "abcdef"),
            ("whitespace_words", 2, "one two", "one two three"),
        )
        for unit, maximum, exact, over in cases:
            with self.subTest(unit=unit):
                task = RevisionTaskInput.model_validate(limited_task_input(unit=unit, maximum=maximum))
                self.assertTrue(validate_draft(task, candidate_with_text(task, exact)).passed)
                result = validate_draft(task, candidate_with_text(task, over))
                self.assertFalse(result.passed)
                self.assertTrue(result.issues)

    def test_over_limit_patch_returns_candidate_and_actionable_issue_without_mutating_inputs(self):
        for unit, maximum, replacement, actual in (("unicode_chars", 4, "12345", 5), ("whitespace_words", 2, "a b c", 3)):
            with self.subTest(unit=unit):
                task = RevisionTaskInput.model_validate(limited_task_input(unit=unit, maximum=maximum))
                patch = patch_with_text(task, replacement)
                task_before, patch_before = task.model_dump(), patch.model_dump()

                candidate, result = apply_patch_to_draft(task, patch)

                self.assertFalse(result.passed)
                self.assertEqual(candidate.blocks[0].text, replacement)
                self.assertEqual(candidate.base_version, task.draft.version)
                issue = next(issue for issue in result.issues if unit in issue)
                self.assertIn(FIRST_BLOCK_ID, issue)
                self.assertRegex(issue, rf"(?<![\dA-Za-z]){actual}(?![\dA-Za-z])")
                self.assertRegex(issue, rf"(?<![\dA-Za-z]){maximum}(?![\dA-Za-z])")
                self.assertEqual(task.model_dump(), task_before)
                self.assertEqual(patch.model_dump(), patch_before)

    def test_shorter_replacement_repairs_over_limit_candidate(self):
        task = RevisionTaskInput.model_validate(limited_task_input(maximum=4))
        task_before = task.model_dump()

        over, failed = apply_patch_to_draft(task, patch_with_text(task, "123456"))
        repaired, passed = apply_patch_to_draft(task, patch_with_text(task, "1234"))

        self.assertFalse(failed.passed)
        self.assertTrue(passed.passed)
        self.assertEqual(passed.issues, [])
        self.assertEqual(over.blocks[0].text, "123456")
        self.assertEqual(repaired.blocks[0].text, "1234")
        self.assertNotEqual(over.candidate_id, repaired.candidate_id)
        self.assertEqual(task.model_dump(), task_before)

    def test_missing_preserved_text_and_length_violation_are_both_reported(self):
        payload = limited_task_input(maximum=3)
        payload["requirements"]["must_keep"] = ["  65W"]
        task = RevisionTaskInput.model_validate(payload)

        candidate, result = apply_patch_to_draft(task, patch_with_text(task, "12345"))

        self.assertFalse(result.passed)
        self.assertEqual(candidate.blocks[0].text, "12345")
        self.assertTrue(any("65W" in issue for issue in result.issues))
        self.assertTrue(any("unicode_chars" in issue for issue in result.issues))
        self.assertGreaterEqual(len(result.issues), 2)

    def test_two_units_for_one_block_are_checked_independently(self):
        payload = limited_task_input(maximum=3)
        payload["requirements"]["length_limits"].append(limit_input(unit="whitespace_words", maximum=1))
        task = RevisionTaskInput.model_validate(payload)

        result = validate_draft(task, candidate_with_text(task, "one two"))

        self.assertFalse(result.passed)
        self.assertEqual(len(result.issues), 2)
        self.assertTrue(any("unicode_chars" in issue for issue in result.issues))
        self.assertTrue(any("whitespace_words" in issue for issue in result.issues))

    def test_missing_limited_candidate_block_fails_without_key_error(self):
        task = RevisionTaskInput.model_validate(limited_task_input())
        candidate = candidate_with_text(task, "short")
        candidate.blocks.pop(0)

        result = validate_draft(task, candidate)

        self.assertFalse(result.passed)
        self.assertTrue(result.issues)

    def test_invalid_limits_mutated_after_construction_are_revalidated(self):
        for mutation in ("nonpositive", "duplicate", "read_only"):
            with self.subTest(mutation=mutation):
                task = RevisionTaskInput.model_validate(limited_task_input())
                patch = patch_with_text(task, "short")
                candidate = candidate_with_text(task, "short")
                if mutation == "nonpositive":
                    task.requirements.length_limits[0].maximum = 0
                elif mutation == "duplicate":
                    task.requirements.length_limits.append(task.requirements.length_limits[0])
                else:
                    task.requirements.length_limits[0].block_id = UUID(SECOND_BLOCK_ID)
                with self.assertRaises(ValueError):
                    apply_patch_to_draft(task, patch)
                with self.assertRaises(ValueError):
                    validate_draft(task, candidate)

    def test_tightened_valid_limit_is_used_when_revalidating_existing_candidate(self):
        task = RevisionTaskInput.model_validate(limited_task_input(maximum=10))
        candidate = candidate_with_text(task, "123456")
        self.assertTrue(validate_draft(task, candidate).passed)

        task.requirements.length_limits[0].maximum = 5

        result = validate_draft(task, candidate)
        self.assertFalse(result.passed)
        self.assertTrue(any("unicode_chars" in issue for issue in result.issues))


if __name__ == "__main__":
    unittest.main()
