"""Real PostgreSQL, JWT and in-process HTTP checks for team authorization.

Opt in with TEAM_AUTH_TEST_DATABASE_URL pointing to a disposable PostgreSQL
database named test_*, audit_* or refactor_*. Every class owns a random schema;
neither the application database nor app.main/startup is used. The two __test__
probe endpoints exercise dependencies, not implemented team-management APIs.
"""

import importlib
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

import jwt
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic_settings import DotEnvSettingsSource
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker


def isolated_database_url(raw_url):
    """Reject unsafe targets before importing application settings or connecting."""
    try:
        url = make_url(raw_url)
    except Exception:
        raise AssertionError("TEAM_AUTH_TEST_DATABASE_URL is not a valid test URL") from None
    if (
        url.get_backend_name() != "postgresql"
        or not (url.database or "").startswith(("test_", "audit_", "refactor_"))
        or any(key.lower() in {"dbname", "database", "service", "servicefile"} for key in url.query)
    ):
        raise AssertionError("Refusing team authorization checks against a non-test database")
    # The test owns search_path; URL-supplied options cannot change its schema.
    return url.difference_update_query(["options"])


class TeamAuthorizationDatabaseSafetyTests(unittest.TestCase):
    def test_only_explicitly_named_disposable_postgresql_databases_are_allowed(self):
        for name in ("test_permissions", "audit_permissions", "refactor_permissions"):
            with self.subTest(name=name):
                url = isolated_database_url(f"postgresql://localhost/{name}")
                self.assertEqual(url.database, name)
        for raw in (
            "postgresql://localhost/globalpilot",
            "postgresql://localhost/postgres",
            "postgresql://localhost/",
            "postgresql://localhost/test_permissions?dbname=globalpilot",
            "postgresql://localhost/test_permissions?service=application",
            "sqlite:///test_permissions",
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(AssertionError):
                    isolated_database_url(raw)

    def test_url_cannot_supply_a_public_search_path(self):
        url = isolated_database_url(
            "postgresql://localhost/test_permissions?options=-csearch_path%3Dpublic"
        )
        self.assertNotIn("options", url.query)


@unittest.skipUnless(
    os.environ.get("TEAM_AUTH_TEST_DATABASE_URL"),
    "requires explicit isolated TEAM_AUTH_TEST_DATABASE_URL",
)
class TeamAuthorizationPostgresFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = isolated_database_url(os.environ["TEAM_AUTH_TEST_DATABASE_URL"])
        cls.schema = "team_authorization_" + uuid4().hex
        cls.engine = create_engine(
            url,
            echo=False,
            connect_args={
                "options": (
                    f"-csearch_path={cls.schema} "
                    "-cstatement_timeout=10000 -clock_timeout=5000"
                ),
            },
        )
        cls.addClassCleanup(cls.engine.dispose)
        with cls.engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{cls.schema}"'))

        def drop_schema():
            with cls.engine.begin() as conn:
                conn.execute(text(f'DROP SCHEMA "{cls.schema}" CASCADE'))

        cls.addClassCleanup(drop_schema)

        # Import the real modules without opening .env. These values only make
        # settings constructible; real requests below use the isolated factory.
        config = {
            "DATABASE_URL": url.render_as_string(hide_password=False),
            "DASHSCOPE_API_KEY": "unused-team-auth-test",
            "DASHSCOPE_BASE_URL": "https://unused.invalid/v1",
            "DASHSCOPE_RERANK_API_KEY": "unused-team-auth-test",
            "DEEPSEEK_API_KEY": "unused-team-auth-test",
            "JWT_SECRET_KEY": "team-auth-test-secret-" * 4,
            "JWT_ALGORITHM": "HS256",
        }
        with patch.dict(os.environ, config), patch.object(
            DotEnvSettingsSource, "_read_env_files", return_value={}
        ):
            cls.database = importlib.import_module("app.db.database")
            cls.security = importlib.import_module("app.modules.auth.security")
            cls.authorization = importlib.import_module("app.modules.teams.dependencies")
            cls.routes = importlib.import_module("app.modules.teams.router")
            cls.ticket_routes = importlib.import_module("app.modules.tickets.router")
            cls.auth_routes = importlib.import_module("app.modules.auth.router")
            cls.auth_service = importlib.import_module("app.modules.auth.service")
            cls.team_service = importlib.import_module("app.modules.teams.service")
            cls.ticket_service = importlib.import_module("app.modules.tickets.service")
            cls.team_authorization_utils = importlib.import_module("app.modules.teams.authorization")
            cls.ticket_schemas = importlib.import_module("app.modules.tickets.schemas")
            cls.errors = importlib.import_module("app.core.http_errors")
            cls.User = importlib.import_module("app.modules.users.models").User
            cls.Brand = importlib.import_module("app.modules.brands.models").Brand
            ticket_models = importlib.import_module("app.modules.tickets.models")
            cls.Ticket = ticket_models.Ticket
            cls.TicketAssignmentAuditLog = ticket_models.TicketAssignmentAuditLog
            models = importlib.import_module("app.modules.teams.models")
            cls.Team, cls.TeamMember = models.Team, models.TeamMember
            cls.TeamMemberAuditLog = models.TeamMemberAuditLog

        # Any accidental use of the module-level application engines must fail
        # before a connection is made, even when another suite imported them.
        def forbid_application_connection(*args, **kwargs):
            raise AssertionError("Only the explicit team authorization test engine may connect")

        for engine in (cls.database.engine, cls.database.async_engine.sync_engine):
            event.listen(engine, "do_connect", forbid_application_connection)
            cls.addClassCleanup(event.remove, engine, "do_connect", forbid_application_connection)

        for key, value in (
            ("jwt_secret_key", "team-auth-test-secret-" * 4),
            ("jwt_algorithm", "HS256"),
        ):
            setting = patch.object(cls.security.settings, key, value)
            setting.start()
            cls.addClassCleanup(setting.stop)

        cls.sessions = sessionmaker(bind=cls.engine, autoflush=False, expire_on_commit=False)
        with cls.engine.begin() as conn:
            for model in (cls.User, cls.Team, cls.TeamMember, cls.TeamMemberAuditLog, cls.Ticket, cls.TicketAssignmentAuditLog, cls.Brand):
                model.__table__.create(conn)
        cls.password = "team-auth-password"
        cls.password_hash = cls.security.hash_password(cls.password)

    def setUp(self):
        with self.sessions.begin() as db:
            for model in (self.TicketAssignmentAuditLog, self.Ticket, self.TeamMemberAuditLog, self.TeamMember, self.Brand, self.Team, self.User):
                db.execute(model.__table__.delete())
            actor = self.User(
                email="actor@team-auth.invalid",
                nickname="Team actor",
                google_sub="team-auth-google-subject",
                password_hash=self.password_hash,
                is_active=True,
            )
            outsider = self.User(email="outsider@team-auth.invalid", is_active=True)
            removed = self.User(email="removed@team-auth.invalid", is_active=True)
            disabled = self.User(email="disabled@team-auth.invalid", is_active=False)
            team_a, team_b = self.Team(name="Team A"), self.Team(name="Team B")
            db.add_all([actor, outsider, removed, disabled, team_a, team_b])
            db.flush()
            member_a = self.TeamMember(
                team_id=team_a.id, user_id=actor.id, role="admin", status="active"
            )
            member_b = self.TeamMember(
                team_id=team_b.id, user_id=actor.id, role="employee", status="active"
            )
            db.add_all([
                member_a,
                member_b,
                self.TeamMember(
                    team_id=team_a.id, user_id=removed.id, role="admin", status="removed"
                ),
                self.TeamMember(
                    team_id=team_a.id, user_id=disabled.id, role="admin", status="active"
                ),
            ])
            db.flush()
            self.actor_id, self.actor_email = actor.id, actor.email
            self.outsider_id, self.removed_id, self.disabled_id = outsider.id, removed.id, disabled.id
            self.outsider_email = outsider.email
            self.removed_email, self.disabled_email = removed.email, disabled.email
            self.team_a_id, self.team_b_id = team_a.id, team_b.id
            self.member_a_id, self.member_b_id = member_a.id, member_b.id

        self.executed = []
        self.request_sessions = []
        self.app = FastAPI()
        self.errors.install_exception_handlers(self.app)
        self.app.include_router(self.routes.router, prefix="/api")
        self.app.include_router(self.ticket_routes.router, prefix="/api")
        self.app.include_router(self.auth_routes.router, prefix="/api")

        def request_session():
            with self.sessions() as db:
                self.request_sessions.append(db)
                yield db

        for dependency in {
            self.database.get_db, self.security.get_db, self.authorization.get_db,
            self.auth_routes.get_db, self.ticket_routes.get_db,
        }:
            self.app.dependency_overrides[dependency] = request_session
        self.assertNotIn(self.security.get_current_user, self.app.dependency_overrides)

        # Test-only probes: these do not implement or claim member-management APIs.
        @self.app.post("/api/team/{team_id}/__test__/admin")
        def admin_probe(member=Depends(self.authorization.require_admin)):
            self.executed.append(("admin", member.team_id))
            return {"role": member.role}

        @self.app.post(
            "/api/team/{team_id}/__test__/member",
            dependencies=[Depends(self.authorization.get_current_member)],
        )
        def member_probe():
            self.executed.append(("member", None))
            return {"ok": True}

        self.client = TestClient(
            self.app, base_url="https://testserver", raise_server_exceptions=False
        )
        self.addCleanup(self.client.close)
        self.addCleanup(self.app.dependency_overrides.clear)

    def headers(self, user_id=None):
        token = self.security.create_access_token(user_id or self.actor_id)
        return {"Authorization": f"Bearer {token}"}

    def member_path(self, team_id=None):
        return f"/api/team/{team_id or self.team_a_id}/me"

    def admin_path(self, team_id=None):
        return f"/api/team/{team_id or self.team_a_id}/__test__/admin"

    def probe_path(self, team_id=None):
        return f"/api/team/{team_id or self.team_a_id}/__test__/member"

    def available_path(self, team_id=None):
        return f"/api/team/{team_id or self.team_a_id}/members/available"

    def members_path(self, team_id=None):
        return f"/api/team/{team_id or self.team_a_id}/members"

    def tickets_path(self, team_id=None):
        return f"/api/team/{team_id or self.team_a_id}/tickets"

    def add_available_members(self, count, *, team_id=None, join_at=None):
        joined = join_at or datetime.now(timezone.utc)
        with self.sessions.begin() as db:
            users = [
                self.User(
                    email=f"candidate-{uuid4().hex}@team-auth.invalid",
                    nickname=None,
                    is_active=True,
                )
                for _ in range(count)
            ]
            db.add_all(users)
            db.flush()
            members = [
                self.TeamMember(
                    team_id=team_id or self.team_a_id,
                    user_id=user.id,
                    role="employee",
                    status="active",
                    join_at=joined,
                )
                for user in users
            ]
            db.add_all(members)
            db.flush()
            return [
                {"membership_id": member.id, "user_id": user.id, "email": user.email}
                for user, member in zip(users, members)
            ]

    def assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.json()["code"], code)
        if status == 401:
            self.assertEqual(response.headers["www-authenticate"], "Bearer")


class TeamAuthorizationPostgresTests(TeamAuthorizationPostgresFixture):
    def test_connections_and_tables_are_in_the_random_schema(self):
        with self.sessions() as db:
            self.assertEqual(db.scalar(text("SHOW search_path")), self.schema)
            self.assertEqual(db.scalar(text("SELECT current_schema()")), self.schema)
            tables = db.execute(text(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = :schema"
            ), {"schema": self.schema}).scalars().all()
        self.assertCountEqual(tables, [
            "users", "teams", "team_members", "team_member_audit_logs", "tickets", "ticket_assignment_audit_logs", "brands",
        ])

    def test_two_team_memberships_return_the_requested_role_and_user_id(self):
        headers = self.headers()
        for team_id, member_id, role in (
            (self.team_a_id, self.member_a_id, "admin"),
            (self.team_b_id, self.member_b_id, "employee"),
        ):
            with self.subTest(role=role):
                response = self.client.get(self.member_path(team_id), headers=headers)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json(), {
                    "user_id": str(self.actor_id), "team_id": str(team_id),
                    "role": role, "status": "active",
                })
                self.assertNotEqual(response.json()["user_id"], str(member_id))
        self.assertEqual(len(self.request_sessions), 2)
        self.assertIsNot(self.request_sessions[0], self.request_sessions[1])
        with self.sessions() as db:
            self.assertEqual(db.get(self.TeamMember, self.member_a_id).role, "admin")
            self.assertEqual(db.get(self.TeamMember, self.member_b_id).role, "employee")

    def test_administrator_authority_does_not_cross_team_boundaries(self):
        headers = self.headers()
        allowed = self.client.post(self.admin_path(), headers=headers)
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertEqual(allowed.json()["role"], "admin")
        self.assert_error(
            self.client.post(self.admin_path(self.team_b_id), headers=headers),
            403, "ADMIN_REQUIRED",
        )
        self.assertEqual(self.executed, [("admin", self.team_a_id)])

    def test_missing_removed_and_unknown_team_memberships_stop_business_execution(self):
        for user_id, team_id in (
            (self.outsider_id, self.team_a_id),
            (self.removed_id, self.team_a_id),
            (self.actor_id, uuid4()),
        ):
            with self.subTest(user_id=str(user_id), team_id=str(team_id)):
                headers = self.headers(user_id)
                self.assert_error(
                    self.client.get(self.member_path(team_id), headers=headers),
                    403, "TEAM_ACCESS_DENIED",
                )
                for path in (self.probe_path(team_id), self.admin_path(team_id)):
                    self.assert_error(
                        self.client.post(path, headers=headers), 403, "TEAM_ACCESS_DENIED"
                    )
        self.assertEqual(self.executed, [])

    def test_disabled_account_cannot_use_active_admin_membership(self):
        headers = self.headers(self.disabled_id)
        self.assert_error(
            self.client.get(self.member_path(), headers=headers), 403, "ACCOUNT_DISABLED"
        )
        for path in (self.admin_path(), self.probe_path()):
            self.assert_error(self.client.post(path, headers=headers), 403, "ACCOUNT_DISABLED")
        self.assertEqual(self.executed, [])

    def test_missing_invalid_expired_refresh_and_unknown_user_tokens_are_rejected(self):
        expired = jwt.encode(
            {"sub": str(self.actor_id), "token_type": "access",
             "exp": datetime.now(timezone.utc) - timedelta(minutes=1)},
            self.security.settings.jwt_secret_key, algorithm=self.security.settings.jwt_algorithm,
        )
        cases = (
            ({}, None),
            ({"Authorization": "Bearer not-a-token"}, "INVALID_TOKEN"),
            ({"Authorization": f"Bearer {expired}"}, "TOKEN_EXPIRED"),
            ({"Authorization": f"Bearer {self.security.create_refresh_token(self.actor_id)}"}, "INVALID_TOKEN"),
            (self.headers(uuid4()), "INVALID_TOKEN"),
        )
        for headers, code in cases:
            with self.subTest(code=code):
                for method, path in (
                    ("GET", self.member_path()), ("POST", self.probe_path()),
                    ("GET", self.available_path()),
                    ("GET", "/api/team"), ("POST", self.members_path()),
                    ("PATCH", f"{self.members_path()}/{self.outsider_id}/role"),
                    ("DELETE", f"{self.members_path()}/{self.outsider_id}"),
                    ("POST", self.tickets_path()),
                ):
                    response = self.client.request(method, path, headers=headers)
                    self.assertEqual(response.status_code, 401, response.text)
                    self.assertEqual(response.headers["www-authenticate"], "Bearer")
                    if code:
                        self.assertEqual(response.json()["code"], code)
        self.assertEqual(self.executed, [])

    def test_available_members_allows_admin_and_employee_with_the_requested_team_role(self):
        headers = self.headers()
        for team_id, role in ((self.team_a_id, "admin"), (self.team_b_id, "employee")):
            with self.subTest(team_id=str(team_id), role=role):
                response = self.client.get(self.available_path(team_id), headers=headers)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json(), [{
                    "user_id": str(self.actor_id),
                    "email": self.actor_email,
                    "nickname": "Team actor",
                    "role": role,
                }])

    def test_available_members_never_leaks_another_teams_candidates(self):
        candidate = self.add_available_members(1, team_id=self.team_b_id)[0]
        headers = self.headers()
        team_a = self.client.get(self.available_path(), headers=headers)
        team_b = self.client.get(self.available_path(self.team_b_id), headers=headers)
        self.assertEqual(team_a.status_code, 200, team_a.text)
        self.assertEqual(team_b.status_code, 200, team_b.text)
        self.assertEqual({row["user_id"] for row in team_a.json()}, {str(self.actor_id)})
        self.assertEqual(
            {row["user_id"] for row in team_b.json()},
            {str(self.actor_id), str(candidate["user_id"])},
        )
        self.assertNotIn(candidate["email"], team_a.text)

    def test_available_members_excludes_removed_members_and_disabled_accounts(self):
        with self.sessions() as db:
            removed = db.scalar(select(self.TeamMember).where(
                self.TeamMember.team_id == self.team_a_id,
                self.TeamMember.user_id == self.removed_id,
            ))
            disabled = db.scalar(select(self.TeamMember).where(
                self.TeamMember.team_id == self.team_a_id,
                self.TeamMember.user_id == self.disabled_id,
            ))
            self.assertEqual(removed.status, "removed")
            self.assertEqual(disabled.status, "active")
            self.assertFalse(db.get(self.User, self.disabled_id).is_active)
        response = self.client.get(self.available_path(), headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        ids = {row["user_id"] for row in response.json()}
        self.assertEqual(ids, {str(self.actor_id)})
        self.assertNotIn(str(self.removed_id), ids)
        self.assertNotIn(str(self.disabled_id), ids)

    def test_available_members_denies_outsiders_removed_and_disabled_callers(self):
        for user_id, team_id, code in (
            (self.outsider_id, self.team_a_id, "TEAM_ACCESS_DENIED"),
            (self.removed_id, self.team_a_id, "TEAM_ACCESS_DENIED"),
            (self.disabled_id, self.team_a_id, "ACCOUNT_DISABLED"),
            (self.actor_id, uuid4(), "TEAM_ACCESS_DENIED"),
        ):
            with self.subTest(user_id=str(user_id), code=code):
                self.assert_error(
                    self.client.get(self.available_path(team_id), headers=self.headers(user_id)),
                    403, code,
                )

    def test_available_members_exposes_only_the_four_assignment_fields(self):
        candidate = self.add_available_members(1)[0]
        response = self.client.get(self.available_path(), headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()), 2)
        for row in response.json():
            self.assertEqual(set(row), {"user_id", "email", "nickname", "role"})
        candidate_row = next(
            row for row in response.json() if row["user_id"] == str(candidate["user_id"])
        )
        self.assertEqual(candidate_row, {
            "user_id": str(candidate["user_id"]), "email": candidate["email"],
            "nickname": None, "role": "employee",
        })
        self.assertNotIn(self.password_hash, response.text)
        self.assertNotIn("team-auth-google-subject", response.text)
        self.assertNotIn(str(candidate["membership_id"]), response.text)

    def test_available_members_returns_a_stable_full_list_when_join_times_are_equal(self):
        joined = datetime(2020, 1, 1, tzinfo=timezone.utc)
        with self.sessions.begin() as db:
            db.get(self.TeamMember, self.member_a_id).join_at = joined
        candidates = self.add_available_members(4, join_at=joined)
        expected = sorted(
            [(self.member_a_id, self.actor_id)]
            + [(row["membership_id"], row["user_id"]) for row in candidates],
            key=lambda row: row[0].int,
        )
        headers = self.headers()
        response = self.client.get(self.available_path(), headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        actual = [row["user_id"] for row in response.json()]
        self.assertEqual(actual, [str(user_id) for _, user_id in expected])
        self.assertEqual(len(actual), len(set(actual)))
        repeated = self.client.get(self.available_path(), headers=headers)
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(repeated.json(), response.json())

    def test_available_members_orders_by_join_time_before_membership_id(self):
        with self.sessions.begin() as db:
            db.get(self.TeamMember, self.member_a_id).join_at = datetime(
                2022, 1, 1, tzinfo=timezone.utc
            )
        earliest = self.add_available_members(
            1, join_at=datetime(2020, 1, 1, tzinfo=timezone.utc)
        )[0]
        latest = self.add_available_members(
            1, join_at=datetime(2024, 1, 1, tzinfo=timezone.utc)
        )[0]
        response = self.client.get(self.available_path(), headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            [row["user_id"] for row in response.json()],
            [str(earliest["user_id"]), str(self.actor_id), str(latest["user_id"])],
        )

    def test_available_members_returns_more_than_one_hundred_members_without_truncation(self):
        candidates = self.add_available_members(101)
        response = self.client.get(self.available_path(), headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()), 102)
        self.assertEqual(
            {row["user_id"] for row in response.json()},
            {str(self.actor_id)} | {str(candidate["user_id"]) for candidate in candidates},
        )

    def test_available_members_rejects_an_invalid_team_uuid(self):
        malformed = self.client.get(
            "/api/team/not-a-uuid/members/available", headers=self.headers()
        )
        self.assert_error(malformed, 422, "VALIDATION_ERROR")
        self.assertEqual(malformed.json()["detail"][0]["loc"], ["path", "team_id"])
        self.assertNotIn("input", malformed.json()["detail"][0])

    def test_available_members_same_token_observes_membership_removal(self):
        headers = self.headers()
        self.assertEqual(self.client.get(self.available_path(), headers=headers).status_code, 200)
        with self.sessions.begin() as db:
            member = db.get(self.TeamMember, self.member_a_id)
            member.status = "removed"
            member.remove_at = datetime.now(timezone.utc)
        self.assert_error(
            self.client.get(self.available_path(), headers=headers), 403, "TEAM_ACCESS_DENIED"
        )
        other_team = self.client.get(self.available_path(self.team_b_id), headers=headers)
        self.assertEqual(other_team.status_code, 200, other_team.text)
        self.assertEqual(other_team.json()[0]["role"], "employee")

    def test_available_members_same_token_observes_account_deactivation(self):
        headers = self.headers()
        self.assertEqual(self.client.get(self.available_path(), headers=headers).status_code, 200)
        with self.sessions.begin() as db:
            db.get(self.User, self.actor_id).is_active = False
        for team_id in (self.team_a_id, self.team_b_id):
            with self.subTest(team_id=str(team_id)):
                self.assert_error(
                    self.client.get(self.available_path(team_id), headers=headers),
                    403, "ACCOUNT_DISABLED",
                )

    def test_available_members_same_token_observes_role_changes_without_cross_team_effects(self):
        headers = self.headers()
        before = self.client.get(self.available_path(), headers=headers)
        self.assertEqual(before.status_code, 200, before.text)
        self.assertEqual(before.json()[0]["role"], "admin")
        with self.sessions.begin() as db:
            db.get(self.TeamMember, self.member_a_id).role = "employee"
            db.get(self.TeamMember, self.member_b_id).role = "admin"
        for team_id, role in ((self.team_a_id, "employee"), (self.team_b_id, "admin")):
            response = self.client.get(self.available_path(team_id), headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()[0]["user_id"], str(self.actor_id))
            self.assertEqual(response.json()[0]["role"], role)

    def test_available_members_openapi_documents_bearer_security_and_only_the_team_path(self):
        response = self.client.get("/openapi.json")
        self.assertEqual(response.status_code, 200, response.text)
        document = response.json()
        operation = document["paths"]["/api/team/{team_id}/members/available"]["get"]
        self.assertNotIn("requestBody", operation)
        self.assertTrue(operation["security"])
        for requirement in operation["security"]:
            for scheme_name in requirement:
                scheme = document["components"]["securitySchemes"][scheme_name]
                self.assertEqual(scheme["type"], "oauth2")
                self.assertEqual(scheme["flows"]["password"]["tokenUrl"], "/api/auth/login")
        parameters = {item["name"]: item for item in operation["parameters"]}
        self.assertEqual(set(parameters), {"team_id"})
        self.assertEqual(parameters["team_id"]["in"], "path")
        self.assertTrue(parameters["team_id"]["required"])
        self.assertEqual(parameters["team_id"]["schema"]["format"], "uuid")

    def test_invalid_team_uuid_uses_validation_contract_and_old_routes_are_absent(self):
        response = self.client.get("/api/team/not-a-uuid/me", headers=self.headers())
        self.assert_error(response, 422, "VALIDATION_ERROR")
        self.assertEqual(response.json()["detail"][0]["loc"], ["path", "team_id"])
        self.assertNotIn("input", response.json()["detail"][0])
        for path in ("/api/teamme", "/api/team/me"):
            self.assert_error(self.client.get(path, headers=self.headers()), 404, "NOT_FOUND")

    def test_same_token_sees_committed_downgrade_upgrade_and_removal(self):
        headers = self.headers()
        self.assertEqual(self.client.post(self.admin_path(), headers=headers).status_code, 200)
        with self.sessions.begin() as db:
            db.get(self.TeamMember, self.member_a_id).role = "employee"
        with self.sessions() as fresh:
            self.assertEqual(fresh.get(self.TeamMember, self.member_a_id).role, "employee")
        self.assert_error(
            self.client.post(self.admin_path(), headers=headers), 403, "ADMIN_REQUIRED"
        )
        employee = self.client.get(self.member_path(), headers=headers)
        self.assertEqual(employee.status_code, 200, employee.text)
        self.assertEqual(employee.json()["role"], "employee")
        with self.sessions.begin() as db:
            db.get(self.TeamMember, self.member_a_id).role = "admin"
        self.assertEqual(self.client.post(self.admin_path(), headers=headers).status_code, 200)
        self.assertEqual(len(self.executed), 2)
        with self.sessions.begin() as db:
            member = db.get(self.TeamMember, self.member_a_id)
            member.status = "removed"
            member.remove_at = datetime.now(timezone.utc)
        with self.sessions() as fresh:
            member = fresh.get(self.TeamMember, self.member_a_id)
            self.assertEqual(member.status, "removed")
            self.assertIsNotNone(member.remove_at)
        self.assert_error(
            self.client.get(self.member_path(), headers=headers), 403, "TEAM_ACCESS_DENIED"
        )
        self.assert_error(
            self.client.post(self.probe_path(), headers=headers), 403, "TEAM_ACCESS_DENIED"
        )
        self.assertEqual(len(self.executed), 2)
        # Team B's membership remains valid after removal from A.
        self.assertEqual(
            self.client.get(self.member_path(self.team_b_id), headers=headers).status_code, 200
        )

    def test_same_token_sees_committed_account_deactivation(self):
        headers = self.headers()
        self.assertEqual(self.client.get(self.member_path(), headers=headers).status_code, 200)
        with self.sessions.begin() as db:
            db.get(self.User, self.actor_id).is_active = False
        with self.sessions() as fresh:
            self.assertFalse(fresh.get(self.User, self.actor_id).is_active)
            self.assertEqual(fresh.get(self.TeamMember, self.member_a_id).status, "active")
        for team_id in (self.team_a_id, self.team_b_id):
            self.assert_error(
                self.client.get(self.member_path(team_id), headers=headers),
                403, "ACCOUNT_DISABLED",
            )
        self.assert_error(
            self.client.post(self.probe_path(), headers=headers), 403, "ACCOUNT_DISABLED"
        )
        self.assertEqual(self.executed, [])

    def test_password_login_and_existing_refresh_cookie_reject_disabled_account(self):
        body = {"email": self.actor_email, "password": self.password}
        login = self.client.post("/api/auth/login", json=body)
        self.assertEqual(login.status_code, 200, login.text)
        access_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        self.assertIn("gp_refresh", self.client.cookies)
        self.assertEqual(self.client.post("/api/auth/refresh").status_code, 200)
        with self.sessions.begin() as db:
            db.get(self.User, self.actor_id).is_active = False
        self.assert_error(
            self.client.post("/api/auth/login", json=body), 403, "ACCOUNT_DISABLED"
        )
        self.assert_error(
            self.client.post("/api/auth/refresh"), 403, "ACCOUNT_DISABLED"
        )
        self.assert_error(
            self.client.get(self.member_path(), headers=access_headers), 403, "ACCOUNT_DISABLED"
        )

    def test_google_login_uses_real_database_and_blocks_disabled_account(self):
        profile = {
            "sub": "team-auth-google-subject", "email": self.actor_email, "name": "Actor"
        }
        # Only the external Google identity verifier is replaced; ORM/auth run.
        with patch.object(self.auth_service, "verify_google_id_token", return_value=profile):
            active = self.client.post("/api/auth/google-login", json={"id_token": "test-google-proof"})
            self.assertEqual(active.status_code, 200, active.text)
            with self.sessions.begin() as db:
                db.get(self.User, self.actor_id).is_active = False
            self.assert_error(
                self.client.post("/api/auth/google-login", json={"id_token": "test-google-proof"}),
                403, "ACCOUNT_DISABLED",
            )

    def test_google_login_does_not_link_or_rename_an_unbound_disabled_account(self):
        with self.sessions.begin() as db:
            actor = db.get(self.User, self.actor_id)
            actor.google_sub = None
            actor.nickname = None
            actor.is_active = False
        profile = {
            "sub": "disabled-new-google-subject",
            "email": self.actor_email,
            "name": "Must not be saved",
        }
        with patch.object(self.auth_service, "verify_google_id_token", return_value=profile):
            response = self.client.post(
                "/api/auth/google-login", json={"id_token": "test-google-proof"}
            )
        self.assert_error(response, 403, "ACCOUNT_DISABLED")
        with self.sessions() as fresh:
            actor = fresh.get(self.User, self.actor_id)
            self.assertIsNone(actor.google_sub)
            self.assertIsNone(actor.nickname)
            self.assertFalse(actor.is_active)

    def test_postgresql_rejects_duplicate_membership_and_invalid_roles_or_statuses(self):
        for values, constraint in (
            ({"user_id": self.actor_id, "role": "employee", "status": "active"}, "uq_team_members_team_user"),
            ({"user_id": self.outsider_id, "role": "owner", "status": "active"}, "ck_team_members_role"),
            ({"user_id": self.outsider_id, "role": "employee", "status": "pending"}, "ck_team_members_status"),
        ):
            with self.subTest(constraint=constraint):
                with self.assertRaises(IntegrityError) as caught:
                    with self.sessions.begin() as db:
                        db.add(self.TeamMember(team_id=self.team_a_id, **values))
                        db.flush()
                self.assertEqual(caught.exception.orig.diag.constraint_name, constraint)
        with self.sessions() as fresh:
            self.assertEqual(len(fresh.scalars(select(self.TeamMember)).all()), 4)

    def test_postgresql_rejects_nonexistent_member_user_and_team(self):
        for team_id, user_id in (
            (self.team_a_id, uuid4()), (uuid4(), self.outsider_id)
        ):
            with self.subTest(team_id=str(team_id), user_id=str(user_id)):
                with self.assertRaises(IntegrityError) as caught:
                    with self.sessions.begin() as db:
                        db.add(self.TeamMember(
                            team_id=team_id, user_id=user_id, role="employee", status="active"
                        ))
                        db.flush()
                original = caught.exception.orig
                self.assertEqual(getattr(original, "sqlstate", getattr(original, "pgcode", None)), "23503")


if __name__ == "__main__":
    unittest.main()
