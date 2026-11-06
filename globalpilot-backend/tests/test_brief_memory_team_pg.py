"""Real PostgreSQL/JWT/HTTP team boundaries for Brief, workflow and memory.

The language model and public-provider calls are replaced. Membership, Brief
ownership, worker scope, memory namespaces and writes use real PostgreSQL.
"""

import importlib
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

import psycopg
from langgraph.store.postgres import PostgresStore
from psycopg.rows import dict_row
from psycopg.conninfo import make_conninfo
from sqlalchemy import select, text

from tests.test_team_authorization_pg import TeamAuthorizationPostgresFixture


class BriefMemoryTeamPostgresTests(TeamAuthorizationPostgresFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Brand = importlib.import_module("app.modules.brands.models").Brand
        cls.Brief = importlib.import_module("app.modules.briefs.models").Brief
        cls.MemoryJob = importlib.import_module("app.modules.memory.models").MemoryJob
        cls.brief_routes = importlib.import_module("app.modules.briefs.router")
        cls.memory_routes = importlib.import_module("app.modules.memory.router")
        cls.workflow_routes = importlib.import_module("app.modules.marketing.router")
        cls.workflow = importlib.import_module("app.modules.marketing.service")
        cls.memory = importlib.import_module("app.modules.memory.services.memory_job")
        cls.brief_service = importlib.import_module("app.modules.briefs.service")
        cls.memory_schema = importlib.import_module("app.modules.memory.schemas.brand_memory")
        cls.memory_storage = importlib.import_module("app.modules.memory.store")
        cls.execution = importlib.import_module("app.modules.marketing.workflow.execution")
        raw_uri = cls.engine.url.set(drivername="postgresql").difference_update_query(["options"]).render_as_string(hide_password=False)
        cls.uri = make_conninfo(
            raw_uri, options=f"-csearch_path={cls.schema} -cstatement_timeout=10000 -clock_timeout=5000",
        )
        with cls.engine.begin() as conn:
            for model in (cls.Brand, cls.Brief, cls.MemoryJob):
                model.__table__.create(conn, checkfirst=True)
        with psycopg.connect(cls.uri, autocommit=True, row_factory=dict_row) as conn:
            assert conn.execute("SELECT current_schema() AS schema").fetchone()["schema"] == cls.schema, "test Store must stay in its private schema"
            PostgresStore(conn).setup()

    @classmethod
    @contextmanager
    def store(cls):
        with psycopg.connect(cls.uri, autocommit=True, row_factory=dict_row) as conn:
            yield PostgresStore(conn)

    def setUp(self):
        with self.engine.begin() as conn:
            for model in (self.MemoryJob, self.Brief, self.Brand):
                conn.execute(model.__table__.delete())
            conn.execute(text("DELETE FROM store"))
        super().setUp()
        self.app.include_router(self.brief_routes.router, prefix="/api")
        self.app.include_router(self.memory_routes.router, prefix="/api")
        self.app.include_router(self.workflow_routes.router, prefix="/api")
        with self.sessions.begin() as db:
            db.add(self.TeamMember(team_id=self.team_a_id, user_id=self.outsider_id,
                                   role="employee", status="active"))
            a = self.Brand(team_id=self.team_a_id, name="Wearables")
            b = self.Brand(team_id=self.team_b_id, name="Wearables")
            legacy = self.Brand(name="Legacy unassigned")
            db.add_all([a, b, legacy])
            db.flush()
            self.brand_a_id, self.brand_b_id, self.legacy_brand_id = a.id, b.id, legacy.id
            self.briefs = {}
            for key, team, owner, brand in (
                ("a", self.team_a_id, self.actor_id, a.id),
                ("b", self.team_b_id, self.actor_id, b.id),
                ("other", self.team_a_id, self.outsider_id, a.id),
                ("legacy", None, self.actor_id, legacy.id),
            ):
                row = self.Brief(team_id=team, user_id=owner, brand_id=brand,
                                 goal=f"Private {key}", selling_points="Blue", region="eu-na",
                                 user_persona="Readers", social_media=["linkedin"],
                                 workflow_status="completed", final_output=f"Private final {key}")
                db.add(row)
                self.briefs[key] = row
            db.flush()
            self.ids = {key: row.id for key, row in self.briefs.items()}
        execution = patch.object(
            self.workflow, "workflow_execution",
            side_effect=lambda brief_id: self.execution.workflow_execution(brief_id, database_url=self.uri),
        )
        execution.start()
        self.addCleanup(execution.stop)
        opened = patch.object(self.memory, "open_memory_store", self.store)
        opened.start()
        self.addCleanup(opened.stop)

    def payload(self, **changes):
        return {"brand_id": str(self.brand_a_id), "goal": "New goal", "selling_points": "Blue",
                "region": "eu-na", "user_persona": "Readers", "social_media": ["linkedin"], **changes}

    def request(self, method, path, *, team=None, user=None, **kwargs):
        params = kwargs.pop("params", {}) | {"team_id": str(team or self.team_a_id)}
        return self.client.request(method, path, params=params, headers=self.headers(user), **kwargs)

    def snapshot(self, key="a", *, final=True):
        row = self.briefs[key]
        return SimpleNamespace(
            values={"final_output": f"Final {key}", "team_id": row.team_id,
                    "actor_user_id": row.user_id} if final else {},
            config={"configurable": {"checkpoint_id": f"checkpoint-{key}"}} if final else {},
            next=(), tasks=(), interrupts=(),
        )

    def remove_actor(self):
        with self.sessions.begin() as db:
            member = db.get(self.TeamMember, self.member_a_id)
            member.status = "removed"

    def memory_path(self, key="a"):
        return f"/api/briefs/{self.ids[key]}/memories"

    def seed_memory(self, key="a", content="Team A secret"):
        row = self.briefs[key]
        scope = self.memory_schema.MemoryScope(
            team_id=row.team_id, user_id=row.user_id, brand_profile_id=row.brand_id,
        )
        value = self.memory_schema.BrandMemory(content=content, regions="eu-na", sources=[
            self.memory_schema.MemorySource(brief_id=row.id, checkpoint_id="source", quote=content),
        ])
        with self.store() as store:
            self.memory_storage.save_memory(store, scope, value)
        return scope, value

    def test_all_eight_routes_require_login_and_explicit_team(self):
        definitions = [
            ("GET", "/api/briefs", None), ("POST", "/api/briefs", self.payload()),
            ("GET", self.memory_path(), None),
            ("GET", "/api/briefs/workflow", None),
            ("POST", "/api/briefs/workflow", {"brief_id": str(self.ids["a"])}),
            ("POST", "/api/briefs/workflow/stream", {"brief_id": str(self.ids["a"])}),
            ("POST", "/api/briefs/workflow/review", {"brief_id": str(self.ids["a"]),
                "expected_checkpoint_id": "checkpoint-a", "action": "approved"}),
            ("POST", "/api/briefs/workflow/review/stream", {"brief_id": str(self.ids["a"]),
                "expected_checkpoint_id": "checkpoint-a", "action": "approved"}),
        ]
        for method, path, body in definitions:
            kwargs = {"json": body} if body else {}
            with self.subTest(path=path, method=method):
                response = self.client.request(method, path, params={"team_id": str(self.team_a_id)}, **kwargs)
                self.assertEqual(response.status_code, 401, response.text)
                response = self.client.request(method, path, headers=self.headers(), **kwargs)
                self.assertEqual(response.status_code, 422, response.text)

    def test_create_stamps_server_team_and_user(self):
        response = self.request("POST", "/api/briefs", user=self.outsider_id, json=self.payload())
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["team_id"], str(self.team_a_id))
        self.assertEqual(response.json()["user_id"], str(self.outsider_id))
        with self.sessions() as db:
            self.assertEqual(db.get(self.Brief, UUID(response.json()["id"])).team_id, self.team_a_id)

    def test_create_cannot_forge_team_user_or_role(self):
        for field, value in (("team_id", str(self.team_b_id)), ("user_id", str(self.outsider_id)), ("role", "admin")):
            with self.subTest(field=field):
                response = self.request("POST", "/api/briefs", json=self.payload(**{field: value}))
                self.assertEqual(response.status_code, 422, response.text)

    def test_create_rejects_other_team_and_unassigned_brand(self):
        for brand in (self.brand_b_id, self.legacy_brand_id, uuid4()):
            response = self.request("POST", "/api/briefs", json=self.payload(brand_id=str(brand)))
            self.assertEqual(response.status_code, 404, response.text)

    def test_list_scopes_same_user_to_selected_team_and_excludes_other_owners_and_legacy(self):
        for team, key in ((self.team_a_id, "a"), (self.team_b_id, "b")):
            response = self.request("GET", "/api/briefs", team=team)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual([row["id"] for row in response.json()], [str(self.ids[key])])

    def test_admin_does_not_gain_other_creators_marketing_data(self):
        with patch.object(self.memory, "open_memory_store", side_effect=AssertionError("must not read")):
            response = self.request("GET", self.memory_path("other"))
            self.assertEqual(response.status_code, 404, response.text)
        with patch.object(self.workflow.graph, "get_state", side_effect=AssertionError("must not read")):
            response = self.request("GET", "/api/briefs/workflow", params={"brief_id": str(self.ids["other"])})
            self.assertEqual(response.status_code, 404, response.text)

    def test_wrong_team_same_owner_and_legacy_memories_are_uniform_404(self):
        for key in ("b", "legacy"):
            response = self.request("GET", self.memory_path(key))
            self.assertEqual(response.status_code, 404, response.text)

    def test_current_team_workflow_read_succeeds(self):
        with patch.object(self.workflow.graph, "get_state", return_value=self.snapshot()):
            response = self.request("GET", "/api/briefs/workflow", params={"brief_id": str(self.ids["a"])})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["final_output"], "Final a")

    def test_unscoped_or_wrong_scope_checkpoint_is_not_returned_or_resumed(self):
        for values in (
            {"final_output": "Legacy global context"},
            {"final_output": "Legacy global context", "team_id": self.team_b_id,
             "actor_user_id": self.actor_id},
            {"final_output": "Legacy global context", "team_id": self.team_a_id,
             "actor_user_id": self.outsider_id},
        ):
            snapshot = self.snapshot()
            snapshot.values = values
            with patch.object(self.workflow.graph, "get_state", return_value=snapshot), \
                 patch.object(self.workflow.graph, "invoke") as invoke:
                response = self.request("GET", "/api/briefs/workflow", params={"brief_id": str(self.ids["a"])})
                self.assertEqual(response.status_code, 409, response.text)
                self.assertNotIn("Legacy global context", response.text)
                response = self.request("POST", "/api/briefs/workflow", json={"brief_id": str(self.ids["a"])})
                self.assertEqual(response.status_code, 409, response.text)
                invoke.assert_not_called()

    def test_all_workflow_posts_reject_cross_team_before_checkpoint_access(self):
        for suffix in ("", "/stream", "/review", "/review/stream"):
            body = {"brief_id": str(self.ids["b"])}
            if "review" in suffix:
                body.update(expected_checkpoint_id="checkpoint-b", action="approved")
            with patch.object(self.workflow.graph, "get_state", side_effect=AssertionError("must not read")):
                response = self.request("POST", "/api/briefs/workflow" + suffix, json=body)
            self.assertEqual(response.status_code, 404, response.text)

    def test_old_jwt_after_member_removal_denies_every_module(self):
        headers = self.headers()
        self.remove_actor()
        for path, params in (("/api/briefs", {}), (self.memory_path(), {}),
                             ("/api/briefs/workflow", {"brief_id": str(self.ids["a"])})):
            response = self.client.get(path, headers=headers, params=params | {"team_id": str(self.team_a_id)})
            self.assertEqual(response.status_code, 403, response.text)
        response = self.client.post("/api/briefs/workflow", headers=headers,
            params={"team_id": str(self.team_a_id)}, json={"brief_id": str(self.ids["a"])})
        self.assertEqual(response.status_code, 403, response.text)

    def test_disabled_account_and_nonmember_cannot_read(self):
        for user in (self.disabled_id, uuid4()):
            response = self.request("GET", "/api/briefs", user=user)
            self.assertIn(response.status_code, (401, 403), response.text)
        response = self.request("GET", "/api/briefs", user=self.removed_id)
        self.assertEqual(response.status_code, 403, response.text)

    def test_role_change_preserves_owner_rule_without_stale_admin_bypass(self):
        headers = self.headers()
        with self.sessions.begin() as db:
            db.get(self.TeamMember, self.member_a_id).role = "employee"
        response = self.client.get("/api/briefs", params={"team_id": str(self.team_a_id)}, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([row["id"] for row in response.json()], [str(self.ids["a"])])

    def test_real_memory_store_is_partitioned_by_team(self):
        self.seed_memory("a", "Only team A")
        self.seed_memory("b", "Only team B")
        for team, key, content in ((self.team_a_id, "a", "Only team A"), (self.team_b_id, "b", "Only team B")):
            response = self.request("GET", self.memory_path(key), team=team)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual([row["content"] for row in response.json()["items"]], [content])

    def test_v1_personal_memories_are_not_implicitly_exposed_in_v2(self):
        _, value = self.seed_memory()
        with self.store() as store:
            store.put(("brand_preferences_v1", str(self.actor_id), str(self.brand_a_id)),
                      str(uuid4()), value.model_copy(update={"content": "Unassigned legacy"}).model_dump(mode="json"))
        response = self.request("GET", self.memory_path())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([row["content"] for row in response.json()["items"]], ["Team A secret"])

    def test_member_revoked_while_memory_store_is_read_cannot_receive_result(self):
        self.seed_memory()
        @contextmanager
        def delayed():
            with self.store() as store:
                def search(*args, **kwargs):
                    rows = store.search(*args, **kwargs)
                    self.remove_actor()
                    return rows
                yield SimpleNamespace(search=search)
        with patch.object(self.memory, "open_memory_store", delayed):
            response = self.request("GET", self.memory_path())
        self.assertEqual(response.status_code, 403, response.text)
        self.assertNotIn("Team A secret", response.text)

    def test_workflow_rechecks_after_checkpoint_read(self):
        def load(*args):
            self.remove_actor()
            return self.snapshot()
        with patch.object(self.workflow.graph, "get_state", side_effect=load):
            response = self.request("GET", "/api/briefs/workflow", params={"brief_id": str(self.ids["a"])})
        self.assertEqual(response.status_code, 403, response.text)
        self.assertNotIn("Final a", response.text)

    def test_execution_layer_rejects_revoked_member_without_http_dependencies(self):
        self.remove_actor()
        with self.execution.workflow_execution(self.ids["a"], database_url=self.uri) as guard:
            with self.assertRaises(Exception) as caught:
                self.workflow._load_execution_brief(guard, self.actor_id, team_id=self.team_a_id)
            self.assertEqual(caught.exception.status_code, 404)

    def test_execution_layer_rejects_wrong_team_and_other_owner(self):
        for key, team in (("a", self.team_b_id), ("other", self.team_a_id), ("legacy", self.team_a_id)):
            with self.execution.workflow_execution(self.ids[key], database_url=self.uri) as guard:
                with self.assertRaises(Exception) as caught:
                    self.workflow._load_execution_brief(guard, self.actor_id, team_id=team)
                self.assertEqual(caught.exception.status_code, 404)

    def test_workflow_cannot_publish_after_member_removed_during_model_work(self):
        def invoke(*args, **kwargs):
            self.remove_actor()
        snapshots = [self.snapshot(final=False), self.snapshot(final=False), self.snapshot(), self.snapshot()]
        with patch.object(self.workflow.graph, "get_state", side_effect=snapshots), \
             patch.object(self.workflow.graph, "invoke", side_effect=invoke):
            response = self.request("POST", "/api/briefs/workflow", json={"brief_id": str(self.ids["a"])})
        self.assertEqual(response.status_code, 404, response.text)
        with self.sessions() as db:
            brief = db.get(self.Brief, self.ids["a"])
            self.assertEqual(brief.workflow_version, 0)
            self.assertEqual(brief.final_output, "Private final a")

    def test_stream_does_not_yield_content_after_revocation(self):
        def chunks(*args, **kwargs):
            self.remove_actor()
            yield {"generate_content": {"strategy": "Secret after revocation"}}
        with patch.object(self.workflow.graph, "get_state", return_value=self.snapshot(final=False)), \
             patch.object(self.workflow.graph, "stream", side_effect=chunks):
            response = self.request("POST", "/api/briefs/workflow/stream", json={"brief_id": str(self.ids["a"])})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('"type": "error"', response.text)
        self.assertNotIn("Secret after revocation", response.text)

    def test_worker_derives_scope_from_real_brief_and_writes_matching_team_namespace(self):
        with self.sessions.begin() as db:
            job = self.MemoryJob(brief_id=self.ids["a"], user_id=self.actor_id, brand_id=self.brand_a_id,
                source_checkpoint_id="accepted", status="running", payload={
                    "feedback": "Team-scoped preference", "regions": "eu-na", "platforms": ["linkedin"]})
            db.add(job)
            db.flush()
            job_id = job.id
        invoked = []
        def actor(scope, source, **kwargs):
            invoked.append(scope)
            with self.store() as store:
                self.memory_storage.save_memory(store, scope, self.memory_schema.BrandMemory(
                    content=source.quote, regions="eu-na", sources=[source]))
        with psycopg.connect(self.uri, autocommit=True, row_factory=dict_row) as conn:
            job = conn.execute("SELECT * FROM memory_jobs WHERE id = %s", (job_id,)).fetchone()
            with patch("langchain_deepseek.ChatDeepSeek", return_value=Mock()), \
                 patch("app.modules.memory.agent.run_memory_agent", side_effect=actor):
                self.memory._run_job(conn, job)
        self.assertEqual(invoked[0].team_id, self.team_a_id)
        response = self.request("GET", self.memory_path())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([row["content"] for row in response.json()["items"]], ["Team-scoped preference"])
        self.assertEqual(response.json()["jobs"][0]["status"], "completed")

    def test_worker_rejects_removed_member_before_model_client_is_created(self):
        self.remove_actor()
        job = {"id": uuid4(), "brief_id": self.ids["a"], "user_id": self.actor_id,
               "brand_id": self.brand_a_id, "payload": {}}
        with psycopg.connect(self.uri, autocommit=True, row_factory=dict_row) as conn, \
             patch("langchain_deepseek.ChatDeepSeek") as model:
            with self.assertLogs(self.memory.logger, level="ERROR"):
                self.memory._run_job(conn, job)
            model.assert_not_called()

    def test_scope_and_history_namespaces_include_team(self):
        scope = self.memory_schema.MemoryScope(team_id=self.team_a_id, user_id=self.actor_id,
                                               brand_profile_id=self.brand_a_id)
        other = scope.model_copy(update={"team_id": self.team_b_id})
        service = importlib.import_module("app.modules.memory.services.brand_memory")
        self.assertNotEqual(self.memory_storage.memory_namespace(scope), self.memory_storage.memory_namespace(other))
        self.assertNotEqual(service.history_namespace(scope, self.ids["a"]), service.history_namespace(other, self.ids["a"]))
        self.assertNotEqual(service.memory_lock_key(scope), service.memory_lock_key(other))
