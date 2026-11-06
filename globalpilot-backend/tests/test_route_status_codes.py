"""Actual route contracts, with isolated dependencies and mocked storage/workers."""
import importlib.util
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from fastapi import FastAPI, Response
from fastapi.testclient import TestClient
import sqlalchemy.orm  # Keep ORM imports outside temporary sys.modules replacements.

from app.core.http_errors import install_exception_handlers


APP_ROOT = Path(__file__).resolve().parents[1] / "app"


def load_isolated(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, APP_ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_db_dependency():
    raise AssertionError("Database dependency must be overridden")


async def fake_async_db_dependency():
    raise AssertionError("Async database dependency must be overridden")


def fake_user_dependency():
    raise AssertionError("Authentication dependency must be overridden")


def mock_module(name: str, functions: tuple[str, ...]):
    module = types.ModuleType(name)
    for function in functions:
        setattr(module, function, Mock(name=function))
    return module


class RouteStatusCodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database = types.ModuleType("app.db.database")
        database.get_db = fake_db_dependency
        database.get_async_db = fake_async_db_dependency
        security = types.ModuleType("app.modules.auth.security")
        security.get_current_user = fake_user_dependency
        authorization = types.ModuleType("app.modules.teams.dependencies")
        authorization.get_current_member_from_query = fake_user_dependency
        rag_service = types.ModuleType("app.modules.rag.service")
        for name in ("aask", "adelete_document", "adocument_file", "aindex_text", "alist_documents"):
            setattr(rag_service, name, AsyncMock(name=name))
        runner = mock_module("app.modules.evaluation.engine.runner", (
            "_fail_run", "launch", "refresh_run_artifacts", "recover_expired_run", "recover_expired_runs",
        ))
        brief_service = mock_module("app.modules.briefs.service", ("create_brief", "list_briefs"))
        with patch.dict(sys.modules, {"app.modules.evaluation.engine.runner": runner}):
            cls.eval_service = load_isolated("route_status_eval_service", "modules/evaluation/service.py")
        with patch.dict(sys.modules, {
            "app.db.database": database,
            "app.modules.auth.security": security,
            "app.modules.teams.dependencies": authorization,
            "app.modules.rag.service": rag_service,
            "app.modules.evaluation.service": cls.eval_service,
            "app.modules.briefs.service": brief_service,
        }):
            cls.rag = load_isolated("route_status_rag", "modules/rag/router.py")
            cls.brands = load_isolated("route_status_brands", "modules/brands/router.py")
            cls.evaluation = load_isolated("route_status_evaluation", "modules/evaluation/router.py")
            cls.brief = load_isolated("route_status_brief", "modules/briefs/router.py")

    def setUp(self):
        self.brand_id = uuid4()
        self.document_id = uuid4()
        self.user = types.SimpleNamespace(id=uuid4(), team_id=uuid4())
        self.user.user_id = self.user.id
        self.db = Mock()
        self.sync_db = Mock()
        self.sync_db.query.return_value.filter.return_value.first.return_value = types.SimpleNamespace(id=self.brand_id)
        self.db.query.return_value.filter.return_value.first.return_value = types.SimpleNamespace(id=self.brand_id)
        self.db.execute = AsyncMock(return_value=Mock())
        self.db.rollback = AsyncMock()
        self.db.execute.return_value.scalar_one_or_none.return_value = types.SimpleNamespace(id=self.brand_id)
        self.app = FastAPI()
        install_exception_handlers(self.app)
        for module in (self.rag, self.brands, self.evaluation, self.brief):
            self.app.include_router(module.router, prefix="/api")
        self.app.dependency_overrides[fake_db_dependency] = lambda: self.sync_db
        async def async_db():
            return self.db
        self.app.dependency_overrides[fake_async_db_dependency] = async_db
        self.app.dependency_overrides[fake_user_dependency] = lambda: self.user
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.document = {
            "id": str(self.document_id), "brand_id": str(self.brand_id), "source": "valid.txt",
            "chunk_count": 1, "version": 1, "created_at": datetime.now(), "updated_at": datetime.now(),
        }

    def test_new_document_is_201_with_fetchable_location_including_brand(self):
        with patch.object(self.rag, "aindex_text", return_value=self.document), \
             patch.object(self.rag, "adocument_file", return_value=Response(b"hello", media_type="text/plain")):
            response = self.client.post(
                "/api/documents", params={"brand_id": str(self.brand_id)},
                files={"file": ("valid.txt", b"hello")},
            )
            self.assertEqual(response.status_code, 201)
            location = response.headers["location"]
            parsed = urlsplit(location)
            self.assertEqual(parsed.path, f"/api/documents/{self.document_id}/file")
            self.assertEqual(parse_qs(parsed.query), {
                "brand_id": [str(self.brand_id)], "team_id": [str(self.user.team_id)],
            })
            self.assertEqual(self.client.get(location).content, b"hello")
            self.assertEqual(response.json()["id"], str(self.document_id))

    def test_document_update_and_json_delete_remain_200(self):
        with patch.object(self.rag, "aindex_text", return_value=self.document):
            response = self.client.put(
                f"/api/documents/{self.document_id}", params={"brand_id": str(self.brand_id), "expected_version": 1},
                files={"file": ("valid.txt", b"hello")},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["id"], str(self.document_id))
        deleted = {"id": str(self.document_id), "deleted_chunks": 1, "version": 1}
        with patch.object(self.rag, "adelete_document", return_value=deleted):
            response = self.client.delete(
                f"/api/documents/{self.document_id}", params={"brand_id": str(self.brand_id), "expected_version": 1},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), deleted)

    def test_document_mutations_require_a_positive_expected_version(self):
        for method in ("PUT", "DELETE"):
            for version in (None, 0, -1):
                params = {"brand_id": str(self.brand_id)}
                if version is not None:
                    params["expected_version"] = version
                response = self.client.request(method, f"/api/documents/{self.document_id}",
                                               params=params, files={"file": ("valid.txt", b"hello")})
                self.assertEqual(response.status_code, 422)
        self.db.execute.assert_not_awaited()

    def test_new_brief_is_201_and_preserves_body_without_invented_location(self):
        body = {
            "brand_id": str(self.brand_id), "goal": "goal", "selling_points": "facts", "region": "eu-na",
            "user_persona": "buyer", "social_media": ["instagram"],
            "purchase_channels": None, "competitor_url": None,
        }
        result = types.SimpleNamespace(
            **body, id=uuid4(), team_id=self.user.team_id, user_id=self.user.id, created_at=datetime.now(), updated_at=datetime.now(),
        )
        with patch.object(self.brief, "create_brief_service", return_value=result):
            response = self.client.post("/api/briefs", json=body)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["id"], str(result.id))
        self.assertNotIn("location", response.headers)

    def test_eval_creation_is_202_with_fetchable_run_location(self):
        run = self.evaluation.EvalRunOut(
            id=uuid4(), team_id=self.user.team_id, label="run", status="queued", is_baseline=False, provenance={}, summary=None,
            report_markdown=None, error=None, started_at=None, finished_at=None,
            created_at=datetime.now(), task_total=30, task_done=0,
        )
        def create_run(user_id, body, *, team_id):
            self.sync_db.rollback.assert_called_once()
            self.assertEqual(user_id, self.user.id)
            self.assertEqual(team_id, self.user.team_id)
            return run

        with patch.object(self.evaluation, "start_run", side_effect=create_run), \
             patch.object(self.evaluation, "get_run", return_value=run):
            response = self.client.post("/api/evals/runs", json={"workflow_version": "current"})
            self.assertEqual(response.status_code, 202)
            location = response.headers["location"]
            self.assertEqual(urlsplit(location).path, f"/api/evals/runs/{run.id}")
            self.assertEqual(self.client.get(location).json()["id"], str(run.id))
            self.assertEqual(response.json()["status"], "queued")

    def test_blank_workflow_version_is_422_before_starting_a_run(self):
        with patch.object(self.evaluation, "start_run") as start:
            response = self.client.post("/api/evals/runs", json={"workflow_version": " \t\n "})
        self.assertEqual(response.status_code, 422)
        start.assert_not_called()

    def test_locked_failed_eval_tasks_use_conflict_status(self):
        run = types.SimpleNamespace(id=uuid4())
        for execution_status, timed_out, empty_output in (
            ("error", False, False), ("timeout", True, False), ("completed", False, True),
        ):
            with self.subTest(status=execution_status, empty_output=empty_output):
                task = types.SimpleNamespace(
                    id=uuid4(), run_id=run.id, case_id=uuid4(), execution_status=execution_status,
                    timed_out=timed_out, empty_output=empty_output,
                )
                write_db = Mock()
                write_db.scalar.side_effect = [run.id, task]
                write_db.get.return_value = types.SimpleNamespace(id=task.case_id)
                factory = MagicMock()
                factory.begin.return_value.__enter__.return_value = write_db
                with patch.object(self.eval_service, "_lock_owned_run", return_value=run), \
                     patch.object(self.eval_service, "lock_team_and_require_member"), \
                     patch.object(self.eval_service, "sessionLocal", factory):
                    response = self.client.patch(
                        f"/api/evals/tasks/{task.id}", json={"facts": "pass", "note": "review"},
                    )
                self.assertEqual(response.status_code, 409)
                self.assertIn("不能从统计里改掉", response.json()["detail"])
                write_db.flush.assert_not_called()
        self.sync_db.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
