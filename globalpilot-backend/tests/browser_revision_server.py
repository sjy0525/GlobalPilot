"""Disposable real-HTTP browser fixture for the targeted-revision frontend.

Run from the backend directory with ``.venv/bin/python -B -m
tests.browser_revision_server``. It starts its own PostgreSQL container, mounts
the real API routes and JWT/password authentication, and substitutes only model
inference. No application .env file or existing database is used. Ctrl+C closes
the server, removes its schema/container and deletes the private fixture file.
The /__test__ routes exist only in this explicit fixture, never in product API.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import time
from urllib.parse import quote
from uuid import uuid4

from fastapi import Request


def _forbid_embedding_inference(scope, embeddings):
    """Block every sync/async embedding entry point on the shared adapter.

    PGVector and the memory store retain this adapter before the browser
    fixture is built, so patch its class methods rather than replacing only
    the module variable. Missing model injection must fail explicitly.
    """
    from unittest.mock import patch

    def denied(*args, **kwargs):
        raise AssertionError("External embedding inference is forbidden in browser verification")

    async def denied_async(*args, **kwargs):
        raise AssertionError("External embedding inference is forbidden in browser verification")

    for name in ("embed_query", "embed_documents"):
        scope.enter_context(patch.object(type(embeddings), name, denied))
    for name in ("aembed_query", "aembed_documents"):
        scope.enter_context(patch.object(type(embeddings), name, denied_async))


def _docker(*args: str) -> str:
    return subprocess.check_output(["docker", *args], text=True).strip()


def start_database():
    name = "globalpilot-revision-browser-" + uuid4().hex[:10]
    password = secrets.token_urlsafe(24)
    _docker(
        "run", "--detach", "--rm", "--name", name,
        "--env", "POSTGRES_USER=postgres",
        "--env", "POSTGRES_DB=test_globalpilot_revision_browser",
        "--env", "POSTGRES_PASSWORD=" + password,
        "--publish", "127.0.0.1::5432", "pgvector/pgvector:0.8.6-pg18",
    )
    try:
        port = int(_docker("port", name, "5432/tcp").rsplit(":", 1)[1])
        for _ in range(100):
            result = subprocess.run(
                ["docker", "exec", name, "pg_isready", "-U", "postgres"],
                capture_output=True,
            )
            if result.returncode == 0:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("Browser test PostgreSQL did not become ready")
        url = (
            "postgresql+psycopg://postgres:" + quote(password, safe="")
            + f"@127.0.0.1:{port}/test_globalpilot_revision_browser"
        )
        return name, url
    except BaseException:
        _docker("rm", "--force", name)
        raise


def build_app():
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import JSONResponse
    from sqlalchemy import select

    from tests.test_marketing_revision_api_pg import RevisionAPIPostgresTests

    fixture_class = RevisionAPIPostgresTests
    fixture_class.setUpClass()
    from app.infra.ai.embeddings import embeddings

    _forbid_embedding_inference(fixture_class.scope, embeddings)
    fixture = fixture_class()
    fixture.setUp()

    # Product startup would normally register these models and prepare stores.
    # The fixture uses the same isolated schema without starting any workers.
    from app.api.router import api_router
    from app.db.base import Base
    import app.db.model_registry  # noqa: F401
    from app.modules.memory.store import open_memory_store

    Base.metadata.create_all(fixture.engine)
    with open_memory_store() as store:
        store.setup()

    with fixture.sessions.begin() as db:
        actor = db.get(fixture.User, fixture.actor_id)
        actor.email = "revision-browser@example.invalid"
        actor.nickname = "改稿测试员"
        actor.password_hash = fixture.security.hash_password("BrowserRevision2026!")
        peer = db.get(fixture.User, fixture.peer_id)
        peer.email = "revision-peer@example.invalid"
        peer.nickname = "另一位审核员"
        peer.password_hash = fixture.security.hash_password("BrowserPeer2026!")
        db.get(fixture.Team, fixture.team_id).name = "定向改稿浏览器测试"
        db.get(fixture.Brand, fixture.brand_id).name = "Wearables"

    app = FastAPI(title="GlobalPilot isolated revision browser verification")
    fixture.errors.install_exception_handlers(app)
    app.include_router(api_router)

    def request_session():
        with fixture.sessions() as db:
            yield db

    app.dependency_overrides[fixture.database.get_db] = request_session
    control_key = secrets.token_urlsafe(24)
    state = {
        "mode": "normal", "delay_ms": 0, "lose_next_response_for": None,
        "requests": [],
    }
    actual_reviser = fixture.fake_reviser

    def reviser(messages):
        delay = state["delay_ms"]
        if delay:
            time.sleep(delay / 1000)
        return actual_reviser(messages)

    # Existing fixture factory reads this instance method at each invocation.
    fixture.fake_reviser = reviser

    def authorize_control(request):
        if request.headers.get("x-revision-test-key") != control_key:
            raise HTTPException(403, "Browser fixture control key required")

    def public_info():
        return {
            "scope": "isolated PostgreSQL + real routes/auth; offline model adapters",
            "actor_id": str(fixture.actor_id), "team_id": str(fixture.team_id),
            "brief_id": str(fixture.brief_id), "peer_brief_id": str(fixture.peer_brief_id),
            "brand_id": str(fixture.brand_id), "evidence_id": str(fixture.evidence_id),
            "platform": fixture.platform,
            "email": "revision-browser@example.invalid",
            "password": "BrowserRevision2026!",
            "peer_email": "revision-peer@example.invalid",
            "peer_password": "BrowserPeer2026!",
        }

    @app.middleware("http")
    async def inspect_requests(request: Request, call_next):
        relevant = (
            request.method == "POST"
            and ("/revisions" in request.url.path or "/revision-tasks/" in request.url.path)
        )
        payload = None
        if relevant:
            try:
                payload = json.loads(await request.body())
            except ValueError:
                pass
        response = await call_next(request)
        if relevant:
            state["requests"].append({
                "path": request.url.path, "body": payload,
                "status": response.status_code,
            })
        lost = state["lose_next_response_for"]
        if lost and relevant and request.url.path.endswith(lost) and response.status_code < 300:
            state["lose_next_response_for"] = None
            return JSONResponse(
                {"detail": "测试：响应丢失，请使用原请求重试", "code": "SERVICE_UNAVAILABLE"},
                status_code=503,
            )
        return response

    @app.get("/__test__/info")
    def info():
        return public_info()

    @app.post("/__test__/mode")
    async def mode(request: Request):
        authorize_control(request)
        body = await request.json()
        selected = body.get("mode", "normal")
        if selected not in {"normal", "clarification", "budget", "failure", "slow"}:
            raise HTTPException(422, "Unknown fixture mode")
        state["mode"] = selected
        state["delay_ms"] = min(10000, max(0, int(body.get("delay_ms", 1500 if selected == "slow" else 0))))
        state["lose_next_response_for"] = body.get("lose_next_response_for")
        fixture.reviser_mode = "error" if selected == "failure" else "changed"
        fixture.reviewer_mode = (
            "needs_clarification" if selected == "clarification"
            else "needs_revision" if selected == "budget" else "passed"
        )
        return {"mode": selected, "delay_ms": state["delay_ms"]}

    @app.post("/__test__/mutate-evidence")
    def mutate_evidence(request: Request):
        authorize_control(request)
        with fixture.sessions.begin() as db:
            doc = db.get(fixture.KnowledgeDocument, fixture.evidence_id)
            doc.version += 1
            doc.body += "\nUpdated fixture evidence."
        return {"changed": True}

    @app.post("/__test__/new-brief")
    async def new_brief(request: Request):
        authorize_control(request)
        body = await request.json()
        identifier = uuid4()
        with fixture.sessions.begin() as db:
            db.add(fixture.Brief(
                id=identifier, team_id=fixture.team_id, user_id=fixture.actor_id,
                brand_id=fixture.brand_id, goal=body.get("goal", "浏览器测试：指定段落改稿"),
                selling_points="65W output", region="eu-na", user_persona="buyer",
                social_media=[fixture.platform, "instagram"], workflow_status="completed",
                workflow_version=1, content=deepcopy(fixture.original_content),
            ))
        fixture.seed_parent_review(identifier, fixture.actor_id)
        return {"brief_id": str(identifier)}

    @app.get("/__test__/snapshot")
    def snapshot(request: Request):
        authorize_control(request)
        with fixture.sessions() as db:
            brief = db.get(fixture.Brief, fixture.brief_id)
            tasks = db.scalars(select(fixture.models.RevisionTask).where(
                fixture.models.RevisionTask.user_id == fixture.actor_id,
            ).order_by(fixture.models.RevisionTask.created_at)).all()
            samples = db.scalars(select(fixture.models.RevisionSample).where(
                fixture.models.RevisionSample.user_id == fixture.actor_id,
            )).all()
            result = {
                "content": brief.content, "workflow_checkpoint_id": brief.workflow_checkpoint_id,
                "workflow_status": brief.workflow_status,
                "tasks": [fixture.service._to_response(fixture.service._row_dict(task)).model_dump(mode="json") for task in tasks],
                "samples": [sample.data for sample in samples],
                "model_revisions": fixture.model_revisions,
                "model_reviews": fixture.model_reviews,
                "requests": state["requests"], "mode": state["mode"],
            }
        return result

    return app, fixture, fixture_class, public_info() | {"control_key": control_key}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--info-file", type=Path)
    args = parser.parse_args()
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", args.port))
        except OSError as exc:
            raise SystemExit("Requested backend port is already occupied; no process was changed") from exc

    container = None
    fixture = fixture_class = None
    info_path = args.info_file
    try:
        container, database_url = start_database()
        os.environ["GLOBALPILOT_REVISION_TEST_DATABASE_URL"] = database_url
        app, fixture, fixture_class, info = build_app()
        if info_path is None:
            descriptor, raw_path = tempfile.mkstemp(prefix="globalpilot-revision-browser-", suffix=".json")
            os.close(descriptor)
            info_path = Path(raw_path)
        info_path.write_text(json.dumps(info | {"database_url": database_url, "container": container}, ensure_ascii=False))
        info_path.chmod(0o600)
        print("BROWSER_HARNESS_READY " + json.dumps({
            "backend": f"http://127.0.0.1:{args.port}",
            "info_file": str(info_path), "container": container,
            **{key: value for key, value in info.items() if key != "control_key"},
        }, ensure_ascii=False), flush=True)
        import uvicorn
        uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, log_level="warning")
    finally:
        if fixture is not None:
            fixture.doCleanups()
        if fixture_class is not None:
            fixture_class.doClassCleanups()
        if container is not None:
            _docker("rm", "--force", container)
        if info_path is not None:
            info_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
