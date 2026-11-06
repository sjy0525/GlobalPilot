"""Upload parsing contracts, using in-memory PDFs and no database or network."""
import unittest
from io import BytesIO
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI, File, UploadFile
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.core.http_errors import install_exception_handlers
from app.modules.rag.processing import loaders, pdf_worker


def text_pdf(content: bytes, *, unsupported_filter: bool = False) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
    })
    stream = DecodedStreamObject()
    stream.set_data(content)
    if unsupported_filter:
        stream[NameObject("/Filter")] = NameObject("/UnsupportedTestFilter")
    page[NameObject("/Contents")] = writer._add_object(stream)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class UploadStatusCodeTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        install_exception_handlers(app)

        @app.post("/upload")
        async def upload(file: UploadFile = File()):
            source, text = loaders.load_upload(file.filename, await file.read())
            return {"source": source, "text": text}

        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def upload(self, name: str, content: bytes):
        return self.client.post("/upload", files={"file": (name, content)})

    def parser_result(self, payload):
        """Mock only the expensive external ML process protocol."""
        def run(command, **kwargs):
            Path(command[-1]).write_text(json.dumps(payload))
            return SimpleNamespace(returncode=0 if payload["ok"] else 1)
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch.dict(os.environ, {"GLOBALPILOT_DOCLING_PYTHON": __file__}))
        stack.enter_context(patch.object(pdf_worker.subprocess, "run", side_effect=run))
        return stack

    def test_unsupported_format_is_415(self):
        response = self.upload("document.docx", b"irrelevant")
        self.assertEqual(response.status_code, 415)
        self.assertEqual(response.json()["code"], "UNSUPPORTED_MEDIA_TYPE")

    def test_pdf_stream_failure_after_reader_construction_is_400(self):
        content = text_pdf(b"BT /F1 12 Tf 100 100 Td (unclosed text")
        PdfReader(BytesIO(content))  # Header/trailer parsing succeeds; text extraction fails.
        with self.parser_result({"ok": False, "status_code": 400, "code": "INVALID_PDF", "detail": "无法解析该 PDF 文件"}):
            response = self.upload("corrupt.pdf", content)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "INVALID_PDF")
        self.assertEqual(response.json()["detail"], "无法解析该 PDF 文件")

    def test_unsupported_pdf_filter_is_400(self):
        content = text_pdf(b"BT /F1 12 Tf (hello) Tj ET", unsupported_filter=True)
        with self.parser_result({"ok": False, "status_code": 400, "code": "INVALID_PDF", "detail": "无法解析该 PDF 文件"}):
            response = self.upload("unsupported.pdf", content)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "INVALID_PDF")

    def test_empty_and_malformed_pdfs_remain_400(self):
        for content in (b"", b"not a PDF"):
            with self.subTest(content_length=len(content)):
                response = self.upload("invalid.pdf", content)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["code"], "INVALID_PDF")

    def test_textless_pdf_is_passed_to_ocr_parser(self):
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        buffer = BytesIO()
        writer.write(buffer)
        with self.parser_result({"ok": True, "text": "OCR recognized labels", "structured": {}, "assets": []}):
            response = self.upload("scan.pdf", buffer.getvalue())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["text"], "OCR recognized labels")

    def test_valid_pdf_and_txt_keep_extracted_content(self):
        pdf = text_pdf(b"BT /F1 12 Tf 100 100 Td (hello) Tj ET")
        for filename, content in (("valid.pdf", pdf), ("valid.txt", b"hello")):
            with self.subTest(filename=filename):
                with self.parser_result({"ok": True, "text": "hello", "structured": {}, "assets": []}):
                    response = self.upload(filename, content)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["text"], "hello")

    def test_invalid_txt_encoding_remains_400(self):
        response = self.upload("invalid.txt", b"\xff")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "文件必须是utf-8编码")

    def test_unexpected_programming_errors_are_not_client_errors(self):
        with patch.object(pdf_worker, "PdfReader", side_effect=RuntimeError("unexpected failure")):
            with self.assertRaisesRegex(RuntimeError, "unexpected failure"):
                loaders.load_upload("valid.pdf", b"unused")

    def test_unavailable_ocr_parser_is_503_without_text_only_downgrade(self):
        pdf = text_pdf(b"BT /F1 12 Tf (hello) Tj ET")
        with patch.dict(os.environ, {"GLOBALPILOT_DOCLING_PYTHON": "/missing/docling/python"}):
            response = self.upload("valid.pdf", pdf)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "PDF_PARSER_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
