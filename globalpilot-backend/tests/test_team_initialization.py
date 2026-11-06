"""Team bootstrap transactions and CLI output using an isolated in-memory DB."""

import builtins
import importlib
import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch
from uuid import UUID, uuid4

from pydantic_settings import DotEnvSettingsSource
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.modules.teams.models import Team, TeamMember
from app.modules.users.models import User
from app.modules.brands.models import Brand, DEFAULT_BRAND_NAMES


class TeamInitializationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config = {
            "DATABASE_URL": "postgresql+psycopg://unused:unused@127.0.0.1:1/unused",
            "DASHSCOPE_API_KEY": "unused",
            "DASHSCOPE_BASE_URL": "https://unused.invalid/v1",
            "DASHSCOPE_RERANK_API_KEY": "unused",
            "DEEPSEEK_API_KEY": "unused",
            "JWT_SECRET_KEY": "team-bootstrap-test-secret-" * 4,
        }
        with patch.dict(os.environ, config), patch.object(
            DotEnvSettingsSource, "_read_env_files", return_value={}
        ):
            cls.database = importlib.import_module("app.db.database")
            cls.service = importlib.import_module("app.modules.teams.service")
            cls.cli = importlib.import_module("scripts.init_team_admin")
            cls.errors = importlib.import_module("app.core.http_errors")

        def forbid_application_connection(*args, **kwargs):
            raise AssertionError("Bootstrap tests must not connect to the application DB")

        for engine in (cls.database.engine, cls.database.async_engine.sync_engine):
            event.listen(engine, "do_connect", forbid_application_connection)
            cls.addClassCleanup(event.remove, engine, "do_connect", forbid_application_connection)

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        self.addCleanup(self.engine.dispose)
        with self.engine.begin() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            for model in (User, Team, TeamMember, Brand):
                model.__table__.create(conn)
        self.sessions = sessionmaker(bind=self.engine, autoflush=False)
        self.user_id = uuid4()
        with self.sessions.begin() as db:
            db.add(User(id=self.user_id, email="admin@example.com", is_active=True))

    def initialize(self, **kwargs):
        with self.sessions.begin() as db:
            return self.service.initialize_team_admin(
                db, admin_email="admin@example.com", **kwargs
            )

    def assert_error(self, code, **kwargs):
        with self.assertRaises(self.errors.AppHTTPException) as caught:
            with self.sessions.begin() as db:
                self.service.initialize_team_admin(
                    db, admin_email=kwargs.pop("admin_email", "admin@example.com"), **kwargs
                )
        self.assertEqual(caught.exception.code, code)

    def assert_no_teams(self):
        with self.sessions() as db:
            self.assertEqual(db.query(Team).count(), 0)
            self.assertEqual(db.query(TeamMember).count(), 0)

    def run_cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch("sys.argv", ["init_team_admin", *args]),
            patch.object(self.database, "engine", self.engine),
            patch.object(self.database, "sessionLocal", self.sessions),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            status = self.cli.main()
        return status, stdout.getvalue(), stderr.getvalue()

    def test_new_team_creates_active_admin_and_json_serializable_result(self):
        with self.sessions.begin() as db:
            result = self.service.initialize_team_admin(
                db, admin_email=" ADMIN@EXAMPLE.COM ", team_name=" Team A "
            )
        json.dumps(result)
        with self.sessions() as db:
            self.assertEqual(db.query(Team).one().name, "Team A")
            member = db.query(TeamMember).one()
            self.assertEqual(member.user_id, self.user_id)
            self.assertEqual((member.role, member.status), ("admin", "active"))
            self.assertEqual(result["team_id"], str(member.team_id))
            self.assertEqual(result["admin_user_id"], str(self.user_id))
            self.assertEqual(
                {brand.name for brand in db.query(Brand).filter_by(team_id=member.team_id)},
                set(DEFAULT_BRAND_NAMES),
            )

    def test_existing_empty_team_can_be_initialized(self):
        team_id = uuid4()
        with self.sessions.begin() as db:
            db.add(Team(id=team_id, name="Existing"))
        result = self.initialize(team_id=team_id)
        self.assertEqual(result["team_id"], str(team_id))
        with self.sessions() as db:
            self.assertEqual(db.query(Team).count(), 1)
            self.assertEqual(db.query(TeamMember).count(), 1)

    def test_members_of_another_team_do_not_block_initialization(self):
        first = self.initialize(team_name="First")
        second_id = uuid4()
        with self.sessions.begin() as db:
            db.add(Team(id=second_id, name="Second"))
        second = self.initialize(team_id=second_id)
        self.assertNotEqual(first["team_id"], second["team_id"])
        with self.sessions() as db:
            self.assertEqual(db.query(TeamMember).count(), 2)

    def test_existing_members_prevent_reinitialization(self):
        result = self.initialize(team_name="Existing")
        self.assert_error("TEAM_ALREADY_INITIALIZED", team_id=UUID(result["team_id"]))
        with self.sessions() as db:
            self.assertEqual(db.query(TeamMember).count(), 1)

    def test_removed_member_history_cannot_be_reinitialized(self):
        result = self.initialize(team_name="Existing")
        with self.sessions.begin() as db:
            db.query(TeamMember).one().status = "removed"
        self.assert_error("TEAM_ALREADY_INITIALIZED", team_id=UUID(result["team_id"]))
        with self.sessions() as db:
            self.assertEqual(db.query(TeamMember).one().status, "removed")

    def test_unknown_user_rolls_back_new_team(self):
        self.assert_error(
            "USER_NOT_FOUND", team_name="New", admin_email="unknown@example.com"
        )
        self.assert_no_teams()

    def test_disabled_user_rolls_back_new_team(self):
        with self.sessions.begin() as db:
            db.get(User, self.user_id).is_active = False
        self.assert_error("ACCOUNT_DISABLED", team_name="New")
        self.assert_no_teams()

    def test_caller_rollback_removes_both_team_and_member(self):
        with self.assertRaisesRegex(RuntimeError, "cancel bootstrap"):
            with self.sessions.begin() as db:
                self.service.initialize_team_admin(
                    db, admin_email="admin@example.com", team_name="Cancelled"
                )
                raise RuntimeError("cancel bootstrap")
        self.assert_no_teams()

    def test_team_arguments_are_mutually_exclusive_and_required(self):
        for kwargs in ({}, {"team_name": "New", "team_id": uuid4()}):
            with self.subTest(kwargs=kwargs):
                self.assert_error("INVALID_INIT_ARGUMENTS", **kwargs)
        self.assert_no_teams()

    def test_invalid_names_are_rejected(self):
        for name in ("   ", "a" * 101):
            with self.subTest(name=name):
                self.assert_error("INVALID_TEAM_NAME", team_name=name)
        self.assert_no_teams()

    def test_unknown_team_is_rejected(self):
        self.assert_error("TEAM_NOT_FOUND", team_id=uuid4())
        self.assert_no_teams()

    def test_cli_commits_team_and_prints_json(self):
        status, stdout, stderr = self.run_cli(
            "--team-name", "CLI team", "--admin-email", "admin@example.com"
        )
        self.assertEqual((status, stderr), (0, ""))
        result = json.loads(stdout)
        with self.sessions() as db:
            member = db.query(TeamMember).one()
            self.assertEqual(result["team_id"], str(member.team_id))

    def test_cli_business_error_has_no_success_output_or_partial_team(self):
        status, stdout, stderr = self.run_cli(
            "--team-name", "CLI team", "--admin-email", "unknown@example.com"
        )
        self.assertEqual((status, stdout), (1, ""))
        self.assertIn("USER_NOT_FOUND", stderr)
        self.assert_no_teams()

    def test_cli_names_the_missing_module(self):
        original_import = builtins.__import__

        def import_without_service(name, *args, **kwargs):
            if name == "app.modules.teams.service":
                raise ModuleNotFoundError("missing service", name=name)
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=import_without_service):
            status, stdout, stderr = self.run_cli(
                "--team-name", "CLI team", "--admin-email", "admin@example.com"
            )
        self.assertEqual((status, stdout), (1, ""))
        self.assertIn("app.modules.teams.service", stderr)
        self.assert_no_teams()


if __name__ == "__main__":
    unittest.main()
