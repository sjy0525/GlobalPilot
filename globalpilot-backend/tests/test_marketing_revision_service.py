"""Source fidelity and fenced persistence boundaries without database access."""

from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi import HTTPException

from app.modules.marketing.revision_service import (
    RevisionPostgresSaver, _prepare_clarification, _search_context, _validate_requested_task,
    join_document_body, split_document_body,
)
from tests.test_marketing_revision_semantic import review_context, review_task


class RevisionDocumentLayoutTests(unittest.TestCase):
    def test_round_trip_preserves_exact_whitespace_and_line_endings(self):
        bodies = (
            "One paragraph.", "  One paragraph.\n", "first\n\nsecond",
            "\n\n  first\r\n\r\nsecond\n\n", "  a  \n \n  b  ",
            "first\nline\n\nsecond", "a\r\n \r\n\t\r\nb\r\n",
        )
        for body in bodies:
            with self.subTest(body=body):
                blocks, layout = split_document_body(body)
                self.assertEqual(join_document_body(blocks, layout), body)
                self.assertEqual(len(layout), len(blocks) + 1)

    def test_replacing_one_paragraph_preserves_all_separators_and_other_text(self):
        body = "\n\n  first\r\n \r\n  second  \n\nthird\n"
        blocks, layout = split_document_body(body)
        blocks[1]["text"] = "replacement"
        self.assertEqual(
            join_document_body(blocks, layout),
            "\n\n  first\r\n \r\n  replacement\n\nthird\n",
        )

    def test_repeated_reads_and_equal_count_edits_keep_block_ids(self):
        old, _ = split_document_body("first\n\nsecond")
        unchanged, _ = split_document_body("first\n\nsecond", old)
        changed, _ = split_document_body("first revised\n\nsecond", old)
        self.assertEqual([row["block_id"] for row in unchanged], [row["block_id"] for row in old])
        self.assertEqual([row["block_id"] for row in changed], [row["block_id"] for row in old])

    def test_inserted_paragraph_gets_new_id_without_shifting_unchanged_ids(self):
        old, _ = split_document_body("first\n\nlast")
        changed, _ = split_document_body("first\n\ninserted\n\nlast", old)
        self.assertEqual(changed[0]["block_id"], old[0]["block_id"])
        self.assertEqual(changed[2]["block_id"], old[1]["block_id"])
        self.assertNotIn(changed[1]["block_id"], {row["block_id"] for row in old})

    def test_layout_or_empty_body_is_rejected(self):
        with self.assertRaises(ValueError):
            join_document_body([{"text": "one"}], [""])
        with self.assertRaises(HTTPException) as caught:
            split_document_body(" \n\t\r\n")
        self.assertEqual(caught.exception.status_code, 409)

    def test_request_cross_field_errors_are_422_not_server_errors(self):
        task = review_task()
        requirements = task.requirements.model_dump(mode="json")
        requirements["allowed_block_ids"] = [str(uuid4())]
        with self.assertRaises(HTTPException) as caught:
            _validate_requested_task(task.draft, requirements)
        self.assertEqual(caught.exception.status_code, 422)
        self.assertNotIn("input", caught.exception.detail[0])


class RevisionEvidenceReadTests(unittest.TestCase):
    def setUp(self):
        self.brief = {"id": uuid4(), "team_id": uuid4(), "user_id": uuid4(), "brand_id": uuid4()}
        self.rows = [{
            "id": uuid4(), "version": 3, "source": "Test catalog",
            "body": "Prefix. This charger supports 65W output. Suffix.",
        }]
        self.cursor = Mock()
        self.cursor.fetchall.side_effect = lambda: deepcopy(self.rows)

        @contextmanager
        def transaction():
            yield self.cursor

        self.guard = SimpleNamespace(transaction=transaction)
        self.scope = patch("app.modules.marketing.revision_service.load_owned_team_brief_cursor", return_value=self.brief)
        self.scope.start()
        self.addCleanup(self.scope.stop)

    def search(self, **kwargs):
        return _search_context(
            self.guard, self.brief, query=kwargs.get("query", "65W charger"),
            brand_rules=["自然简洁"], selected_ids=kwargs.get("selected_ids", []),
        )

    def test_evidence_has_real_identity_version_and_exact_source_quote(self):
        context = self.search()
        self.assertEqual(len(context.evidence), 1)
        evidence = context.evidence[0]
        self.assertEqual(evidence.document_id, self.rows[0]["id"])
        self.assertEqual(evidence.document_version, 3)
        self.assertIn(evidence.quote, self.rows[0]["body"])
        self.assertIn(str(evidence.document_id), evidence.evidence_id)
        self.assertIn(":v3:", evidence.evidence_id)

    def test_unrelated_docs_do_not_become_fabricated_evidence(self):
        self.assertEqual(self.search(query="unrelated watermelon").evidence, [])

    def test_selected_doc_must_exist_in_authorized_scope(self):
        with self.assertRaises(HTTPException) as caught:
            self.search(selected_ids=[uuid4()])
        self.assertEqual(caught.exception.status_code, 404)

    def test_quote_count_and_size_are_bounded(self):
        self.rows = [dict(self.rows[0], id=uuid4(), body="65W " + "x" * 4000) for _ in range(20)]
        context = self.search()
        self.assertEqual(len(context.evidence), 6)
        self.assertTrue(all(len(item.quote) <= 1200 for item in context.evidence))


class RevisionCheckpointFenceTests(unittest.TestCase):
    def setUp(self):
        self.task_id = uuid4()
        self.raw = Mock()
        self.enters = 0

        @contextmanager
        def saver(serde):
            self.enters += 1
            yield self.raw

        self.proxy = RevisionPostgresSaver(SimpleNamespace(connection=Mock(), checkpoint_saver=saver), self.task_id)
        self.config = {"configurable": {"thread_id": str(self.task_id), "checkpoint_ns": ""}}

    def test_each_operation_enters_physical_guard_and_returns_result(self):
        self.raw.get_tuple.return_value = "checkpoint"
        self.assertEqual(self.proxy.get_tuple(self.config), "checkpoint")
        self.proxy.put(self.config, {}, {}, {})
        self.proxy.put_writes(self.config, [("x", 1)], "node-task")
        self.assertEqual(self.enters, 3)
        self.raw.put.assert_called_once()
        self.raw.put_writes.assert_called_once()

    def test_other_tasks_and_namespaces_cannot_use_proxy(self):
        for config in (
            {"configurable": {"thread_id": str(uuid4())}},
            {"configurable": {"thread_id": str(self.task_id), "checkpoint_ns": "foreign"}},
        ):
            with self.subTest(config=config):
                with self.assertRaises(RuntimeError):
                    self.proxy.get_tuple(config)
        self.assertEqual(self.enters, 0)

    def test_lost_guard_prevents_writes_without_reconnecting(self):
        @contextmanager
        def lost(serde):
            raise RuntimeError("physical session lost")
            yield

        self.proxy.guard.checkpoint_saver = lost
        with self.assertRaises(RuntimeError):
            self.proxy.put(self.config, {}, {}, {})
        self.raw.put.assert_not_called()


class RevisionClarificationRecoveryTests(unittest.TestCase):
    def test_retry_rebuilds_from_operation_baseline_without_duplicating_feedback(self):
        task = review_task()
        baseline = task.requirements.model_dump(mode="json")
        row = {
            "id": uuid4(), "team_id": uuid4(), "user_id": uuid4(), "brief_id": uuid4(),
            "original_draft": task.draft.model_dump(mode="json"),
            "requirements": {**baseline, "goal": baseline["goal"] + "\n补充修改要求：强调便携"},
            "review_context": review_context().model_dump(mode="json"),
        }
        operation = {"payload": {"_requirements_before": deepcopy(baseline), "_selected_document_ids_before": []}}
        payload = SimpleNamespace(goal=None, must_keep=None, length_limits=None, feedback="强调便携", evidence_document_ids=[])
        cursor = Mock()

        @contextmanager
        def transaction():
            yield cursor

        guard = SimpleNamespace(transaction=transaction)
        with (
            patch("app.modules.marketing.revision_service._assert_source_current", return_value={}),
            patch("app.modules.marketing.revision_service._search_context", return_value=review_context()),
            patch("app.modules.marketing.revision_service.load_owned_team_brief_cursor"),
        ):
            first = _prepare_clarification(guard, row, payload, operation=operation)
            row["requirements"] = first["task"]["requirements"]
            second = _prepare_clarification(guard, row, payload, operation=operation)
        self.assertEqual(first, second)
        self.assertEqual(second["task"]["requirements"]["goal"].count("强调便携"), 1)
        self.assertEqual(operation["payload"]["_requirements_before"], baseline)


if __name__ == "__main__":
    unittest.main()
