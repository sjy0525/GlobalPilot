"""Shared authorization helpers and ticket creation on disposable PostgreSQL."""

import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier, Event
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import event, select

if __package__:
    from . import test_team_authorization_pg as team_authorization
else:
    import test_team_authorization_pg as team_authorization


class TeamAuthorizationUtilsAndTicketsPostgresTests(team_authorization.TeamAuthorizationPostgresFixture):
    def setUp(self):
        super().setUp()
        with self.sessions.begin() as db:
            reviewer = self.User(email="ticket-reviewer@example.com", is_active=True)
            db.add(reviewer)
            db.flush()
            assignee = self.TeamMember(
                team_id=self.team_a_id, user_id=self.outsider_id,
                role="employee", status="active",
            )
            db.add_all([
                assignee,
                self.TeamMember(
                    team_id=self.team_a_id, user_id=reviewer.id,
                    role="employee", status="active",
                ),
            ])
            db.flush()
            self.reviewer_id, self.reviewer_email = reviewer.id, reviewer.email
            self.assignee_member_id = assignee.id

    def body(self, **overrides):
        return {
            "title": "Create ticket", "description": "Details",
            "assignee_id": str(self.outsider_id), "reviewer_id": str(self.reviewer_id),
            **overrides,
        }

    def create(self, *, actor_id=None, team_id=None, body=None, client=None):
        return (client or self.client).post(
            self.tickets_path(team_id), headers=self.headers(actor_id),
            json=body if body is not None else self.body(),
        )

    def tickets(self):
        with self.sessions() as db:
            return db.scalars(select(self.Ticket)).all()

    def target_membership(self):
        with self.sessions() as db:
            return db.get(self.TeamMember, self.assignee_member_id)

    def foreign_member(self):
        with self.sessions.begin() as db:
            user = self.User(email=f"foreign-{uuid4().hex}@example.com", is_active=True)
            db.add(user)
            db.flush()
            db.add(self.TeamMember(
                team_id=self.team_b_id, user_id=user.id, role="admin", status="active"
            ))
            return user.id

    def direct_team_operations(self):
        return (
            (self.team_service.add_team_member, {"email": self.reviewer_email}),
            (self.team_service.update_team_member_role, {
                "target_user_id": self.outsider_id, "role": "admin",
            }),
            (self.team_service.remove_team_member, {"target_user_id": self.outsider_id}),
        )

    def wait_then_change_state(self, *, change):
        reached_lock = Event()

        def observe_lock(conn, cursor, statement, parameters, context, executemany):
            normalized = " ".join(statement.lower().split())
            if normalized.startswith("select") and "from teams" in normalized and "for update" in normalized:
                reached_lock.set()

        def independent_create():
            with TestClient(
                self.app, base_url="https://testserver", raise_server_exceptions=False
            ) as client:
                return self.create(client=client)

        with self.sessions() as holder:
            holder.query(self.Team.id).filter(self.Team.id == self.team_a_id).with_for_update().one()
            event.listen(self.engine, "before_cursor_execute", observe_lock)
            workers = ThreadPoolExecutor(max_workers=1)
            try:
                future = workers.submit(independent_create)
                self.assertTrue(reached_lock.wait(timeout=3))
                change(holder)
                holder.commit()
                return future.result(timeout=15)
            finally:
                holder.rollback()
                event.remove(self.engine, "before_cursor_execute", observe_lock)
                workers.shutdown(wait=True)

    def test_active_member_helper_refreshes_a_cached_role_before_admin_checks(self):
        with self.sessions() as stale:
            cached = stale.get(self.TeamMember, self.member_a_id)
            self.assertEqual(cached.role, "admin")
            with self.sessions.begin() as writer:
                writer.get(self.TeamMember, self.member_a_id).role = "employee"
            self.assertEqual(cached.role, "admin")
            refreshed = self.team_authorization_utils.require_active_team_member(
                stale, team_id=self.team_a_id, user_id=self.actor_id,
            )
            self.assertIs(refreshed, cached)
            self.assertEqual(refreshed.role, "employee")
            with self.assertRaises(self.errors.AppHTTPException) as caught:
                self.team_authorization_utils.require_team_admin(refreshed)
            self.assertEqual((caught.exception.status_code, caught.exception.code), (403, "ADMIN_REQUIRED"))

    def test_active_member_helper_rejects_removed_or_disabled_state_despite_cached_entities(self):
        for state, code in (("removed", "TEAM_ACCESS_DENIED"), ("disabled", "ACCOUNT_DISABLED")):
            with self.subTest(state=state):
                with self.sessions() as stale:
                    cached_member = stale.get(self.TeamMember, self.member_a_id)
                    cached_user = stale.get(self.User, self.actor_id)
                    self.assertEqual(cached_member.status, "active")
                    self.assertTrue(cached_user.is_active)
                    with self.sessions.begin() as writer:
                        if state == "removed":
                            writer.get(self.TeamMember, self.member_a_id).status = "removed"
                        else:
                            writer.get(self.User, self.actor_id).is_active = False
                    with self.assertRaises(self.errors.AppHTTPException) as caught:
                        self.team_authorization_utils.require_active_team_member(
                            stale, team_id=self.team_a_id, user_id=self.actor_id,
                        )
                    self.assertEqual((caught.exception.status_code, caught.exception.code), (403, code))
                with self.sessions.begin() as restore:
                    restore.get(self.TeamMember, self.member_a_id).status = "active"
                    restore.get(self.User, self.actor_id).is_active = True

    def test_effective_member_ids_accepts_generators_and_filters_each_team_and_state(self):
        foreign_id = self.foreign_member()
        ids = [self.actor_id, self.actor_id, self.outsider_id, self.reviewer_id,
               self.removed_id, self.disabled_id, foreign_id, uuid4()]
        with self.sessions() as db:
            current = self.team_authorization_utils.get_active_team_member_ids(
                db, team_id=self.team_a_id, user_ids=(user_id for user_id in ids),
            )
            other = self.team_authorization_utils.get_active_team_member_ids(
                db, team_id=self.team_b_id, user_ids=ids,
            )
            empty = self.team_authorization_utils.get_active_team_member_ids(
                db, team_id=self.team_a_id, user_ids=iter(()),
            )
        self.assertEqual(current, {self.actor_id, self.outsider_id, self.reviewer_id})
        self.assertEqual(other, {self.actor_id, foreign_id})
        self.assertEqual(empty, set())

    def test_lock_helpers_allow_members_and_admins_at_their_actual_team_role(self):
        with self.sessions() as db:
            team = self.team_authorization_utils.lock_team(db, team_id=self.team_a_id)
            self.assertEqual(team.id, self.team_a_id)
            employee = self.team_authorization_utils.lock_team_and_require_member(
                db, team_id=self.team_b_id, actor_user_id=self.actor_id,
            )
            self.assertEqual(employee.role, "employee")
            admin = self.team_authorization_utils.lock_team_and_require_admin(
                db, team_id=self.team_a_id, actor_user_id=self.actor_id,
            )
            self.assertEqual(admin.user_id, self.actor_id)
            with self.assertRaises(self.errors.AppHTTPException) as caught:
                self.team_authorization_utils.lock_team_and_require_admin(
                    db, team_id=self.team_b_id, actor_user_id=self.actor_id,
                )
            self.assertEqual(caught.exception.code, "ADMIN_REQUIRED")
            with self.assertRaises(self.errors.AppHTTPException) as caught:
                self.team_authorization_utils.lock_team(db, team_id=uuid4())
            self.assertEqual((caught.exception.status_code, caught.exception.code), (404, "TEAM_NOT_FOUND"))

    def test_team_services_reject_an_employee_when_called_without_routes(self):
        for operation, kwargs in self.direct_team_operations():
            with self.subTest(operation=operation.__name__):
                with self.sessions.begin() as db:
                    with self.assertRaises(self.errors.AppHTTPException) as caught:
                        operation(db, team_id=self.team_a_id, actor_user_id=self.outsider_id, **kwargs)
                    self.assertEqual((caught.exception.status_code, caught.exception.code), (403, "ADMIN_REQUIRED"))
        self.assertEqual((self.target_membership().role, self.target_membership().status), ("employee", "active"))

    def test_team_services_reject_invalid_actor_states_when_called_without_routes(self):
        for actor_id, code in (
            (self.removed_id, "TEAM_ACCESS_DENIED"),
            (self.disabled_id, "ACCOUNT_DISABLED"), (uuid4(), "TEAM_ACCESS_DENIED"),
        ):
            for operation, kwargs in self.direct_team_operations():
                with self.subTest(actor_id=str(actor_id), operation=operation.__name__):
                    with self.sessions.begin() as db:
                        with self.assertRaises(self.errors.AppHTTPException) as caught:
                            operation(db, team_id=self.team_a_id, actor_user_id=actor_id, **kwargs)
                        self.assertEqual((caught.exception.status_code, caught.exception.code), (403, code))
        self.assertEqual(self.target_membership().status, "active")
        with self.sessions() as db:
            self.assertEqual(db.scalars(select(self.TeamMemberAuditLog)).all(), [])

    def test_ticket_service_rejects_invalid_actors_when_called_without_routes(self):
        body = self.ticket_schemas.TicketCreate(**self.body())
        for actor_id, code in (
            (self.removed_id, "TEAM_ACCESS_DENIED"),
            (self.disabled_id, "ACCOUNT_DISABLED"), (uuid4(), "TEAM_ACCESS_DENIED"),
        ):
            with self.subTest(actor_id=str(actor_id)):
                with self.sessions.begin() as db:
                    with self.assertRaises(self.errors.AppHTTPException) as caught:
                        self.ticket_service.create_ticket(
                            db, team_id=self.team_a_id, actor_user_id=actor_id, body=body,
                        )
                    self.assertEqual((caught.exception.status_code, caught.exception.code), (403, code))
        self.assertEqual(self.tickets(), [])

    def test_employee_creates_ticket_with_effective_members_and_server_owned_fields(self):
        body = self.body(title="  Create ticket  ", description="  Details  ", assignee_id=str(self.actor_id))
        response = self.create(actor_id=self.outsider_id, body=body)
        self.assertEqual(response.status_code, 201, response.text)
        payload = response.json()
        self.assertEqual(set(payload), {
            "id", "team_id", "title", "description", "created_by", "assignee_id",
            "reviewer_id", "status", "created_at", "updated_at", "processing_result", "review_comment",
        })
        self.assertEqual(payload["team_id"], str(self.team_a_id))
        self.assertEqual(payload["created_by"], str(self.outsider_id))
        self.assertEqual(payload["assignee_id"], str(self.actor_id))
        self.assertEqual(payload["reviewer_id"], str(self.reviewer_id))
        self.assertEqual((payload["title"], payload["description"], payload["status"]), ("Create ticket", "Details", "processing"))
        self.assertIsNone(payload["processing_result"])
        self.assertIsNone(payload["review_comment"])
        for field in ("created_at", "updated_at"):
            self.assertIsNotNone(datetime.fromisoformat(payload[field].replace("Z", "+00:00")).utcoffset())
        with self.sessions() as db:
            saved = db.get(self.Ticket, UUID(payload["id"]))
            self.assertEqual(saved.created_by, self.outsider_id)
            self.assertEqual(saved.status, "processing")
        self.assertNotIn(self.password_hash, response.text)

    def test_admin_can_create_and_share_all_participant_roles_with_the_same_member(self):
        response = self.create(body=self.body(
            assignee_id=str(self.actor_id), reviewer_id=str(self.actor_id),
        ))
        self.assertEqual(response.status_code, 201, response.text)
        for field in ("created_by", "assignee_id", "reviewer_id"):
            self.assertEqual(response.json()[field], str(self.actor_id))
        self.assertEqual(len(self.tickets()), 1)

    def test_ticket_creation_defaults_an_omitted_optional_description_to_empty_text(self):
        body = self.body()
        del body["description"]
        response = self.create(actor_id=self.outsider_id, body=body)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["description"], "")
        self.assertEqual(response.json()["created_by"], str(self.outsider_id))
        self.assertEqual(response.json()["status"], "processing")
        saved = self.tickets()
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].description, "")

    def test_ticket_assignments_reject_unknown_removed_disabled_and_foreign_members(self):
        foreign_id = self.foreign_member()
        for field in ("assignee_id", "reviewer_id"):
            for user_id in (uuid4(), self.removed_id, self.disabled_id, foreign_id):
                with self.subTest(field=field, user_id=str(user_id)):
                    response = self.create(body=self.body(**{field: str(user_id)}))
                    self.assert_error(response, 400, "INVALID_TICKET_ASSIGNMENT")
                    self.assertEqual(self.tickets(), [])

    def test_ticket_creation_rejects_invalid_requester_memberships(self):
        foreign_id = self.foreign_member()
        for user_id, code in (
            (self.removed_id, "TEAM_ACCESS_DENIED"),
            (self.disabled_id, "ACCOUNT_DISABLED"), (foreign_id, "TEAM_ACCESS_DENIED"),
        ):
            with self.subTest(user_id=str(user_id)):
                self.assert_error(self.create(actor_id=user_id), 403, code)
        self.assert_error(self.create(actor_id=self.outsider_id, team_id=self.team_b_id), 403, "TEAM_ACCESS_DENIED")
        self.assertEqual(self.tickets(), [])

    def test_ticket_body_rejects_server_owned_field_injection_and_invalid_required_values(self):
        for extra in (
            {"created_by": str(self.actor_id)}, {"team_id": str(self.team_b_id)},
            {"status": "completed"}, {"role": "admin"}, {"created_at": "2020-01-01T00:00:00Z"},
        ):
            with self.subTest(extra=extra):
                response = self.create(body=self.body(**extra))
                self.assert_error(response, 422, "VALIDATION_ERROR")
        for changed in ({"title": "   "}, {"assignee_id": "not-a-uuid"}, {"reviewer_id": None}):
            with self.subTest(changed=changed):
                self.assert_error(self.create(body=self.body(**changed)), 422, "VALIDATION_ERROR")
        self.assertEqual(self.tickets(), [])

    def test_ticket_service_leaves_commit_or_rollback_to_its_caller(self):
        with self.sessions() as db:
            created = self.ticket_service.create_ticket(
                db, team_id=self.team_a_id, actor_user_id=self.outsider_id,
                body=self.ticket_schemas.TicketCreate(**self.body()),
            )
            self.assertIsNotNone(db.get(self.Ticket, created.id))
            db.rollback()
        self.assertEqual(self.tickets(), [])

    def test_waiting_ticket_creation_rechecks_the_requesters_membership_after_the_lock(self):
        response = self.wait_then_change_state(
            change=lambda db: setattr(db.get(self.TeamMember, self.member_a_id), "status", "removed")
        )
        self.assert_error(response, 403, "TEAM_ACCESS_DENIED")
        self.assertEqual(self.tickets(), [])

    def test_waiting_ticket_creation_rechecks_the_assigned_member_after_the_lock(self):
        response = self.wait_then_change_state(
            change=lambda db: setattr(db.get(self.TeamMember, self.assignee_member_id), "status", "removed")
        )
        self.assert_error(response, 400, "INVALID_TICKET_ASSIGNMENT")
        self.assertEqual(self.tickets(), [])

    def test_unfinished_handling_or_review_assignments_prevent_member_removal(self):
        for status in ("processing", "awaiting_review", "needs_info"):
            for relation in ("assignee", "reviewer"):
                with self.subTest(status=status, relation=relation):
                    with self.sessions.begin() as db:
                        ticket = self.Ticket(
                            team_id=self.team_a_id, title="Open assignment", description="",
                            created_by=self.actor_id, status=status,
                            assignee_id=self.outsider_id if relation == "assignee" else self.actor_id,
                            reviewer_id=self.outsider_id if relation == "reviewer" else self.reviewer_id,
                        )
                        db.add(ticket)
                        db.flush()
                        ticket_id = ticket.id
                    removed = self.client.delete(
                        f"{self.members_path()}/{self.outsider_id}", headers=self.headers(),
                    )
                    self.assert_error(removed, 409, "MEMBER_HAS_OPEN_TICKETS")
                    self.assertEqual(self.target_membership().status, "active")
                    with self.sessions.begin() as db:
                        db.delete(db.get(self.Ticket, ticket_id))
        with self.sessions() as db:
            self.assertEqual(db.scalars(select(self.TeamMemberAuditLog)).all(), [])

    def test_completed_and_creator_only_tickets_allow_removal_and_remain_historical(self):
        with self.sessions.begin() as db:
            db.add_all([
                self.Ticket(
                    team_id=self.team_a_id, title="Completed", description="",
                    created_by=self.actor_id, assignee_id=self.outsider_id,
                    reviewer_id=self.reviewer_id, status="completed",
                ),
                self.Ticket(
                    team_id=self.team_a_id, title="Creator only", description="",
                    created_by=self.outsider_id, assignee_id=self.actor_id,
                    reviewer_id=self.reviewer_id, status="processing",
                ),
            ])
        response = self.client.delete(
            f"{self.members_path()}/{self.outsider_id}", headers=self.headers(),
        )
        self.assertEqual(response.status_code, 204, response.text)
        self.assertEqual(self.target_membership().status, "removed")
        saved = self.tickets()
        self.assertEqual(len(saved), 2)
        self.assertTrue(any(ticket.created_by == self.outsider_id for ticket in saved))
        with self.sessions() as db:
            self.assertIsNotNone(db.get(self.User, self.outsider_id))

    def test_other_team_assignments_do_not_prevent_removal_from_this_team(self):
        with self.sessions.begin() as db:
            db.add(self.TeamMember(
                team_id=self.team_b_id, user_id=self.outsider_id, role="employee", status="active"
            ))
            db.add(self.Ticket(
                team_id=self.team_b_id, title="Other team work", description="",
                created_by=self.actor_id, assignee_id=self.outsider_id,
                reviewer_id=self.actor_id, status="processing",
            ))
        response = self.client.delete(
            f"{self.members_path()}/{self.outsider_id}", headers=self.headers(),
        )
        self.assertEqual(response.status_code, 204, response.text)
        self.assertEqual(self.target_membership().status, "removed")
        self.assertEqual(len(self.tickets()), 1)
        with self.sessions() as db:
            other = db.scalar(select(self.TeamMember).where(
                self.TeamMember.team_id == self.team_b_id, self.TeamMember.user_id == self.outsider_id,
            ))
            self.assertEqual(other.status, "active")

    def test_concurrent_creation_and_removal_cannot_leave_a_ticket_assigned_to_a_removed_member(self):
        gate = Barrier(2)
        lock_attempts = []

        def synchronize_team_locks(conn, cursor, statement, parameters, context, executemany):
            normalized = " ".join(statement.lower().split())
            if normalized.startswith("select") and "from teams" in normalized and "for update" in normalized:
                lock_attempts.append(normalized)
                gate.wait(timeout=5)

        def independent(operation):
            with TestClient(
                self.app, base_url="https://testserver", raise_server_exceptions=False
            ) as client:
                if operation == "create":
                    return self.create(client=client)
                return client.delete(
                    f"{self.members_path()}/{self.outsider_id}", headers=self.headers(),
                )

        event.listen(self.engine, "before_cursor_execute", synchronize_team_locks)
        try:
            with ThreadPoolExecutor(max_workers=2) as workers:
                creation = workers.submit(independent, "create")
                removal = workers.submit(independent, "remove")
                created, removed = creation.result(timeout=15), removal.result(timeout=15)
        finally:
            event.remove(self.engine, "before_cursor_execute", synchronize_team_locks)
        self.assertEqual(len(lock_attempts), 2)
        if created.status_code == 201:
            self.assert_error(removed, 409, "MEMBER_HAS_OPEN_TICKETS")
            self.assertEqual(self.target_membership().status, "active")
            self.assertEqual(len(self.tickets()), 1)
        else:
            self.assert_error(created, 400, "INVALID_TICKET_ASSIGNMENT")
            self.assertEqual(removed.status_code, 204, removed.text)
            self.assertEqual(self.target_membership().status, "removed")
            self.assertEqual(self.tickets(), [])

    def test_ticket_openapi_has_one_member_scoped_post_with_only_client_owned_body_fields(self):
        response = self.client.get("/openapi.json")
        self.assertEqual(response.status_code, 200, response.text)
        document = response.json()
        operation = document["paths"]["/api/team/{team_id}/tickets"]["post"]
        self.assertTrue(operation["security"])
        self.assertIn("201", operation["responses"])
        self.assertEqual([parameter["name"] for parameter in operation["parameters"]], ["team_id"])
        body = operation["requestBody"]["content"]["application/json"]["schema"]
        schema = document["components"]["schemas"][body["$ref"].rsplit("/", 1)[-1]]
        self.assertEqual(set(schema["properties"]), {"title", "description", "assignee_id", "reviewer_id"})
        self.assertEqual(set(schema["required"]), {"title", "assignee_id", "reviewer_id"})
        self.assertFalse(schema["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
