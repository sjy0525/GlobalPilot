"""Real PostgreSQL/JWT checks for adding members and discovering own teams.

Reuse the explicit disposable-URL guard and random-schema fixture. Only database
session dependencies are overridden; auth, services, commits and audit writes run.
"""

import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier, Event

from fastapi.testclient import TestClient
from sqlalchemy import event, select, text

if __package__:
    from . import test_team_authorization_pg as team_authorization
else:
    import test_team_authorization_pg as team_authorization


class TeamMemberManagementPostgresTests(team_authorization.TeamAuthorizationPostgresFixture):
    def post_member(self, *, email=None, team_id=None, headers=None):
        return self.client.post(
            self.members_path(team_id),
            headers=headers if headers is not None else self.headers(),
            json={"email": email if email is not None else self.outsider_email},
        )

    def persisted_members(self, *, user_id=None, team_id=None):
        with self.sessions() as db:
            return db.scalars(select(self.TeamMember).where(
                self.TeamMember.team_id == (team_id or self.team_a_id),
                self.TeamMember.user_id == (user_id or self.outsider_id),
            )).all()

    def audit_logs(self):
        with self.sessions() as db:
            return db.scalars(select(self.TeamMemberAuditLog)).all()

    def assert_no_member_added(self):
        self.assertEqual(self.persisted_members(), [])
        self.assertEqual(self.audit_logs(), [])

    def post_member_from_independent_client(self, headers):
        with TestClient(
            self.app, base_url="https://testserver", raise_server_exceptions=False
        ) as client:
            return client.post(
                self.members_path(), headers=headers, json={"email": self.outsider_email}
            )

    def test_admin_adds_registered_user_as_employee_and_commits_exactly_one_audit(self):
        response = self.post_member()
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json(), {
            "user_id": str(self.outsider_id), "email": self.outsider_email,
            "nickname": None, "role": "employee",
        })
        members = self.persisted_members()
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0].team_id, self.team_a_id)
        self.assertEqual(members[0].role, "employee")
        self.assertEqual(members[0].status, "active")
        self.assertIsNotNone(members[0].join_at)
        self.assertIsNone(members[0].remove_at)
        logs = self.audit_logs()
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].team_id, self.team_a_id)
        self.assertEqual(logs[0].actor_user_id, self.actor_id)
        self.assertEqual(logs[0].target_user_id, self.outsider_id)
        self.assertEqual(logs[0].action, "member_added")
        self.assertEqual(
            (logs[0].before_role, logs[0].after_role,
             logs[0].before_status, logs[0].after_status),
            (None, "employee", None, "active"),
        )
        self.assertIsNotNone(logs[0].create_at)
        self.assertIsNotNone(logs[0].create_at.utcoffset())
        # Both rows must have been inserted by the same PostgreSQL transaction.
        with self.sessions() as db:
            member_tx = db.scalar(text(
                "SELECT xmin::text FROM team_members WHERE id = :id"
            ), {"id": members[0].id})
            audit_tx = db.scalar(text(
                "SELECT xmin::text FROM team_member_audit_logs WHERE id = :id"
            ), {"id": logs[0].id})
        self.assertEqual(member_tx, audit_tx)

    def test_employee_cannot_add_members(self):
        response = self.post_member(team_id=self.team_b_id)
        self.assert_error(response, 403, "ADMIN_REQUIRED")
        self.assertEqual(self.persisted_members(team_id=self.team_b_id), [])
        self.assert_no_member_added()

    def test_foreign_admin_removed_and_disabled_callers_cannot_add_members(self):
        with self.sessions.begin() as db:
            db.add(self.TeamMember(
                team_id=self.team_b_id, user_id=self.outsider_id,
                role="admin", status="active",
            ))
        for user_id, code in (
            (self.outsider_id, "TEAM_ACCESS_DENIED"),
            (self.removed_id, "TEAM_ACCESS_DENIED"),
            (self.disabled_id, "ACCOUNT_DISABLED"),
        ):
            with self.subTest(user_id=str(user_id), code=code):
                self.assert_error(self.post_member(headers=self.headers(user_id)), 403, code)
                self.assert_no_member_added()

    def test_add_member_rejects_an_unregistered_email_without_audit(self):
        self.assert_error(
            self.post_member(email="not-registered@example.com"), 404, "USER_NOT_FOUND"
        )
        self.assert_no_member_added()

    def test_add_member_rejects_a_disabled_target_without_audit(self):
        self.assert_error(self.post_member(email=self.disabled_email), 403, "ACCOUNT_DISABLED")
        self.assert_no_member_added()
        with self.sessions() as db:
            self.assertFalse(db.get(self.User, self.disabled_id).is_active)

    def test_add_member_rejects_an_existing_active_member_without_changing_role(self):
        self.assert_error(self.post_member(email=self.actor_email), 409, "TEAM_MEMBER_EXISTS")
        with self.sessions() as db:
            member = db.get(self.TeamMember, self.member_a_id)
            self.assertEqual(member.role, "admin")
            self.assertEqual(member.status, "active")
        self.assert_no_member_added()

    def test_add_member_does_not_reactivate_an_existing_removed_membership(self):
        removed_at = datetime.now(timezone.utc)
        with self.sessions.begin() as db:
            member = db.scalar(select(self.TeamMember).where(
                self.TeamMember.team_id == self.team_a_id,
                self.TeamMember.user_id == self.removed_id,
            ))
            member.remove_at = removed_at
            historical_member_id = member.id
        self.assert_error(self.post_member(email=self.removed_email), 409, "TEAM_MEMBER_EXISTS")
        with self.sessions() as db:
            member = db.get(self.TeamMember, historical_member_id)
            self.assertEqual(member.status, "removed")
            self.assertEqual(member.role, "admin")
            self.assertEqual(member.remove_at, removed_at)
            rows = db.scalars(select(self.TeamMember).where(
                self.TeamMember.team_id == self.team_a_id,
                self.TeamMember.user_id == self.removed_id,
            )).all()
            self.assertEqual(len(rows), 1)
        self.assert_no_member_added()

    def test_add_member_rejects_role_team_and_actor_injection_in_the_body(self):
        for extra in (
            {"role": "admin"}, {"team_id": str(self.team_b_id)}, {"status": "active"},
            {"user_id": str(self.disabled_id)}, {"actor_user_id": str(self.removed_id)},
        ):
            with self.subTest(field=next(iter(extra))):
                response = self.client.post(
                    self.members_path(), headers=self.headers(),
                    json={"email": self.outsider_email, **extra},
                )
                self.assert_error(response, 422, "VALIDATION_ERROR")
                self.assertIn(
                    ["body", next(iter(extra))],
                    [error["loc"] for error in response.json()["detail"]],
                )
                self.assert_no_member_added()

    def test_add_member_normalizes_email_and_normalized_duplicates_remain_conflicts(self):
        mixed_email = f"  {self.outsider_email.upper()}  "
        response = self.post_member(email=mixed_email)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["email"], self.outsider_email)
        self.assertEqual(response.json()["role"], "employee")
        self.assert_error(self.post_member(email=mixed_email), 409, "TEAM_MEMBER_EXISTS")
        self.assertEqual(len(self.persisted_members()), 1)
        self.assertEqual(len(self.audit_logs()), 1)

    def test_add_member_keeps_the_targets_role_in_another_team_independent(self):
        with self.sessions.begin() as db:
            foreign_member = self.TeamMember(
                team_id=self.team_b_id, user_id=self.outsider_id,
                role="admin", status="active",
            )
            db.add(foreign_member)
            db.flush()
            foreign_member_id = foreign_member.id
        response = self.post_member()
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["role"], "employee")
        with self.sessions() as db:
            foreign = db.get(self.TeamMember, foreign_member_id)
            self.assertEqual(foreign.role, "admin")
            self.assertEqual(foreign.status, "active")
        self.assertEqual(self.persisted_members()[0].role, "employee")
        self.assertEqual([log.team_id for log in self.audit_logs()], [self.team_a_id])

    def test_added_user_can_log_in_and_access_the_team_as_an_employee(self):
        with self.sessions.begin() as db:
            db.get(self.User, self.outsider_id).password_hash = self.password_hash
        old_headers = self.headers(self.outsider_id)
        self.assert_error(
            self.client.get(self.member_path(), headers=old_headers), 403, "TEAM_ACCESS_DENIED"
        )
        self.assertEqual(self.post_member().status_code, 201)
        login = self.client.post("/api/auth/login", json={
            "email": self.outsider_email, "password": self.password,
        })
        self.assertEqual(login.status_code, 200, login.text)
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        membership = self.client.get(self.member_path(), headers=headers)
        self.assertEqual(membership.status_code, 200, membership.text)
        self.assertEqual(membership.json()["user_id"], str(self.outsider_id))
        self.assertEqual(membership.json()["role"], "employee")
        self.assertEqual(membership.json()["status"], "active")
        self.assertEqual(self.client.get(self.member_path(), headers=old_headers).status_code, 200)
        available = self.client.get(self.available_path(), headers=headers)
        self.assertEqual(available.status_code, 200, available.text)
        self.assertIn(str(self.outsider_id), [row["user_id"] for row in available.json()])
        self.assert_error(self.post_member(headers=headers), 403, "ADMIN_REQUIRED")

    def test_audit_insert_failure_rolls_back_the_member_and_audit_together(self):
        constraint = "ck_test_reject_member_audit"
        with self.engine.begin() as conn:
            conn.execute(text(
                f"ALTER TABLE team_member_audit_logs ADD CONSTRAINT {constraint} "
                "CHECK (action <> 'member_added')"
            ))

        def remove_constraint():
            with self.engine.begin() as conn:
                conn.execute(text(
                    f"ALTER TABLE team_member_audit_logs DROP CONSTRAINT {constraint}"
                ))

        self.addCleanup(remove_constraint)
        with self.assertLogs(self.errors.logger, level="ERROR"):
            response = self.post_member()
        self.assert_error(response, 500, "INTERNAL_ERROR")
        self.assert_no_member_added()

    def test_two_concurrent_duplicate_additions_commit_one_member_and_one_audit(self):
        start = Barrier(2)
        lock_attempts = []

        def wait_before_team_lock(conn, cursor, statement, parameters, context, executemany):
            normalized = " ".join(statement.lower().split())
            if normalized.startswith("select") and "from teams" in normalized and "for update" in normalized:
                lock_attempts.append(normalized)
                start.wait(timeout=5)

        headers = self.headers()

        event.listen(self.engine, "before_cursor_execute", wait_before_team_lock)
        try:
            with ThreadPoolExecutor(max_workers=2) as workers:
                futures = [
                    workers.submit(self.post_member_from_independent_client, headers)
                    for _ in range(2)
                ]
                responses = [future.result(timeout=15) for future in futures]
        finally:
            event.remove(self.engine, "before_cursor_execute", wait_before_team_lock)
        self.assertEqual(len(lock_attempts), 2)
        self.assertEqual(sorted(response.status_code for response in responses), [201, 409])
        conflict = next(response for response in responses if response.status_code == 409)
        self.assertEqual(conflict.json()["code"], "TEAM_MEMBER_EXISTS")
        self.assertEqual(len(self.persisted_members()), 1)
        logs = self.audit_logs()
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].actor_user_id, self.actor_id)
        self.assertEqual(logs[0].target_user_id, self.outsider_id)

    def test_waiting_addition_rechecks_admin_role_after_the_team_lock_is_released(self):
        reached_team_lock = Event()

        def observe_team_lock(conn, cursor, statement, parameters, context, executemany):
            normalized = " ".join(statement.lower().split())
            if normalized.startswith("select") and "from teams" in normalized and "for update" in normalized:
                reached_team_lock.set()

        with self.sessions() as lock_holder:
            lock_holder.query(self.Team.id).filter(
                self.Team.id == self.team_a_id
            ).with_for_update().one()
            # The real HTTP request first authenticates as admin, then waits for
            # this transaction's team lock while its role changes in the DB.
            event.listen(self.engine, "before_cursor_execute", observe_team_lock)
            workers = ThreadPoolExecutor(max_workers=1)
            try:
                future = workers.submit(self.post_member_from_independent_client, self.headers())
                self.assertTrue(reached_team_lock.wait(timeout=3))
                lock_holder.get(self.TeamMember, self.member_a_id).role = "employee"
                lock_holder.commit()
                response = future.result(timeout=15)
            finally:
                lock_holder.rollback()
                event.remove(self.engine, "before_cursor_execute", observe_team_lock)
                workers.shutdown(wait=True)
        self.assert_error(response, 403, "ADMIN_REQUIRED")
        self.assert_no_member_added()

    def test_same_admin_token_cannot_add_after_committed_role_or_status_changes(self):
        headers = self.headers()
        for role, status, account_active, code in (
            ("employee", "active", True, "ADMIN_REQUIRED"),
            ("admin", "removed", True, "TEAM_ACCESS_DENIED"),
            ("admin", "active", False, "ACCOUNT_DISABLED"),
        ):
            with self.subTest(code=code):
                with self.sessions.begin() as db:
                    member = db.get(self.TeamMember, self.member_a_id)
                    member.role, member.status = role, status
                    db.get(self.User, self.actor_id).is_active = account_active
                self.assert_error(self.post_member(headers=headers), 403, code)
                self.assert_no_member_added()

    def test_team_discovery_returns_only_own_active_memberships_and_team_specific_roles(self):
        with self.sessions.begin() as db:
            foreign_team = self.Team(name="Private foreign team")
            db.add(foreign_team)
            db.flush()
            db.add(self.TeamMember(
                team_id=foreign_team.id, user_id=self.outsider_id,
                role="admin", status="active",
            ))
            foreign_team_id = foreign_team.id
        response = self.client.get("/api/team", headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsInstance(response.json(), list)
        self.assertEqual(len(response.json()), 2)
        by_team = {row["team_id"]: row for row in response.json()}
        self.assertEqual(set(by_team), {str(self.team_a_id), str(self.team_b_id)})
        for team_id, name, role in (
            (self.team_a_id, "Team A", "admin"), (self.team_b_id, "Team B", "employee")
        ):
            self.assertEqual(by_team[str(team_id)], {
                "team_id": str(team_id), "team_name": name, "user_id": str(self.actor_id),
                "role": role, "status": "active",
            })
        self.assertNotIn(str(foreign_team_id), response.text)
        self.assertNotIn(self.actor_email, response.text)

    def test_team_discovery_returns_empty_for_users_with_no_effective_membership(self):
        for user_id in (self.outsider_id, self.removed_id):
            with self.subTest(user_id=str(user_id)):
                response = self.client.get("/api/team", headers=self.headers(user_id))
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json(), [])

    def test_team_discovery_same_token_observes_role_change_and_membership_removal(self):
        headers = self.headers()
        self.assertEqual(self.client.get("/api/team", headers=headers).status_code, 200)
        with self.sessions.begin() as db:
            db.get(self.TeamMember, self.member_a_id).status = "removed"
            db.get(self.TeamMember, self.member_b_id).role = "admin"
        response = self.client.get("/api/team", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), [{
            "team_id": str(self.team_b_id), "team_name": "Team B",
            "user_id": str(self.actor_id), "role": "admin", "status": "active",
        }])

    def test_team_discovery_rejects_disabled_accounts_even_with_active_memberships(self):
        self.assert_error(
            self.client.get("/api/team", headers=self.headers(self.disabled_id)),
            403, "ACCOUNT_DISABLED",
        )
        headers = self.headers()
        self.assertEqual(self.client.get("/api/team", headers=headers).status_code, 200)
        with self.sessions.begin() as db:
            db.get(self.User, self.actor_id).is_active = False
        self.assert_error(self.client.get("/api/team", headers=headers), 403, "ACCOUNT_DISABLED")

    def test_member_addition_openapi_accepts_only_email_and_requires_bearer_auth(self):
        response = self.client.get("/openapi.json")
        self.assertEqual(response.status_code, 200, response.text)
        document = response.json()
        operation = document["paths"]["/api/team/{team_id}/members"]["post"]
        self.assertTrue(operation["security"])
        self.assertIn("201", operation["responses"])
        self.assertEqual([item["name"] for item in operation["parameters"]], ["team_id"])
        body_schema = operation["requestBody"]["content"]["application/json"]["schema"]
        schema_name = body_schema["$ref"].rsplit("/", 1)[-1]
        schema = document["components"]["schemas"][schema_name]
        self.assertEqual(set(schema["properties"]), {"email"})
        self.assertEqual(schema["required"], ["email"])
        self.assertFalse(schema["additionalProperties"])
        discovered = document["paths"]["/api/team"]["get"]
        self.assertTrue(discovered["security"])
        self.assertNotIn("requestBody", discovered)
        self.assertEqual(discovered.get("parameters", []), [])


if __name__ == "__main__":
    unittest.main()
