"""Structural retrieval contracts, without model providers or PostgreSQL."""

import base64
import unittest
from unittest.mock import patch

from app.modules.rag.processing import structured as processing


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aF1sAAAAASUVORK5CYII=")


def document_fixture():
    def location(page):
        return [{"page_no": page, "bbox": {"l": 36, "t": 750, "r": 580, "b": 650},
                 "coord_origin": "BOTTOMLEFT", "page_width": 612, "page_height": 792}]
    contents = [
        {"content_id": "heading-001", "kind": "heading", "text": "Technical specifications", "markdown": "## Technical specifications",
         "section_path": ["Technical specifications"], "page_no": 1, "provenance": location(1)},
        {"content_id": "text-001", "kind": "text", "text": "The certified maximum output is 65 watts.", "markdown": "The certified maximum output is 65 watts.",
         "section_path": ["Technical specifications"], "page_no": 1, "provenance": location(1)},
        {"content_id": "table-001", "kind": "table", "text": "Model | Output\nAlpha | 65W\nBeta | 30W",
         "markdown": "| Model | Output |\n| --- | --- |\n| Alpha | 65W |\n| Beta | 30W |", "section_path": ["Technical specifications"], "page_no": 2, "provenance": location(2),
         "table": {"num_rows": 3, "num_cols": 2, "table_cells": [{"text": "Alpha", "start_row_offset_idx": 1, "start_col_offset_idx": 0}, {"text": "65W", "start_row_offset_idx": 1, "start_col_offset_idx": 1}]}},
        {"content_id": "picture-001", "kind": "picture", "text": "OCR: USB-C 65W", "markdown": "OCR: USB-C 65W", "description": "Power label. OCR: USB-C 65W",
         "section_path": ["Technical specifications"], "page_no": 2, "provenance": location(2), "asset_id": "picture-001"},
    ]
    structure = {"schema_version": 1, "parser": "docling", "parser_version": "test-contract", "page_count": 2,
                 "pages": [{"page_no": page, "width": 612, "height": 792} for page in (1, 2)], "contents": contents}
    assets = [{"id": "picture-001", "media_type": "image/png", "data": PNG, "kind": "picture", "page_no": 2}]
    return structure, assets


class StructuredRagTests(unittest.TestCase):
    def setUp(self):
        self.structure, self.assets = document_fixture()
        self.metadata = {"team_id": "team", "brand_id": "brand", "document_id": "document", "document_version": 1, "source": "spec.pdf"}

    def test_sections_tables_pictures_and_original_locations_are_retained(self):
        processing.validate_structure(self.structure, self.assets)
        canonical, chunks = processing.structured_documents(self.structure, self.metadata)
        self.assertEqual(len(chunks), 3)
        self.assertEqual(chunks[0].metadata["content_ids"], ["heading-001", "text-001"])
        self.assertEqual(chunks[0].metadata["page_numbers"], [1])
        self.assertEqual(chunks[1].metadata["kind"], "table")
        self.assertIn("| Alpha | 65W |", chunks[1].page_content)
        self.assertEqual(chunks[2].metadata["kind"], "picture")
        self.assertIn("USB-C 65W", chunks[2].page_content)
        self.assertEqual(canonical["contents"], self.structure["contents"])
        self.assertNotIn("retrieval_units", self.structure, "Input parser evidence must not be mutated")
        self.assertEqual(canonical["retrieval_units"][1]["metadata"]["provenance"][0]["coord_origin"], "BOTTOMLEFT")

    def test_table_row_boundaries_and_repeated_headers_survive_chunking(self):
        table = self.structure["contents"][2]
        rows = [f"| Device-{index} | {index + 10}W |" for index in range(20)]
        table["markdown"] = "| Model | Output |\n| --- | --- |\n" + "\n".join(rows)
        self.structure["contents"] = [table]
        with patch.object(processing.settings, "rag_chunk_size", 100):
            canonical, chunks = processing.structured_documents(self.structure, self.metadata)
        self.assertGreater(len(chunks), 1)
        all_rows = [line for chunk in chunks for line in chunk.page_content.splitlines()[2:]]
        self.assertEqual(all_rows, rows)
        self.assertTrue(all(chunk.page_content.startswith("| Model | Output |\n| --- | --- |") for chunk in chunks))
        self.assertTrue(all(chunk.metadata["content_ids"] == ["table-001"] for chunk in chunks))
        self.assertEqual(canonical["contents"][0]["table"], table["table"])

    def test_table_caption_keeps_applicability_without_changing_original_cells(self):
        table = self.structure["contents"][2]
        table["caption"] = "Measurements at 240 V AC only; values do not apply at 120 V AC."
        self.structure["contents"] = [table]
        canonical, chunks = processing.structured_documents(self.structure, self.metadata)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].page_content, table["markdown"])
        self.assertEqual(chunks[0].metadata["caption"], table["caption"])
        self.assertEqual(canonical["contents"][0]["caption"], table["caption"])
        self.assertEqual(canonical["retrieval_units"][0]["text"], table["markdown"])
        self.assertEqual(canonical["retrieval_units"][0]["metadata"]["caption"], table["caption"])

    def test_page_boundaries_prevent_unrelated_pages_being_merged(self):
        self.structure["contents"] = [self.structure["contents"][1], {**self.structure["contents"][1], "content_id": "text-002", "page_no": 2, "provenance": self.structure["contents"][2]["provenance"]}]
        _, chunks = processing.structured_documents(self.structure, self.metadata)
        self.assertEqual([chunk.metadata["page_numbers"] for chunk in chunks], [[1], [2]])

    def test_table_units_link_to_original_neighboring_context(self):
        table = self.structure["contents"][2]
        unit = {**self.structure["contents"][1], "content_id": "unit-001", "text": "单位：万元",
                "markdown": "单位：万元", "page_no": 2, "provenance": table["provenance"]}
        table["context_text"] = unit["text"]
        table["related_content_ids"] = [unit["content_id"]]
        self.structure["contents"] = [table, unit]
        processing.validate_structure(self.structure, self.assets)
        _, chunks = processing.structured_documents(self.structure, self.metadata)
        self.assertEqual(chunks[0].metadata["kind"], "table")
        self.assertEqual(chunks[0].metadata["content_ids"], ["table-001", "unit-001"])
        self.assertEqual(chunks[0].metadata["context_text"], "单位：万元")
        self.assertEqual(chunks[0].page_content, table["markdown"])

    def test_unreadable_picture_placeholder_is_saved_but_not_indexed(self):
        picture = self.structure["contents"][-1]
        picture.update(text="", markdown="", description="图像待补充描述", retrieval_ready=False)
        self.structure["contents"] = [picture]
        canonical, chunks = processing.structured_documents(self.structure, self.metadata)
        self.assertEqual(chunks, [])
        self.assertEqual(canonical["contents"][0]["asset_id"], "picture-001")

    def test_table_preamble_is_repeated_with_complete_rows(self):
        table = self.structure["contents"][2]
        table["markdown"] = "部门公开表 1\n单位：万元\n| 项目 | 金额 |\n| --- | --- |\n" + "\n".join(f"| 项目{index} | {index}.00 |" for index in range(12))
        self.structure["contents"] = [table]
        with patch.object(processing.settings, "rag_chunk_size", 100):
            _, chunks = processing.structured_documents(self.structure, self.metadata)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk.page_content.startswith("部门公开表 1\n单位：万元\n| 项目 | 金额 |\n| --- | --- |") for chunk in chunks))
        rows = [row for chunk in chunks for row in chunk.page_content.splitlines()[4:]]
        self.assertEqual(rows, [f"| 项目{index} | {index}.00 |" for index in range(12)])

    def test_invalid_evidence_identity_page_and_assets_fail_validation(self):
        for mutate in (
            lambda value: value["contents"].append(value["contents"][0]),
            lambda value: value["contents"][0].update(page_no=0),
            lambda value: value["contents"][0].update(content_id="../../private"),
            lambda value: value["contents"][0]["provenance"][0].update(coord_origin="UNKNOWN"),
            lambda value: value["contents"][0]["provenance"][0].update(page_no=999),
            lambda value: value["contents"][0]["provenance"][0].update(page_width=0),
            lambda value: value["contents"][0]["provenance"][0]["bbox"].update(l=float("nan")),
            lambda value: value["contents"][-1].update(asset_id="missing"),
        ):
            structure, assets = document_fixture()
            mutate(structure)
            with self.assertRaises(ValueError):
                processing.validate_structure(structure, assets)


if __name__ == "__main__":
    unittest.main()
