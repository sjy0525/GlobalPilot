"""Current-ticket visibility for assignment history using real JWT and PG."""

import unittest
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import jwt
from sqlalchemy import event, select, text

if __package__:
    from . import test_team_authorization_pg as team_authorization
else:
    import test_team_authorization_pg as team_authorization


class TicketAssignmentLogsPostgresTests(team_authorization.TeamAuthorizationPostgresFixture):
    fields = {
        "id", "team_id", "ticket_id", "actor_user_id", "before_assignee_id",
        "after_assignee_id", "before_reviewer_id", "after_reviewer_id", "created_at",
    }

    def setUp(self):
        super().setUp()
        self.base_time = datetime(2024, 1, 1, tzinfo=timezone.utc)
        with self.sessions.begin() as db:
            people = [self.User(email=f"logs-{key}@example.com", is_active=True) for key in (
                "worker", "reviewer", "next-worker", "next-reviewer", "history", "coadmin",
            )]
            db.add_all(people)
            db.flush()
            (self.worker_id, self.reviewer_id, self.next_worker_id, self.next_reviewer_id,
             self.history_id, self.coadmin_id) = [person.id for person in people]
            for user_id in (self.outsider_id, *[person.id for person in people]):
                db.add(self.TeamMember(
                    team_id=self.team_a_id, user_id=user_id,
                    role="admin" if user_id == self.coadmin_id else "employee", status="active",
                ))
            db.add(self.TeamMember(
                team_id=self.team_b_id, user_id=self.outsider_id, role="employee", status="active",
            ))
            definitions = (
                ("main", self.team_a_id, self.outsider_id, self.worker_id, self.reviewer_id),
                ("empty", self.team_a_id, self.outsider_id, self.worker_id, self.reviewer_id),
                ("unrelated", self.team_a_id, self.next_worker_id, self.next_worker_id, self.next_reviewer_id),
                ("foreign", self.team_b_id, self.outsider_id, self.outsider_id, self.outsider_id),
                ("other_related", self.team_b_id, self.actor_id, self.outsider_id, self.outsider_id),
            )
            self.tickets = {}
            for key, team_id, creator, worker, reviewer in definitions:
                ticket = self.Ticket(
                    team_id=team_id, title=f"Assignment history {key}", description="Private ticket details",
                    created_by=creator, assignee_id=worker, reviewer_id=reviewer, status="processing",
                    created_at=self.base_time, updated_at=self.base_time,
                )
                db.add(ticket)
                self.tickets[key] = ticket
            db.flush()
            self.main_id = self.tickets["main"].id
            self.initial_logs = [
                self.log(id=UUID(int=300), created_at=self.base_time),
                self.log(id=UUID(int=200), created_at=self.base_time),
                self.log(id=UUID(int=100), created_at=self.base_time + timedelta(seconds=1)),
            ]
            db.add_all(self.initial_logs)
        self.expected_ids = [str(UUID(int=200)), str(UUID(int=300)), str(UUID(int=100))]

    def log(self, *, id=None, ticket_id=None, team_id=None, created_at=None):
        return self.TicketAssignmentAuditLog(
            id=id or uuid4(), team_id=team_id or self.team_a_id,
            ticket_id=ticket_id or self.main_id, actor_user_id=self.history_id,
            before_assignee_id=self.history_id, after_assignee_id=self.worker_id,
            before_reviewer_id=self.history_id, after_reviewer_id=self.reviewer_id,
            created_at=created_at or self.base_time,
        )

    def path(self, ticket_id=None, team_id=None):
        return f"{self.tickets_path(team_id)}/{ticket_id or self.main_id}/assignment-logs"

    def read(self, *, user_id=None, ticket_id=None, team_id=None, headers=None, params=None):
        return self.client.get(
            self.path(ticket_id, team_id), params=params,
            headers=headers if headers is not None else self.headers(user_id or self.outsider_id),
        )

    def assert_logs(self, response, expected_ids=None):
        self.assertEqual(response.status_code, 200, response.text)
        rows = response.json()
        self.assertIsInstance(rows, list)
        self.assertEqual([row["id"] for row in rows], self.expected_ids if expected_ids is None else expected_ids)
        for row in rows:
            self.assertEqual(set(row), self.fields)
            self.assertEqual(row["team_id"], str(self.team_a_id))
            self.assertEqual(row["ticket_id"], str(self.main_id))
            self.assertIsNotNone(datetime.fromisoformat(row["created_at"]).utcoffset())
        self.assertNotIn(self.password_hash, response.text)
        self.assertNotIn("Private ticket details", response.text)
        return rows

    def reassign(self):
        with self.sessions() as db:
            stamp = db.get(self.Ticket, self.main_id).updated_at
        response = self.client.patch(
            f"{self.tickets_path()}/{self.main_id}/assignment", headers=self.headers(),
            json={"assignee_id": str(self.next_worker_id), "reviewer_id": str(self.next_reviewer_id),
                  "expected_updated_at": stamp.isoformat()},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def test_creator_can_read_all_nine_history_fields_without_being_assigned(self):
        rows = self.assert_logs(self.read())
        for row in rows:
            self.assertEqual(row["actor_user_id"], str(self.history_id))
            self.assertEqual(row["before_assignee_id"], str(self.history_id))
            self.assertEqual(row["after_assignee_id"], str(self.worker_id))
            self.assertEqual(row["before_reviewer_id"], str(self.history_id))
            self.assertEqual(row["after_reviewer_id"], str(self.reviewer_id))

    def test_current_assignee_can_read_history(self):
        self.assert_logs(self.read(user_id=self.worker_id))

    def test_current_reviewer_can_read_history(self):
        self.assert_logs(self.read(user_id=self.reviewer_id))

    def test_team_admin_can_read_history_without_a_ticket_relationship(self):
        self.assert_logs(self.read(user_id=self.actor_id))

    def test_historical_actor_or_assignment_identity_does_not_grant_access(self):
        self.assert_error(self.read(user_id=self.history_id), 404, "TICKET_DETAIL_NOT_FOUND")

    def test_invisible_foreign_and_nonexistent_tickets_have_the_same_not_found_payload(self):
        responses = [
            self.read(ticket_id=self.tickets["unrelated"].id),
            self.read(ticket_id=self.tickets["foreign"].id),
            self.read(ticket_id=uuid4()),
            self.read(team_id=self.team_b_id),
        ]
        for response in responses:
            self.assert_error(response, 404, "TICKET_DETAIL_NOT_FOUND")
            self.assertEqual(response.json(), {"detail": "工单不存在", "code": "TICKET_DETAIL_NOT_FOUND"})

    def test_admin_role_in_one_team_does_not_expand_another_team_employee_scope(self):
        self.assert_error(self.read(
            user_id=self.actor_id, ticket_id=self.tickets["foreign"].id, team_id=self.team_b_id,
        ), 404, "TICKET_DETAIL_NOT_FOUND")
        response = self.read(user_id=self.actor_id, ticket_id=self.tickets["other_related"].id, team_id=self.team_b_id)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), [])

    def test_visible_ticket_without_logs_returns_an_empty_array_for_every_read_relationship(self):
        for user_id in (self.outsider_id, self.worker_id, self.reviewer_id, self.actor_id):
            with self.subTest(user_id=user_id):
                response = self.read(user_id=user_id, ticket_id=self.tickets["empty"].id)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json(), [])

    def test_history_is_sorted_by_created_time_then_id_ascending(self):
        rows = self.assert_logs(self.read())
        self.assertEqual(rows[0]["created_at"], rows[1]["created_at"])
        self.assertLess(UUID(rows[0]["id"]), UUID(rows[1]["id"]))
        self.assertLess(datetime.fromisoformat(rows[1]["created_at"]), datetime.fromisoformat(rows[2]["created_at"]))

    def test_all_history_is_returned_without_a_default_or_query_parameter_page_limit(self):
        with self.sessions.begin() as db:
            extra = [self.log(created_at=self.base_time + timedelta(minutes=index + 1)) for index in range(101)]
            db.add_all(extra)
        expected = self.expected_ids + [str(row.id) for row in extra]
        self.assert_logs(self.read(params={"limit": 1, "offset": 100}), expected)

    def test_both_ticket_and_team_identifiers_scope_history_even_for_inconsistent_legacy_rows(self):
        with self.sessions.begin() as db:
            db.add_all([
                self.log(team_id=self.team_b_id),
                self.log(ticket_id=self.tickets["unrelated"].id),
                self.log(team_id=self.team_b_id, ticket_id=self.tickets["foreign"].id),
                self.log(ticket_id=self.tickets["foreign"].id),
            ])
        self.assert_logs(self.read())
        response = self.read(ticket_id=self.tickets["foreign"].id, team_id=self.team_b_id)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(response.json()[0]["team_id"], str(self.team_b_id))
        self.assertEqual(response.json()[0]["ticket_id"], str(self.tickets["foreign"].id))

    def test_the_same_jwt_observes_role_downgrade_and_promotion_on_later_requests(self):
        headers = self.headers()
        self.assert_logs(self.read(headers=headers))
        path = f"{self.members_path()}/{self.actor_id}/role"
        for role, expected_status in (("employee", 404), ("admin", 200)):
            changed = self.client.patch(path, headers=self.headers(self.coadmin_id), json={"role": role})
            self.assertEqual(changed.status_code, 200, changed.text)
            response = self.read(headers=headers)
            if expected_status == 404:
                self.assert_error(response, 404, "TICKET_DETAIL_NOT_FOUND")
            else:
                self.assert_logs(response)

    def test_the_same_jwt_is_denied_after_team_removal_without_losing_other_team_membership(self):
        headers = self.headers()
        self.assert_logs(self.read(headers=headers))
        removed = self.client.delete(f"{self.members_path()}/{self.actor_id}", headers=self.headers(self.coadmin_id))
        self.assertEqual(removed.status_code, 204, removed.text)
        self.assert_error(self.read(headers=headers), 403, "TEAM_ACCESS_DENIED")
        other = self.read(headers=headers, ticket_id=self.tickets["other_related"].id, team_id=self.team_b_id)
        self.assertEqual(other.status_code, 200, other.text)
        self.assertEqual(other.json(), [])

    def test_the_same_jwt_is_denied_after_account_disabling(self):
        headers = self.headers(self.worker_id)
        self.assert_logs(self.read(headers=headers))
        with self.sessions.begin() as db:
            db.get(self.User, self.worker_id).is_active = False
        denied = self.read(headers=headers)
        self.assert_error(denied, 403, "ACCOUNT_DISABLED")
        self.assertEqual(denied.headers["cache-control"], "no-store")

    def test_removed_disabled_and_nonmembers_cannot_read_team_history(self):
        for user_id, code in (
            (self.removed_id, "TEAM_ACCESS_DENIED"), (self.disabled_id, "ACCOUNT_DISABLED"),
        ):
            self.assert_error(self.read(user_id=user_id), 403, code)
        with self.sessions.begin() as db:
            user = self.User(email="logs-no-team@example.com", is_active=True)
            db.add(user)
            db.flush()
            user_id = user.id
        self.assert_error(self.read(user_id=user_id), 403, "TEAM_ACCESS_DENIED")

    def test_reassignment_revokes_old_relationships_grants_new_ones_and_retains_creator_access(self):
        for user_id in (self.worker_id, self.reviewer_id):
            self.assert_logs(self.read(user_id=user_id))
        for user_id in (self.next_worker_id, self.next_reviewer_id):
            self.assert_error(self.read(user_id=user_id), 404, "TICKET_DETAIL_NOT_FOUND")
        self.reassign()
        for user_id in (self.worker_id, self.reviewer_id):
            self.assert_error(self.read(user_id=user_id), 404, "TICKET_DETAIL_NOT_FOUND")
        for user_id in (self.outsider_id, self.next_worker_id, self.next_reviewer_id):
            response = self.read(user_id=user_id)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(len(response.json()), 4)
            latest = response.json()[-1]
            self.assertEqual(latest["actor_user_id"], str(self.actor_id))
            self.assertEqual(latest["before_assignee_id"], str(self.worker_id))
            self.assertEqual(latest["after_assignee_id"], str(self.next_worker_id))
            self.assertEqual(latest["before_reviewer_id"], str(self.reviewer_id))
            self.assertEqual(latest["after_reviewer_id"], str(self.next_reviewer_id))

    def test_soft_removal_of_historical_people_preserves_all_history_and_user_records(self):
        before = self.read().json()
        removed = self.client.delete(f"{self.members_path()}/{self.history_id}", headers=self.headers())
        self.assertEqual(removed.status_code, 204, removed.text)
        self.assertEqual(self.read().json(), before)
        with self.sessions() as db:
            self.assertIsNotNone(db.get(self.User, self.history_id))
            member = db.scalar(select(self.TeamMember).where(
                self.TeamMember.team_id == self.team_a_id, self.TeamMember.user_id == self.history_id,
            ))
            self.assertEqual(member.status, "removed")
            self.assertIsNotNone(member.remove_at)
            self.assertEqual(len(db.scalars(select(self.TicketAssignmentAuditLog)).all()), 3)

    def test_get_never_writes_or_commits_and_preserves_ticket_and_history_versions(self):
        def versions():
            with self.sessions() as db:
                return {
                    table: [tuple(row) for row in db.execute(text(f"SELECT id::text, xmin::text FROM {table} ORDER BY id"))]
                    for table in ("tickets", "ticket_assignment_audit_logs")
                }

        before = versions()
        statements, commits = [], []

        def observe_sql(conn, cursor, statement, parameters, context, executemany):
            statements.append(" ".join(statement.lower().split()))

        def observe_commit(conn):
            commits.append(True)

        event.listen(self.engine, "before_cursor_execute", observe_sql)
        event.listen(self.engine, "commit", observe_commit)
        try:
            self.assert_logs(self.read())
        finally:
            event.remove(self.engine, "before_cursor_execute", observe_sql)
            event.remove(self.engine, "commit", observe_commit)
        self.assertTrue(statements)
        self.assertTrue(all(statement.startswith("select ") for statement in statements), statements)
        self.assertEqual(commits, [])
        self.assertEqual(versions(), before)

    def test_history_read_requires_a_valid_access_jwt(self):
        expired = jwt.encode(
            {"sub": str(self.worker_id), "token_type": "access", "exp": datetime.now(timezone.utc) - timedelta(minutes=1)},
            self.security.settings.jwt_secret_key, algorithm=self.security.settings.jwt_algorithm,
        )
        for headers, code in (
            ({}, "UNAUTHORIZED"), ({"Authorization": "Bearer invalid"}, "INVALID_TOKEN"),
            ({"Authorization": f"Bearer {expired}"}, "TOKEN_EXPIRED"),
            ({"Authorization": f"Bearer {self.security.create_refresh_token(self.worker_id)}"}, "INVALID_TOKEN"),
            (self.headers(uuid4()), "INVALID_TOKEN"),
        ):
            with self.subTest(code=code):
                self.assert_error(self.read(headers=headers), 401, code)

    def test_invalid_team_or_ticket_uuid_is_a_validation_error(self):
        for path, field in (
            (f"/api/team/not-a-uuid/tickets/{self.main_id}/assignment-logs", "team_id"),
            (f"{self.tickets_path()}/not-a-uuid/assignment-logs", "ticket_id"),
        ):
            response = self.client.get(path, headers=self.headers())
            self.assert_error(response, 422, "VALIDATION_ERROR")
            self.assertIn(["path", field], [error["loc"] for error in response.json()["detail"]])

    def test_openapi_declares_only_uuid_path_parameters_bearer_and_the_exact_log_response(self):
        response = self.client.get("/openapi.json")
        self.assertEqual(response.status_code, 200, response.text)
        document = response.json()
        operation = document["paths"]["/api/team/{team_id}/tickets/{ticket_id}/assignment-logs"]["get"]
        self.assertTrue(operation["security"])
        self.assertNotIn("requestBody", operation)
        self.assertEqual({parameter["name"] for parameter in operation["parameters"]}, {"team_id", "ticket_id"})
        for parameter in operation["parameters"]:
            self.assertEqual(parameter["in"], "path")
            self.assertTrue(parameter["required"])
            self.assertEqual(parameter["schema"]["format"], "uuid")
        result = operation["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(result["type"], "array")
        schema = document["components"]["schemas"][result["items"]["$ref"].rsplit("/", 1)[-1]]
        self.assertEqual(set(schema["properties"]), self.fields)
        self.assertEqual(set(schema["required"]), self.fields)

    def test_direct_service_refreshes_a_cached_admin_role_before_reading_history(self):
        with self.sessions() as reader:
            cached = reader.get(self.TeamMember, self.member_a_id)
            first = self.ticket_service.get_ticket_assignment_logs(
                reader, team_id=self.team_a_id, actor_user_id=self.actor_id, ticket_id=self.main_id,
            )
            self.assertEqual(len(first), 3)
            with self.sessions.begin() as writer:
                writer.get(self.TeamMember, self.member_a_id).role = "employee"
            self.assertEqual(cached.role, "admin")
            with self.assertRaises(self.errors.AppHTTPException) as raised:
                self.ticket_service.get_ticket_assignment_logs(
                    reader, team_id=self.team_a_id, actor_user_id=self.actor_id, ticket_id=self.main_id,
                )
            self.assertEqual((raised.exception.status_code, raised.exception.code), (404, "TICKET_DETAIL_NOT_FOUND"))
            self.assertEqual(cached.role, "employee")

    def test_direct_service_rejects_removed_disabled_and_unknown_identities_without_a_route(self):
        for user_id, code in (
            (self.removed_id, "TEAM_ACCESS_DENIED"), (self.disabled_id, "ACCOUNT_DISABLED"), (uuid4(), "TEAM_ACCESS_DENIED"),
        ):
            with self.subTest(code=code), self.sessions() as db:
                with self.assertRaises(self.errors.AppHTTPException) as raised:
                    self.ticket_service.get_ticket_assignment_logs(
                        db, team_id=self.team_a_id, actor_user_id=user_id, ticket_id=self.main_id,
                    )
                self.assertEqual((raised.exception.status_code, raised.exception.code), (403, code))

    def read_with_reassignment_before_log_select(self, user_id):
        injected = []

        def reassign_before_select(conn, cursor, statement, parameters, context, executemany):
            normalized = " ".join(statement.lower().split())
            if injected or not normalized.startswith("select ") or "ticket_assignment_audit_logs" not in normalized:
                return
            injected.append(True)
            with self.sessions() as writer:
                stamp = writer.get(self.Ticket, self.main_id).updated_at
                self.ticket_service.reassign_ticket(
                    writer, team_id=self.team_a_id, actor_user_id=self.actor_id, ticket_id=self.main_id,
                    body=self.ticket_schemas.TicketReassign(
                        assignee_id=self.next_worker_id, reviewer_id=self.next_reviewer_id, expected_updated_at=stamp,
                    ),
                )
                writer.commit()

        event.listen(self.engine, "before_cursor_execute", reassign_before_select)
        try:
            response = self.read(user_id=user_id)
        finally:
            event.remove(self.engine, "before_cursor_execute", reassign_before_select)
        self.assertEqual(injected, [True])
        with self.sessions() as db:
            ticket = db.get(self.Ticket, self.main_id)
            self.assertEqual((ticket.assignee_id, ticket.reviewer_id), (self.next_worker_id, self.next_reviewer_id))
            self.assertEqual(len(db.scalars(select(self.TicketAssignmentAuditLog)).all()), 4)
        return response

    def test_reassignment_committed_after_identity_check_before_select_revokes_old_assignee_access(self):
        response = self.read_with_reassignment_before_log_select(self.worker_id)
        self.assert_error(response, 404, "TICKET_DETAIL_NOT_FOUND")

    def test_reassignment_committed_before_select_is_visible_to_a_creator_who_retains_access(self):
        response = self.read_with_reassignment_before_log_select(self.outsider_id)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()), 4)
        self.assertEqual(response.json()[-1]["after_assignee_id"], str(self.next_worker_id))
        self.assertEqual(response.json()[-1]["after_reviewer_id"], str(self.next_reviewer_id))

    def test_current_visibility_allows_history_in_every_ticket_status_and_query_roles_cannot_expand_it(self):
        for status in ("processing", "awaiting_review", "needs_info", "completed"):
            with self.sessions.begin() as db:
                db.get(self.Ticket, self.main_id).status = status
            self.assert_logs(self.read(user_id=self.worker_id))
        forged = {"role": "admin", "actor_user_id": str(self.actor_id), "user_id": str(self.actor_id), "team_id": str(self.team_b_id)}
        self.assert_error(self.read(user_id=self.history_id, params=forged), 404, "TICKET_DETAIL_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
