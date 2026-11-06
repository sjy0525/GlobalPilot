"""Audit-table forward upgrades on an explicit, isolated PostgreSQL database.

TEAM_AUTH_TEST_DATABASE_URL must select test_*, audit_* or refactor_*. Each test
owns a random schema; this module never imports application settings or engines.
"""

import os
import unittest
from uuid import uuid4

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from app.db.concurrency_migrations import apply_team_member_audit_migrations


@unittest.skipUnless(
    os.environ.get("TEAM_AUTH_TEST_DATABASE_URL"),
    "requires explicit isolated TEAM_AUTH_TEST_DATABASE_URL",
)
class TeamMemberAuditMigrationsPostgresTests(unittest.TestCase):
    def setUp(self):
        try:
            url = make_url(os.environ["TEAM_AUTH_TEST_DATABASE_URL"])
        except Exception:
            raise AssertionError("TEAM_AUTH_TEST_DATABASE_URL is not a valid test URL") from None
        if (
            url.get_backend_name() != "postgresql"
            or not (url.database or "").startswith(("test_", "audit_", "refactor_"))
            or any(key.lower() in {"dbname", "database", "service", "servicefile"} for key in url.query)
        ):
            raise AssertionError("Refusing audit migration tests against a non-test database")
        self.schema = "team_audit_migration_" + uuid4().hex
        self.engine = create_engine(
            url.difference_update_query(["options"]),
            echo=False,
            connect_args={
                "options": (
                    f"-csearch_path={self.schema} "
                    "-cstatement_timeout=10000 -clock_timeout=5000"
                ),
            },
        )
        self.addCleanup(self.engine.dispose)
        with self.engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{self.schema}"'))

        def drop_schema():
            with self.engine.begin() as conn:
                conn.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))

        self.addCleanup(drop_schema)
        self.team_id, self.actor_id, self.target_id = (uuid4() for _ in range(3))
        with self.engine.begin() as conn:
            conn.execute(text("CREATE TABLE teams (id UUID PRIMARY KEY)"))
            conn.execute(text("CREATE TABLE users (id UUID PRIMARY KEY)"))
            conn.execute(text("INSERT INTO teams(id) VALUES (:id)"), {"id": self.team_id})
            conn.execute(text("INSERT INTO users(id) VALUES (:id)"), [
                {"id": self.actor_id}, {"id": self.target_id},
            ])

    def legacy_table(self):
        with self.engine.begin() as conn:
            conn.execute(text("CREATE TABLE team_member_audit_logs (id UUID PRIMARY KEY)"))

    def migrate(self):
        with self.engine.begin() as conn:
            apply_team_member_audit_migrations(conn)

    def insert_event(self, conn, *, team_id=None):
        identity = uuid4()
        conn.execute(text("""
            INSERT INTO team_member_audit_logs
                (id, team_id, actor_user_id, target_user_id, action)
            VALUES (:id, :team, :actor, :target, 'member_added')
        """), {
            "id": identity, "team": team_id or self.team_id,
            "actor": self.actor_id, "target": self.target_id,
        })
        return identity

    def test_missing_audit_table_is_a_safe_noop(self):
        self.migrate()
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text("SHOW search_path")).scalar_one(), self.schema)
            self.assertFalse(inspect(conn).has_table("team_member_audit_logs", schema=self.schema))

    def test_empty_id_only_table_gains_required_columns_defaults_and_restrict_foreign_keys(self):
        self.legacy_table()
        self.migrate()
        with self.engine.begin() as conn:
            columns = {
                column["name"]: column
                for column in inspect(conn).get_columns("team_member_audit_logs", schema=self.schema)
            }
            self.assertEqual(set(columns), {
                "id", "team_id", "actor_user_id", "target_user_id", "action", "create_at",
                "before_role", "after_role", "before_status", "after_status",
            })
            snapshots = {"before_role", "after_role", "before_status", "after_status"}
            for name, column in columns.items():
                self.assertEqual(column["nullable"], name in snapshots, name)
                if name in snapshots:
                    self.assertIsNone(column["default"], name)
            self.assertTrue(columns["create_at"]["type"].timezone)
            self.assertIsNotNone(columns["create_at"]["default"])
            identity = self.insert_event(conn)
            created = conn.execute(text(
                "SELECT create_at FROM team_member_audit_logs WHERE id=:id"
            ), {"id": identity}).scalar_one()
            self.assertIsNotNone(created.tzinfo)
            fks = inspect(conn).get_foreign_keys("team_member_audit_logs", schema=self.schema)
            self.assertEqual(len(fks), 3)
            self.assertTrue(all(fk["options"]["ondelete"] == "RESTRICT" for fk in fks))
        with self.assertRaises(IntegrityError):
            with self.engine.begin() as conn:
                self.insert_event(conn, team_id=uuid4())
        with self.assertRaises(IntegrityError):
            with self.engine.begin() as conn:
                conn.execute(text("DELETE FROM users WHERE id=:id"), {"id": self.actor_id})

    def test_repeated_migration_preserves_rows_and_repairs_an_existing_timestamp_default(self):
        self.legacy_table()
        self.migrate()
        with self.engine.begin() as conn:
            identity = self.insert_event(conn)
            before = conn.execute(text(
                "SELECT id, team_id, actor_user_id, target_user_id, action, create_at, xmin::text "
                "FROM team_member_audit_logs WHERE id=:id"
            ), {"id": identity}).one()
            conn.execute(text("ALTER TABLE team_member_audit_logs ALTER COLUMN create_at DROP DEFAULT"))
        self.migrate()
        self.migrate()
        with self.engine.begin() as conn:
            after = conn.execute(text(
                "SELECT id, team_id, actor_user_id, target_user_id, action, create_at, xmin::text "
                "FROM team_member_audit_logs WHERE id=:id"
            ), {"id": identity}).one()
            self.assertEqual(tuple(after), tuple(before))
            second = self.insert_event(conn)
            self.assertIsNotNone(conn.execute(text(
                "SELECT create_at FROM team_member_audit_logs WHERE id=:id"
            ), {"id": second}).scalar_one())
            self.assertEqual(conn.execute(text("SELECT count(*) FROM team_member_audit_logs")).scalar_one(), 2)

    def test_nonempty_legacy_events_gain_nullable_snapshots_without_rewriting_history(self):
        with self.engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE team_member_audit_logs (
                    id UUID PRIMARY KEY,
                    team_id UUID NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
                    actor_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
                    target_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
                    action VARCHAR(100) NOT NULL,
                    create_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """))
            identity = self.insert_event(conn)
            before = conn.execute(text(
                "SELECT id, team_id, actor_user_id, target_user_id, action, create_at, xmin::text "
                "FROM team_member_audit_logs WHERE id=:id"
            ), {"id": identity}).one()
        self.migrate()
        self.migrate()
        with self.engine.begin() as conn:
            after = conn.execute(text(
                "SELECT id, team_id, actor_user_id, target_user_id, action, create_at, xmin::text "
                "FROM team_member_audit_logs WHERE id=:id"
            ), {"id": identity}).one()
            self.assertEqual(tuple(after), tuple(before))
            snapshots = conn.execute(text(
                "SELECT before_role, after_role, before_status, after_status "
                "FROM team_member_audit_logs WHERE id=:id"
            ), {"id": identity}).one()
            self.assertEqual(tuple(snapshots), (None, None, None, None))
            new_id = uuid4()
            conn.execute(text("""
                INSERT INTO team_member_audit_logs (
                    id, team_id, actor_user_id, target_user_id, action,
                    before_role, after_role, before_status, after_status
                ) VALUES (
                    :id, :team, :actor, :target, 'member_role_changed',
                    'employee', 'admin', 'active', 'active'
                )
            """), {
                "id": new_id, "team": self.team_id,
                "actor": self.actor_id, "target": self.target_id,
            })
            new_before = conn.execute(text(
                "SELECT before_role, after_role, before_status, after_status, xmin::text "
                "FROM team_member_audit_logs WHERE id=:id"
            ), {"id": new_id}).one()
        self.migrate()
        with self.engine.connect() as conn:
            new_after = conn.execute(text(
                "SELECT before_role, after_role, before_status, after_status, xmin::text "
                "FROM team_member_audit_logs WHERE id=:id"
            ), {"id": new_id}).one()
            self.assertEqual(tuple(new_after), tuple(new_before))
            self.assertEqual(tuple(new_after[:4]), ("employee", "admin", "active", "active"))

    def test_nonempty_incomplete_table_is_rejected_without_changing_or_deleting_rows(self):
        self.legacy_table()
        identity = uuid4()
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO team_member_audit_logs(id) VALUES (:id)"), {"id": identity})
        with self.assertRaisesRegex(RuntimeError, "nonempty incomplete"):
            self.migrate()
        with self.engine.connect() as conn:
            columns = inspect(conn).get_columns("team_member_audit_logs", schema=self.schema)
            self.assertEqual([column["name"] for column in columns], ["id"])
            self.assertEqual(conn.execute(text("SELECT id FROM team_member_audit_logs")).scalars().all(), [identity])


if __name__ == "__main__":
    unittest.main()
