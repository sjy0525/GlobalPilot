"""Offline contract checks for paragraph-revision inputs and draft snapshots."""

from copy import deepcopy
import json
import unittest
from uuid import UUID

from pydantic import ValidationError

from app.modules.marketing.schemas.revision import (
    DraftBlock,
    DraftSnapshot,
    RevisionRequirements,
    RevisionTaskInput,
)


FIRST_BLOCK_ID = "00000000-0000-4000-8000-000000000002"
SECOND_BLOCK_ID = "00000000-0000-4000-8000-000000000001"
OUTSIDE_BLOCK_ID = "00000000-0000-4000-8000-000000000003"


def revision_input() -> dict:
    return {
        "draft": {
            "draft_id": "10000000-0000-4000-8000-000000000001",
            "version": 1,
            "source_brief_id": "20000000-0000-4000-8000-000000000001",
            "source_checkpoint_id": "  checkpoint-12  ",
            "platform": "  instagram  ",
            "blocks": [
                {"block_id": FIRST_BLOCK_ID, "text": "  65W GaN 充电器，\n双 USB-C。  "},
                {"block_id": SECOND_BLOCK_ID, "text": "  支持 30 天退货。\n"},
            ],
        },
        "requirements": {
            "goal": "  精简第一段，保留产品参数  ",
            "allowed_block_ids": [FIRST_BLOCK_ID],
            "must_keep": ["  65W", "30 天退货"],
        },
    }


def at_path(value: dict, path: tuple):
    current = value
    for part in path:
        current = current[part]
    return current


class MarketingRevisionSchemaTests(unittest.TestCase):
    def test_nested_json_round_trip_preserves_ids_order_and_source_text(self):
        payload = revision_input()
        payload["requirements"]["allowed_block_ids"] = [SECOND_BLOCK_ID, FIRST_BLOCK_ID]

        task = RevisionTaskInput.model_validate_json(json.dumps(payload, ensure_ascii=False))

        self.assertIsInstance(task.draft.draft_id, UUID)
        self.assertIsInstance(task.draft.source_brief_id, UUID)
        self.assertEqual(
            [block.block_id for block in task.draft.blocks],
            [UUID(FIRST_BLOCK_ID), UUID(SECOND_BLOCK_ID)],
        )
        self.assertEqual(
            task.requirements.allowed_block_ids,
            [UUID(SECOND_BLOCK_ID), UUID(FIRST_BLOCK_ID)],
        )
        self.assertEqual(task.requirements.goal, "精简第一段，保留产品参数")
        self.assertEqual(task.draft.source_checkpoint_id, "checkpoint-12")
        self.assertEqual(task.draft.platform, "instagram")
        self.assertEqual(
            [block.text for block in task.draft.blocks],
            [block["text"] for block in payload["draft"]["blocks"]],
        )
        self.assertEqual(task.requirements.must_keep, payload["requirements"]["must_keep"])
        self.assertEqual(
            RevisionTaskInput.model_validate_json(task.model_dump_json()),
            task,
        )

    def test_must_keep_defaults_to_an_independent_empty_list(self):
        values = {"goal": "精简正文", "allowed_block_ids": [FIRST_BLOCK_ID]}
        first = RevisionRequirements.model_validate(values)
        second = RevisionRequirements.model_validate(values)

        self.assertEqual(first.must_keep, [])
        first.must_keep.append("65W")
        self.assertEqual(second.must_keep, [])

    def test_required_fields_cannot_be_omitted(self):
        fields = [
            ((), "draft"),
            ((), "requirements"),
            (("draft",), "draft_id"),
            (("draft",), "version"),
            (("draft",), "source_brief_id"),
            (("draft",), "source_checkpoint_id"),
            (("draft",), "platform"),
            (("draft",), "blocks"),
            (("draft", "blocks", 0), "block_id"),
            (("draft", "blocks", 0), "text"),
            (("requirements",), "goal"),
            (("requirements",), "allowed_block_ids"),
        ]
        for path, field in fields:
            with self.subTest(path=path, field=field):
                payload = revision_input()
                del at_path(payload, path)[field]
                with self.assertRaises(ValidationError) as raised:
                    RevisionTaskInput.model_validate(payload)
                self.assertIn(
                    {"loc": (*path, field), "type": "missing"},
                    [
                        {"loc": error["loc"], "type": error["type"]}
                        for error in raised.exception.errors()
                    ],
                )

    def test_identity_fields_reject_invalid_uuid_values(self):
        paths = [
            ("draft", "draft_id"),
            ("draft", "source_brief_id"),
            ("draft", "blocks", 0, "block_id"),
            ("requirements", "allowed_block_ids", 0),
        ]
        for path in paths:
            for invalid in ("not-a-uuid", "", None):
                with self.subTest(path=path, invalid=invalid):
                    payload = revision_input()
                    at_path(payload, path[:-1])[path[-1]] = invalid
                    with self.assertRaises(ValidationError) as raised:
                        RevisionTaskInput.model_validate(payload)
                    self.assertIn(
                        {
                            "loc": path,
                            "type": "uuid_type" if invalid is None else "uuid_parsing",
                        },
                        [
                            {"loc": error["loc"], "type": error["type"]}
                            for error in raised.exception.errors()
                        ],
                    )

    def test_blank_goal_is_rejected(self):
        for goal in ("", " ", "\t\n"):
            with self.subTest(goal=goal):
                payload = revision_input()
                payload["requirements"]["goal"] = goal
                with self.assertRaises(ValidationError):
                    RevisionTaskInput.model_validate(payload)

    def test_blank_source_paragraph_is_rejected(self):
        for text in ("", " ", "\t\n"):
            with self.subTest(text=text):
                with self.assertRaises(ValidationError):
                    DraftBlock.model_validate({"block_id": FIRST_BLOCK_ID, "text": text})

    def test_blank_preserved_fragment_is_rejected(self):
        for fragment in ("", " ", "\t\n"):
            with self.subTest(fragment=fragment):
                payload = revision_input()
                payload["requirements"]["must_keep"] = [fragment]
                with self.assertRaises(ValidationError):
                    RevisionTaskInput.model_validate(payload)

    def test_empty_or_duplicate_allowed_blocks_are_rejected(self):
        for block_ids in ([], [FIRST_BLOCK_ID, FIRST_BLOCK_ID]):
            with self.subTest(block_ids=block_ids):
                payload = revision_input()
                payload["requirements"]["allowed_block_ids"] = block_ids
                with self.assertRaises(ValidationError):
                    RevisionTaskInput.model_validate(payload)

    def test_empty_or_duplicate_snapshot_blocks_are_rejected(self):
        snapshot = revision_input()["draft"]
        for blocks in ([], [snapshot["blocks"][0], deepcopy(snapshot["blocks"][0])]):
            with self.subTest(blocks=blocks):
                candidate = deepcopy(snapshot)
                candidate["blocks"] = blocks
                with self.assertRaises(ValidationError):
                    DraftSnapshot.model_validate(candidate)

    def test_version_requires_a_positive_integer_without_coercion(self):
        for version in (True, False, "1", 1.0, None, 0, -1):
            with self.subTest(version=version):
                snapshot = revision_input()["draft"]
                snapshot["version"] = version
                with self.assertRaises(ValidationError):
                    DraftSnapshot.model_validate(snapshot)

        snapshot = revision_input()["draft"]
        snapshot["version"] = 12
        self.assertEqual(DraftSnapshot.model_validate(snapshot).version, 12)

    def test_source_metadata_rejects_blank_or_overlong_values(self):
        for field, limit in (("source_checkpoint_id", 128), ("platform", 50)):
            for value in ("", " \t\n", "x" * (limit + 1)):
                with self.subTest(field=field, value=value):
                    snapshot = revision_input()["draft"]
                    snapshot[field] = value
                    with self.assertRaises(ValidationError):
                        DraftSnapshot.model_validate(snapshot)

            snapshot = revision_input()["draft"]
            snapshot[field] = "x" * limit
            self.assertEqual(getattr(DraftSnapshot.model_validate(snapshot), field), "x" * limit)

    def test_extra_fields_are_rejected_at_all_four_model_levels(self):
        for path in ((), ("draft",), ("draft", "blocks", 0), ("requirements",)):
            with self.subTest(path=path):
                payload = revision_input()
                at_path(payload, path)["unexpected_field"] = "untrusted"
                with self.assertRaises(ValidationError):
                    RevisionTaskInput.model_validate(payload)

    def test_allowed_block_must_exist_in_the_loaded_snapshot(self):
        payload = revision_input()
        payload["requirements"]["allowed_block_ids"] = [FIRST_BLOCK_ID, OUTSIDE_BLOCK_ID]
        with self.assertRaises(ValidationError):
            RevisionTaskInput.model_validate(payload)

    def test_preserved_fragment_can_belong_to_a_read_only_paragraph(self):
        payload = revision_input()
        payload["requirements"]["must_keep"] = ["30 天退货"]

        task = RevisionTaskInput.model_validate(payload)

        self.assertEqual(task.requirements.allowed_block_ids, [UUID(FIRST_BLOCK_ID)])
        self.assertNotIn(UUID(SECOND_BLOCK_ID), task.requirements.allowed_block_ids)
        self.assertEqual(task.requirements.must_keep, ["30 天退货"])

    def test_preserved_fragment_must_match_source_text_exactly(self):
        for fragment in ("终身保修", "65WGaN", "30天退货", "30  天退货"):
            with self.subTest(fragment=fragment):
                payload = revision_input()
                payload["requirements"]["must_keep"] = [fragment]
                with self.assertRaises(ValidationError):
                    RevisionTaskInput.model_validate(payload)

    def test_preserved_fragment_cannot_be_joined_across_paragraph_boundaries(self):
        payload = revision_input()
        payload["draft"]["blocks"][0]["text"] = "支持 30 天"
        payload["draft"]["blocks"][1]["text"] = "退货。"
        for fragment in ("30 天退货", "30 天\n退货"):
            with self.subTest(fragment=fragment):
                payload["requirements"]["must_keep"] = [fragment]
                with self.assertRaises(ValidationError):
                    RevisionTaskInput.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
