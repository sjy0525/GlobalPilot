import unittest

from langchain_core.documents import Document

from app.modules.rag.retrieval.hybrid import merge_hits, rank_by_rrf
from app.modules.rag.retrieval.lexical import keyword_query, lexical_metadata, with_lexical_metadata


class HybridRagTests(unittest.TestCase):
    def test_rrf_preserves_channels_and_distinguishes_table_rows_and_versions(self):
        def chunk(text, index, *, version=1):
            return Document(page_content=text, metadata={
                "document_id": "doc", "document_version": version,
                "chunk_index": index, "content_ids": ["same-table"],
            })

        common = chunk("both channels", 0)
        keyword_only = chunk("keyword rescues distant vector", 1)
        vector_only = chunk("same table, separate row", 2)
        old_version = chunk("same position, separate version", 0, version=0)
        merged = merge_hits(
            [(vector_only, 0.1), (common, 0.2), (keyword_only, 0.9)],
            [(common, 0.91), (keyword_only, 0.8), (old_version, 0.3), (common, 0.1)],
            max_distance=0.64,
        )
        self.assertEqual(len(merged), 4)
        valid = [(doc, scores) for doc, scores in merged if doc.metadata["document_version"] == 1]
        ranked = rank_by_rrf(valid, constant=60, limit=3)
        self.assertEqual([doc.page_content for doc, _ in ranked], [
            common.page_content, vector_only.page_content, keyword_only.page_content,
        ])
        self.assertAlmostEqual(ranked[0][0].metadata["retrieval"]["rrf_score"], 1 / 61 + 1 / 62)
        self.assertEqual(ranked[0][0].metadata["retrieval"]["channels"], ["vector", "keyword"])
        self.assertEqual(ranked[0][0].metadata["retrieval"]["keyword_score"], 0.91)
        keyword_doc, distance = ranked[-1]
        self.assertIsNone(distance)
        self.assertEqual(keyword_doc.metadata["retrieval"]["channels"], ["keyword"])

    def test_index_and_query_share_product_and_power_aliases(self):
        original = Document(page_content="输出 65 W", metadata={
            "source": "规格.pdf", "section_path": ["接口"], "caption": "ＺＸ９１‑Ｐｒｏ",
            "context_text": "USB-C 快充", "page_no": 2,
        })
        indexed = with_lexical_metadata([original])[0]
        terms = set(indexed.metadata["lexical_text"].split())
        query = set(keyword_query("ZX91 Pro 的 USB C 是否支持 65瓦？").split(" | "))
        aliases = {"zx91pro", "usbc", "65w"}
        self.assertTrue(aliases <= terms)
        self.assertTrue(aliases <= query)
        self.assertTrue({"接口", "快充"} <= terms)
        self.assertEqual(lexical_metadata(original.page_content, original.metadata)["tokenizer_version"], 1)
        self.assertNotIn("lexical_text", original.metadata)
        self.assertEqual(indexed.page_content, original.page_content)
        self.assertEqual(keyword_query("请问，这个可以吗？"), "")
