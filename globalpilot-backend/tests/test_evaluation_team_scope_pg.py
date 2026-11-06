"""Team-scoped evaluation endpoints with real PostgreSQL/JWT and no providers."""

import importlib
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from tests.test_team_authorization_pg import TeamAuthorizationPostgresFixture


@unittest.skipUnless(os.environ.get("TEAM_AUTH_TEST_DATABASE_URL"), "requires explicit isolated PostgreSQL")
class EvaluationTeamScopeTests(TeamAuthorizationPostgresFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.evaluation = importlib.import_module("app.modules.evaluation.service")
        cls.runner = importlib.import_module("app.modules.evaluation.engine.runner")
        cls.versions = importlib.import_module("app.modules.evaluation.engine.versions")
        cls.eval_routes = importlib.import_module("app.modules.evaluation.router")
        cls.eval_schemas = importlib.import_module("app.modules.evaluation.schemas")
        models = importlib.import_module("app.modules.evaluation.models")
        cls.EvalRun, cls.EvalTask = models.EvalRun, models.EvalTask
        cls.EvalCase, cls.EvalTestSet = models.EvalCase, models.EvalTestSet
        cls.EvalExecutorLease = models.EvalExecutorLease
        cls.Brand = importlib.import_module("app.modules.brands.models").Brand
        cls.KnowledgeDocument = importlib.import_module("app.modules.rag.models").KnowledgeDocument
        cls.extra_models = (
            cls.Brand, cls.KnowledgeDocument, cls.EvalTestSet, cls.EvalCase,
            cls.EvalRun, cls.EvalTask, cls.EvalExecutorLease,
        )
        with cls.engine.begin() as conn:
            for model in cls.extra_models:
                model.__table__.create(conn, checkfirst=True)
        for module in (cls.evaluation, cls.runner):
            setting = patch.object(module, "sessionLocal", cls.sessions)
            setting.start()
            cls.addClassCleanup(setting.stop)

    def setUp(self):
        with self.sessions.begin() as db:
            for model in reversed(self.extra_models):
                db.execute(model.__table__.delete())
        super().setUp()
        self.app.include_router(self.eval_routes.router, prefix="/api")
        for target, name, value in (
            (self.evaluation, "launch", lambda token: None),
            (self.evaluation, "workflow_version", lambda: "team-test-workflow"),
            (self.versions, "workflow_version", lambda: "team-test-workflow"),
            (self.versions, "prompt_identity", lambda: ("test-prompt", "test-hash")),
        ):
            setting = patch.object(target, name, value)
            setting.start()
            self.addCleanup(setting.stop)
        with self.sessions.begin() as db:
            db.add(self.TeamMember(
                team_id=self.team_a_id, user_id=self.outsider_id,
                role="employee", status="active",
            ))
            test_set = self.EvalTestSet(
                version=self.evaluation.TEST_SET_VERSION, name="Synthetic team set", description="Synthetic only",
            )
            db.add(test_set)
            db.flush()
            self.test_set_id = test_set.id
            case = self.EvalCase(
                test_set_id=test_set.id, code="T1", ordinal=1, title="Synthetic case",
                user_request="Synthetic request", brand_profile="Synthetic category",
                brand_name="Wearables", platforms=[], must_include=[], forbidden_claims=[],
                region="eu-na", persona="Synthetic user",
            )
            db.add(case)
            db.flush()
            self.case_id = case.id
            self.run_ids = {}
            self.task_ids = {}
            for key, team_id, user_id in (
                ("a", self.team_a_id, self.actor_id),
                ("a2", self.team_a_id, self.actor_id),
                ("b", self.team_b_id, self.actor_id),
                ("other", self.team_a_id, self.outsider_id),
                ("legacy", None, self.actor_id),
            ):
                run = self.EvalRun(
                    team_id=team_id, user_id=user_id, test_set_id=test_set.id,
                    label=key, status="completed", provenance={"test_set_version": "synthetic"},
                )
                db.add(run)
                db.flush()
                task = self.EvalTask(
                    run_id=run.id, case_id=case.id, execution_status="completed",
                    scores=self.scores(), auto_scores=self.scores(), qualified=True,
                )
                db.add(task)
                db.flush()
                self.run_ids[key], self.task_ids[key] = run.id, task.id
            self.brand_ids = {}
            for key, team_id in (("a", self.team_a_id), ("b", self.team_b_id)):
                brand = self.Brand(team_id=team_id, name="Wearables")
                db.add(brand)
                db.flush()
                self.brand_ids[key] = brand.id
                db.add(self.KnowledgeDocument(
                    team_id=team_id, brand_id=brand.id, source=f"{key}-private-source", chunk_count=1,
                ))

    @staticmethod
    def scores():
        return {key: {"verdict": "pass", "reason": "synthetic", "source": "model"}
                for key in ("facts", "completeness", "tone", "format")}

    def url(self, suffix, *, team_id=None):
        joiner = "&" if "?" in suffix else "?"
        return f"/api/evals{suffix}{joiner}team_id={team_id or self.team_a_id}"

    def get(self, suffix, *, team_id=None, user_id=None):
        return self.client.get(self.url(suffix, team_id=team_id), headers=self.headers(user_id))

    def paths(self):
        run_id, task_id = self.run_ids["a"], self.task_ids["a"]
        return (
            ("GET", "/snapshot", None), ("GET", "/testset", None),
            ("GET", "/runs", None),
            ("POST", "/runs", {"workflow_version": "team-test-workflow"}),
            ("GET", f"/runs/{run_id}", None), ("GET", f"/runs/{run_id}/tasks", None),
            ("POST", f"/runs/{run_id}/baseline", None), ("POST", f"/runs/{run_id}/report", None),
            ("GET", f"/compare?left={run_id}&right={self.run_ids['a2']}", None),
            ("PATCH", f"/tasks/{task_id}", {"facts": "fail", "note": "synthetic correction"}),
        )

    def test_all_ten_routes_require_login(self):
        for method, path, body in self.paths():
            with self.subTest(path=path):
                response = self.client.request(method, self.url(path), json=body)
                self.assertEqual(response.status_code, 401, response.text)

    def test_all_ten_routes_require_explicit_team(self):
        for method, path, body in self.paths():
            with self.subTest(path=path):
                response = self.client.request(method, f"/api/evals{path}", headers=self.headers(), json=body)
                self.assertEqual(response.status_code, 422, response.text)

    def test_all_ten_routes_reject_removed_members_with_old_jwt(self):
        headers = self.headers()
        with self.sessions.begin() as db:
            db.execute(update(self.TeamMember).where(self.TeamMember.id == self.member_a_id).values(status="removed"))
        for method, path, body in self.paths():
            with self.subTest(path=path):
                response = self.client.request(method, self.url(path), headers=headers, json=body)
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(response.json()["code"], "TEAM_ACCESS_DENIED")
        self.assertEqual(self.get("/runs", team_id=self.team_b_id).status_code, 200)

    def test_all_ten_routes_reject_disabled_accounts(self):
        for method, path, body in self.paths():
            with self.subTest(path=path):
                response = self.client.request(
                    method, self.url(path), headers=self.headers(self.disabled_id), json=body,
                )
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(response.json()["code"], "ACCOUNT_DISABLED")

    def test_lists_filter_team_owner_and_unassigned_history(self):
        a = self.get("/runs")
        b = self.get("/runs", team_id=self.team_b_id)
        self.assertEqual(a.status_code, 200, a.text)
        self.assertEqual(b.status_code, 200, b.text)
        self.assertEqual({row["label"] for row in a.json()}, {"a", "a2"})
        self.assertEqual({row["label"] for row in b.json()}, {"b"})
        self.assertTrue(all(row["team_id"] == str(self.team_a_id) for row in a.json()))

    def test_cross_team_other_owner_legacy_and_missing_details_are_uniform_404(self):
        for run_id in (self.run_ids["b"], self.run_ids["other"], self.run_ids["legacy"], uuid4()):
            for suffix in (f"/runs/{run_id}", f"/runs/{run_id}/tasks"):
                with self.subTest(path=suffix):
                    response = self.get(suffix)
                    self.assertEqual(response.status_code, 404, response.text)

    def test_cross_team_writes_and_compare_do_not_mutate(self):
        for key in ("b", "other", "legacy"):
            run_id = self.run_ids[key]
            for suffix in ("baseline", "report"):
                response = self.client.post(self.url(f"/runs/{run_id}/{suffix}"), headers=self.headers())
                self.assertEqual(response.status_code, 404, response.text)
            response = self.client.patch(
                self.url(f"/tasks/{self.task_ids[key]}"), headers=self.headers(),
                json={"facts": "fail", "note": "Must be rejected"},
            )
            self.assertEqual(response.status_code, 404, response.text)
            response = self.get(f"/compare?left={self.run_ids['a']}&right={run_id}")
            self.assertEqual(response.status_code, 404, response.text)
        with self.sessions() as db:
            self.assertEqual(db.query(self.EvalRun).filter(self.EvalRun.is_baseline.is_(True)).count(), 0)
            self.assertIsNone(db.get(self.EvalTask, self.task_ids["b"]).human_note)

    def test_administrator_role_does_not_grant_other_users_evaluations(self):
        self.assertEqual(self.get(f"/runs/{self.run_ids['other']}").status_code, 404)
        headers = self.headers()
        with self.sessions.begin() as db:
            db.execute(update(self.TeamMember).where(self.TeamMember.id == self.member_a_id).values(role="employee"))
        response = self.client.get(self.url(f"/runs/{self.run_ids['a']}"), headers=headers)
        self.assertEqual(response.status_code, 200, response.text)

    def test_baselines_are_independent_between_teams(self):
        for key, team_id in (("a", self.team_a_id), ("b", self.team_b_id), ("a2", self.team_a_id)):
            response = self.client.post(
                self.url(f"/runs/{self.run_ids[key]}/baseline", team_id=team_id), headers=self.headers(),
            )
            self.assertEqual(response.status_code, 200, response.text)
        with self.sessions() as db:
            baselines = set(db.scalars(select(self.EvalRun.id).where(self.EvalRun.is_baseline.is_(True))))
            self.assertEqual(baselines, {self.run_ids["a2"], self.run_ids["b"]})

    def test_database_rejects_duplicate_baseline_in_same_team(self):
        with self.assertRaises(IntegrityError):
            with self.sessions.begin() as db:
                db.execute(update(self.EvalRun).where(self.EvalRun.team_id == self.team_a_id).values(is_baseline=True))

    def test_allowed_task_review_updates_only_current_team(self):
        response = self.client.patch(
            self.url(f"/tasks/{self.task_ids['a']}"), headers=self.headers(),
            json={"facts": "fail", "note": "Synthetic reviewer correction"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        with self.sessions() as db:
            self.assertEqual(db.get(self.EvalTask, self.task_ids["a"]).scores["facts"]["verdict"], "fail")
            self.assertEqual(db.get(self.EvalTask, self.task_ids["b"]).scores["facts"]["verdict"], "pass")

    def test_snapshot_knowledge_version_is_current_team_only(self):
        before_a = self.get("/snapshot")
        before_b = self.get("/snapshot", team_id=self.team_b_id)
        self.assertEqual(before_a.status_code, 200, before_a.text)
        self.assertEqual(before_b.status_code, 200, before_b.text)
        with self.sessions.begin() as db:
            db.execute(update(self.KnowledgeDocument).where(
                self.KnowledgeDocument.team_id == self.team_b_id,
            ).values(source="Changed only B"))
        after_a = self.get("/snapshot").json()
        after_b = self.get("/snapshot", team_id=self.team_b_id).json()
        self.assertEqual(before_a.json()["knowledge_version"], after_a["knowledge_version"])
        self.assertNotEqual(before_b.json()["knowledge_version"], after_b["knowledge_version"])

    def test_new_run_is_bound_by_server_and_location_contains_team(self):
        response = self.client.post(
            self.url("/runs"), headers=self.headers(),
            json={"workflow_version": "team-test-workflow", "label": "new scoped run"},
        )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertIn(f"team_id={self.team_a_id}", response.headers["Location"])
        self.assertEqual(response.json()["team_id"], str(self.team_a_id))
        with self.sessions() as db:
            run = db.get(self.EvalRun, response.json()["id"])
            self.assertEqual((run.team_id, run.user_id), (self.team_a_id, self.actor_id))

    def test_body_cannot_override_team_or_owner(self):
        response = self.client.post(
            self.url("/runs"), headers=self.headers(),
            json={"workflow_version": "team-test-workflow", "team_id": str(self.team_b_id)},
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_direct_services_recheck_members_after_route_snapshot(self):
        with self.sessions.begin() as db:
            db.execute(update(self.TeamMember).where(self.TeamMember.id == self.member_a_id).values(status="removed"))
        operations = (
            lambda: self.evaluation.set_baseline(self.actor_id, self.run_ids["a"], team_id=self.team_a_id),
            lambda: self.evaluation.rebuild_report(self.actor_id, self.run_ids["a"], team_id=self.team_a_id),
            lambda: self.evaluation.review_task(
                self.actor_id, self.task_ids["a"], self.eval_schemas.EvalReviewInput(facts="fail", note="Denied"),
                team_id=self.team_a_id,
            ),
            lambda: self.evaluation.start_run(
                self.actor_id, self.eval_schemas.EvalRunCreate(workflow_version="team-test-workflow"),
                team_id=self.team_a_id,
            ),
        )
        for operation in operations:
            with self.assertRaises(HTTPException) as error:
                operation()
            self.assertEqual(error.exception.status_code, 403)
        with self.sessions() as db:
            with self.assertRaises(HTTPException) as error:
                self.evaluation.list_runs(db, SimpleNamespace(id=self.actor_id), team_id=self.team_a_id)
            self.assertEqual(error.exception.status_code, 403)

    def test_worker_uses_team_brand_and_passes_scope_to_retrieval(self):
        run = self.evaluation.start_run(
            self.actor_id, self.eval_schemas.EvalRunCreate(workflow_version="team-test-workflow"),
            team_id=self.team_b_id,
        )
        with self.sessions() as db:
            lease = db.get(self.EvalExecutorLease, "global")
            token = importlib.import_module("app.modules.evaluation.engine.lease").LeaseToken(
                run_id=run.id, owner=lease.owner, generation=lease.generation,
            )
        observed = []
        outcome = dict(
            execution_status="completed", empty_output=False, timed_out=False, qualified=True,
            content={}, strategy="synthetic", retrieved="synthetic", duration_ms=1,
            api_cost_usd=0, judge_cost_usd=0, usage={}, error=None, scores=self.scores(),
        )
        def fake_case(payload):
            observed.append(payload)
            return outcome
        with patch.object(self.runner, "_run_case", side_effect=fake_case):
            self.runner.execute(token)
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["brand_id"], self.brand_ids["b"])
        self.assertEqual(observed[0]["team_id"], self.team_b_id)
        self.assertEqual(observed[0]["actor_user_id"], self.actor_id)

    def test_removed_member_stops_worker_and_cleanup_releases_lease(self):
        run = self.evaluation.start_run(
            self.actor_id, self.eval_schemas.EvalRunCreate(workflow_version="team-test-workflow"),
            team_id=self.team_a_id,
        )
        with self.sessions.begin() as db:
            lease = db.get(self.EvalExecutorLease, "global")
            token = importlib.import_module("app.modules.evaluation.engine.lease").LeaseToken(
                run_id=run.id, owner=lease.owner, generation=lease.generation,
            )
            db.execute(update(self.TeamMember).where(self.TeamMember.id == self.member_a_id).values(status="removed"))
        with patch.object(self.runner, "_run_case") as provider:
            with self.assertRaises(HTTPException) as error:
                self.runner.execute(token)
            self.assertEqual(error.exception.status_code, 403)
            provider.assert_not_called()
        self.runner._fail_run(token, "Team access revoked")
        with self.sessions() as db:
            self.assertIsNone(db.get(self.EvalExecutorLease, "global").owner)
            self.assertEqual(db.get(self.EvalRun, run.id).status, "failed")

    def test_membership_removal_during_provider_io_discards_output(self):
        run = self.evaluation.start_run(
            self.actor_id, self.eval_schemas.EvalRunCreate(workflow_version="team-test-workflow"),
            team_id=self.team_a_id,
        )
        with self.sessions() as db:
            lease = db.get(self.EvalExecutorLease, "global")
            token = importlib.import_module("app.modules.evaluation.engine.lease").LeaseToken(
                run_id=run.id, owner=lease.owner, generation=lease.generation,
            )
            task_id = db.scalar(select(self.EvalTask.id).where(self.EvalTask.run_id == run.id))

        def revoke_during_io(payload):
            # This is a separate committed transaction while no team lock is
            # retained by the worker's model call.
            with self.sessions.begin() as db:
                db.execute(update(self.TeamMember).where(
                    self.TeamMember.id == self.member_a_id,
                ).values(status="removed"))
            return {"content": {"private": "must never be published"}}

        with patch.object(self.runner, "_run_case", side_effect=revoke_during_io):
            with self.assertRaises(HTTPException) as error:
                self.runner._execute_task(token, task_id)
            self.assertEqual(error.exception.status_code, 403)
        self.runner._fail_run(token, "Team access revoked during generation")
        with self.sessions() as db:
            self.assertIsNone(db.get(self.EvalTask, task_id).generated_content)
            self.assertIsNone(db.get(self.EvalExecutorLease, "global").owner)

    def test_openapi_all_ten_routes_require_team_and_bearer(self):
        schema = self.app.openapi()
        paths = [(p, operations) for p, operations in schema["paths"].items() if p.startswith("/api/evals")]
        count = 0
        for path, operations in paths:
            for operation in operations.values():
                count += 1
                self.assertTrue(operation.get("security"), path)
                parameter = next(p for p in operation["parameters"] if p["name"] == "team_id")
                self.assertTrue(parameter["required"], path)
                self.assertEqual(parameter["in"], "query")
        self.assertEqual(count, 10)
