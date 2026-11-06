"""Forward, concurrent and idempotent installation of measured query indexes."""
from concurrent.futures import ThreadPoolExecutor
import os
import unittest
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from sqlalchemy.engine import make_url

from app.db.query_indexes import QUERY_INDEXES, apply_query_indexes, ensure_query_indexes

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


if __name__ == "__main__":
    unittest.main()
