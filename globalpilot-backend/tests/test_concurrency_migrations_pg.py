"""Forward upgrades, data preservation and unique baseline constraints on PG."""
import os
import unittest
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from app.db.base import Base
from app.db.concurrency_migrations import apply_concurrency_migrations
from app.modules.brands.models import Brand
from app.modules.briefs.models import Brief
from app.modules.evaluation.models import EvalCase, EvalExecutorLease, EvalRun, EvalTask, EvalTestSet
from app.modules.rag.models import KnowledgeDocument
from app.modules.users.models import User

URL = os.environ.get("CONCURRENCY_MIGRATIONS_TEST_DATABASE_URL")


@unittest.skipUnless(URL, "CONCURRENCY_MIGRATIONS_TEST_DATABASE_URL is not set")
class ConcurrencyMigrationsTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(URL)
        self.schema = "audit_" + uuid4().hex
        self.user_id, self.set_id, self.old_id, self.new_id = (uuid4() for _ in range(4))
        with self.engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{self.schema}"'))
            conn.execute(text(f'SET LOCAL search_path TO "{self.schema}"'))
            Base.metadata.create_all(conn)
            conn.execute(text("DROP INDEX uq_eval_baseline"))
            conn.execute(text("DROP INDEX IF EXISTS uq_eval_baseline_legacy"))
            conn.execute(text("ALTER TABLE briefs DROP COLUMN workflow_version, DROP COLUMN workflow_checkpoint_id"))
            conn.execute(text("ALTER TABLE knowledge_documents DROP COLUMN version"))
            conn.execute(text("ALTER TABLE users DROP COLUMN is_active"))
            conn.execute(text("INSERT INTO users(id,email,created_at,updated_at) VALUES (:id,:email,now(),now())"), {"id":self.user_id,"email":"migration@example.invalid"})
            conn.execute(text("INSERT INTO eval_test_sets(id,version,name,description,created_at) VALUES (:id,'audit','audit','audit',now())"), {"id":self.set_id})
            for run_id, date in ((self.old_id, "2020-01-01"), (self.new_id, "2020-01-02")):
                conn.execute(text("""INSERT INTO eval_runs(id,user_id,test_set_id,label,status,is_baseline,provenance,created_at,finished_at)
                    VALUES (:id,:user,:set,'audit','completed',TRUE,'{}',:date,:date)"""),
                    {"id":run_id,"user":self.user_id,"set":self.set_id,"date":date})

    def tearDown(self):
        with self.engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        self.engine.dispose()

    def migrate(self):
        with self.engine.begin() as conn:
            conn.execute(text(f'SET LOCAL search_path TO "{self.schema}"'))
            apply_concurrency_migrations(conn)

    def test_forward_upgrade_is_idempotent_and_keeps_all_runs(self):
        self.migrate()
        self.migrate()
        with self.engine.connect() as conn:
            conn.execute(text(f'SET LOCAL search_path TO "{self.schema}"'))
            rows = dict(conn.execute(text("SELECT id,is_baseline FROM eval_runs")).all())
            self.assertEqual(rows, {self.old_id:False,self.new_id:True})
            columns = set(conn.execute(text("""SELECT table_name,column_name FROM information_schema.columns
                WHERE table_schema=:schema"""), {"schema":self.schema}).all())
            self.assertIn(("briefs","workflow_version"),columns)
            self.assertIn(("briefs","workflow_checkpoint_id"),columns)
            self.assertIn(("knowledge_documents","version"),columns)
            self.assertIn(("users","is_active"),columns)
            self.assertIs(
                conn.execute(text("SELECT is_active FROM users WHERE id=:id"), {"id": self.user_id}).scalar_one(),
                True,
            )
            status_column = conn.execute(text("""
                SELECT data_type, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema=:schema AND table_name='users'
                AND column_name='is_active'
            """), {"schema": self.schema}).one()
            self.assertEqual(tuple(status_column), ("boolean", "NO", "true"))

    def test_repeated_upgrade_keeps_disabled_accounts_disabled(self):
        self.migrate()
        with self.engine.begin() as conn:
            conn.execute(text(f'SET LOCAL search_path TO "{self.schema}"'))
            conn.execute(text("UPDATE users SET is_active=FALSE WHERE id=:id"), {"id": self.user_id})

        self.migrate()
        with self.engine.connect() as conn:
            conn.execute(text(f'SET LOCAL search_path TO "{self.schema}"'))
            self.assertIs(
                conn.execute(text("SELECT is_active FROM users WHERE id=:id"), {"id": self.user_id}).scalar_one(),
                False,
            )

    def test_database_rejects_a_second_baseline_after_upgrade(self):
        self.migrate()
        with self.assertRaises(IntegrityError):
            with self.engine.begin() as conn:
                conn.execute(text(f'SET LOCAL search_path TO "{self.schema}"'))
                conn.execute(text("UPDATE eval_runs SET is_baseline=TRUE WHERE id=:id"),{"id":self.old_id})
