"""Real JWT/HTTP and isolated PostgreSQL checks for member role/removal changes."""

import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import event, select, text

if __package__:
    from . import test_team_authorization_pg as team_authorization
else:
    import test_team_authorization_pg as team_authorization


class TeamMemberMutationsPostgresTests(team_authorization.TeamAuthorizationPostgresFixture):
    def setUp(self):
        super().setUp()
        with self.sessions.begin() as db:
            member = self.TeamMember(
                team_id=self.team_a_id, user_id=self.outsider_id,
                role="employee", status="active",
            )
            db.add(member)
            db.flush()
            self.target_member_id = member.id

    def role_path(self, user_id=None, team_id=None):
        return f"{self.members_path(team_id)}/{user_id or self.outsider_id}/role"

    def remove_path(self, user_id=None, team_id=None):
        return f"{self.members_path(team_id)}/{user_id or self.outsider_id}"

    def change_role(self, role, *, user_id=None, team_id=None, headers=None):
        return self.client.patch(
            self.role_path(user_id, team_id),
            headers=headers if headers is not None else self.headers(), json={"role": role},
        )

    def remove_member(self, *, user_id=None, team_id=None, headers=None):
        return self.client.delete(
            self.remove_path(user_id, team_id),
            headers=headers if headers is not None else self.headers(),
        )

    def member(self, user_id=None, team_id=None):
        with self.sessions() as db:
            return db.scalar(select(self.TeamMember).where(
                self.TeamMember.team_id == (team_id or self.team_a_id),
                self.TeamMember.user_id == (user_id or self.outsider_id),
            ))

    def logs(self):
        with self.sessions() as db:
            return db.scalars(select(self.TeamMemberAuditLog)).all()

    def make_target_admin(self):
        with self.sessions.begin() as db:
            db.get(self.TeamMember, self.target_member_id).role = "admin"

    def new_member(self, *, team_id=None, role="employee", status="active", active=True):
        with self.sessions.begin() as db:
            user = self.User(email=f"mutation-{uuid4().hex}@example.com", is_active=active)
            db.add(user)
            db.flush()
            member = self.TeamMember(
                team_id=team_id or self.team_a_id, user_id=user.id,
                role=role, status=status,
            )
            db.add(member)
            db.flush()
            return user.id, member.id

    def effective_admins(self):
        with self.sessions() as db:
            return set(db.scalars(
                select(self.TeamMember.user_id)
                .join(self.User, self.User.id == self.TeamMember.user_id)
                .where(
                    self.TeamMember.team_id == self.team_a_id,
                    self.TeamMember.status == "active", self.TeamMember.role == "admin",
                    self.User.is_active.is_(True),
                )
            ).all())

    def assert_target_unchanged_and_no_audit(self):
        target = self.member()
        self.assertEqual((target.role, target.status, target.remove_at), ("employee", "active", None))
        self.assertEqual(self.logs(), [])

    def assert_snapshot(self, log, *, action, before_role, after_role, before_status, after_status,
                        actor_id=None, target_id=None):
        self.assertEqual(log.team_id, self.team_a_id)
        self.assertEqual(log.actor_user_id, actor_id or self.actor_id)
        self.assertEqual(log.target_user_id, target_id or self.outsider_id)
        self.assertEqual(log.action, action)
        self.assertEqual(
            (log.before_role, log.after_role, log.before_status, log.after_status),
            (before_role, after_role, before_status, after_status),
        )
        self.assertIsNotNone(log.create_at)
        self.assertIsNotNone(log.create_at.utcoffset())

    def assert_same_transaction(self, member_id, audit_id):
        with self.sessions() as db:
            member_tx = db.scalar(text(
                "SELECT xmin::text FROM team_members WHERE id=:id"
            ), {"id": member_id})
            audit_tx = db.scalar(text(
                "SELECT xmin::text FROM team_member_audit_logs WHERE id=:id"
            ), {"id": audit_id})
        self.assertEqual(member_tx, audit_tx)

    def reject_audit_action(self, action):
        self.assertIn(action, {"member_role_changed", "member_removed"})
        constraint = "ck_test_reject_mutation_audit"
        with self.engine.begin() as conn:
            conn.execute(text(
                f"ALTER TABLE team_member_audit_logs ADD CONSTRAINT {constraint} "
                f"CHECK (action <> '{action}')"
            ))

        def remove_constraint():
            with self.engine.begin() as conn:
                conn.execute(text(
                    f"ALTER TABLE team_member_audit_logs DROP CONSTRAINT {constraint}"
                ))

        self.addCleanup(remove_constraint)

    def independent_request(self, *, method, actor_id, target_id, role="employee"):
        path = self.role_path(target_id) if method == "PATCH" else self.remove_path(target_id)
        kwargs = {"json": {"role": role}} if method == "PATCH" else {}
        with TestClient(
            self.app, base_url="https://testserver", raise_server_exceptions=False
        ) as client:
            return client.request(method, path, headers=self.headers(actor_id), **kwargs)

    def concurrent_mutations(self, operations):
        barrier = Barrier(len(operations))
        lock_attempts = []

        def synchronize_team_locks(conn, cursor, statement, parameters, context, executemany):
            normalized = " ".join(statement.lower().split())
            if normalized.startswith("select") and "from teams" in normalized and "for update" in normalized:
                lock_attempts.append(normalized)
                barrier.wait(timeout=5)

        event.listen(self.engine, "before_cursor_execute", synchronize_team_locks)
        try:
            with ThreadPoolExecutor(max_workers=len(operations)) as workers:
                futures = [workers.submit(self.independent_request, **item) for item in operations]
                responses = [future.result(timeout=15) for future in futures]
        finally:
            event.remove(self.engine, "before_cursor_execute", synchronize_team_locks)
        self.assertEqual(len(lock_attempts), len(operations))
        return responses

    def mutation_after_waiting_for_actor_change(self, *, method, actor_status="active", actor_role="admin"):
        reached_lock = Event()

        def observe_lock(conn, cursor, statement, parameters, context, executemany):
            normalized = " ".join(statement.lower().split())
            if normalized.startswith("select") and "from teams" in normalized and "for update" in normalized:
                reached_lock.set()

        with self.sessions() as holder:
            holder.query(self.Team.id).filter(self.Team.id == self.team_a_id).with_for_update().one()
            event.listen(self.engine, "before_cursor_execute", observe_lock)
            workers = ThreadPoolExecutor(max_workers=1)
            try:
                future = workers.submit(
                    self.independent_request, method=method,
                    actor_id=self.actor_id, target_id=self.outsider_id, role="admin",
                )
                self.assertTrue(reached_lock.wait(timeout=3))
                actor = holder.get(self.TeamMember, self.member_a_id)
                actor.role, actor.status = actor_role, actor_status
                holder.commit()
                response = future.result(timeout=15)
            finally:
                holder.rollback()
                event.remove(self.engine, "before_cursor_execute", observe_lock)
                workers.shutdown(wait=True)
        return response

    def test_admin_promotes_employee_and_commits_role_audit_in_the_same_transaction(self):
        response = self.change_role("admin")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {
            "user_id": str(self.outsider_id), "email": self.outsider_email,
            "nickname": None, "role": "admin",
        })
        self.assertNotIn(self.password_hash, response.text)
        target = self.member()
        self.assertEqual((target.role, target.status), ("admin", "active"))
        logs = self.logs()
        self.assertEqual(len(logs), 1)
        self.assert_snapshot(logs[0], action="member_role_changed", before_role="employee",
                             after_role="admin", before_status="active", after_status="active")
        self.assert_same_transaction(target.id, logs[0].id)

    def test_admin_demotes_another_effective_administrator(self):
        self.make_target_admin()
        response = self.change_role("employee")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["role"], "employee")
        self.assertEqual(self.effective_admins(), {self.actor_id})
        logs = self.logs()
        self.assertEqual(len(logs), 1)
        self.assert_snapshot(logs[0], action="member_role_changed", before_role="admin",
                             after_role="employee", before_status="active", after_status="active")

    def test_role_noop_returns_the_dto_without_duplicate_audit(self):
        unchanged = self.change_role("employee")
        self.assertEqual(unchanged.status_code, 200, unchanged.text)
        self.assertEqual(unchanged.json()["role"], "employee")
        self.assertEqual(self.logs(), [])
        promoted = self.change_role("admin")
        self.assertEqual(promoted.status_code, 200, promoted.text)
        before_log = self.logs()[0]
        repeated = self.change_role("admin")
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(repeated.json(), promoted.json())
        self.assertEqual([log.id for log in self.logs()], [before_log.id])

    def test_self_demotion_revokes_old_token_management_but_keeps_employee_business_access(self):
        self.make_target_admin()
        headers = self.headers()
        response = self.change_role("employee", user_id=self.actor_id, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["role"], "employee")
        membership = self.client.get(self.member_path(), headers=headers)
        self.assertEqual(membership.status_code, 200, membership.text)
        self.assertEqual(membership.json()["role"], "employee")
        self.assertEqual(self.client.get(self.available_path(), headers=headers).status_code, 200)
        self.assertEqual(self.client.post(self.probe_path(), headers=headers).status_code, 200)
        self.assert_error(self.change_role("employee", headers=headers), 403, "ADMIN_REQUIRED")
        self.assert_error(self.remove_member(headers=headers), 403, "ADMIN_REQUIRED")
        self.assert_error(self.client.post(
            self.members_path(), headers=headers, json={"email": self.disabled_email}
        ), 403, "ADMIN_REQUIRED")
        self.assertEqual(self.effective_admins(), {self.outsider_id})

    def test_last_effective_admin_cannot_be_demoted_when_only_ineligible_admins_exist(self):
        self.new_member(team_id=self.team_b_id, role="admin")
        # Existing removed and disabled admins in Team A, and a foreign admin,
        # cannot substitute for the one effective administrator of Team A.
        self.assertEqual(self.effective_admins(), {self.actor_id})
        self.assert_error(
            self.change_role("employee", user_id=self.actor_id), 409, "LAST_ADMIN_REQUIRED"
        )
        self.assertEqual(self.member(self.actor_id).role, "admin")
        self.assertEqual(self.logs(), [])

    def test_role_update_rejects_a_disabled_target(self):
        self.assert_error(
            self.change_role("employee", user_id=self.disabled_id), 403, "ACCOUNT_DISABLED"
        )
        self.assertEqual(self.member(self.disabled_id).role, "admin")
        self.assertEqual(self.logs(), [])

    def test_role_update_rejects_missing_removed_and_foreign_only_targets(self):
        foreign_id, _ = self.new_member(team_id=self.team_b_id)
        for user_id in (uuid4(), self.removed_id, foreign_id):
            with self.subTest(user_id=str(user_id)):
                self.assert_error(
                    self.change_role("admin", user_id=user_id), 404, "TEAM_MEMBER_NOT_FOUND"
                )
        self.assert_target_unchanged_and_no_audit()

    def test_role_update_keeps_the_targets_other_team_role_independent(self):
        self.make_target_admin()
        with self.sessions.begin() as db:
            db.add(self.TeamMember(
                team_id=self.team_b_id, user_id=self.outsider_id, role="admin", status="active"
            ))
        response = self.change_role("employee")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.member().role, "employee")
        foreign = self.member(self.outsider_id, self.team_b_id)
        self.assertEqual((foreign.role, foreign.status), ("admin", "active"))
        self.assertEqual([log.team_id for log in self.logs()], [self.team_a_id])

    def test_role_body_rejects_unknown_roles_and_injected_fields(self):
        bodies = [
            {}, {"role": "owner"}, {"role": "reviewer"}, {"role": "ADMIN"},
            {"role": None}, {"role": 1}, {"role": ["admin"]},
            {"role": "admin", "team_id": str(self.team_b_id)},
            {"role": "admin", "user_id": str(self.actor_id)},
            {"role": "admin", "actor_user_id": str(self.actor_id)},
            {"role": "admin", "status": "removed"},
        ]
        for body in bodies:
            with self.subTest(body=body):
                response = self.client.patch(self.role_path(), headers=self.headers(), json=body)
                self.assert_error(response, 422, "VALIDATION_ERROR")
                self.assertTrue(all("input" not in error for error in response.json()["detail"]))
                self.assert_target_unchanged_and_no_audit()

    def test_unauthorized_callers_cannot_update_roles_or_remove_members(self):
        foreign_admin_id, _ = self.new_member(team_id=self.team_b_id, role="admin")
        with self.sessions.begin() as db:
            unknown = self.User(email=f"no-team-{uuid4().hex}@example.com", is_active=True)
            db.add(unknown)
            db.flush()
            unknown_id = unknown.id
        for user_id, code in (
            (self.outsider_id, "ADMIN_REQUIRED"),
            (unknown_id, "TEAM_ACCESS_DENIED"),
            (foreign_admin_id, "TEAM_ACCESS_DENIED"),
            (self.removed_id, "TEAM_ACCESS_DENIED"),
            (self.disabled_id, "ACCOUNT_DISABLED"),
        ):
            with self.subTest(user_id=str(user_id), code=code):
                headers = self.headers(user_id)
                self.assert_error(self.change_role("admin", headers=headers), 403, code)
                self.assert_error(self.remove_member(headers=headers), 403, code)
                self.assert_target_unchanged_and_no_audit()
        self.assert_error(
            self.change_role("admin", team_id=self.team_b_id), 403, "ADMIN_REQUIRED"
        )
        self.assert_error(self.remove_member(team_id=self.team_b_id), 403, "ADMIN_REQUIRED")

    def test_remove_soft_deletes_membership_and_records_snapshots_without_deleting_user(self):
        before = self.member()
        response = self.remove_member()
        self.assertEqual(response.status_code, 204, response.text)
        self.assertEqual(response.content, b"")
        removed = self.member()
        self.assertEqual(removed.id, self.target_member_id)
        self.assertEqual((removed.role, removed.status), ("employee", "removed"))
        self.assertEqual(removed.join_at, before.join_at)
        self.assertIsNotNone(removed.remove_at)
        self.assertIsNotNone(removed.remove_at.utcoffset())
        with self.sessions() as db:
            user = db.get(self.User, self.outsider_id)
            self.assertEqual(user.email, self.outsider_email)
            self.assertTrue(user.is_active)
        logs = self.logs()
        self.assertEqual(len(logs), 1)
        self.assert_snapshot(logs[0], action="member_removed", before_role="employee",
                             after_role="employee", before_status="active", after_status="removed")
        self.assert_same_transaction(removed.id, logs[0].id)

    def test_self_removal_with_another_admin_blocks_old_token_only_in_that_team(self):
        self.make_target_admin()
        headers = self.headers()
        response = self.remove_member(user_id=self.actor_id, headers=headers)
        self.assertEqual(response.status_code, 204, response.text)
        self.assertEqual(response.content, b"")
        for path in (self.member_path(), self.available_path()):
            self.assert_error(self.client.get(path, headers=headers), 403, "TEAM_ACCESS_DENIED")
        other = self.client.get(self.member_path(self.team_b_id), headers=headers)
        self.assertEqual(other.status_code, 200, other.text)
        self.assertEqual(other.json()["role"], "employee")
        discovered = self.client.get("/api/team", headers=headers)
        self.assertEqual(discovered.status_code, 200, discovered.text)
        self.assertEqual([row["team_id"] for row in discovered.json()], [str(self.team_b_id)])
        self.assertEqual(self.effective_admins(), {self.outsider_id})
        with self.sessions() as db:
            self.assertTrue(db.get(self.User, self.actor_id).is_active)

    def test_last_effective_admin_cannot_be_removed(self):
        self.assertEqual(self.effective_admins(), {self.actor_id})
        self.assert_error(self.remove_member(user_id=self.actor_id), 409, "LAST_ADMIN_REQUIRED")
        actor = self.member(self.actor_id)
        self.assertEqual((actor.role, actor.status, actor.remove_at), ("admin", "active", None))
        self.assertEqual(self.logs(), [])

    def test_remove_allows_a_disabled_target_and_keeps_the_account_disabled(self):
        response = self.remove_member(user_id=self.disabled_id)
        self.assertEqual(response.status_code, 204, response.text)
        self.assertEqual(self.member(self.disabled_id).status, "removed")
        with self.sessions() as db:
            self.assertFalse(db.get(self.User, self.disabled_id).is_active)
        self.assertEqual(self.effective_admins(), {self.actor_id})
        self.assertEqual(len(self.logs()), 1)

    def test_remove_missing_or_already_removed_members_returns_404_without_an_audit(self):
        for user_id in (uuid4(), self.removed_id):
            with self.subTest(user_id=str(user_id)):
                self.assert_error(
                    self.remove_member(user_id=user_id), 404, "TEAM_MEMBER_NOT_FOUND"
                )
        self.assert_target_unchanged_and_no_audit()
        first = self.remove_member()
        self.assertEqual(first.status_code, 204, first.text)
        removed_at = self.member().remove_at
        log_id = self.logs()[0].id
        self.assert_error(self.remove_member(), 404, "TEAM_MEMBER_NOT_FOUND")
        self.assertEqual(self.member().remove_at, removed_at)
        self.assertEqual([log.id for log in self.logs()], [log_id])

    def test_remove_does_not_change_the_targets_other_team_membership_or_account(self):
        with self.sessions.begin() as db:
            db.add(self.TeamMember(
                team_id=self.team_b_id, user_id=self.outsider_id, role="admin", status="active"
            ))
        headers = self.headers(self.outsider_id)
        response = self.remove_member()
        self.assertEqual(response.status_code, 204, response.text)
        other = self.member(self.outsider_id, self.team_b_id)
        self.assertEqual((other.role, other.status, other.remove_at), ("admin", "active", None))
        self.assert_error(
            self.client.get(self.member_path(), headers=headers), 403, "TEAM_ACCESS_DENIED"
        )
        self.assertEqual(
            self.client.get(self.member_path(self.team_b_id), headers=headers).status_code, 200
        )
        with self.sessions() as db:
            self.assertTrue(db.get(self.User, self.outsider_id).is_active)

    def test_remove_preserves_previous_role_audits(self):
        self.assertEqual(self.change_role("admin").status_code, 200)
        prior = self.logs()[0]
        before = (prior.id, prior.action, prior.before_role, prior.after_role,
                  prior.before_status, prior.after_status, prior.create_at)
        response = self.remove_member()
        self.assertEqual(response.status_code, 204, response.text)
        logs = self.logs()
        self.assertEqual(len(logs), 2)
        original = next(log for log in logs if log.id == prior.id)
        self.assertEqual(
            (original.id, original.action, original.before_role, original.after_role,
             original.before_status, original.after_status, original.create_at), before,
        )
        removed = next(log for log in logs if log.action == "member_removed")
        self.assert_snapshot(removed, action="member_removed", before_role="admin",
                             after_role="admin", before_status="active", after_status="removed")

    def test_role_audit_insert_failure_rolls_back_the_role_change(self):
        self.reject_audit_action("member_role_changed")
        with self.assertLogs(self.errors.logger, level="ERROR"):
            response = self.change_role("admin")
        self.assert_error(response, 500, "INTERNAL_ERROR")
        self.assert_target_unchanged_and_no_audit()

    def test_remove_audit_insert_failure_rolls_back_the_status_change(self):
        self.reject_audit_action("member_removed")
        with self.assertLogs(self.errors.logger, level="ERROR"):
            response = self.remove_member()
        self.assert_error(response, 500, "INTERNAL_ERROR")
        self.assert_target_unchanged_and_no_audit()

    def test_two_admins_concurrently_demoting_themselves_keep_one_effective_admin(self):
        self.make_target_admin()
        responses = self.concurrent_mutations([
            {"method": "PATCH", "actor_id": user_id, "target_id": user_id, "role": "employee"}
            for user_id in (self.actor_id, self.outsider_id)
        ])
        self.assertEqual(sorted(response.status_code for response in responses), [200, 409])
        conflict = next(response for response in responses if response.status_code == 409)
        self.assertEqual(conflict.json()["code"], "LAST_ADMIN_REQUIRED")
        self.assertEqual(len(self.effective_admins()), 1)
        self.assertEqual(len(self.logs()), 1)
        self.assertEqual(self.logs()[0].action, "member_role_changed")

    def test_two_admins_concurrently_removing_themselves_keep_one_effective_admin(self):
        self.make_target_admin()
        responses = self.concurrent_mutations([
            {"method": "DELETE", "actor_id": user_id, "target_id": user_id}
            for user_id in (self.actor_id, self.outsider_id)
        ])
        self.assertEqual(sorted(response.status_code for response in responses), [204, 409])
        conflict = next(response for response in responses if response.status_code == 409)
        self.assertEqual(conflict.json()["code"], "LAST_ADMIN_REQUIRED")
        self.assertEqual(len(self.effective_admins()), 1)
        self.assertEqual(len(self.logs()), 1)
        self.assertEqual(self.logs()[0].action, "member_removed")
        self.assertCountEqual(
            [self.member(self.actor_id).status, self.member().status], ["active", "removed"]
        )

    def test_two_admins_concurrent_cross_removals_cannot_remove_both_admins(self):
        self.make_target_admin()
        responses = self.concurrent_mutations([
            {"method": "DELETE", "actor_id": self.actor_id, "target_id": self.outsider_id},
            {"method": "DELETE", "actor_id": self.outsider_id, "target_id": self.actor_id},
        ])
        self.assertEqual(sorted(response.status_code for response in responses), [204, 403])
        forbidden = next(response for response in responses if response.status_code == 403)
        self.assertEqual(forbidden.json()["code"], "TEAM_ACCESS_DENIED")
        admins = self.effective_admins()
        self.assertEqual(len(admins), 1)
        logs = self.logs()
        self.assertEqual(len(logs), 1)
        self.assertIn(logs[0].actor_user_id, admins)
        self.assertEqual(self.member(logs[0].target_user_id).status, "removed")

    def test_role_update_rechecks_actor_after_waiting_for_the_team_lock(self):
        response = self.mutation_after_waiting_for_actor_change(method="PATCH", actor_role="employee")
        self.assert_error(response, 403, "ADMIN_REQUIRED")
        self.assert_target_unchanged_and_no_audit()

    def test_remove_rechecks_actor_after_waiting_for_the_team_lock(self):
        response = self.mutation_after_waiting_for_actor_change(method="DELETE", actor_status="removed")
        self.assert_error(response, 403, "TEAM_ACCESS_DENIED")
        self.assert_target_unchanged_and_no_audit()

    def test_invalid_team_or_user_uuid_returns_422_for_both_mutations(self):
        for team_id, user_id in (("not-a-uuid", self.outsider_id), (self.team_a_id, "not-a-uuid")):
            for method in ("PATCH", "DELETE"):
                with self.subTest(method=method, team_id=str(team_id), user_id=str(user_id)):
                    path = self.role_path(user_id, team_id) if method == "PATCH" else self.remove_path(user_id, team_id)
                    kwargs = {"json": {"role": "admin"}} if method == "PATCH" else {}
                    response = self.client.request(method, path, headers=self.headers(), **kwargs)
                    self.assert_error(response, 422, "VALIDATION_ERROR")
                    field = "team_id" if team_id == "not-a-uuid" else "user_id"
                    self.assertIn(["path", field], [item["loc"] for item in response.json()["detail"]])
        self.assert_target_unchanged_and_no_audit()

    def test_mutation_openapi_documents_bearer_role_body_and_no_content_delete(self):
        response = self.client.get("/openapi.json")
        self.assertEqual(response.status_code, 200, response.text)
        document = response.json()
        patch = document["paths"]["/api/team/{team_id}/members/{user_id}/role"]["patch"]
        delete = document["paths"]["/api/team/{team_id}/members/{user_id}"]["delete"]
        for operation in (patch, delete):
            self.assertTrue(operation["security"])
            parameters = {item["name"]: item for item in operation["parameters"]}
            self.assertEqual(set(parameters), {"team_id", "user_id"})
            for parameter in parameters.values():
                self.assertEqual(parameter["in"], "path")
                self.assertTrue(parameter["required"])
                self.assertEqual(parameter["schema"]["format"], "uuid")
        body = patch["requestBody"]["content"]["application/json"]["schema"]
        role_schema = document["components"]["schemas"][body["$ref"].rsplit("/", 1)[-1]]
        self.assertEqual(set(role_schema["properties"]), {"role"})
        self.assertEqual(role_schema["required"], ["role"])
        self.assertEqual(set(role_schema["properties"]["role"]["enum"]), {"admin", "employee"})
        self.assertFalse(role_schema["additionalProperties"])
        output = patch["responses"]["200"]["content"]["application/json"]["schema"]
        output_schema = document["components"]["schemas"][output["$ref"].rsplit("/", 1)[-1]]
        self.assertEqual(set(output_schema["properties"]), {"user_id", "email", "nickname", "role"})
        self.assertNotIn("requestBody", delete)
        self.assertNotIn("content", delete["responses"]["204"])


if __name__ == "__main__":
    unittest.main()
