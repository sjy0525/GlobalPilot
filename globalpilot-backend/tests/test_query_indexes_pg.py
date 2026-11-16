"""Forward, concurrent and idempotent installation of measured query indexes."""
from concurrent.futures import ThreadPoolExecutor
import os
import unittest
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from sqlalchemy.engine import make_url

from app.db.query_indexes import QUERY_INDEXES, apply_query_indexes, ensure_query_indexes
from app.modules.rag.retrieval.lexical import TOKENIZER_VERSION
from scripts.backfill_rag_lexical import backfill

URL = os.environ.get("QUERY_INDEX_TEST_DATABASE_URL")


@unittest.skipUnless(URL, "requires explicit QUERY_INDEX_TEST_DATABASE_URL")
class QueryIndexMigrationTests(unittest.TestCase):
    def setUp(self):
        self.schema = "query_index_" + uuid4().hex
        admin = make_url(URL).set(drivername="postgresql")
        self.admin_url = admin.render_as_string(hide_password=False)
        self.url = admin.update_query_dict({"options": f"-csearch_path={self.schema}"}).render_as_string(hide_password=False)
        with psycopg.connect(self.admin_url, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.addCleanup(self.cleanup_schema)
        with psycopg.connect(self.url, autocommit=True) as conn:
            conn.execute("CREATE TABLE langchain_pg_embedding (collection_id uuid, cmetadata jsonb)")
            conn.execute("CREATE TABLE briefs (user_id uuid, updated_at timestamp, workflow_status text)")
            conn.execute("CREATE TABLE eval_tasks (run_id uuid, execution_status text)")

    def cleanup_schema(self):
        with psycopg.connect(self.admin_url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    def test_existing_schema_gets_valid_indexes_and_reapply_is_idempotent(self):
        first = ensure_query_indexes(self.url)
        self.assertEqual([r["status"] for r in first], ["created"] * len(QUERY_INDEXES))
        self.assertEqual([r["status"] for r in ensure_query_indexes(self.url)], ["existing"] * len(QUERY_INDEXES))
        with psycopg.connect(self.url, autocommit=True) as conn:
            rows = conn.execute("""SELECT c.relname,i.indisvalid FROM pg_index i
                JOIN pg_class c ON c.oid=i.indexrelid
                JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=current_schema()""").fetchall()
            self.assertEqual(dict(rows), {i.name: True for i in QUERY_INDEXES})

    def test_conflicting_existing_definition_is_rejected_without_dropping_it(self):
        with psycopg.connect(self.url, autocommit=True) as conn:
            conn.execute("CREATE INDEX ix_embedding_collection_brand_document ON langchain_pg_embedding (collection_id)")
        with self.assertRaisesRegex(RuntimeError, "unexpected definition"):
            ensure_query_indexes(self.url)
        with psycopg.connect(self.url, autocommit=True) as conn:
            self.assertEqual(conn.execute("SELECT indnkeyatts FROM pg_index WHERE indexrelid='ix_embedding_collection_brand_document'::regclass").fetchone()[0], 1)

    def test_parallel_installers_share_mutex_and_finish_without_duplicate_ddl(self):
        with ThreadPoolExecutor(2) as workers:
            results = list(workers.map(ensure_query_indexes, [self.url, self.url]))
        self.assertEqual(sorted(r[0]["status"] for r in results), ["created", "existing"])

    def test_transactional_connection_is_rejected_before_ddl(self):
        with psycopg.connect(self.url, row_factory=dict_row) as conn:
            with self.assertRaisesRegex(ValueError, "autocommit"):
                apply_query_indexes(conn)

    def test_same_keys_with_wrong_collation_or_opclass_are_rejected(self):
        variants = (
            "collection_id, (cmetadata ->> 'brand_id') COLLATE \"C\", (cmetadata ->> 'document_id')",
            "collection_id, (cmetadata ->> 'brand_id') text_pattern_ops, (cmetadata ->> 'document_id')",
        )
        for keys in variants:
            with self.subTest(keys=keys):
                with psycopg.connect(self.url, autocommit=True) as conn:
                    conn.execute("CREATE INDEX ix_embedding_collection_brand_document ON langchain_pg_embedding (" + keys + ")")
                try:
                    with self.assertRaisesRegex(RuntimeError, "unexpected definition"):
                        ensure_query_indexes(self.url)
                finally:
                    with psycopg.connect(self.url, autocommit=True) as conn:
                        conn.execute("DROP INDEX ix_embedding_collection_brand_document")

    def test_lexical_backfill_only_updates_current_chunks_and_is_repeatable(self):
        collection, team, brand, current_doc, legacy_doc = (uuid4() for _ in range(5))
        body = "X1-Pro 支持 USB-C 65 W 快充"
        embedding = [1.0, 2.0, 3.0]
        shared = {"team_id": str(team), "brand_id": str(brand)}
        metadata = {
            "current": {**shared, "document_id": str(current_doc), "document_version": 2},
            "stale": {**shared, "document_id": str(current_doc), "document_version": 1},
            "legacy": {**shared, "document_id": str(legacy_doc)},
            "versionless": {**shared, "document_id": str(current_doc)},
        }
        with psycopg.connect(self.url, autocommit=True) as conn:
            # Backfill only accesses metadata/body; an array records a vector in
            # the index migration suite without requiring a database extension.
            conn.execute("""
                ALTER TABLE langchain_pg_embedding
                    ADD COLUMN id varchar PRIMARY KEY,
                    ADD COLUMN document text,
                    ADD COLUMN embedding double precision[];
                CREATE TABLE langchain_pg_collection (uuid uuid PRIMARY KEY, name text);
                CREATE TABLE brands (id uuid PRIMARY KEY, team_id uuid);
                CREATE TABLE knowledge_documents (
                    id uuid PRIMARY KEY, team_id uuid, brand_id uuid, version int, structured jsonb
                );
            """)
            conn.execute("INSERT INTO langchain_pg_collection VALUES (%s, 'brand_knowledge')", (collection,))
            conn.execute("INSERT INTO brands VALUES (%s, %s)", (brand, team,))
            conn.execute(
                "INSERT INTO knowledge_documents VALUES (%s,%s,%s,2,NULL), (%s,%s,%s,1,NULL)",
                (current_doc, team, brand, legacy_doc, team, brand),
            )
            with conn.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO langchain_pg_embedding (id, collection_id, document, embedding, cmetadata) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    [(key, collection, body, embedding, Jsonb(meta)) for key, meta in metadata.items()],
                )
        ensure_query_indexes(self.url)
        self.assertEqual(backfill(self.url, batch_size=1), {"selected": 2, "updated": 2, "skipped": 0})
        self.assertEqual(backfill(self.url, batch_size=1), {"selected": 0, "updated": 0, "skipped": 0})
        with psycopg.connect(self.url) as conn:
            rows = conn.execute("SELECT id, cmetadata, document, embedding FROM langchain_pg_embedding").fetchall()
            self.assertEqual({row[0] for row in rows}, set(metadata))
            for key, meta, cached_body, cached_embedding in rows:
                self.assertEqual(cached_body, body)
                self.assertEqual(cached_embedding, embedding)
                if key in {"current", "legacy"}:
                    terms = meta.pop("lexical_text").split()
                    self.assertTrue({"x1pro", "usbc", "65w"}.issubset(terms))
                    self.assertEqual(meta.pop("tokenizer_version"), TOKENIZER_VERSION)
                self.assertEqual(meta, metadata[key])


if __name__ == "__main__":
    unittest.main()
