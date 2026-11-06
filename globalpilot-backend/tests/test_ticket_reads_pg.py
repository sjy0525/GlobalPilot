"""Ticket list/detail visibility with real JWT and disposable PostgreSQL."""

import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt

if __package__:
    from . import test_team_authorization_pg as team_authorization
else:
    import test_team_authorization_pg as team_authorization


class TicketReadsPostgresTests(team_authorization.TeamAuthorizationPostgresFixture):
    def setUp(self):
        super().setUp()
        self.base_time = datetime(2024, 1, 1, tzinfo=timezone.utc)
        with self.sessions.begin() as db:
            peer_admin = self.User(email="read-peer-admin@example.com", is_active=True)
            peer_employee = self.User(email="read-peer-employee@example.com", is_active=True)
            foreign = self.User(email="read-no-membership@example.com", is_active=True)
            db.add_all([peer_admin, peer_employee, foreign])
            db.flush()
            self.peer_admin_id, self.peer_employee_id, self.foreign_id = (
                peer_admin.id, peer_employee.id, foreign.id
            )
            for team_id in (self.team_a_id, self.team_b_id):
                db.add_all([
                    self.TeamMember(
                        team_id=team_id, user_id=self.outsider_id, role="employee", status="active"
                    ),
                    self.TeamMember(
                        team_id=team_id, user_id=peer_admin.id,
                        role="admin" if team_id == self.team_a_id else "employee", status="active",
                    ),
                    self.TeamMember(
                        team_id=team_id, user_id=peer_employee.id, role="employee", status="active"
                    ),
                ])
            definitions = [
                ("created", self.team_a_id, self.outsider_id, peer_admin.id, peer_employee.id, "completed"),
                ("assigned", self.team_a_id, self.actor_id, self.outsider_id, peer_admin.id, "awaiting_review"),
                ("reviewed", self.team_a_id, peer_admin.id, peer_employee.id, self.outsider_id, "needs_info"),
                ("unrelated", self.team_a_id, self.actor_id, peer_admin.id, peer_employee.id, "completed"),
                ("overlap", self.team_a_id, self.outsider_id, self.outsider_id, self.outsider_id, "processing"),
                ("other_related", self.team_b_id, self.outsider_id, self.actor_id, self.actor_id, "processing"),
                ("other_actor", self.team_b_id, self.actor_id, self.actor_id, self.actor_id, "processing"),
                ("other_unrelated", self.team_b_id, peer_admin.id, peer_admin.id, peer_employee.id, "processing"),
            ]
            self.records = {}
            for index, (name, team_id, creator, assignee, reviewer, status) in enumerate(definitions):
                ticket = self.Ticket(
                    team_id=team_id, title=f"Ticket {name}", description=f"Evidence {name}",
                    created_by=creator, assignee_id=assignee, reviewer_id=reviewer, status=status,
                    created_at=self.base_time + timedelta(minutes=index),
                    updated_at=self.base_time + timedelta(minutes=index),
                )
                db.add(ticket)
                self.records[name] = ticket
            db.flush()
        self.employee_ids = {self.records[name].id for name in ("created", "assigned", "reviewed", "overlap")}
        self.admin_ids = {ticket.id for ticket in self.records.values() if ticket.team_id == self.team_a_id}
        self.actor_employee_ids = {self.records[name].id for name in ("assigned", "unrelated")}

    def detail_path(self, ticket_id, team_id=None):
        return f"{self.tickets_path(team_id)}/{ticket_id}"

    def list_response(self, *, user_id=None, team_id=None, headers=None, params=None):
        return self.client.get(
            self.tickets_path(team_id),
            headers=headers if headers is not None else self.headers(user_id or self.outsider_id),
            params=params,
        )

    def detail_response(self, ticket_id, *, user_id=None, team_id=None, headers=None, params=None):
        return self.client.get(
            self.detail_path(ticket_id, team_id),
            headers=headers if headers is not None else self.headers(user_id or self.outsider_id),
            params=params,
        )

    def result_ids(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsInstance(response.json(), list)
        return {row["id"] for row in response.json()}

    def assert_detail(self, name):
        ticket = self.records[name]
        response = self.detail_response(ticket.id)
        self.assertEqual(response.status_code, 200, response.text)
        row = response.json()
        self.assertEqual(set(row), {
            "id", "team_id", "title", "description", "created_by", "assignee_id",
            "reviewer_id", "status", "created_at", "updated_at", "processing_result", "review_comment",
        })
        for field in ("id", "team_id", "created_by", "assignee_id", "reviewer_id"):
            self.assertEqual(row[field], str(getattr(ticket, field)))
        for field in ("title", "description", "status"):
            self.assertEqual(row[field], getattr(ticket, field))
        self.assertIsNone(row["processing_result"])
        self.assertIsNone(row["review_comment"])
        self.assertNotIn(self.password_hash, response.text)
        return response

    def test_employee_can_read_a_ticket_they_created_without_being_assigned(self):
        self.assert_detail("created")

    def test_employee_can_read_a_ticket_assigned_to_them_without_being_the_creator(self):
        self.assert_detail("assigned")

    def test_employee_can_read_a_ticket_they_review_without_being_creator_or_assignee(self):
        self.assert_detail("reviewed")

    def test_employee_list_uses_the_three_relationships_as_or_without_duplicates_or_team_leaks(self):
        response = self.list_response()
        self.assertEqual(self.result_ids(response), {str(identity) for identity in self.employee_ids})
        self.assertEqual(len(response.json()), 4)
        for row in response.json():
            detail = self.detail_response(row["id"])
            self.assertEqual(detail.status_code, 200, detail.text)
            self.assertEqual(detail.json(), row)
        other = self.list_response(team_id=self.team_b_id)
        self.assertEqual(self.result_ids(other), {str(self.records["other_related"].id)})

    def test_admin_sees_all_tickets_of_the_current_team(self):
        response = self.list_response(user_id=self.actor_id)
        self.assertEqual(self.result_ids(response), {str(identity) for identity in self.admin_ids})
        self.assertEqual(len(response.json()), 5)
        for ticket in self.records.values():
            if ticket.team_id == self.team_a_id:
                detail = self.detail_response(ticket.id, user_id=self.actor_id)
                self.assertEqual(detail.status_code, 200, detail.text)

    def test_admin_role_in_one_team_does_not_expand_employee_scope_in_another(self):
        response = self.list_response(user_id=self.actor_id, team_id=self.team_b_id)
        self.assertEqual(self.result_ids(response), {
            str(self.records["other_related"].id), str(self.records["other_actor"].id),
        })
        self.assert_error(self.detail_response(
            self.records["other_unrelated"].id, user_id=self.actor_id, team_id=self.team_b_id,
        ), 404, "TICKET_DETAIL_NOT_FOUND")

    def test_unrelated_foreign_team_and_nonexistent_details_have_identical_404_payloads(self):
        responses = [
            self.detail_response(self.records["unrelated"].id),
            self.detail_response(self.records["other_related"].id),
            self.detail_response(uuid4()),
            self.detail_response(self.records["created"].id, team_id=self.team_b_id),
        ]
        for response in responses:
            self.assert_error(response, 404, "TICKET_DETAIL_NOT_FOUND")
            self.assertEqual(response.json(), responses[0].json())
        self.assertEqual(responses[0].json(), {
            "detail": "工单不存在", "code": "TICKET_DETAIL_NOT_FOUND",
        })

    def test_invalid_team_or_ticket_uuid_uses_the_422_contract(self):
        for path, field in (
            ("/api/team/not-a-uuid/tickets", "team_id"),
            (f"/api/team/not-a-uuid/tickets/{self.records['created'].id}", "team_id"),
            (f"{self.tickets_path()}/not-a-uuid", "ticket_id"),
        ):
            with self.subTest(field=field, path=path):
                response = self.client.get(path, headers=self.headers(self.outsider_id))
                self.assert_error(response, 422, "VALIDATION_ERROR")
                self.assertIn(["path", field], [item["loc"] for item in response.json()["detail"]])
                self.assertTrue(all("input" not in item for item in response.json()["detail"]))

    def test_both_read_endpoints_require_a_valid_access_token(self):
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
            for path in (self.tickets_path(), self.detail_path(self.records["created"].id)):
                with self.subTest(path=path, authenticated=bool(headers)):
                    response = self.client.get(path, headers=headers)
                    self.assertEqual(response.status_code, 401, response.text)
                    self.assertEqual(response.headers["www-authenticate"], "Bearer")

    def test_removed_disabled_and_nonmember_users_cannot_read_team_tickets(self):
        for user_id, code in (
            (self.removed_id, "TEAM_ACCESS_DENIED"),
            (self.disabled_id, "ACCOUNT_DISABLED"), (self.foreign_id, "TEAM_ACCESS_DENIED"),
        ):
            with self.subTest(user_id=str(user_id)):
                self.assert_error(self.list_response(user_id=user_id), 403, code)
                self.assert_error(self.detail_response(self.records["created"].id, user_id=user_id), 403, code)

    def test_same_jwt_observes_role_downgrade_and_upgrade_on_later_reads(self):
        headers = self.headers()
        self.assertEqual(self.result_ids(self.list_response(headers=headers)), {str(identity) for identity in self.admin_ids})
        manager_headers = self.headers(self.peer_admin_id)
        role_path = f"{self.members_path()}/{self.actor_id}/role"
        lowered = self.client.patch(role_path, headers=manager_headers, json={"role": "employee"})
        self.assertEqual(lowered.status_code, 200, lowered.text)
        self.assertEqual(
            self.result_ids(self.list_response(headers=headers)),
            {str(identity) for identity in self.actor_employee_ids},
        )
        self.assert_error(self.detail_response(
            self.records["created"].id, headers=headers,
        ), 404, "TICKET_DETAIL_NOT_FOUND")
        raised = self.client.patch(role_path, headers=manager_headers, json={"role": "admin"})
        self.assertEqual(raised.status_code, 200, raised.text)
        self.assertEqual(self.result_ids(self.list_response(headers=headers)), {str(identity) for identity in self.admin_ids})

    def test_same_jwt_loses_removed_team_access_but_keeps_its_other_team_membership(self):
        headers = self.headers()
        self.assertEqual(self.list_response(headers=headers).status_code, 200)
        removed = self.client.delete(
            f"{self.members_path()}/{self.actor_id}", headers=self.headers(self.peer_admin_id),
        )
        self.assertEqual(removed.status_code, 204, removed.text)
        self.assert_error(self.list_response(headers=headers), 403, "TEAM_ACCESS_DENIED")
        self.assert_error(self.detail_response(
            self.records["assigned"].id, headers=headers,
        ), 403, "TEAM_ACCESS_DENIED")
        other = self.list_response(team_id=self.team_b_id, headers=headers)
        self.assertEqual(other.status_code, 200, other.text)

    def test_same_jwt_cannot_read_any_team_after_account_deactivation(self):
        headers = self.headers()
        self.assertEqual(self.list_response(headers=headers).status_code, 200)
        with self.sessions.begin() as db:
            db.get(self.User, self.actor_id).is_active = False
        for team_id in (self.team_a_id, self.team_b_id):
            self.assert_error(self.list_response(team_id=team_id, headers=headers), 403, "ACCOUNT_DISABLED")
        self.assert_error(self.detail_response(
            self.records["created"].id, headers=headers,
        ), 403, "ACCOUNT_DISABLED")

    def test_direct_read_services_refresh_a_cached_admin_role_before_applying_scope(self):
        with self.sessions() as cached:
            member = cached.get(self.TeamMember, self.member_a_id)
            self.assertEqual(member.role, "admin")
            initial = self.ticket_service.get_ticket_list(cached, self.team_a_id, self.actor_id)
            self.assertEqual({row.id for row in initial}, self.admin_ids)
            with self.sessions.begin() as writer:
                writer.get(self.TeamMember, self.member_a_id).role = "employee"
            self.assertEqual(member.role, "admin")
            latest = self.ticket_service.get_ticket_list(cached, self.team_a_id, self.actor_id)
            self.assertEqual({row.id for row in latest}, self.actor_employee_ids)
            self.assertEqual(member.role, "employee")
            with self.assertRaises(self.errors.AppHTTPException) as caught:
                self.ticket_service.get_ticket_detail(
                    cached, self.team_a_id, self.actor_id, self.records["created"].id,
                )
            self.assertEqual((caught.exception.status_code, caught.exception.code), (404, "TICKET_DETAIL_NOT_FOUND"))

    def test_direct_read_services_reject_invalid_identities_without_route_dependencies(self):
        for actor_id, code in (
            (self.removed_id, "TEAM_ACCESS_DENIED"), (self.disabled_id, "ACCOUNT_DISABLED"),
            (self.foreign_id, "TEAM_ACCESS_DENIED"), (uuid4(), "TEAM_ACCESS_DENIED"),
        ):
            with self.subTest(actor_id=str(actor_id)):
                with self.sessions() as db:
                    for operation, args in (
                        (self.ticket_service.get_ticket_list, (db, self.team_a_id, actor_id)),
                        (self.ticket_service.get_ticket_detail, (db, self.team_a_id, actor_id, self.records["created"].id)),
                    ):
                        with self.assertRaises(self.errors.AppHTTPException) as caught:
                            operation(*args)
                        self.assertEqual((caught.exception.status_code, caught.exception.code), (403, code))

    def test_list_orders_tickets_by_newest_creation_time_first(self):
        response = self.list_response()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            [row["id"] for row in response.json()],
            [str(self.records[name].id) for name in ("overlap", "reviewed", "assigned", "created")],
        )

    def test_equal_creation_times_use_descending_ids_with_stable_repeated_results(self):
        with self.sessions.begin() as db:
            for identity in self.admin_ids:
                db.get(self.Ticket, identity).created_at = self.base_time
        first, second = self.list_response(), self.list_response()
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(
            [row["id"] for row in first.json()],
            [str(identity) for identity in sorted(self.employee_ids, key=lambda identity: identity.int, reverse=True)],
        )

    def test_list_returns_more_than_one_hundred_visible_tickets_without_truncation(self):
        with self.sessions.begin() as db:
            extra = [
                self.Ticket(
                    team_id=self.team_a_id, title=f"Extra {index}", description="",
                    created_by=self.outsider_id, assignee_id=self.peer_admin_id,
                    reviewer_id=self.peer_employee_id, status="processing",
                )
                for index in range(101)
            ]
            db.add_all(extra)
            db.flush()
            expected = self.employee_ids | {ticket.id for ticket in extra}
        response = self.list_response()
        self.assertEqual(self.result_ids(response), {str(identity) for identity in expected})
        self.assertEqual(len(response.json()), 105)

    def test_query_parameters_cannot_impersonate_an_admin_or_another_user(self):
        forged = {"role": "admin", "actor_user_id": str(self.actor_id),
                  "user_id": str(self.actor_id), "team_id": str(self.team_b_id)}
        response = self.list_response(params=forged)
        self.assertEqual(self.result_ids(response), {str(identity) for identity in self.employee_ids})
        self.assert_error(self.detail_response(
            self.records["unrelated"].id, params=forged,
        ), 404, "TICKET_DETAIL_NOT_FOUND")

    def test_an_active_employee_with_no_relevant_tickets_gets_an_empty_list(self):
        with self.sessions.begin() as db:
            db.add(self.TeamMember(
                team_id=self.team_a_id, user_id=self.foreign_id, role="employee", status="active"
            ))
        response = self.list_response(user_id=self.foreign_id)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), [])
        self.assert_error(self.detail_response(
            self.records["created"].id, user_id=self.foreign_id,
        ), 404, "TICKET_DETAIL_NOT_FOUND")

    def test_read_openapi_has_bearer_security_only_path_parameters_and_no_request_bodies(self):
        response = self.client.get("/openapi.json")
        self.assertEqual(response.status_code, 200, response.text)
        document = response.json()
        for path, names in (
            ("/api/team/{team_id}/tickets", {"team_id"}),
            ("/api/team/{team_id}/tickets/{ticket_id}", {"team_id", "ticket_id"}),
        ):
            operation = document["paths"][path]["get"]
            self.assertTrue(operation["security"])
            self.assertNotIn("requestBody", operation)
            parameters = {item["name"]: item for item in operation["parameters"]}
            self.assertEqual(set(parameters), names)
            for item in parameters.values():
                self.assertEqual(item["in"], "path")
                self.assertTrue(item["required"])
                self.assertEqual(item["schema"]["format"], "uuid")
            self.assertIn("200", operation["responses"])


if __name__ == "__main__":
    unittest.main()
