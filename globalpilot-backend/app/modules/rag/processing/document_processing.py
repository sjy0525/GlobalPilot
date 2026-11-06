"""A reusable spawned document CPU pool with bounded admission and shutdown."""

from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from multiprocessing import get_context
from threading import BoundedSemaphore, Condition

from app.core.http_errors import AppHTTPException
from app.modules.rag.processing.pdf_worker import parse_pdf
from app.modules.rag.processing.text_worker import split_text_parts

PDF_WORKERS = 2
# Two executing document jobs and at most two queued jobs. Extra callers are rejected
# immediately instead of retaining unlimited byte payloads or waiting threads.
PDF_MAX_IN_FLIGHT = 4


def _processing_busy() -> AppHTTPException:
    return AppHTTPException(
        status_code=503,
        detail="文档解析繁忙，请稍后重试",
        code="DOCUMENT_PROCESSING_BUSY",
        headers={"Retry-After": "1"},
    )


def _shutdown_executors(executors: list) -> tuple[list, BaseException | None]:
    failed = []
    first_error = None
    for executor in executors:
        try:
            executor.shutdown(wait=True, cancel_futures=True)
        except BaseException as exc:
            failed.append(executor)
            if first_error is None:
                first_error = exc
    return failed, first_error


class _DocumentProcessingPool:
    def __init__(self):
        self._executor: ProcessPoolExecutor | None = None
        self._condition = Condition()
        self._closing = False
        self._capacity = BoundedSemaphore(PDF_MAX_IN_FLIGHT)
        self._retired_executors = []
        self._active_cleanups = 0
        self._active_submissions = 0

    def _mark_broken(self, executor: ProcessPoolExecutor) -> None:
        with self._condition:
            if self._executor is executor:
                self._executor = None
                self._retired_executors.append(executor)

    def _cleanup_retired(self) -> None:
        # Only sync startup/submission workers call this. Future done callbacks
        # merely retire instances: joining from the pool's manager thread would
        # deadlock or attempt to join that same thread.
        with self._condition:
            if self._closing:
                raise _processing_busy()
            retired, self._retired_executors = self._retired_executors, []
            if not retired:
                return
            self._active_cleanups += 1
        try:
            failed, first_error = _shutdown_executors(retired)
            if failed:
                with self._condition:
                    self._retired_executors.extend(failed)
            if first_error is not None:
                raise first_error
        finally:
            with self._condition:
                self._active_cleanups -= 1
                self._condition.notify_all()

    def _job_completed(self, future: Future, executor: ProcessPoolExecutor) -> None:
        try:
            if not future.cancelled() and isinstance(future.exception(), BrokenProcessPool):
                self._mark_broken(executor)
        finally:
            self._capacity.release()

    def _initialize_locked(self) -> ProcessPoolExecutor:
        if self._closing:
            raise _processing_busy()
        if self._executor is None:
            # spawn does not inherit the parent's database connections, SDK
            # clients, or running threads as a forked worker would.
            self._executor = ProcessPoolExecutor(
                max_workers=PDF_WORKERS, mp_context=get_context("spawn")
            )
        return self._executor

    def initialize(self) -> None:
        self._cleanup_retired()
        with self._condition:
            self._initialize_locked()

    def submit(self, content: bytes) -> Future:
        return self._submit(parse_pdf, content)

    def submit_text_split(self, text: str, chunk_size: int, overlap: int) -> Future:
        return self._submit(split_text_parts, text, chunk_size, overlap)

    def _submit(self, function, *args) -> Future:
        # This entire method runs in a thread: submit may start child processes
        # on first use, which must not happen on the request event loop.
        self._cleanup_retired()
        with self._condition:
            if not self._capacity.acquire(blocking=False):
                raise _processing_busy()
            try:
                executor = self._initialize_locked()
            except BaseException:
                self._capacity.release()
                raise
            self._active_submissions += 1
        try:
            # ProcessPoolExecutor.submit takes its own shutdown lock. Never
            # hold our condition while entering it or registering a callback:
            # a manager's BrokenProcessPool callback may need this condition
            # while already holding that executor shutdown lock.
            try:
                future = executor.submit(function, *args)
            except BrokenProcessPool:
                self._mark_broken(executor)
                self._capacity.release()
                raise
            except BaseException:
                self._capacity.release()
                raise
            # A cancelled await does not release admission until the real job
            # completes/cancels, so cancelled running jobs still count toward
            # the bound and cannot create an unbounded background queue.
            future.add_done_callback(lambda completed: self._job_completed(completed, executor))
            return future
        finally:
            with self._condition:
                self._active_submissions -= 1
                self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            if self._closing:
                self._condition.wait_for(lambda: not self._closing)
                return
            self._closing = True
            self._condition.wait_for(lambda: self._active_cleanups == 0 and self._active_submissions == 0)
            executors, self._retired_executors = self._retired_executors, []
            if self._executor is not None:
                executors.append(self._executor)
                self._executor = None
        failed = []
        try:
            # Cancel work that has not started, then join every child. Attempt
            # every current/retired pool even if an earlier cleanup fails.
            failed, first_error = _shutdown_executors(executors)
            if first_error is not None:
                raise first_error
        finally:
            with self._condition:
                self._retired_executors.extend(failed)
                self._closing = False
                self._condition.notify_all()


document_processing_pool = _DocumentProcessingPool()


def initialize_document_processing() -> None:
    document_processing_pool.initialize()


def close_document_processing() -> None:
    document_processing_pool.close()


def submit_pdf(content: bytes) -> Future:
    return document_processing_pool.submit(content)


def submit_text_split(text: str, chunk_size: int, overlap: int) -> Future:
    return document_processing_pool.submit_text_split(text, chunk_size, overlap)
