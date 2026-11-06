"""Reviewer decisions and audited reassignment using real JWT and PostgreSQL."""

import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event
from uuid import uuid4

import jwt
from fastapi.testclient import TestClient
from sqlalchemy import event, inspect, select, text

if __package__:
    from . import test_team_authorization_pg as team_authorization
else:
    import test_team_authorization_pg as team_authorization


class TicketReviewAssignmentPostgresTests(team_authorization.TeamAuthorizationPostgresFixture):
    def setUp(self):
        super().setUp()
        self.original_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
        with self.sessions.begin() as db:
            people = [self.User(email=f"{name}@review-test.example.com", is_active=True) for name in (
                "reviewer", "next-assignee", "next-reviewer", "foreign-admin",
            )]
            db.add_all(people)
            db.flush()
            self.reviewer_id, self.next_assignee_id, self.next_reviewer_id, self.foreign_id = [user.id for user in people]
            db.add_all([
                self.TeamMember(team_id=self.team_a_id, user_id=self.outsider_id, role="employee", status="active"),
                self.TeamMember(team_id=self.team_b_id, user_id=self.outsider_id, role="employee", status="active"),
                self.TeamMember(team_id=self.team_a_id, user_id=self.reviewer_id, role="employee", status="active"),
                self.TeamMember(team_id=self.team_b_id, user_id=self.reviewer_id, role="employee", status="active"),
                self.TeamMember(team_id=self.team_a_id, user_id=self.next_assignee_id, role="employee", status="active"),
                self.TeamMember(team_id=self.team_a_id, user_id=self.next_reviewer_id, role="employee", status="active"),
                self.TeamMember(team_id=self.team_b_id, user_id=self.foreign_id, role="admin", status="active"),
            ])
            ticket = self.Ticket(
                team_id=self.team_a_id, title="Review this work", description="Original description",
                created_by=self.outsider_id, assignee_id=self.outsider_id, reviewer_id=self.reviewer_id,
                status="awaiting_review", processing_result="Original result", review_comment="Previous feedback",
                created_at=self.original_time, updated_at=self.original_time,
            )
            foreign = self.Ticket(
                team_id=self.team_b_id, title="Foreign work", description="",
                created_by=self.outsider_id, assignee_id=self.outsider_id,
                reviewer_id=self.reviewer_id, status="awaiting_review", processing_result="Foreign result",
            )
            db.add_all([ticket, foreign])
            db.flush()
            self.ticket_id, self.foreign_ticket_id = ticket.id, foreign.id

    def saved(self):
        with self.sessions() as db:
            return db.get(self.Ticket, self.ticket_id)

    def audits(self):
        with self.sessions() as db:
            return db.scalars(select(self.TicketAssignmentAuditLog)).all()

    def path(self, action, *, ticket_id=None, team_id=None):
        return f"{self.tickets_path(team_id)}/{ticket_id or self.ticket_id}/{action}"

    def review_body(self, *, decision="approve", comment="", expected=None):
        return {
            "decision": decision, "comment": comment,
            "expected_updated_at": (expected or self.saved().updated_at).isoformat(),
        }

    def assignment_body(self, *, assignee=None, reviewer=None, expected=None):
        return {
            "assignee_id": str(assignee or self.next_assignee_id),
            "reviewer_id": str(reviewer or self.next_reviewer_id),
            "expected_updated_at": (expected or self.saved().updated_at).isoformat(),
        }

    def review(self, *, body=None, actor_id=None, ticket_id=None, team_id=None, client=None, headers=None):
        return (client or self.client).post(
            self.path("review", ticket_id=ticket_id, team_id=team_id),
            headers=headers if headers is not None else self.headers(actor_id or self.reviewer_id),
            json=body if body is not None else self.review_body(),
        )

    def reassign(self, *, body=None, actor_id=None, ticket_id=None, team_id=None, client=None, headers=None):
        return (client or self.client).patch(
            self.path("assignment", ticket_id=ticket_id, team_id=team_id),
            headers=headers if headers is not None else self.headers(actor_id),
            json=body if body is not None else self.assignment_body(),
        )

    def assert_original(self):
        ticket = self.saved()
        self.assertEqual((ticket.status, ticket.processing_result, ticket.review_comment),
                         ("awaiting_review", "Original result", "Previous feedback"))
        self.assertEqual((ticket.assignee_id, ticket.reviewer_id), (self.outsider_id, self.reviewer_id))
        self.assertEqual(ticket.updated_at, self.original_time)
        self.assertEqual(self.audits(), [])

    def independent(self, operation, body, actor_id=None):
        with TestClient(self.app, base_url="https://testserver", raise_server_exceptions=False) as client:
            if operation == "review":
                return self.review(client=client, body=body, actor_id=actor_id)
            return self.reassign(client=client, body=body, actor_id=actor_id)

    def concurrent(self, operations):
        barrier = Barrier(len(operations))
        attempts = []

        def gate(conn, cursor, statement, parameters, context, executemany):
            normalized = " ".join(statement.lower().split())
            if normalized.startswith("select") and "from teams" in normalized and "for update" in normalized:
                attempts.append(normalized)
                barrier.wait(timeout=5)

        event.listen(self.engine, "before_cursor_execute", gate)
        try:
            with ThreadPoolExecutor(max_workers=len(operations)) as workers:
                futures = [workers.submit(self.independent, *operation) for operation in operations]
                responses = [future.result(timeout=15) for future in futures]
        finally:
            event.remove(self.engine, "before_cursor_execute", gate)
        self.assertEqual(len(attempts), len(operations))
        return responses

    def wait_then_change(self, operation, change):
        reached = Event()
        body = self.review_body() if operation == "review" else self.assignment_body()

        def observe(conn, cursor, statement, parameters, context, executemany):
            normalized = " ".join(statement.lower().split())
            if normalized.startswith("select") and "from teams" in normalized and "for update" in normalized:
                reached.set()

        with self.sessions() as holder:
            holder.query(self.Team.id).filter(self.Team.id == self.team_a_id).with_for_update().one()
            event.listen(self.engine, "before_cursor_execute", observe)
            workers = ThreadPoolExecutor(max_workers=1)
            try:
                future = workers.submit(self.independent, operation, body)
                self.assertTrue(reached.wait(timeout=3))
                change(holder)
                holder.commit()
                return future.result(timeout=15)
            finally:
                holder.rollback()
                event.remove(self.engine, "before_cursor_execute", observe)
                workers.shutdown(wait=True)

    def test_current_employee_reviewer_can_approve_and_only_complete_the_ticket(self):
        response = self.review(body=self.review_body(comment="  Accepted  "))
        self.assertEqual(response.status_code, 200, response.text)
        saved = self.saved()
        self.assertEqual((saved.status, saved.review_comment), ("completed", "Accepted"))
        self.assertEqual(saved.processing_result, "Original result")
        self.assertEqual((saved.assignee_id, saved.reviewer_id), (self.outsider_id, self.reviewer_id))
        self.assertGreater(saved.updated_at, self.original_time)

    def test_return_stores_a_trimmed_comment_and_keeps_the_processing_result(self):
        response = self.review(body=self.review_body(decision="return", comment="  Supply evidence  "))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((self.saved().status, self.saved().review_comment), ("needs_info", "Supply evidence"))
        self.assertEqual(self.saved().processing_result, "Original result")

    def test_admin_must_be_the_assigned_reviewer_and_approval_comment_is_optional(self):
        self.assert_error(self.review(actor_id=self.actor_id), 403, "TICKET_REVIEWER_REQUIRED")
        with self.sessions.begin() as db:
            db.get(self.Ticket, self.ticket_id).reviewer_id = self.actor_id
        body = self.review_body()
        del body["comment"]
        response = self.review(actor_id=self.actor_id, body=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(self.saved().review_comment)

    def test_creator_and_assignee_relationships_do_not_grant_review_permission(self):
        self.assert_error(self.review(actor_id=self.outsider_id), 403, "TICKET_REVIEWER_REQUIRED")
        with self.sessions.begin() as db:
            db.get(self.Ticket, self.ticket_id).created_by = self.next_assignee_id
        self.assert_error(self.review(actor_id=self.next_assignee_id), 403, "TICKET_REVIEWER_REQUIRED")

    def test_hidden_foreign_and_nonexistent_tickets_keep_the_existing_not_found_contract(self):
        responses = [self.review(actor_id=self.next_assignee_id)]
        for ticket_id in (self.foreign_ticket_id, uuid4()):
            responses.extend([self.review(ticket_id=ticket_id), self.reassign(ticket_id=ticket_id)])
        for response in responses:
            self.assert_error(response, 404, "TICKET_DETAIL_NOT_FOUND")
            self.assertEqual(response.json(), responses[0].json())
        self.assert_original()

    def test_review_requires_awaiting_review_status(self):
        for status in ("processing", "needs_info", "completed"):
            with self.subTest(status=status):
                with self.sessions.begin() as db:
                    db.get(self.Ticket, self.ticket_id).status = status
                self.assert_error(self.review(), 409, "TICKET_STATUS_CONFLICT")
                self.assertEqual((self.saved().status, self.saved().review_comment), (status, "Previous feedback"))

    def test_stale_review_snapshot_is_rejected_without_overwriting_current_work(self):
        body = self.review_body()
        with self.sessions.begin() as db:
            db.get(self.Ticket, self.ticket_id).processing_result = "New work"
        self.assert_error(self.review(body=body), 409, "TICKET_REVIEW_OUTDATED")
        self.assertEqual((self.saved().status, self.saved().processing_result), ("awaiting_review", "New work"))

    def test_review_body_requires_valid_decision_aware_timestamp_and_nonblank_return_comment(self):
        base = self.review_body()
        bodies = [
            {**base, "decision": "reject"}, {**base, "decision": "admin"},
            {**base, "expected_updated_at": "2020-01-01T00:00:00"},
            {"decision": "approve"}, {**base, "comment": None},
            {**base, "decision": "return", "comment": " \n\t "},
            {**base, "status": "completed"}, {**base, "reviewer_id": str(self.actor_id)},
            {**base, "role": "admin"},
        ]
        for body in bodies:
            with self.subTest(body=body):
                response = self.review(body=body)
                self.assert_error(response, 422, "VALIDATION_ERROR")
                self.assertTrue(all("input" not in item for item in response.json()["detail"]))
                self.assert_original()

    def test_repeating_a_review_cannot_replace_the_first_decision(self):
        body = self.review_body(comment="First decision")
        first = self.review(body=body)
        self.assertEqual(first.status_code, 200, first.text)
        updated = self.saved().updated_at
        self.assert_error(self.review(body=body), 409, "TICKET_STATUS_CONFLICT")
        self.assertEqual((self.saved().status, self.saved().review_comment), ("completed", "First decision"))
        self.assertEqual(self.saved().updated_at, updated)

    def test_same_transaction_submit_return_resubmit_strictly_advances_snapshots_and_rejects_old_review(self):
        with self.sessions.begin() as setup:
            db_ticket = setup.get(self.Ticket, self.ticket_id)
            db_ticket.status, db_ticket.processing_result = "processing", None
        with self.sessions() as db:
            submitted = self.ticket_service.submit_ticket_result(
                db, team_id=self.team_a_id, actor_user_id=self.outsider_id, ticket_id=self.ticket_id,
                body=self.ticket_schemas.TicketResultSubmit(processing_result="First work"),
            )
            old_review = submitted.updated_at
            returned = self.ticket_service.review_ticket(
                db, team_id=self.team_a_id, actor_user_id=self.reviewer_id, ticket_id=self.ticket_id,
                body=self.ticket_schemas.TicketReview(
                    decision="return", expected_updated_at=old_review, comment="Need evidence",
                ),
            )
            resubmitted = self.ticket_service.submit_ticket_result(
                db, team_id=self.team_a_id, actor_user_id=self.outsider_id, ticket_id=self.ticket_id,
                body=self.ticket_schemas.TicketResultSubmit(processing_result="Second work"),
            )
            self.assertGreater(returned.updated_at, old_review)
            self.assertGreater(resubmitted.updated_at, returned.updated_at)
            self.assertEqual(resubmitted.review_comment, "Need evidence")
            with self.assertRaises(self.errors.AppHTTPException) as caught:
                self.ticket_service.review_ticket(
                    db, team_id=self.team_a_id, actor_user_id=self.reviewer_id, ticket_id=self.ticket_id,
                    body=self.ticket_schemas.TicketReview(decision="approve", expected_updated_at=old_review),
                )
            self.assertEqual(caught.exception.code, "TICKET_REVIEW_OUTDATED")
            db.rollback()

    def test_direct_services_require_valid_current_identity_and_operation_permission(self):
        for operation in ("review", "assignment"):
            cases = [(self.removed_id, "TEAM_ACCESS_DENIED"), (self.disabled_id, "ACCOUNT_DISABLED")]
            cases.append((self.actor_id, "TICKET_REVIEWER_REQUIRED") if operation == "review" else (self.reviewer_id, "ADMIN_REQUIRED"))
            for actor_id, code in cases:
                with self.subTest(operation=operation, actor_id=str(actor_id)):
                    with self.sessions() as db:
                        with self.assertRaises(self.errors.AppHTTPException) as caught:
                            if operation == "review":
                                self.ticket_service.review_ticket(
                                    db, team_id=self.team_a_id, actor_user_id=actor_id, ticket_id=self.ticket_id,
                                    body=self.ticket_schemas.TicketReview(**self.review_body()),
                                )
                            else:
                                self.ticket_service.reassign_ticket(
                                    db, team_id=self.team_a_id, actor_user_id=actor_id, ticket_id=self.ticket_id,
                                    body=self.ticket_schemas.TicketReassign(**self.assignment_body()),
                                )
                        self.assertEqual((caught.exception.status_code, caught.exception.code), (403, code))
        self.assert_original()

    def test_cached_reviewer_assignment_is_refreshed_before_authorization(self):
        with self.sessions.begin() as setup:
            setup.get(self.Ticket, self.ticket_id).created_by = self.reviewer_id
        with self.sessions() as cached:
            ticket = cached.get(self.Ticket, self.ticket_id)
            expected = ticket.updated_at
            with self.sessions.begin() as writer:
                writer.get(self.Ticket, self.ticket_id).reviewer_id = self.next_reviewer_id
            self.assertEqual(ticket.reviewer_id, self.reviewer_id)
            with self.assertRaises(self.errors.AppHTTPException) as caught:
                self.ticket_service.review_ticket(
                    cached, team_id=self.team_a_id, actor_user_id=self.reviewer_id, ticket_id=self.ticket_id,
                    body=self.ticket_schemas.TicketReview(decision="approve", expected_updated_at=expected),
                )
            self.assertEqual(caught.exception.code, "TICKET_REVIEWER_REQUIRED")
            self.assertEqual(ticket.reviewer_id, self.next_reviewer_id)

    def test_review_lock_wait_rechecks_member_removal_and_account_disabling(self):
        for state, code in (("removed", "TEAM_ACCESS_DENIED"), ("disabled", "ACCOUNT_DISABLED")):
            with self.subTest(state=state):
                def change(db):
                    if state == "disabled":
                        db.get(self.User, self.reviewer_id).is_active = False
                    else:
                        member = db.scalar(select(self.TeamMember).where(
                            self.TeamMember.team_id == self.team_a_id, self.TeamMember.user_id == self.reviewer_id,
                        ))
                        member.status = "removed"

                self.assert_error(self.wait_then_change("review", change), 403, code)
                with self.sessions.begin() as restore:
                    restore.get(self.User, self.reviewer_id).is_active = True
                    member = restore.scalar(select(self.TeamMember).where(
                        self.TeamMember.team_id == self.team_a_id, self.TeamMember.user_id == self.reviewer_id,
                    ))
                    member.status = "active"
                self.assert_original()

    def test_concurrent_reviews_commit_only_one_decision(self):
        expected = self.saved().updated_at
        responses = self.concurrent([
            ("review", self.review_body(comment="Approved", expected=expected)),
            ("review", self.review_body(decision="return", comment="More evidence", expected=expected)),
        ])
        self.assertEqual(sorted(item.status_code for item in responses), [200, 409])
        winner = next(item for item in responses if item.status_code == 200)
        loser = next(item for item in responses if item.status_code == 409)
        self.assertEqual(loser.json()["code"], "TICKET_STATUS_CONFLICT")
        self.assertEqual((self.saved().status, self.saved().review_comment),
                         (winner.json()["status"], winner.json()["review_comment"]))

    def test_review_flush_failure_rolls_back_decision_comment_and_timestamp(self):
        constraint = "ck_test_reject_review_completion"
        with self.engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE tickets ADD CONSTRAINT {constraint} CHECK (status <> 'completed')"))

        def cleanup():
            with self.engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE tickets DROP CONSTRAINT {constraint}"))

        self.addCleanup(cleanup)
        with self.assertLogs(self.errors.logger, level="ERROR"):
            response = self.review()
        self.assert_error(response, 500, "INTERNAL_ERROR")
        self.assert_original()

    def test_review_and_assignment_services_leave_transaction_ownership_to_the_caller(self):
        for operation in ("review", "assignment"):
            with self.subTest(operation=operation):
                with self.sessions() as db:
                    if operation == "review":
                        result = self.ticket_service.review_ticket(
                            db, team_id=self.team_a_id, actor_user_id=self.reviewer_id, ticket_id=self.ticket_id,
                            body=self.ticket_schemas.TicketReview(**self.review_body()),
                        )
                        self.assertEqual(result.status, "completed")
                    else:
                        result = self.ticket_service.reassign_ticket(
                            db, team_id=self.team_a_id, actor_user_id=self.actor_id, ticket_id=self.ticket_id,
                            body=self.ticket_schemas.TicketReassign(**self.assignment_body()),
                        )
                        self.assertEqual(result.assignee_id, self.next_assignee_id)
                    db.rollback()
                self.assert_original()

    def test_admin_reassignment_preserves_work_and_commits_one_precise_audit_in_the_same_transaction(self):
        expected = self.saved().updated_at.astimezone(timezone(timedelta(hours=8)))
        response = self.reassign(body=self.assignment_body(expected=expected))
        self.assertEqual(response.status_code, 200, response.text)
        saved = self.saved()
        self.assertEqual((saved.assignee_id, saved.reviewer_id), (self.next_assignee_id, self.next_reviewer_id))
        self.assertEqual((saved.status, saved.processing_result, saved.review_comment),
                         ("awaiting_review", "Original result", "Previous feedback"))
        self.assertEqual(saved.created_by, self.outsider_id)
        self.assertGreater(saved.updated_at, self.original_time)
        logs = self.audits()
        self.assertEqual(len(logs), 1)
        audit = logs[0]
        self.assertEqual((audit.team_id, audit.ticket_id, audit.actor_user_id), (self.team_a_id, self.ticket_id, self.actor_id))
        self.assertEqual((audit.before_assignee_id, audit.after_assignee_id, audit.before_reviewer_id, audit.after_reviewer_id),
                         (self.outsider_id, self.next_assignee_id, self.reviewer_id, self.next_reviewer_id))
        self.assertIsNotNone(audit.created_at.utcoffset())
        with self.engine.connect() as conn:
            columns = inspect(conn).get_columns("ticket_assignment_audit_logs", schema=self.schema)
            self.assertEqual({column["name"] for column in columns}, {
                "id", "team_id", "ticket_id", "actor_user_id", "before_assignee_id", "after_assignee_id",
                "before_reviewer_id", "after_reviewer_id", "created_at",
            })
            foreign_keys = inspect(conn).get_foreign_keys("ticket_assignment_audit_logs", schema=self.schema)
            self.assertEqual(len(foreign_keys), 7)
            self.assertTrue(all(key["options"]["ondelete"] == "RESTRICT" for key in foreign_keys))
            ticket_tx = conn.execute(text("SELECT xmin::text FROM tickets WHERE id=:id"), {"id": self.ticket_id}).scalar_one()
            audit_tx = conn.execute(text("SELECT xmin::text FROM ticket_assignment_audit_logs WHERE id=:id"), {"id": audit.id}).scalar_one()
            self.assertEqual(ticket_tx, audit_tx)

    def test_same_assignment_is_a_noop_only_with_a_fresh_snapshot(self):
        body = self.assignment_body(assignee=self.outsider_id, reviewer=self.reviewer_id)
        response = self.reassign(body=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_original()
        with self.sessions.begin() as db:
            db.get(self.Ticket, self.ticket_id).processing_result = "New result"
        self.assert_error(self.reassign(body=body), 409, "TICKET_ASSIGNMENT_OUTDATED")
        self.assertEqual(self.audits(), [])

    def test_completed_tickets_reject_reassignment_even_when_the_ids_are_unchanged(self):
        with self.sessions.begin() as db:
            db.get(self.Ticket, self.ticket_id).status = "completed"
        for body in (self.assignment_body(), self.assignment_body(assignee=self.outsider_id, reviewer=self.reviewer_id)):
            self.assert_error(self.reassign(body=body), 409, "TICKET_STATUS_CONFLICT")
        self.assertEqual(self.audits(), [])

    def test_reassignment_rejects_nonexistent_removed_disabled_and_foreign_members(self):
        for field in ("assignee_id", "reviewer_id"):
            for user_id in (uuid4(), self.removed_id, self.disabled_id, self.foreign_id):
                with self.subTest(field=field, user_id=str(user_id)):
                    body = {**self.assignment_body(), field: str(user_id)}
                    self.assert_error(self.reassign(body=body), 400, "INVALID_TICKET_ASSIGNMENT")
                    self.assert_original()

    def test_reassignment_requires_current_admin_authority_in_the_requested_team(self):
        for actor_id, code in (
            (self.outsider_id, "ADMIN_REQUIRED"), (self.reviewer_id, "ADMIN_REQUIRED"),
            (self.foreign_id, "TEAM_ACCESS_DENIED"), (self.removed_id, "TEAM_ACCESS_DENIED"),
            (self.disabled_id, "ACCOUNT_DISABLED"),
        ):
            self.assert_error(self.reassign(actor_id=actor_id), 403, code)
        self.assert_error(self.reassign(team_id=self.team_b_id), 403, "ADMIN_REQUIRED")
        self.assert_original()

    def test_reassignment_rejects_an_outdated_snapshot_without_an_audit(self):
        body = self.assignment_body()
        with self.sessions.begin() as db:
            db.get(self.Ticket, self.ticket_id).review_comment = "Changed feedback"
        self.assert_error(self.reassign(body=body), 409, "TICKET_ASSIGNMENT_OUTDATED")
        self.assertEqual(self.saved().review_comment, "Changed feedback")
        self.assertEqual(self.audits(), [])

    def test_reassignment_body_rejects_missing_ids_naive_timestamps_and_injected_fields(self):
        base = self.assignment_body()
        bodies = [
            {"assignee_id": str(self.next_assignee_id)},
            {**base, "assignee_id": "not-a-uuid"}, {**base, "reviewer_id": None},
            {**base, "expected_updated_at": "2020-01-01T00:00:00"},
            {**base, "role": "admin"}, {**base, "status": "completed"},
            {**base, "team_id": str(self.team_b_id)}, {**base, "actor_user_id": str(self.actor_id)},
        ]
        for body in bodies:
            self.assert_error(self.reassign(body=body), 422, "VALIDATION_ERROR")
            self.assert_original()

    def test_new_mutations_require_access_tokens_and_valid_path_uuids(self):
        expired = jwt.encode(
            {"sub": str(self.actor_id), "token_type": "access", "exp": datetime.now(timezone.utc) - timedelta(minutes=1)},
            self.security.settings.jwt_secret_key, algorithm=self.security.settings.jwt_algorithm,
        )
        for headers in ({}, {"Authorization": "Bearer invalid"}, {"Authorization": f"Bearer {expired}"},
                        {"Authorization": f"Bearer {self.security.create_refresh_token(self.actor_id)}"}):
            for response in (self.review(headers=headers), self.reassign(headers=headers)):
                self.assertEqual(response.status_code, 401, response.text)
                self.assertEqual(response.headers["www-authenticate"], "Bearer")
        for action, method, body in (("review", "POST", self.review_body()), ("assignment", "PATCH", self.assignment_body())):
            for path in (f"/api/team/not-a-uuid/tickets/{self.ticket_id}/{action}", f"{self.tickets_path()}/not-a-uuid/{action}"):
                self.assert_error(self.client.request(method, path, headers=self.headers(), json=body), 422, "VALIDATION_ERROR")
        self.assert_original()

    def test_audit_insert_failure_rolls_back_both_assignment_and_audit(self):
        constraint = "ck_test_reject_assignment_actor"
        with self.engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE ticket_assignment_audit_logs ADD CONSTRAINT {constraint} CHECK (actor_user_id <> '{self.actor_id}')"))

        def cleanup():
            with self.engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE ticket_assignment_audit_logs DROP CONSTRAINT {constraint}"))

        self.addCleanup(cleanup)
        with self.assertLogs(self.errors.logger, level="ERROR"):
            response = self.reassign()
        self.assert_error(response, 500, "INTERNAL_ERROR")
        self.assert_original()

    def test_deferred_audit_commit_failure_rolls_back_a_successful_assignment_flush(self):
        with self.engine.begin() as conn:
            conn.execute(text("""
                CREATE FUNCTION reject_assignment_commit() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN RAISE EXCEPTION 'forced assignment commit failure' USING ERRCODE='23514'; END $$
            """))
            conn.execute(text("""
                CREATE CONSTRAINT TRIGGER reject_assignment_commit_trigger
                AFTER INSERT ON ticket_assignment_audit_logs DEFERRABLE INITIALLY DEFERRED
                FOR EACH ROW EXECUTE FUNCTION reject_assignment_commit()
            """))

        def cleanup():
            with self.engine.begin() as conn:
                conn.execute(text("DROP TRIGGER reject_assignment_commit_trigger ON ticket_assignment_audit_logs"))
                conn.execute(text("DROP FUNCTION reject_assignment_commit()"))

        self.addCleanup(cleanup)
        updates = []

        def observe(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().lower().startswith("update tickets"):
                updates.append(statement)

        event.listen(self.engine, "after_cursor_execute", observe)
        try:
            with self.assertLogs(self.errors.logger, level="ERROR"):
                response = self.reassign()
        finally:
            event.remove(self.engine, "after_cursor_execute", observe)
        self.assertEqual(len(updates), 1)
        self.assert_error(response, 500, "INTERNAL_ERROR")
        self.assert_original()

    def test_handoff_moves_operation_rights_to_new_assignee_and_reviewer(self):
        with self.sessions.begin() as db:
            db.get(self.Ticket, self.ticket_id).status = "needs_info"
        moved = self.reassign()
        self.assertEqual(moved.status_code, 200, moved.text)
        old_submit = self.client.post(self.path("submit"), headers=self.headers(self.outsider_id), json={"processing_result": "Old actor"})
        self.assert_error(old_submit, 403, "TICKET_ASSIGNEE_REQUIRED")
        self.assert_error(self.review(), 404, "TICKET_DETAIL_NOT_FOUND")
        submitted = self.client.post(self.path("submit"), headers=self.headers(self.next_assignee_id), json={"processing_result": "New actor work"})
        self.assertEqual(submitted.status_code, 200, submitted.text)
        self.assertEqual(submitted.json()["review_comment"], "Previous feedback")
        approved = self.review(actor_id=self.next_reviewer_id)
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(self.saved().status, "completed")

    def test_handoff_allows_removing_old_members_and_preserves_assignment_history(self):
        self.assertEqual(self.reassign().status_code, 200)
        audit = self.audits()[0]
        for user_id in (self.outsider_id, self.reviewer_id):
            response = self.client.delete(f"{self.members_path()}/{user_id}", headers=self.headers())
            self.assertEqual(response.status_code, 204, response.text)
        for user_id in (self.next_assignee_id, self.next_reviewer_id):
            response = self.client.delete(f"{self.members_path()}/{user_id}", headers=self.headers())
            self.assert_error(response, 409, "MEMBER_HAS_OPEN_TICKETS")
        self.assertEqual([row.id for row in self.audits()], [audit.id])
        with self.sessions() as db:
            self.assertIsNotNone(db.get(self.User, self.outsider_id))
            self.assertIsNotNone(db.get(self.User, self.reviewer_id))
        self.assertEqual(self.saved().created_by, self.outsider_id)

    def test_assignment_lock_wait_rechecks_the_admin_role_and_account_state(self):
        for state, code in (("employee", "ADMIN_REQUIRED"), ("disabled", "ACCOUNT_DISABLED")):
            with self.subTest(state=state):
                def change(db):
                    if state == "disabled":
                        db.get(self.User, self.actor_id).is_active = False
                    else:
                        db.get(self.TeamMember, self.member_a_id).role = "employee"

                self.assert_error(self.wait_then_change("assignment", change), 403, code)
                with self.sessions.begin() as restore:
                    restore.get(self.User, self.actor_id).is_active = True
                    restore.get(self.TeamMember, self.member_a_id).role = "admin"
                self.assert_original()

    def test_assignment_lock_wait_rechecks_the_new_members_eligibility(self):
        response = self.wait_then_change("assignment", lambda db: setattr(db.get(self.User, self.next_reviewer_id), "is_active", False))
        self.assert_error(response, 400, "INVALID_TICKET_ASSIGNMENT")
        self.assert_original()

    def test_cached_assignment_and_snapshot_are_refreshed_before_service_write(self):
        with self.sessions() as cached:
            ticket = cached.get(self.Ticket, self.ticket_id)
            body = self.ticket_schemas.TicketReassign(**self.assignment_body(expected=ticket.updated_at))
            with self.sessions.begin() as writer:
                changed = writer.get(self.Ticket, self.ticket_id)
                changed.assignee_id = self.next_assignee_id
                changed.review_comment = "Fresh feedback"
            self.assertEqual(ticket.assignee_id, self.outsider_id)
            with self.assertRaises(self.errors.AppHTTPException) as caught:
                self.ticket_service.reassign_ticket(
                    cached, team_id=self.team_a_id, actor_user_id=self.actor_id, ticket_id=self.ticket_id, body=body,
                )
            self.assertEqual(caught.exception.code, "TICKET_ASSIGNMENT_OUTDATED")
            self.assertEqual(ticket.review_comment, "Fresh feedback")
        self.assertEqual(self.audits(), [])

    def test_concurrent_reassignments_accept_one_snapshot_and_write_one_audit(self):
        expected = self.saved().updated_at
        responses = self.concurrent([
            ("assignment", self.assignment_body(expected=expected)),
            ("assignment", self.assignment_body(assignee=self.reviewer_id, reviewer=self.next_assignee_id, expected=expected)),
        ])
        self.assertEqual(sorted(item.status_code for item in responses), [200, 409])
        winner = next(item for item in responses if item.status_code == 200)
        loser = next(item for item in responses if item.status_code == 409)
        self.assertEqual(loser.json()["code"], "TICKET_ASSIGNMENT_OUTDATED")
        self.assertEqual(str(self.saved().assignee_id), winner.json()["assignee_id"])
        self.assertEqual(str(self.saved().reviewer_id), winner.json()["reviewer_id"])
        self.assertEqual(len(self.audits()), 1)

    def test_review_and_reassignment_race_cannot_approve_work_from_a_stale_assignment(self):
        expected = self.saved().updated_at
        reviewed, reassigned = self.concurrent([
            ("review", self.review_body(comment="Accepted", expected=expected)),
            ("assignment", self.assignment_body(assignee=self.next_assignee_id, reviewer=self.reviewer_id, expected=expected)),
        ])
        if reviewed.status_code == 200:
            self.assert_error(reassigned, 409, "TICKET_STATUS_CONFLICT")
            self.assertEqual((self.saved().status, self.saved().assignee_id), ("completed", self.outsider_id))
            self.assertEqual(self.audits(), [])
        else:
            self.assertEqual(reassigned.status_code, 200, reassigned.text)
            self.assert_error(reviewed, 409, "TICKET_REVIEW_OUTDATED")
            self.assertEqual((self.saved().status, self.saved().assignee_id), ("awaiting_review", self.next_assignee_id))
            self.assertEqual(self.saved().review_comment, "Previous feedback")
            self.assertEqual(len(self.audits()), 1)

    def test_new_openapi_bodies_expose_only_review_or_assignment_fields(self):
        document = self.client.get("/openapi.json").json()
        for path, method, names in (
            ("/api/team/{team_id}/tickets/{ticket_id}/review", "post", {"decision", "expected_updated_at", "comment"}),
            ("/api/team/{team_id}/tickets/{ticket_id}/assignment", "patch", {"assignee_id", "reviewer_id", "expected_updated_at"}),
        ):
            operation = document["paths"][path][method]
            self.assertTrue(operation["security"])
            self.assertEqual({item["name"] for item in operation["parameters"]}, {"team_id", "ticket_id"})
            body = operation["requestBody"]["content"]["application/json"]["schema"]
            schema = document["components"]["schemas"][body["$ref"].rsplit("/", 1)[-1]]
            self.assertEqual(set(schema["properties"]), names)
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(schema["properties"]["expected_updated_at"]["format"], "date-time")
            self.assertIn("200", operation["responses"])


if __name__ == "__main__":
    unittest.main()
