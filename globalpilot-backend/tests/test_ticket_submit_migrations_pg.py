"""Transactional result/review-comment upgrades on disposable PostgreSQL."""

import os
import unittest
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import create_engine, inspect, text

from app.db.concurrency_migrations import apply_ticket_migrations

if __package__:
    from . import test_team_authorization_pg as team_authorization
else:
    import test_team_authorization_pg as team_authorization


@unittest.skipUnless(
    os.environ.get("TEAM_AUTH_TEST_DATABASE_URL"),
    "requires explicit isolated TEAM_AUTH_TEST_DATABASE_URL",
)
class TicketSubmitMigrationsPostgresTests(unittest.TestCase):
    def setUp(self):
        url = team_authorization.isolated_database_url(os.environ["TEAM_AUTH_TEST_DATABASE_URL"])
        self.schema = "ticket_submit_migration_" + uuid4().hex
        self.engine = create_engine(
            url, echo=False,
            connect_args={"options": f"-csearch_path={self.schema} -cstatement_timeout=10000 -clock_timeout=5000"},
        )
        self.addCleanup(self.engine.dispose)
        with self.engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{self.schema}"'))

        def drop_schema():
            with self.engine.begin() as conn:
                conn.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))

        self.addCleanup(drop_schema)

    def legacy_table(self, *, with_result=False, with_review_comment=False):
        extra = ", processing_result TEXT" if with_result else ""
        if with_review_comment:
            extra += ", review_comment TEXT"
        with self.engine.begin() as conn:
            conn.execute(text(f"""
                CREATE TABLE tickets (
                    id UUID PRIMARY KEY, title TEXT NOT NULL,
                    status VARCHAR(32) NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL{extra}
                )
            """))
            stamp = datetime(2020, 1, 1, tzinfo=timezone.utc)
            conn.execute(text("""
                INSERT INTO tickets (id, title, status, created_at, updated_at)
                VALUES (:id, :title, :status, :stamp, :stamp)
            """), [
                {"id": uuid4(), "title": f"Legacy {status}", "status": status, "stamp": stamp}
                for status in ("processing", "awaiting_review", "needs_info", "completed")
            ])

    def migrate(self):
        with self.engine.begin() as conn:
            apply_ticket_migrations(conn)

    def historical_rows(self):
        with self.engine.connect() as conn:
            return [tuple(row) for row in conn.execute(text(
                "SELECT id, title, status, created_at, updated_at, xmin::text FROM tickets ORDER BY id"
            )).all()]

    def test_missing_ticket_table_is_a_safe_noop(self):
        self.migrate()
        with self.engine.connect() as conn:
            self.assertFalse(inspect(conn).has_table("tickets", schema=self.schema))

    def test_old_rows_gain_a_nullable_text_result_without_rewriting_history(self):
        self.legacy_table()
        before = self.historical_rows()
        self.migrate()
        with self.engine.connect() as conn:
            columns = {column["name"]: column for column in inspect(conn).get_columns("tickets", schema=self.schema)}
            self.assertEqual(set(columns), {
                "id", "title", "status", "created_at", "updated_at",
                "processing_result", "review_comment",
            })
            for field in ("processing_result", "review_comment"):
                result = columns[field]
                self.assertTrue(result["nullable"], field)
                self.assertEqual(str(result["type"]).upper(), "TEXT", field)
                self.assertIsNone(result["default"], field)
            self.assertEqual([
                tuple(row) for row in conn.execute(text(
                    "SELECT processing_result, review_comment FROM tickets"
                )).all()
            ], [(None, None)] * 4)
        self.assertEqual(self.historical_rows(), before)

    def test_result_only_legacy_table_gains_null_review_comments_without_rewriting_results(self):
        self.legacy_table(with_result=True)
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE tickets SET processing_result='Existing result' WHERE status='awaiting_review'"))
            before = [tuple(row) for row in conn.execute(text(
                "SELECT id, title, status, processing_result, created_at, updated_at, xmin::text FROM tickets ORDER BY id"
            )).all()]
        self.migrate()
        self.migrate()
        with self.engine.connect() as conn:
            after = [tuple(row) for row in conn.execute(text(
                "SELECT id, title, status, processing_result, created_at, updated_at, xmin::text FROM tickets ORDER BY id"
            )).all()]
            self.assertEqual(conn.execute(text("SELECT review_comment FROM tickets")).scalars().all(), [None] * 4)
        self.assertEqual(after, before)
        self.assertEqual(sum(row[3] == "Existing result" for row in after), 1)

    def test_repeated_upgrade_preserves_both_existing_results_and_null_history(self):
        self.legacy_table(with_result=True, with_review_comment=True)
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE tickets SET processing_result='Existing result' WHERE status='awaiting_review'"))
            conn.execute(text("UPDATE tickets SET review_comment='Please add evidence' WHERE status='needs_info'"))
            before = [tuple(row) for row in conn.execute(text(
                "SELECT id, title, status, processing_result, review_comment, created_at, updated_at, xmin::text FROM tickets ORDER BY id"
            )).all()]
        self.migrate()
        self.migrate()
        with self.engine.connect() as conn:
            after = [tuple(row) for row in conn.execute(text(
                "SELECT id, title, status, processing_result, review_comment, created_at, updated_at, xmin::text FROM tickets ORDER BY id"
            )).all()]
        self.assertEqual(after, before)
        self.assertEqual(sum(row[3] == "Existing result" for row in after), 1)
        self.assertEqual(sum(row[3] is None for row in after), 3)
        self.assertEqual(sum(row[4] == "Please add evidence" for row in after), 1)
        self.assertEqual(sum(row[4] is None for row in after), 3)

    def test_migration_obeys_its_callers_transaction_and_rolls_back_cleanly(self):
        self.legacy_table()
        before = self.historical_rows()
        with self.engine.connect() as conn:
            transaction = conn.begin()
            apply_ticket_migrations(conn)
            columns = {
                column["name"] for column in inspect(conn).get_columns("tickets", schema=self.schema)
            }
            self.assertTrue({"processing_result", "review_comment"}.issubset(columns))
            transaction.rollback()
        with self.engine.connect() as conn:
            columns = {
                column["name"] for column in inspect(conn).get_columns("tickets", schema=self.schema)
            }
            self.assertFalse({"processing_result", "review_comment"} & columns)
        self.assertEqual(self.historical_rows(), before)


if __name__ == "__main__":
    unittest.main()
