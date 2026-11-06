"""Real spawn/subprocess lifecycle; ML quality is checked by the retained corpus.

The lightweight parser executable is a test double for the Docling ML process.
Neither the shared process pool nor its cancellation/shutdown are mocked in the
SpawnedPDFTests. Real OCR/layout/table checks run in check_structured_pdf_corpus.
"""

import asyncio
import multiprocessing
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import textwrap
import threading
import time
import unittest
from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool
from io import BytesIO
from unittest.mock import Mock, patch

import httpx
from fastapi import FastAPI
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from starlette.concurrency import run_in_threadpool

from app.core import http_errors
from app.modules.rag.processing import document_processing, loaders, pdf_worker


def make_pdf(*, pages=1, lines=1, encrypted=False, text=True):
    writer = PdfWriter()
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    font_reference = writer._add_object(font)
    for _ in range(pages):
        page = writer.add_blank_page(width=612, height=792)
        if text:
            page[NameObject("/Resources")] = DictionaryObject({
                NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_reference}),
            })
            stream = DecodedStreamObject()
            stream.set_data(b"BT /F1 12 Tf 72 720 Td\n" + b"(hello PDF worker) Tj 0 -14 Td\n" * lines + b"ET")
            page[NameObject("/Contents")] = writer._add_object(stream)
    if encrypted:
        writer.encrypt("test-password")
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class SpawnedPDFTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = TemporaryDirectory(prefix="globalpilot-parser-test-")
        self.addCleanup(folder.cleanup)
        executable = Path(folder.name) / "parser-fixture"
        executable.write_text(f"#!{sys.executable}\n" + textwrap.dedent('''\
            import json, sys
            from pathlib import Path
            from pypdf import PdfReader

            # argv[1] is the production worker script; this executable models its
            # process protocol without loading Docling/Torch for lifecycle tests.
            reader = PdfReader(sys.argv[2])
            pages = [(page.extract_text() or "").strip() for page in reader.pages]
            text = "\\n\\n".join(part for part in pages if part)
            Path(sys.argv[3]).write_text(json.dumps({
                "ok": True, "text": text or "OCR fixture recovered image text",
                "structured": {"schema_version": 1, "parser": "docling-test-double",
                               "page_count": len(reader.pages), "contents": []},
                "assets": [],
            }))
        '''))
        executable.chmod(0o700)
        scope = patch.dict(os.environ, {"GLOBALPILOT_DOCLING_PYTHON": str(executable)})
        scope.start()
        self.addCleanup(scope.stop)

    async def asyncTearDown(self):
        await run_in_threadpool(document_processing.close_document_processing)

    async def test_valid_pdf_runs_in_spawned_child_and_shutdown_joins_it(self):
        await run_in_threadpool(document_processing.initialize_document_processing)
        future = await run_in_threadpool(document_processing.submit_pdf, make_pdf())
        result = await asyncio.wrap_future(future)
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "hello PDF worker")
        self.assertNotEqual(result["pid"], os.getpid())
        executor = document_processing.document_processing_pool._executor
        self.assertEqual(executor._mp_context.get_start_method(), "spawn")
        worker_pids = {process.pid for process in executor._processes.values()}
        self.assertIn(result["pid"], worker_pids)
        await run_in_threadpool(document_processing.close_document_processing)
        self.assertIsNone(document_processing.document_processing_pool._executor)
        self.assertTrue(worker_pids.isdisjoint({child.pid for child in multiprocessing.active_children()}))

    async def test_actual_spawn_expected_pdf_failures_restore_parent_http_exception(self):
        for content, detail in (
            (b"not a PDF", "无法解析该 PDF 文件"),
            (make_pdf(encrypted=True), "不支持加密的 PDF 文件"),
        ):
            with self.subTest(detail=detail):
                with self.assertRaises(http_errors.AppHTTPException) as caught:
                    await loaders.aload_upload("invalid.pdf", content)
                self.assertEqual(caught.exception.status_code, 400)
                self.assertEqual(caught.exception.code, "INVALID_PDF")
                self.assertEqual(caught.exception.detail, detail)

    async def test_pdf_cpu_work_allows_same_event_loop_heartbeat(self):
        # Real shared spawn pool + parser subprocess IO must yield the loop.
        content = make_pdf(pages=40, lines=200)
        parse = asyncio.create_task(loaders.aload_upload("large.pdf", content))
        heartbeats = 0
        while not parse.done():
            heartbeats += 1
            await asyncio.sleep(0.002)
        source, text = await parse
        self.assertEqual(source, "large.pdf")
        self.assertEqual(text.count("hello PDF worker"), 8000)
        self.assertGreaterEqual(heartbeats, 3)

    async def test_textless_pdf_reaches_ocr_process_instead_of_being_rejected(self):
        source, text = await loaders.aload_upload("scan.pdf", make_pdf(text=False))
        self.assertEqual(source, "scan.pdf")
        self.assertEqual(text, "OCR fixture recovered image text")

    async def test_cancelled_parse_is_owned_by_pool_and_shutdown_leaves_no_children(self):
        task = asyncio.create_task(loaders.aload_upload("cancelled.pdf", make_pdf(pages=40, lines=200)))

        async def worker_pids():
            while True:
                executor = document_processing.document_processing_pool._executor
                processes = executor._processes if executor is not None else None
                if processes:
                    return {process.pid for process in processes.copy().values()}
                await asyncio.sleep(0.002)

        try:
            pids = await asyncio.wait_for(worker_pids(), timeout=3)
            self.assertFalse(task.done())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await run_in_threadpool(document_processing.close_document_processing)
        self.assertTrue(pids.isdisjoint({child.pid for child in multiprocessing.active_children()}))

    async def test_empty_pdf_unsupported_format_and_txt_do_not_submit_process_jobs(self):
        with patch.object(loaders, "submit_pdf") as submit:
            self.assertEqual(await loaders.aload_upload("valid.txt", b"hello"), ("valid.txt", "hello"))
            for filename, content, status in (("empty.pdf", b"", 400), ("bad.docx", b"bad", 415), ("bad.txt", b"\xff", 400)):
                with self.subTest(filename=filename):
                    with self.assertRaises(http_errors.HTTPException) as caught:
                        await loaders.aload_upload(filename, content)
                    self.assertEqual(caught.exception.status_code, status)
            submit.assert_not_called()

    async def test_async_cancellation_cancels_a_queued_future(self):
        future = Future()
        with patch.object(loaders, "submit_pdf", return_value=future) as submit:
            task = asyncio.create_task(loaders.aload_upload("queued.pdf", b"placeholder"))
            while not submit.called:
                await asyncio.sleep(0)
            # Let the submitted Future be attached to its asyncio wrapper.
            await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(future.cancelled())

    async def test_unknown_worker_exception_stays_500_without_private_details(self):
        future = Future()
        future.set_exception(RuntimeError("private parser failure"))
        app = FastAPI()
        http_errors.install_exception_handlers(app)

        @app.post("/upload")
        async def upload():
            name, text = await loaders.aload_upload("unexpected.pdf", b"placeholder")
            return {"name": name, "text": text}

        with patch.object(loaders, "submit_pdf", return_value=future), \
             self.assertLogs(http_errors.logger, level="ERROR"):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                response = await client.post("/upload")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["code"], "INTERNAL_ERROR")
        self.assertNotIn("private parser failure", response.text)


class PoolAdmissionTests(unittest.TestCase):
    def test_two_spawn_workers_and_four_in_flight_jobs_bound_memory_and_queue(self):
        pool = document_processing._DocumentProcessingPool()
        executor = Mock()
        futures = [Future() for _ in range(document_processing.PDF_MAX_IN_FLIGHT + 1)]
        executor.submit.side_effect = futures
        with patch.object(document_processing, "ProcessPoolExecutor", return_value=executor) as factory:
            pool.initialize()
            self.assertEqual(factory.call_args.kwargs["max_workers"], 2)
            self.assertEqual(factory.call_args.kwargs["mp_context"].get_start_method(), "spawn")
            for _ in range(document_processing.PDF_MAX_IN_FLIGHT):
                pool.submit(b"PDF bytes")
            with self.assertRaises(http_errors.AppHTTPException) as caught:
                pool.submit(b"excess bytes")
            self.assertEqual(caught.exception.status_code, 503)
            self.assertEqual(caught.exception.code, "DOCUMENT_PROCESSING_BUSY")
            self.assertEqual(caught.exception.headers, {"Retry-After": "1"})
            self.assertEqual(executor.submit.call_count, 4)
            futures[0].set_result({"ok": True, "text": "done"})
            self.assertIs(pool.submit(b"replacement"), futures[-1])
            pool.close()
            pool.close()
        executor.shutdown.assert_called_once_with(wait=True, cancel_futures=True)

    def test_cancelled_running_job_retains_capacity_until_real_completion(self):
        pool = document_processing._DocumentProcessingPool()
        executor = Mock()
        futures = [Future() for _ in range(document_processing.PDF_MAX_IN_FLIGHT)]
        executor.submit.side_effect = futures
        with patch.object(document_processing, "ProcessPoolExecutor", return_value=executor):
            submitted = [pool.submit(b"PDF") for _ in futures]
            submitted[0].set_running_or_notify_cancel()
            self.assertFalse(submitted[0].cancel())
            with self.assertRaises(http_errors.AppHTTPException):
                pool.submit(b"excess")
            self.assertTrue(submitted[1].cancel())
            replacement = Future()
            executor.submit.side_effect = None
            executor.submit.return_value = replacement
            self.assertIs(pool.submit(b"replacement"), replacement)
            pool.close()

    def test_failed_submit_releases_admission_and_does_not_convert_unknown_error(self):
        pool = document_processing._DocumentProcessingPool()
        executor = Mock()
        executor.submit.side_effect = RuntimeError("unexpected submit failure")
        with patch.object(document_processing, "ProcessPoolExecutor", return_value=executor):
            for _ in range(document_processing.PDF_MAX_IN_FLIGHT + 1):
                with self.assertRaisesRegex(RuntimeError, "unexpected submit failure"):
                    pool.submit(b"PDF")
            self.assertEqual(executor.submit.call_count, 5)
            pool.close()

    def test_worker_expected_errors_are_basic_serializable_data(self):
        result = pdf_worker.parse_pdf(b"")
        self.assertEqual(result["ok"], False)
        self.assertEqual(result["detail"], "PDF 文件不能为空")
        self.assertTrue(all(isinstance(value, (bool, int, str)) for value in result.values()))

    def test_pdf_and_text_split_share_one_pool_and_one_capacity_bound(self):
        pool = document_processing._DocumentProcessingPool()
        executor = Mock()
        executor.submit.side_effect = [Future() for _ in range(4)]
        with patch.object(document_processing, "ProcessPoolExecutor", return_value=executor) as factory:
            pool.submit(b"first PDF")
            pool.submit_text_split("first text", 500, 80)
            pool.submit(b"second PDF")
            pool.submit_text_split("second text", 500, 80)
            with self.assertRaises(http_errors.AppHTTPException):
                pool.submit_text_split("excess text", 500, 80)
            factory.assert_called_once()
            self.assertEqual(executor.submit.call_args_list[1].args, (
                document_processing.split_text_parts, "first text", 500, 80,
            ))
            pool.close()

    def test_broken_submit_is_not_retried_and_next_request_replaces_pool(self):
        pool = document_processing._DocumentProcessingPool()
        broken, replacement = Mock(), Mock()
        error = BrokenProcessPool("old pool is broken")
        broken.submit.side_effect = error
        future = Future()
        replacement.submit.return_value = future
        with patch.object(document_processing, "ProcessPoolExecutor", side_effect=[broken, replacement]) as factory:
            with self.assertRaises(BrokenProcessPool) as caught:
                pool.submit(b"first PDF")
            self.assertIs(caught.exception, error)
            broken.submit.assert_called_once()
            broken.shutdown.assert_not_called()
            self.assertIsNone(pool._executor)
            self.assertIs(pool.submit(b"next PDF"), future)
            broken.shutdown.assert_called_once_with(wait=True, cancel_futures=True)
            self.assertEqual(factory.call_count, 2)
            pool.close()
        replacement.shutdown.assert_called_once_with(wait=True, cancel_futures=True)

    def test_broken_future_only_retires_in_callback_and_next_request_recovers(self):
        pool = document_processing._DocumentProcessingPool()
        broken, replacement = Mock(), Mock()
        failed, healthy = Future(), Future()
        broken.submit.return_value = failed
        replacement.submit.return_value = healthy
        with patch.object(document_processing, "ProcessPoolExecutor", side_effect=[broken, replacement]):
            self.assertIs(pool.submit_text_split("first text", 500, 80), failed)
            failed.set_exception(BrokenProcessPool("worker exited"))
            self.assertIsNone(pool._executor)
            broken.shutdown.assert_not_called()  # never join inside done callback
            with self.assertRaises(BrokenProcessPool):
                failed.result()
            self.assertIs(pool.submit_text_split("next text", 500, 80), healthy)
            broken.shutdown.assert_called_once_with(wait=True, cancel_futures=True)
            pool.close()

    def test_ordinary_worker_exception_does_not_replace_healthy_pool(self):
        pool = document_processing._DocumentProcessingPool()
        executor = Mock()
        failed, healthy = Future(), Future()
        executor.submit.side_effect = [failed, healthy]
        with patch.object(document_processing, "ProcessPoolExecutor", return_value=executor) as factory:
            pool.submit(b"first PDF")
            failed.set_exception(RuntimeError("unexpected parser error"))
            self.assertIs(pool._executor, executor)
            self.assertIs(pool.submit(b"next PDF"), healthy)
            factory.assert_called_once()
            executor.shutdown.assert_not_called()
            pool.close()

    def test_close_attempts_retired_and_current_pools_even_after_failure(self):
        pool = document_processing._DocumentProcessingPool()
        retired, current = Mock(), Mock()
        original = RuntimeError("retired cleanup failed")
        retired.shutdown.side_effect = original
        current.shutdown.side_effect = ValueError("current cleanup failed")
        pool._retired_executors.append(retired)
        pool._executor = current
        with self.assertRaises(RuntimeError) as caught:
            pool.close()
        self.assertIs(caught.exception, original)
        retired.shutdown.assert_called_once_with(wait=True, cancel_futures=True)
        current.shutdown.assert_called_once_with(wait=True, cancel_futures=True)
        self.assertFalse(pool._closing)
        retired.shutdown.side_effect = current.shutdown.side_effect = None
        pool.close()
        self.assertEqual(retired.shutdown.call_count, 2)
        self.assertEqual(current.shutdown.call_count, 2)

    def test_broken_manager_callback_and_concurrent_submit_do_not_invert_locks(self):
        pool = document_processing._DocumentProcessingPool()
        executor = Mock()
        shutdown_lock = threading.Lock()
        manager_entered, submit_entered = threading.Event(), threading.Event()
        fail_now, manager_finished = threading.Event(), threading.Event()
        first = Future()
        failure = BrokenProcessPool("worker died")
        outcomes = []
        manager_errors = []
        calls = 0

        def submit(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                return first
            submit_entered.set()
            # Model CPython's submit() taking the executor shutdown lock.
            with shutdown_lock:
                raise failure

        def terminate_broken():
            shutdown_lock.acquire()
            try:
                manager_entered.set()
                if not fail_now.wait(2):
                    raise AssertionError("manager failure was not released")
                # CPython invokes done callbacks while holding shutdown_lock.
                first.set_exception(failure)
                manager_finished.set()
            except BaseException as exc:
                manager_errors.append(exc)
            finally:
                if shutdown_lock.locked():
                    shutdown_lock.release()

        def submit_again():
            try:
                pool.submit(b"concurrent PDF")
            except BaseException as exc:
                outcomes.append(exc)

        executor.submit.side_effect = submit
        with patch.object(document_processing, "ProcessPoolExecutor", return_value=executor):
            pool.submit(b"first PDF")
            manager = threading.Thread(target=terminate_broken, daemon=True)
            submitter = threading.Thread(target=submit_again, daemon=True)
            manager.start()
            try:
                self.assertTrue(manager_entered.wait(1))
                submitter.start()
                self.assertTrue(submit_entered.wait(1))
                fail_now.set()
                completed = manager_finished.wait(1)
                self.assertTrue(completed, "manager callback deadlocked against concurrent submission")
            finally:
                fail_now.set()
                # Bound regression failure cleanup too; a primitive Lock can be
                # released here to break a deliberately modelled lock cycle.
                if not manager_finished.is_set() and shutdown_lock.locked():
                    shutdown_lock.release()
                manager.join(2)
                if submitter.ident is not None:
                    submitter.join(2)
            self.assertFalse(manager.is_alive())
            self.assertFalse(submitter.is_alive())
            self.assertEqual(manager_errors, [])
            self.assertEqual(outcomes, [failure])
            self.assertEqual(pool._active_submissions, 0)
            pool.close()

    def test_close_waits_for_in_flight_submit_and_rejects_new_jobs(self):
        pool = document_processing._DocumentProcessingPool()
        executor = Mock()
        entered, release = threading.Event(), threading.Event()
        future = Future()
        submit_results = []

        def submit(*args):
            entered.set()
            if not release.wait(2):
                raise AssertionError("submit gate was not released")
            return future

        executor.submit.side_effect = submit
        with patch.object(document_processing, "ProcessPoolExecutor", return_value=executor):
            submitter = threading.Thread(target=lambda: submit_results.append(pool.submit(b"PDF")))
            closer = threading.Thread(target=pool.close)
            submitter.start()
            try:
                self.assertTrue(entered.wait(1))
                closer.start()
                deadline = time.monotonic() + 1
                while not pool._closing and time.monotonic() < deadline:
                    time.sleep(0.001)
                self.assertTrue(pool._closing)
                self.assertTrue(closer.is_alive())
                executor.shutdown.assert_not_called()
                with self.assertRaises(http_errors.AppHTTPException) as caught:
                    pool.submit(b"new PDF")
                self.assertEqual(caught.exception.status_code, 503)
            finally:
                release.set()
                submitter.join(2)
                if closer.ident is not None:
                    closer.join(2)
            self.assertFalse(submitter.is_alive())
            self.assertFalse(closer.is_alive())
            self.assertEqual(submit_results, [future])
            self.assertEqual(pool._active_submissions, 0)
            executor.shutdown.assert_called_once_with(wait=True, cancel_futures=True)


if __name__ == "__main__":
    unittest.main()
