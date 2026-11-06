"""Measure real HTTP responsiveness while actual upload/PDF/SQL work runs.

Only an explicitly supplied disposable test database is accepted. The real
application lifespan, routes, PostgreSQL, PGVector, multipart parsing and PDF
spawn pool run in a separate one-worker Uvicorn process. Only external model
and embedding services are replaced. No application .env database is used.
"""
import argparse
import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import multiprocessing
import os
from pathlib import Path
import queue
import socket
import statistics
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def serve(database_url, listener, events, schema):
    os.environ["DATABASE_URL"] = database_url
    sys.path.insert(0, str(ROOT))
    import threading
    import types
    import uvicorn
    from langchain_core.embeddings import Embeddings

    request_case = ContextVar("http_check_case", default="startup")

    def emit(kind, **fields):
        events.put({"kind": kind, "at": time.perf_counter(), **fields})

    class LocalEmbeddings(Embeddings):
        def embed_documents(self, texts):
            raise AssertionError("HTTP upload must use the async embedding method")

        def embed_query(self, text):
            raise AssertionError("No external query embedding is part of this check")

        async def aembed_documents(self, texts):
            label = request_case.get()
            emit("embedding_start", case=label, chunks=len(texts))
            if label.startswith("embedding_wait"):
                await asyncio.sleep(0.5)
            elif label == "blocking_control":
                # Negative control: the harness must observe an intentional
                # event-loop block from a synchronous provider implementation.
                time.sleep(0.3)
            else:
                await asyncio.sleep(0)
            vector = [1.0] + [0.0] * 1023
            result = [vector] * len(texts)
            emit("embedding_end", case=label)
            return result

    fake = types.ModuleType("app.infra.ai.embeddings")
    fake.embeddings = LocalEmbeddings()
    sys.modules["app.infra.ai.embeddings"] = fake
    from app import main
    from app.db.database import async_engine, engine
    from app.modules.rag.processing import document_processing, loaders, splitter
    from app.modules.rag.retrieval import vector_store
    engine.echo = False
    ownership = {None: schema}
    engine.update_execution_options(schema_translate_map=ownership)
    async_engine.update_execution_options(schema_translate_map=ownership)
    original_vector_engine = vector_store.create_engine

    def private_vector_engine(*args, **kwargs):
        owned = original_vector_engine(*args, **kwargs)
        owned.update_execution_options(schema_translate_map=ownership)
        return owned

    vector_store.create_engine = private_vector_engine

    def observe_submit(original, phase):
        def submit(*args):
            label = request_case.get()
            job = f"{phase}-{time.perf_counter_ns()}"
            emit(phase + "_start", case=label, job=job)
            future = original(*args)

            def completed(done):
                fields = {}
                try:
                    value = done.result()
                    if isinstance(value, dict):
                        fields["worker_pid"] = value.get("pid")
                        fields["parser_ok"] = value.get("ok")
                except BaseException as exc:
                    fields["error"] = type(exc).__name__
                emit(phase + "_end", case=label, job=job, **fields)
            future.add_done_callback(completed)
            return future
        return submit

    loaders.submit_pdf = observe_submit(document_processing.submit_pdf, "pdf")
    splitter.submit_text_split = observe_submit(document_processing.submit_text_split, "split")
    app = main.app

    @app.middleware("http")
    async def observe_request(request, call_next):
        token = request_case.set(request.headers.get("x-check-case", "probe"))
        try:
            return await call_next(request)
        finally:
            request_case.reset(token)

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def measured_lifespan(application):
        async with original_lifespan(application):
            from app.modules.auth.security import create_access_token
            from app.db.database import sessionLocal
            from app.modules.teams.models import Team, TeamMember
            from app.modules.users.models import User
            from app.modules.brands.service import seed_team_brands

            # A fixture in this harness's private schema, never an application
            # account or guessed existing team. Requests use normal JWT checks.
            with sessionLocal.begin() as db:
                actor = User(email=f"responsiveness-{uuid4().hex}@example.invalid", is_active=True)
                team = Team(name="Synthetic HTTP responsiveness team")
                db.add_all([actor, team])
                db.flush()
                actor_id, team_id = actor.id, team.id
                db.add(TeamMember(team_id=team_id, user_id=actor_id, role="admin", status="active"))
                seed_team_brands(db, team_id=team_id)
            async def heartbeat():
                deadline = time.perf_counter() + 0.005
                while True:
                    await asyncio.sleep(max(0, deadline - time.perf_counter()))
                    now = time.perf_counter()
                    emit("heartbeat", lag_ms=max(0, (now - deadline) * 1000))
                    deadline = now + 0.005
            beat = asyncio.create_task(heartbeat())
            emit("ready", server_pid=os.getpid(), thread=threading.get_ident(),
                 team_id=str(team_id), access_token=create_access_token(actor_id))
            try:
                yield
            finally:
                beat.cancel()
                try:
                    await beat
                except asyncio.CancelledError:
                    pass
    app.router.lifespan_context = measured_lifespan
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, lifespan="on"))

    @app.post("/__check__/stop")
    async def stop():
        server.should_exit = True
        return {"stopping": True}

    server.run(sockets=[listener])


def make_pdf(pages=40, lines=200):
    from io import BytesIO
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    writer = PdfWriter()
    font = writer._add_object(DictionaryObject({NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")}))
    for _ in range(pages):
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 12 Tf 72 720 Td\n" + b"(real PDF response check) Tj 0 -14 Td\n" * lines + b"ET")
        page[NameObject("/Contents")] = writer._add_object(stream)
    result = BytesIO()
    writer.write(result)
    return result.getvalue()


def summary(samples):
    values = sorted(samples)
    if not values:
        return {"samples": 0}
    return {"samples": len(values), "p50_ms": statistics.median(values),
            "p95_ms": values[max(0, int(len(values) * 0.95 + 0.999) - 1)], "max_ms": max(values)}


async def run_checks(base_url, database_url, process, event_queue, rounds, output, *, smoke=False):
    import httpx
    events, cases = [], []
    async with httpx.AsyncClient(base_url=base_url, timeout=90, trust_env=False) as client:
        for _ in range(200):
            if not process.is_alive():
                raise RuntimeError("Isolated HTTP server failed to start")
            try:
                response = await client.get("/")
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.05)
        else:
            raise TimeoutError("Isolated application did not become ready")
        ready = None
        for _ in range(200):
            try:
                event = event_queue.get_nowait()
                if event["kind"] == "ready":
                    ready = event
                    break
                events.append(event)
            except queue.Empty:
                await asyncio.sleep(0.01)
        if ready is None:
            raise TimeoutError("Isolated actor/team fixture did not become ready")
        client.headers["Authorization"] = "Bearer " + ready.pop("access_token")
        team_id = ready["team_id"]
        events.append(ready)  # Never persist the access token in report events.
        response = await client.get("/api/brands", params={"team_id": team_id})
        response.raise_for_status()
        brand = response.json()[0]["id"]

        def drain():
            while True:
                try:
                    events.append(event_queue.get_nowait())
                except queue.Empty:
                    break

        async def probe(path, collection):
            start = time.perf_counter()
            response = await client.get(path, params={"team_id": team_id})
            finish = time.perf_counter()
            collection.append({"path": path, "start": start, "end": finish,
                               "ms": (finish - start) * 1000, "status": response.status_code})

        baseline = []
        for _ in range(25):
            await asyncio.gather(probe("/", baseline), probe("/api/brands", baseline))
            await asyncio.sleep(0.01)
        report = {"generated_at": datetime.now(timezone.utc).isoformat(),
                  "mode": "smoke" if smoke else "full",
                  "scope": "real loopback TCP HTTP, one Uvicorn worker, real app lifespan/routes/PostgreSQL/PGVector" + ("; PDF not exercised" if smoke else "/PDF subprocesses"),
                  "external_services": "1024-dimensional deterministic local async embeddings; no provider calls",
                  "baseline": {path: summary([r["ms"] for r in baseline if r["path"] == path]) for path in ("/", "/api/brands")},
                  "thresholds": {"probe_p95_ms": 100, "probe_max_ms": 150, "heartbeat_max_ms": 100}, "cases": cases}
        pdf = make_pdf()
        scenarios = [(f"embedding_wait_{i}", "small.txt", b"upload response check", 1) for i in range(rounds)]
        scenarios += [(f"real_pdf_{i}", "real.pdf", pdf, 1) for i in range(rounds)]
        large_text = (b"large UTF-8 document for disk-spooled upload. " * 200000)[:8 * 1024 * 1024]
        scenarios += [("parallel_pdf", "real.pdf", pdf, 2), ("large_txt", "large.txt", large_text, 1),
                      ("many_chunks", "many.txt", b"x\n\n" * 14000, 1),
                      ("blocking_control", "control.txt", b"negative control", 1)]
        if smoke:
            scenarios = [scenarios[0], scenarios[-1]]
        try:
            for label, filename, content, count in scenarios:
                probes = []
                start = time.perf_counter()
                upload_finishes = []

                async def upload():
                    response = await client.post("/api/documents", params={"brand_id": brand, "team_id": team_id},
                        files={"file": (filename, content)}, headers={"x-check-case": label})
                    upload_finishes.append(time.perf_counter())
                    return response

                uploads = [asyncio.create_task(upload()) for _ in range(count)]
                while not all(task.done() for task in uploads):
                    await asyncio.gather(probe("/", probes), probe("/api/brands", probes))
                    drain()
                    await asyncio.sleep(0.01)
                responses = await asyncio.gather(*uploads)
                end = time.perf_counter()
                await asyncio.sleep(0.02)
                drain()
                own = [e for e in events if e.get("case") == label]
                windows = []
                for phase in ("pdf", "split", "embedding"):
                    starts = [e for e in own if e["kind"] == phase + "_start"]
                    ends = [e for e in own if e["kind"] == phase + "_end"]
                    for index, begin in enumerate(starts):
                        finish = next((e for e in ends if e.get("job") == begin.get("job")), None) if begin.get("job") else (ends[index] if index < len(ends) else None)
                        if finish:
                            windows.append({"phase": phase, "ms": (finish["at"] - begin["at"]) * 1000,
                                "probes_completed_during_phase": sum(begin["at"] <= r["end"] < finish["at"] for r in probes),
                                **({"worker_pid": finish["worker_pid"]} if "worker_pid" in finish else {})})
                lags = [e["lag_ms"] for e in events if e["kind"] == "heartbeat" and start <= e["at"] <= end]
                entry = {"case": label, "upload_count": count, "input_bytes": len(content),
                         "upload_statuses": [r.status_code for r in responses], "wall_ms": (end - start) * 1000,
                         "probes": {path: summary([r["ms"] for r in probes if r["path"] == path]) for path in ("/", "/api/brands")},
                         "heartbeat": summary(lags), "phases": windows,
                         "chunk_counts": [r.json().get("chunk_count") for r in responses if r.status_code == 201],
                         "probe_errors": [r["status"] for r in probes if r["status"] != 200],
                         "probes_completed_before_upload_response": sum(r["end"] < max(upload_finishes) for r in probes)}

                def count_vectors():
                    import psycopg
                    from sqlalchemy.engine import make_url
                    conninfo = make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)
                    ids = [r.json()["id"] for r in responses if r.status_code == 201]
                    with psycopg.connect(conninfo) as connection:
                        return connection.execute("""SELECT count(*) FROM langchain_pg_embedding
                            WHERE cmetadata->>'document_id' = ANY(%s::text[])
                            AND cmetadata->>'brand_id' = %s
                            AND cmetadata->>'team_id' = %s""", (ids, brand, team_id)).fetchone()[0]
                entry["persisted_vector_count"] = await asyncio.to_thread(count_vectors)
                if label == "blocking_control":
                    entry["passed"] = max(r["ms"] for r in probes) >= 150
                    entry["interpretation"] = "Expected failure: intentional 300 ms blocking fake provider must be detected"
                else:
                    entry["passed"] = (all(r.status_code == 201 for r in responses) and not entry["probe_errors"]
                        and all(v.get("p95_ms", 0) < 100 and v.get("max_ms", 0) < 150 for v in entry["probes"].values())
                        and entry["heartbeat"].get("max_ms", 0) < 100
                        and entry["probes_completed_before_upload_response"] > 0
                        and entry["persisted_vector_count"] == sum(entry["chunk_counts"]))
                    if label.startswith("real_pdf") or label == "parallel_pdf":
                        entry["passed"] = entry["passed"] and any(w["phase"] == "pdf" and w["probes_completed_during_phase"] > 0 for w in windows)
                cases.append(entry)
                print(json.dumps({"case": label, "passed": entry["passed"], "statuses": entry["upload_statuses"],
                                  "probes": entry["probes"], "heartbeat_max_ms": entry["heartbeat"].get("max_ms")}), flush=True)
            drain()
            ready = next(e for e in events if e["kind"] == "ready")
            report["server_pid"] = ready["server_pid"]
            report["pdf_worker_pids"] = sorted({w["worker_pid"] for c in cases for w in c["phases"] if w.get("worker_pid")})
            report["pdf_process_isolation"] = (
                None if smoke else bool(report["pdf_worker_pids"]) and ready["server_pid"] not in report["pdf_worker_pids"]
            )
            report["passed"] = all(c["passed"] for c in cases) and (smoke or report["pdf_process_isolation"])
            report["limits"] = ["Local controlled workload; not a production throughput/SLA claim.",
                "Embedding network wait is simulated; parser, splitting, database, middleware and TCP are real.",
                "The negative control intentionally blocks only this disposable server."]
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            return report["passed"]
        finally:
            await client.post("/__check__/stop")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--rounds", type=int, default=2, choices=range(1, 4))
    parser.add_argument("--smoke", action="store_true", help="Only small upload and blocking control; no PDF performance claim")
    parser.add_argument("--output", type=Path, default=ROOT / "docs/verification/http-responsiveness.json")
    args = parser.parse_args()
    from sqlalchemy.engine import make_url
    url = make_url(args.database_url)
    if (url.get_backend_name() != "postgresql" or not (url.database or "").startswith(("test_", "audit_", "refactor_"))
            or any(key.lower() in {"dbname", "database", "service", "servicefile"} for key in url.query)):
        parser.error("Use an explicitly selected disposable test/audit/refactor database")
    import psycopg
    from psycopg import sql
    schema = "http_responsiveness_" + uuid4().hex
    admin_url = url.difference_update_query(["options"]).set(drivername="postgresql")
    conninfo = admin_url.render_as_string(hide_password=False)
    with psycopg.connect(conninfo, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    scoped_url = admin_url.update_query_dict({"options": f"-csearch_path={schema},public"}).set(drivername="postgresql+psycopg").render_as_string(hide_password=False)
    context = multiprocessing.get_context("spawn")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    events = context.Queue()
    server = context.Process(target=serve, args=(scoped_url, listener, events, schema))
    server.start()
    passed = False
    try:
        passed = asyncio.run(run_checks(
            f"http://127.0.0.1:{listener.getsockname()[1]}", scoped_url,
            server, events, args.rounds, args.output, smoke=args.smoke,
        ))
    finally:
        server.join(timeout=15)
        if server.is_alive():
            server.terminate()
            server.join(timeout=5)
        listener.close()
        events.close()
        events.join_thread()
        with psycopg.connect(conninfo, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
