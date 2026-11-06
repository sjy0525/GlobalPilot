"""Large text uses the shared spawned CPU pool without crossing ORM/Document state."""

import asyncio
import threading
import unittest
from concurrent.futures import Future
from unittest.mock import patch

from langchain_core.documents import Document
from starlette.concurrency import run_in_threadpool

from app.modules.rag.processing import document_processing, splitter, text_worker


class TextProcessingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await run_in_threadpool(document_processing.close_document_processing)

    async def test_short_text_keeps_sync_result_without_submitting_process_job(self):
        text = "短段落。\n\nAnother paragraph. " * 5
        metadata = {"brand_id": "brand", "source": "notes.txt"}
        with patch.object(splitter, "submit_text_split") as submit:
            actual = await splitter.asplit_to_documents(text, metadata)
        self.assertEqual(actual, splitter.split_to_documents(text, metadata))
        submit.assert_not_called()

    async def test_ascii_and_multibyte_byte_threshold_use_worker_and_parent_documents(self):
        sentinel = object()
        metadata = {"brand_id": "brand", "sentinel": sentinel}
        for text in ("a" * (64 * 1024), "界" * 21846):
            with self.subTest(characters=len(text)):
                future = Future()
                future.set_result(["worker chunk"])
                submission_threads = []

                def submit(*args):
                    submission_threads.append(threading.get_ident())
                    self.assertEqual(args, (text, splitter.settings.rag_chunk_size, splitter.settings.rag_chunk_overlap))
                    return future

                with patch.object(splitter, "submit_text_split", side_effect=submit):
                    documents = await splitter.asplit_to_documents(text, metadata)
                self.assertIsInstance(documents[0], Document)
                self.assertEqual(documents[0].page_content, "worker chunk")
                self.assertEqual(documents[0].metadata["chunk_index"], 0)
                self.assertIs(documents[0].metadata["sentinel"], sentinel)
                self.assertEqual(len(submission_threads), 1)
                self.assertNotEqual(submission_threads[0], threading.get_ident())

    async def test_actual_spawn_large_text_matches_sync_chunk_content_order_and_metadata(self):
        text = ("第一句。第二句！third sentence? end.\n下一行 more words " * 2500) + "\n\n最后一段"
        metadata = {"brand_id": "brand", "document_id": "document", "source": "large.pdf"}
        expected = await run_in_threadpool(splitter.split_to_documents, text, metadata)
        actual = await splitter.asplit_to_documents(text, metadata)
        self.assertEqual(actual, expected)
        self.assertTrue(all(isinstance(document, Document) for document in actual))
        self.assertEqual([document.metadata["chunk_index"] for document in actual], list(range(len(actual))))

    async def test_large_text_split_allows_event_loop_heartbeat(self):
        text = "中文句子。Some words without a blank paragraph! " * 20000
        task = asyncio.create_task(splitter.asplit_to_documents(text, {"source": "large.pdf"}))
        heartbeats = 0
        while not task.done():
            heartbeats += 1
            await asyncio.sleep(0.002)
        documents = await task
        self.assertGreater(len(documents), 100)
        self.assertGreaterEqual(heartbeats, 3)

    async def test_cancelled_large_split_cancels_queued_future(self):
        future = Future()
        with patch.object(splitter, "submit_text_split", return_value=future) as submit:
            task = asyncio.create_task(splitter.asplit_to_documents("a" * 65536, {}))
            while not submit.called:
                await asyncio.sleep(0)
            await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(future.cancelled())

    async def test_unknown_split_error_is_not_converted_to_client_error(self):
        future = Future()
        future.set_exception(RuntimeError("unexpected split failure"))
        with patch.object(splitter, "submit_text_split", return_value=future):
            with self.assertRaisesRegex(RuntimeError, "unexpected split failure"):
                await splitter.asplit_to_documents("a" * 65536, {})


class PureTextWorkerTests(unittest.TestCase):
    def test_same_library_algorithm_for_boundary_punctuation_overlap_and_whitespace(self):
        samples = (
            "", " \n\t ", "a" * 500, "a" * 501, "\n" * 5,
            "中文。English words!下一句？\n line " * 80,
            "single long word" * 100, "short\n\nparagraph\n\nend", "界" * 3000,
        )
        for text in samples:
            with self.subTest(characters=len(text)):
                result = text_worker.split_text_parts(
                    text, splitter.settings.rag_chunk_size, splitter.settings.rag_chunk_overlap
                )
                self.assertEqual(result, splitter.split_text(text))
                self.assertTrue(all(isinstance(chunk, str) for chunk in result))


if __name__ == "__main__":
    unittest.main()
