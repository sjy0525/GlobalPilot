"""Assignee-only processing submission with real JWT and isolated PostgreSQL."""

import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event
from uuid import uuid4

import jwt
from fastapi.testclient import TestClient
from sqlalchemy import event, text

if __package__:
    from . import test_team_authorization_pg as team_authorization
else:
    import test_team_authorization_pg as team_authorization


class TicketSubmitPostgresTests(team_authorization.TeamAuthorizationPostgresFixture):
    def setUp(self):
        super().setUp()
        self.original_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
        with self.sessions.begin() as db:
            reviewer = self.User(email="submit-reviewer@example.com", is_active=True)
            unrelated = self.User(email="submit-unrelated@example.com", is_active=True)
            db.add_all([reviewer, unrelated])
            db.flush()
            self.reviewer_id, self.unrelated_id = reviewer.id, unrelated.id
            assignee = self.TeamMember(
                team_id=self.team_a_id, user_id=self.outsider_id, role="employee", status="active"
            )
            db.add_all([
                assignee,
                self.TeamMember(team_id=self.team_b_id, user_id=self.outsider_id, role="employee", status="active"),
                self.TeamMember(team_id=self.team_a_id, user_id=reviewer.id, role="employee", status="active"),
                self.TeamMember(team_id=self.team_a_id, user_id=unrelated.id, role="employee", status="active"),
            ])
            ticket = self.Ticket(
                team_id=self.team_a_id, title="Processing task", description="Original description",
                created_by=self.outsider_id, assignee_id=self.outsider_id,
                reviewer_id=reviewer.id, status="processing",
                created_at=self.original_time, updated_at=self.original_time,
            )
            foreign = self.Ticket(
                team_id=self.team_b_id, title="Foreign task", description="",
                created_by=self.outsider_id, assignee_id=self.outsider_id,
                reviewer_id=self.actor_id, status="processing",
            )
            db.add_all([ticket, foreign])
            db.flush()
            self.ticket_id, self.foreign_ticket_id = ticket.id, foreign.id
            self.assignee_member_id = assignee.id

    def submit_path(self, ticket_id=None, team_id=None):
        return f"{self.tickets_path(team_id)}/{ticket_id or self.ticket_id}/submit"

    def submit(self, *, actor_id=None, ticket_id=None, team_id=None, body=None, headers=None, client=None):
        return (client or self.client).post(
            self.submit_path(ticket_id, team_id),
            headers=headers if headers is not None else self.headers(actor_id or self.outsider_id),
            json=body if body is not None else {"processing_result": "Completed work"},
        )

    def saved(self):
        with self.sessions() as db:
            return db.get(self.Ticket, self.ticket_id)

    def assert_unsubmitted(self):
        saved = self.saved()
        self.assertEqual(saved.status, "processing")
        self.assertIsNone(saved.processing_result)
        self.assertEqual(saved.updated_at, self.original_time)

    def new_ticket(self, *, creator=None, assignee=None, reviewer=None, status="processing"):
        with self.sessions.begin() as db:
            ticket = self.Ticket(
                team_id=self.team_a_id, title="Additional task", description="",
                created_by=creator or self.outsider_id,
                assignee_id=assignee or self.outsider_id,
                reviewer_id=reviewer or self.reviewer_id, status=status,
            )
            db.add(ticket)
            db.flush()
            return ticket.id

    def service_submit(self, db, *, actor_id=None, body=None):
        return self.ticket_service.submit_ticket_result(
            db, team_id=self.team_a_id, actor_user_id=actor_id or self.outsider_id,
            ticket_id=self.ticket_id,
            body=body or self.ticket_schemas.TicketResultSubmit(processing_result="Service result"),
        )

    def wait_then_change(self, *, lock_target, change):
        reached_lock = Event()

        def observe_lock(conn, cursor, statement, parameters, context, executemany):
            normalized = " ".join(statement.lower().split())
            if normalized.startswith("select") and f"from {lock_target}" in normalized and "for update" in normalized:
                reached_lock.set()

        def independent_submit():
            with TestClient(self.app, base_url="https://testserver", raise_server_exceptions=False) as client:
                return self.submit(client=client)

        with self.sessions() as holder:
            if lock_target == "teams":
                holder.query(self.Team.id).filter(self.Team.id == self.team_a_id).with_for_update().one()
            else:
                holder.query(self.Ticket).filter(self.Ticket.id == self.ticket_id).with_for_update().one()
            event.listen(self.engine, "before_cursor_execute", observe_lock)
            workers = ThreadPoolExecutor(max_workers=1)
            try:
                future = workers.submit(independent_submit)
                self.assertTrue(reached_lock.wait(timeout=3))
                change(holder)
                holder.commit()
                return future.result(timeout=15)
            finally:
                holder.rollback()
                event.remove(self.engine, "before_cursor_execute", observe_lock)
                workers.shutdown(wait=True)

    def test_assigned_employee_submits_a_trimmed_result_and_advances_only_to_awaiting_review(self):
        response = self.submit(body={"processing_result": " \n 已完成处理 \n "})
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["status"], "awaiting_review")
        self.assertEqual(payload["processing_result"], "已完成处理")
        saved = self.saved()
        self.assertEqual((saved.status, saved.processing_result), ("awaiting_review", "已完成处理"))
        self.assertEqual(saved.created_by, self.outsider_id)
        self.assertEqual(saved.assignee_id, self.outsider_id)
        self.assertEqual(saved.reviewer_id, self.reviewer_id)
        self.assertEqual(saved.created_at, self.original_time)
        self.assertGreater(saved.updated_at, self.original_time)

    def test_administrator_can_submit_when_currently_assigned(self):
        with self.sessions.begin() as db:
            db.get(self.Ticket, self.ticket_id).assignee_id = self.actor_id
        response = self.submit(actor_id=self.actor_id)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "awaiting_review")

    def test_administrator_cannot_bypass_the_current_assignee(self):
        self.assert_error(self.submit(actor_id=self.actor_id), 403, "TICKET_ASSIGNEE_REQUIRED")
        self.assert_unsubmitted()

    def test_creator_only_and_reviewer_only_members_cannot_submit(self):
        created = self.new_ticket(creator=self.reviewer_id, reviewer=self.actor_id)
        for ticket_id in (self.ticket_id, created):
            with self.subTest(ticket_id=str(ticket_id)):
                self.assert_error(
                    self.submit(actor_id=self.reviewer_id, ticket_id=ticket_id),
                    403, "TICKET_ASSIGNEE_REQUIRED",
                )
        self.assert_unsubmitted()

    def test_unrelated_foreign_team_and_unknown_tickets_have_the_same_not_found_response(self):
        responses = [
            self.submit(actor_id=self.unrelated_id),
            self.submit(ticket_id=self.foreign_ticket_id),
            self.submit(ticket_id=uuid4()),
            self.submit(team_id=self.team_b_id),
        ]
        for response in responses:
            self.assert_error(response, 404, "TICKET_DETAIL_NOT_FOUND")
            self.assertEqual(response.json(), responses[0].json())
        self.assert_unsubmitted()

    def test_awaiting_review_and_completed_reject_submission_without_overwriting_result(self):
        for status in ("awaiting_review", "completed"):
            with self.subTest(status=status):
                with self.sessions.begin() as db:
                    ticket = db.get(self.Ticket, self.ticket_id)
                    ticket.status, ticket.processing_result = status, "Historical result"
                response = self.submit(body={"processing_result": "Must not replace history"})
                self.assert_error(response, 409, "TICKET_STATUS_CONFLICT")
                saved = self.saved()
                self.assertEqual((saved.status, saved.processing_result), (status, "Historical result"))

    def test_needs_info_can_be_resubmitted_and_retains_the_latest_review_comment(self):
        with self.sessions.begin() as db:
            ticket = db.get(self.Ticket, self.ticket_id)
            ticket.status = "needs_info"
            ticket.processing_result = "Earlier result"
            ticket.review_comment = "Please supply evidence"
        before = self.saved().updated_at
        response = self.submit(body={"processing_result": "  Updated result  "})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "awaiting_review")
        self.assertEqual(response.json()["processing_result"], "Updated result")
        self.assertEqual(response.json()["review_comment"], "Please supply evidence")
        self.assertGreater(self.saved().updated_at, before)

    def test_duplicate_submission_is_a_conflict_and_keeps_the_first_result(self):
        first = self.submit(body={"processing_result": "First result"})
        self.assertEqual(first.status_code, 200, first.text)
        first_updated = self.saved().updated_at
        self.assert_error(
            self.submit(body={"processing_result": "Second result"}), 409, "TICKET_STATUS_CONFLICT"
        )
        saved = self.saved()
        self.assertEqual(saved.processing_result, "First result")
        self.assertEqual(saved.updated_at, first_updated)

    def test_body_rejects_empty_whitespace_wrong_types_and_injected_fields(self):
        bodies = [
            {}, {"processing_result": ""}, {"processing_result": " \n\t "},
            {"processing_result": None}, {"processing_result": 1}, {"processing_result": []},
            {"processing_result": "Result", "status": "completed"},
            {"processing_result": "Result", "assignee_id": str(self.actor_id)},
            {"processing_result": "Result", "reviewer_id": str(self.actor_id)},
            {"processing_result": "Result", "team_id": str(self.team_b_id)},
            {"processing_result": "Result", "role": "admin"},
        ]
        for body in bodies:
            with self.subTest(body=body):
                response = self.submit(body=body)
                self.assert_error(response, 422, "VALIDATION_ERROR")
                self.assertTrue(all("input" not in item for item in response.json()["detail"]))
                self.assert_unsubmitted()

    def test_endpoint_requires_a_valid_access_token(self):
        expired = jwt.encode(
            {"sub": str(self.outsider_id), "token_type": "access",
             "exp": datetime.now(timezone.utc) - timedelta(minutes=1)},
            self.security.settings.jwt_secret_key, algorithm=self.security.settings.jwt_algorithm,
        )
        for headers in (
            {}, {"Authorization": "Bearer invalid"}, {"Authorization": f"Bearer {expired}"},
            {"Authorization": f"Bearer {self.security.create_refresh_token(self.outsider_id)}"},
            self.headers(uuid4()),
        ):
            response = self.submit(headers=headers)
            self.assertEqual(response.status_code, 401, response.text)
            self.assertEqual(response.headers["www-authenticate"], "Bearer")
        self.assert_unsubmitted()

    def test_removed_disabled_and_nonmember_accounts_cannot_submit(self):
        for actor_id, code in ((self.removed_id, "TEAM_ACCESS_DENIED"), (self.disabled_id, "ACCOUNT_DISABLED")):
            self.assert_error(self.submit(actor_id=actor_id), 403, code)
        with self.sessions.begin() as db:
            db.get(self.TeamMember, self.assignee_member_id).status = "removed"
        self.assert_error(self.submit(), 403, "TEAM_ACCESS_DENIED")
        self.assert_unsubmitted()

    def test_invalid_team_or_ticket_uuid_returns_422(self):
        for path, field in (
            (f"/api/team/not-a-uuid/tickets/{self.ticket_id}/submit", "team_id"),
            (f"{self.tickets_path()}/not-a-uuid/submit", "ticket_id"),
        ):
            response = self.client.post(path, headers=self.headers(self.outsider_id), json={"processing_result": "Result"})
            self.assert_error(response, 422, "VALIDATION_ERROR")
            self.assertIn(["path", field], [item["loc"] for item in response.json()["detail"]])
        self.assert_unsubmitted()

    def test_direct_service_rejects_invalid_identities_without_route_dependencies(self):
        for actor_id, status, code in (
            (self.removed_id, 403, "TEAM_ACCESS_DENIED"),
            (self.disabled_id, 403, "ACCOUNT_DISABLED"), (uuid4(), 403, "TEAM_ACCESS_DENIED"),
            (self.actor_id, 403, "TICKET_ASSIGNEE_REQUIRED"),
            (self.unrelated_id, 404, "TICKET_DETAIL_NOT_FOUND"),
        ):
            with self.subTest(actor_id=str(actor_id)):
                with self.sessions() as db:
                    with self.assertRaises(self.errors.AppHTTPException) as caught:
                        self.service_submit(db, actor_id=actor_id)
                    self.assertEqual((caught.exception.status_code, caught.exception.code), (status, code))
        self.assert_unsubmitted()

    def test_service_leaves_transaction_commit_and_rollback_to_its_caller(self):
        with self.sessions() as db:
            result = self.service_submit(db)
            self.assertEqual(result.status, "awaiting_review")
            self.assertEqual(db.get(self.Ticket, self.ticket_id).processing_result, "Service result")
            db.rollback()
        self.assert_unsubmitted()

    def test_real_flush_failure_rolls_back_status_result_and_timestamp(self):
        constraint = "ck_test_reject_processing_result"
        with self.engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE tickets ADD CONSTRAINT {constraint} CHECK (processing_result IS NULL)"))

        def cleanup():
            with self.engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE tickets DROP CONSTRAINT {constraint}"))

        self.addCleanup(cleanup)
        with self.assertLogs(self.errors.logger, level="ERROR"):
            response = self.submit()
        self.assert_error(response, 500, "INTERNAL_ERROR")
        self.assert_unsubmitted()

    def test_real_deferred_commit_failure_rolls_back_a_successful_flush(self):
        with self.engine.begin() as conn:
            conn.execute(text("""
                CREATE FUNCTION reject_ticket_submit_commit() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    IF NEW.status = 'awaiting_review' THEN
                        RAISE EXCEPTION 'forced ticket submit commit failure' USING ERRCODE = '23514';
                    END IF;
                    RETURN NEW;
                END $$
            """))
            conn.execute(text("""
                CREATE CONSTRAINT TRIGGER reject_ticket_submit_commit_trigger
                AFTER UPDATE ON tickets DEFERRABLE INITIALLY DEFERRED
                FOR EACH ROW EXECUTE FUNCTION reject_ticket_submit_commit()
            """))

        def cleanup():
            with self.engine.begin() as conn:
                conn.execute(text("DROP TRIGGER reject_ticket_submit_commit_trigger ON tickets"))
                conn.execute(text("DROP FUNCTION reject_ticket_submit_commit()"))

        self.addCleanup(cleanup)
        updates = []

        def observe_update(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().lower().startswith("update tickets"):
                updates.append(statement)

        event.listen(self.engine, "after_cursor_execute", observe_update)
        try:
            with self.assertLogs(self.errors.logger, level="ERROR"):
                response = self.submit()
        finally:
            event.remove(self.engine, "after_cursor_execute", observe_update)
        self.assertEqual(len(updates), 1)
        self.assert_error(response, 500, "INTERNAL_ERROR")
        self.assert_unsubmitted()

    def test_cached_ticket_assignee_is_refreshed_before_service_authorization(self):
        with self.sessions() as stale:
            cached = stale.get(self.Ticket, self.ticket_id)
            with self.sessions.begin() as writer:
                writer.get(self.Ticket, self.ticket_id).assignee_id = self.reviewer_id
            self.assertEqual(cached.assignee_id, self.outsider_id)
            with self.assertRaises(self.errors.AppHTTPException) as caught:
                self.service_submit(stale)
            self.assertEqual(caught.exception.code, "TICKET_ASSIGNEE_REQUIRED")
            self.assertEqual(cached.assignee_id, self.reviewer_id)
        self.assertEqual(self.saved().status, "processing")
        self.assertIsNone(self.saved().processing_result)

    def test_cached_ticket_status_is_refreshed_before_service_transition(self):
        with self.sessions() as stale:
            cached = stale.get(self.Ticket, self.ticket_id)
            with self.sessions.begin() as writer:
                ticket = writer.get(self.Ticket, self.ticket_id)
                ticket.status, ticket.processing_result = "awaiting_review", "Committed elsewhere"
            self.assertEqual(cached.status, "processing")
            with self.assertRaises(self.errors.AppHTTPException) as caught:
                self.service_submit(stale)
            self.assertEqual(caught.exception.code, "TICKET_STATUS_CONFLICT")
            self.assertEqual((cached.status, cached.processing_result), ("awaiting_review", "Committed elsewhere"))
        self.assertEqual(self.saved().processing_result, "Committed elsewhere")

    def test_team_lock_wait_rechecks_committed_member_removal_and_account_disabling(self):
        for state, code in (("removed", "TEAM_ACCESS_DENIED"), ("disabled", "ACCOUNT_DISABLED")):
            with self.subTest(state=state):
                def change(db):
                    if state == "removed":
                        db.get(self.TeamMember, self.assignee_member_id).status = "removed"
                    else:
                        db.get(self.User, self.outsider_id).is_active = False

                response = self.wait_then_change(lock_target="teams", change=change)
                self.assert_error(response, 403, code)
                self.assert_unsubmitted()
                with self.sessions.begin() as restore:
                    restore.get(self.TeamMember, self.assignee_member_id).status = "active"
                    restore.get(self.User, self.outsider_id).is_active = True

    def test_ticket_lock_wait_rechecks_a_concurrent_reassignment(self):
        response = self.wait_then_change(
            lock_target="tickets",
            change=lambda db: setattr(db.get(self.Ticket, self.ticket_id), "assignee_id", self.reviewer_id),
        )
        self.assert_error(response, 403, "TICKET_ASSIGNEE_REQUIRED")
        self.assertEqual(self.saved().assignee_id, self.reviewer_id)
        self.assertIsNone(self.saved().processing_result)

    def test_ticket_lock_wait_rechecks_a_concurrent_status_transition(self):
        def change(db):
            ticket = db.get(self.Ticket, self.ticket_id)
            ticket.status, ticket.processing_result = "awaiting_review", "Other submission"

        response = self.wait_then_change(lock_target="tickets", change=change)
        self.assert_error(response, 409, "TICKET_STATUS_CONFLICT")
        self.assertEqual((self.saved().status, self.saved().processing_result), ("awaiting_review", "Other submission"))

    def test_same_jwt_role_changes_neither_grant_a_bypass_nor_remove_assignee_business_rights(self):
        headers = self.headers(self.outsider_id)
        other = self.new_ticket(assignee=self.reviewer_id, reviewer=self.actor_id)
        self.assert_error(self.submit(ticket_id=other, headers=headers), 403, "TICKET_ASSIGNEE_REQUIRED")
        role_path = f"{self.members_path()}/{self.outsider_id}/role"
        promoted = self.client.patch(role_path, headers=self.headers(), json={"role": "admin"})
        self.assertEqual(promoted.status_code, 200, promoted.text)
        self.assert_error(self.submit(ticket_id=other, headers=headers), 403, "TICKET_ASSIGNEE_REQUIRED")
        demoted = self.client.patch(role_path, headers=self.headers(), json={"role": "employee"})
        self.assertEqual(demoted.status_code, 200, demoted.text)
        accepted = self.submit(headers=headers)
        self.assertEqual(accepted.status_code, 200, accepted.text)

    def test_two_concurrent_submissions_follow_team_then_ticket_locks_and_commit_once(self):
        gate = Barrier(2)
        locks = {}

        def synchronize_and_record(conn, cursor, statement, parameters, context, executemany):
            normalized = " ".join(statement.lower().split())
            if not normalized.startswith("select") or "for update" not in normalized:
                return
            if "from teams" in normalized:
                locks.setdefault(id(conn), []).append("team")
                gate.wait(timeout=5)
            elif "from tickets" in normalized:
                locks.setdefault(id(conn), []).append("ticket")

        def independent(result):
            with TestClient(self.app, base_url="https://testserver", raise_server_exceptions=False) as client:
                return self.submit(client=client, body={"processing_result": result})

        event.listen(self.engine, "before_cursor_execute", synchronize_and_record)
        try:
            with ThreadPoolExecutor(max_workers=2) as workers:
                futures = [workers.submit(independent, value) for value in ("First contender", "Second contender")]
                responses = [future.result(timeout=15) for future in futures]
        finally:
            event.remove(self.engine, "before_cursor_execute", synchronize_and_record)
        self.assertEqual(sorted(response.status_code for response in responses), [200, 409])
        winner = next(response for response in responses if response.status_code == 200)
        conflict = next(response for response in responses if response.status_code == 409)
        self.assertEqual(conflict.json()["code"], "TICKET_STATUS_CONFLICT")
        self.assertEqual(self.saved().processing_result, winner.json()["processing_result"])
        self.assertEqual(self.saved().status, "awaiting_review")
        self.assertEqual(len(locks), 2)
        for sequence in locks.values():
            self.assertEqual(sequence, ["team", "ticket"])

    def test_submitted_result_is_preserved_in_the_existing_list_and_detail_contracts(self):
        response = self.submit()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(set(response.json()), {
            "id", "team_id", "title", "description", "created_by", "assignee_id", "reviewer_id",
            "status", "created_at", "updated_at", "processing_result", "review_comment",
        })
        headers = self.headers(self.outsider_id)
        detail = self.client.get(f"{self.tickets_path()}/{self.ticket_id}", headers=headers)
        listed = self.client.get(self.tickets_path(), headers=headers)
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(detail.json(), response.json())
        self.assertEqual(next(row for row in listed.json() if row["id"] == str(self.ticket_id)), response.json())

    def test_submit_openapi_exposes_only_processing_result_and_bearer_path_parameters(self):
        response = self.client.get("/openapi.json")
        self.assertEqual(response.status_code, 200, response.text)
        document = response.json()
        operation = document["paths"]["/api/team/{team_id}/tickets/{ticket_id}/submit"]["post"]
        self.assertTrue(operation["security"])
        parameters = {item["name"]: item for item in operation["parameters"]}
        self.assertEqual(set(parameters), {"team_id", "ticket_id"})
        self.assertTrue(all(item["in"] == "path" and item["required"] for item in parameters.values()))
        body = operation["requestBody"]["content"]["application/json"]["schema"]
        schema = document["components"]["schemas"][body["$ref"].rsplit("/", 1)[-1]]
        self.assertEqual(set(schema["properties"]), {"processing_result"})
        self.assertEqual(schema["required"], ["processing_result"])
        self.assertEqual(schema["properties"]["processing_result"]["minLength"], 1)
        self.assertFalse(schema["additionalProperties"])
        self.assertIn("200", operation["responses"])


if __name__ == "__main__":
    unittest.main()
