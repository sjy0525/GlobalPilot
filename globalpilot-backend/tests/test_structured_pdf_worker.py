"""Parser process boundary: structure and images survive; failures never downgrade."""

from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pypdf import PdfWriter

from app.modules.rag.processing import pdf_worker
from app.modules.rag.processing.docling_worker import _attach_table_context


def blank_pdf(*, encrypted=False):
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=200)
    if encrypted:
        writer.encrypt("private")
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


class StructuredPdfWorkerTests(unittest.TestCase):
    def test_missing_parser_is_503_and_never_extracts_plain_text(self):
        with patch.dict("os.environ", {"GLOBALPILOT_DOCLING_PYTHON": "/missing/parser/python"}):
            result = pdf_worker.parse_pdf(blank_pdf())
        self.assertFalse(result["ok"])
        self.assertEqual(result["status_code"], 503)
        self.assertEqual(result["code"], "PDF_PARSER_UNAVAILABLE")

    def test_structure_and_binary_assets_cross_process_boundary(self):
        def run(command, **kwargs):
            output = Path(command[-1])
            (output.parent / "picture-one.png").write_bytes(b"PNG asset bytes")
            output.write_text(json.dumps({
                "ok": True, "text": "图片文字：预算",
                "structured": {"schema_version": 1, "parser": "docling", "contents": [
                    {"content_id": "content-one", "kind": "picture", "page_no": 1,
                     "asset_id": "picture-one", "text": "预算", "provenance": [
                         {"page_no": 1, "bbox": {"l": 1, "t": 2, "r": 3, "b": 4},
                          "coord_origin": "TOPLEFT", "page_width": 100, "page_height": 200}
                     ]}
                ]},
                "assets": [{"id": "picture-one", "file": "picture-one.png",
                            "media_type": "image/png", "kind": "picture", "page_no": 1}],
            }), encoding="utf-8")
            return SimpleNamespace(returncode=0)

        with patch.dict("os.environ", {"GLOBALPILOT_DOCLING_PYTHON": __file__}), \
             patch.object(pdf_worker.subprocess, "run", side_effect=run):
            result = pdf_worker.parse_pdf(blank_pdf())
        self.assertTrue(result["ok"])
        self.assertEqual(result["structured"]["contents"][0]["page_no"], 1)
        self.assertEqual(result["assets"][0]["data"], b"PNG asset bytes")
        self.assertNotIn("file", result["assets"][0])

    def test_parser_failure_does_not_publish_partial_structure(self):
        def run(command, **kwargs):
            Path(command[-1]).write_text(json.dumps({
                "ok": False, "status_code": 503, "code": "PDF_PARSER_UNAVAILABLE",
                "detail": "OCR model unavailable",
            }), encoding="utf-8")
            return SimpleNamespace(returncode=1)

        with patch.dict("os.environ", {"GLOBALPILOT_DOCLING_PYTHON": __file__}), \
             patch.object(pdf_worker.subprocess, "run", side_effect=run):
            result = pdf_worker.parse_pdf(blank_pdf())
        self.assertFalse(result["ok"])
        self.assertEqual(result["status_code"], 503)
        self.assertNotIn("text", result)

    def test_encrypted_pdf_fails_before_launching_parser(self):
        with patch.object(pdf_worker.subprocess, "run") as run:
            result = pdf_worker.parse_pdf(blank_pdf(encrypted=True))
        run.assert_not_called()
        self.assertEqual(result["status_code"], 400)
        self.assertEqual(result["detail"], "不支持加密的 PDF 文件")


class TableContextTests(unittest.TestCase):
    @staticmethod
    def content(identifier, kind, top, bottom, *, page=1, origin="TOPLEFT", text=""):
        if origin == "BOTTOMLEFT":
            top, bottom = 800 - top, 800 - bottom
        return {"content_id": identifier, "kind": kind, "text": text, "provenance": [{
            "page_no": page, "coord_origin": origin, "page_height": 800,
            "bbox": {"l": 100, "r": 500, "t": top, "b": bottom},
        }]}

    def test_units_are_linked_only_to_nearest_table_on_the_same_page(self):
        first = self.content("table-one", "table", 100, 300)
        second = self.content("table-two", "table", 400, 650)
        unit_one = self.content("unit-one", "text", 80, 90, text="单位：万元")
        unit_two = self.content("unit-two", "text", 380, 390, text="单位：元")
        foreign = self.content("foreign-unit", "text", 80, 90, page=2, text="单位：亿元")
        _attach_table_context([first, second, unit_one, unit_two, foreign])
        self.assertEqual(first["context_text"], "单位：万元")
        self.assertEqual(first["related_content_ids"], ["unit-one"])
        self.assertEqual(second["context_text"], "单位：元")
        self.assertEqual(second["related_content_ids"], ["unit-two"])

    def test_bottom_left_coordinates_preserve_the_same_unit_relationship(self):
        table = self.content("table", "table", 100, 300, origin="BOTTOMLEFT")
        unit = self.content("unit", "text", 85, 95, origin="BOTTOMLEFT", text="单 位 : 万 元")
        below = self.content("below", "text", 305, 315, origin="BOTTOMLEFT", text="单位：千元")
        _attach_table_context([table, unit, below])
        self.assertEqual(table["related_content_ids"], ["unit"])
        self.assertEqual(table["context_text"], "单 位 : 万 元")


if __name__ == "__main__":
    unittest.main()
