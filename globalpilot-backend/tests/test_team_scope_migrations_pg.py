"""Forward team ownership migrations against a real, isolated legacy schema."""

import os
import unittest
from uuid import uuid4

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.db.team_scope_migrations import apply_team_scope_migrations
from tests.test_team_authorization_pg import isolated_database_url


@unittest.skipUnless(os.getenv("TEAM_AUTH_TEST_DATABASE_URL"), "requires isolated PostgreSQL")
class TeamScopeMigrationsTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(isolated_database_url(os.environ["TEAM_AUTH_TEST_DATABASE_URL"]))
        self.schema = "team_migration_" + uuid4().hex
        self.a, self.b, self.user, self.brand, self.doc, self.brief, self.testset = [uuid4() for _ in range(7)]
        with self.engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{self.schema}"'))
            conn.execute(text(f'SET LOCAL search_path TO "{self.schema}"'))
            for statement in (
                "CREATE TABLE teams (id UUID PRIMARY KEY)",
                "CREATE TABLE brands (id UUID PRIMARY KEY, name TEXT UNIQUE NOT NULL)",
                "CREATE TABLE briefs (id UUID PRIMARY KEY, brand_id UUID REFERENCES brands(id), user_id UUID, goal TEXT)",
                "CREATE TABLE knowledge_documents (id UUID PRIMARY KEY, brand_id UUID REFERENCES brands(id), body TEXT)",
                "CREATE TABLE eval_runs (id UUID PRIMARY KEY, user_id UUID, test_set_id UUID, status TEXT, is_baseline BOOLEAN, created_at TIMESTAMP, finished_at TIMESTAMP)",
                "CREATE UNIQUE INDEX uq_eval_baseline ON eval_runs(user_id,test_set_id) WHERE is_baseline",
            ):
                conn.execute(text(statement))
            conn.execute(text("INSERT INTO teams VALUES (:a),(:b)"), {"a": self.a, "b": self.b})
            conn.execute(text("INSERT INTO brands VALUES (:id,'Audio')"), {"id": self.brand})
            conn.execute(text("INSERT INTO briefs VALUES (:id,:brand,:user,'legacy goal')"), {"id": self.brief,"brand":self.brand,"user":self.user})
            conn.execute(text("INSERT INTO knowledge_documents VALUES (:id,:brand,'legacy bytes')"), {"id": self.doc,"brand":self.brand})

    def tearDown(self):
        with self.engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        self.engine.dispose()

    def migrate(self):
        with self.engine.begin() as conn:
            conn.execute(text(f'SET LOCAL search_path TO "{self.schema}"'))
            apply_team_scope_migrations(conn)

    def scoped(self, conn):
        conn.execute(text(f'SET LOCAL search_path TO "{self.schema}"'))

    def test_legacy_rows_and_content_remain_unassigned_after_repeated_upgrade(self):
        self.migrate()
        self.migrate()
        with self.engine.begin() as conn:
            self.scoped(conn)
            self.assertEqual(tuple(conn.execute(text("SELECT id,goal,team_id FROM briefs")).one()), (self.brief,"legacy goal",None))
            self.assertEqual(tuple(conn.execute(text("SELECT id,body,team_id FROM knowledge_documents")).one()), (self.doc,"legacy bytes",None))
            self.assertEqual(tuple(conn.execute(text("SELECT id,name,team_id FROM brands")).one()), (self.brand,"Audio",None))

    def test_team_columns_foreign_keys_and_indexes_are_present(self):
        self.migrate()
        with self.engine.connect() as conn:
            for name in ("brands","briefs","knowledge_documents","eval_runs"):
                columns = {c["name"]:c for c in inspect(conn).get_columns(name,schema=self.schema)}
                self.assertTrue(columns["team_id"]["nullable"])
                self.assertTrue(any(fk["constrained_columns"] == ["team_id"] and fk["referred_table"] == "teams" for fk in inspect(conn).get_foreign_keys(name,schema=self.schema)))
                self.assertIn("ix_"+name+"_team_id", {idx["name"] for idx in inspect(conn).get_indexes(name,schema=self.schema)})

    def test_two_teams_can_have_same_brand_name_but_one_team_cannot_duplicate(self):
        self.migrate()
        with self.engine.begin() as conn:
            self.scoped(conn)
            for team in (self.a,self.b):
                conn.execute(text("INSERT INTO brands(id,name,team_id) VALUES (:id,'Audio',:team)"),{"id":uuid4(),"team":team})
        with self.assertRaises(IntegrityError):
            with self.engine.begin() as conn:
                self.scoped(conn)
                conn.execute(text("INSERT INTO brands(id,name,team_id) VALUES (:id,'Audio',:team)"),{"id":uuid4(),"team":self.a})

    def test_document_and_brief_cannot_reference_another_teams_brand(self):
        self.migrate()
        with self.engine.begin() as conn:
            self.scoped(conn)
            conn.execute(text("UPDATE brands SET team_id=:a WHERE id=:id"),{"a":self.a,"id":self.brand})
        for table in ("briefs","knowledge_documents"):
            with self.subTest(table=table), self.assertRaises(IntegrityError):
                with self.engine.begin() as conn:
                    self.scoped(conn)
                    conn.execute(text(f"UPDATE {table} SET team_id=:b"),{"b":self.b})

    def test_unknown_team_is_rejected_by_database(self):
        self.migrate()
        with self.assertRaises(IntegrityError):
            with self.engine.begin() as conn:
                self.scoped(conn)
                conn.execute(text("UPDATE brands SET team_id=:team"),{"team":uuid4()})

    def test_baselines_are_independent_by_team_and_legacy_scope(self):
        self.migrate()
        with self.engine.begin() as conn:
            self.scoped(conn)
            for team in (self.a,self.b,None):
                conn.execute(text("INSERT INTO eval_runs VALUES (:id,:user,:set,'completed',TRUE,now(),now(),:team)"),{"id":uuid4(),"user":self.user,"set":self.testset,"team":team})
        self.migrate()
        with self.engine.begin() as conn:
            self.scoped(conn)
            self.assertEqual(conn.execute(text("SELECT count(*) FROM eval_runs WHERE is_baseline")).scalar_one(),3)
        for team in (self.a,None):
            with self.subTest(team=team),self.assertRaises(IntegrityError):
                with self.engine.begin() as conn:
                    self.scoped(conn)
                    conn.execute(text("INSERT INTO eval_runs VALUES (:id,:user,:set,'completed',TRUE,now(),now(),:team)"),{"id":uuid4(),"user":self.user,"set":self.testset,"team":team})

    def test_explicitly_assigned_ownership_survives_repeated_upgrade(self):
        self.migrate()
        with self.engine.begin() as conn:
            self.scoped(conn)
            conn.execute(text("UPDATE brands SET team_id=:a"),{"a":self.a})
            conn.execute(text("UPDATE briefs SET team_id=:a"),{"a":self.a})
            conn.execute(text("UPDATE knowledge_documents SET team_id=:a"),{"a":self.a})
        self.migrate()
        with self.engine.begin() as conn:
            self.scoped(conn)
            self.assertEqual(conn.execute(text("SELECT team_id FROM briefs")).scalar_one(),self.a)

    def test_invalid_foreign_key_is_rejected_without_partial_schema_upgrade(self):
        self.migrate()
        with self.engine.begin() as conn:
            self.scoped(conn)
            conn.execute(text("ALTER TABLE briefs DROP CONSTRAINT fk_briefs_team_id"))
            conn.execute(text("ALTER TABLE briefs ADD CONSTRAINT fk_briefs_team_id FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE"))
        with self.assertRaisesRegex(RuntimeError,"Unexpected team foreign key"):
            self.migrate()

    def test_existing_baseline_index_with_wrong_predicate_is_rejected(self):
        self.migrate()
        with self.engine.begin() as conn:
            self.scoped(conn)
            conn.execute(text("DROP INDEX uq_eval_baseline"))
            conn.execute(text("CREATE UNIQUE INDEX uq_eval_baseline ON eval_runs(team_id,user_id,test_set_id)"))
        with self.assertRaisesRegex(RuntimeError,"Unexpected uq_eval_baseline index"):
            self.migrate()

    def test_existing_legacy_baseline_index_without_uniqueness_is_rejected(self):
        self.migrate()
        with self.engine.begin() as conn:
            self.scoped(conn)
            conn.execute(text("DROP INDEX uq_eval_baseline_legacy"))
            conn.execute(text("CREATE INDEX uq_eval_baseline_legacy ON eval_runs(user_id,test_set_id) WHERE is_baseline AND team_id IS NULL"))
        with self.assertRaisesRegex(RuntimeError,"Unexpected uq_eval_baseline_legacy index"):
            self.migrate()
