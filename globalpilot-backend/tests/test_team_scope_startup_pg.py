"""Real startup schema preparation for fresh and partially upgraded databases.

Each test owns a private schema in the explicit disposable PostgreSQL database.
No application lifespan, vector/model provider, business data or browser is used.
"""

import os
import unittest
from uuid import uuid4

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from tests.test_team_authorization_pg import isolated_database_url


@unittest.skipUnless(os.getenv("TEAM_AUTH_TEST_DATABASE_URL"), "requires isolated PostgreSQL")
class TeamScopeStartupTests(unittest.TestCase):
    def setUp(self):
        from app import main
        from app.modules.brands.models import Brand
        from app.modules.teams.models import Team
        from app.modules.brands.service import seed_all_team_brands

        self.main, self.Brand, self.Team = main, Brand, Team
        self.seed_all_team_brands = seed_all_team_brands
        self.schema = "team_startup_" + uuid4().hex
        self.engine = create_engine(
            isolated_database_url(os.environ["TEAM_AUTH_TEST_DATABASE_URL"]),
            connect_args={"options": f"-csearch_path={self.schema} -cstatement_timeout=15000 -clock_timeout=5000"},
        )
        self.addCleanup(self.engine.dispose)
        with self.engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        self.addCleanup(self.drop_schema)
        self.brand_id, self.brief_id, self.doc_id, self.old_user = [uuid4() for _ in range(4)]

    def drop_schema(self):
        with self.engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))

    def prepare(self):
        with self.engine.begin() as conn:
            self.main._prepare_schema(conn)

    def legacy_brand(self, *, schema=None):
        with self.engine.begin() as conn:
            if schema is not None:
                conn.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            conn.execute(text("""CREATE TABLE brands (
                id UUID PRIMARY KEY, name VARCHAR(100) UNIQUE NOT NULL,
                created_at TIMESTAMP
            )"""))
            conn.execute(text("INSERT INTO brands(id,name,created_at) VALUES (:id,'Audio',now())"),
                         {"id": self.brand_id})

    def legacy_brief(self, *, schema=None):
        with self.engine.begin() as conn:
            if schema is not None:
                conn.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            conn.execute(text("""CREATE TABLE briefs (
                id UUID PRIMARY KEY, user_id UUID NOT NULL,
                brand_id UUID NOT NULL REFERENCES brands(id),
                goal TEXT NOT NULL, selling_points TEXT NOT NULL,
                region VARCHAR(50) NOT NULL, user_persona VARCHAR(255) NOT NULL,
                social_media VARCHAR(50)[] NOT NULL,
                purchase_channels VARCHAR(255), competitor_url VARCHAR(255),
                created_at TIMESTAMP, updated_at TIMESTAMP
            )"""))
            conn.execute(text("""INSERT INTO briefs
                (id,user_id,brand_id,goal,selling_points,region,user_persona,social_media)
                VALUES (:id,:user,:brand,'legacy goal','legacy facts','eu-na','reader',ARRAY['linkedin'])
            """), {"id": self.brief_id, "user": self.old_user, "brand": self.brand_id})

    def legacy_document(self, *, schema=None):
        with self.engine.begin() as conn:
            if schema is not None:
                conn.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            conn.execute(text("""CREATE TABLE knowledge_documents (
                id UUID PRIMARY KEY, brand_id UUID NOT NULL REFERENCES brands(id),
                source VARCHAR(255) NOT NULL, body TEXT, media_type VARCHAR(127),
                file_bytes BYTEA, chunk_count INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP, updated_at TIMESTAMP
            )"""))
            conn.execute(text("""INSERT INTO knowledge_documents
                (id,brand_id,source,body,file_bytes,chunk_count)
                VALUES (:id,:brand,'legacy.txt','legacy document',:payload,1)
            """), {"id": self.doc_id, "brand": self.brand_id, "payload": b"unchanged legacy bytes"})

    def assert_complete_team_schema(self):
        with self.engine.connect() as conn:
            inspector = inspect(conn)
            for name in ("brands", "briefs", "knowledge_documents", "eval_runs"):
                self.assertTrue(inspector.has_table(name, schema=self.schema))
                columns = {item["name"]: item for item in inspector.get_columns(name, schema=self.schema)}
                self.assertTrue(columns["team_id"]["nullable"], name)
                self.assertTrue(any(fk["constrained_columns"] == ["team_id"] and
                                    fk["referred_table"] == "teams" and
                                    fk["referred_schema"] in (None, self.schema) for fk in
                                    inspector.get_foreign_keys(name, schema=self.schema)), name)
            for name in ("briefs", "knowledge_documents"):
                self.assertTrue(any(fk["constrained_columns"] == ["team_id", "brand_id"] and
                                    fk["referred_table"] == "brands" and
                                    fk["referred_columns"] == ["team_id", "id"] and
                                    fk["referred_schema"] in (None, self.schema) for fk in
                                    inspector.get_foreign_keys(name, schema=self.schema)), name)
            self.assertNotIn(["name"], [item["column_names"] for item in
                                       inspector.get_unique_constraints("brands", schema=self.schema)])

    def test_fresh_database_prepares_all_tables_and_can_repeat(self):
        self.prepare()
        self.prepare()
        self.assert_complete_team_schema()
        with self.engine.connect() as conn:
            self.assertEqual(conn.scalar(text("SELECT count(*) FROM brands")), 0)
            self.assertEqual(conn.scalar(text("SELECT count(*) FROM teams")), 0)

    def test_only_legacy_brands_upgrades_keys_before_creating_dependent_tables(self):
        self.legacy_brand()
        self.prepare()
        self.prepare()
        self.assert_complete_team_schema()
        with self.engine.connect() as conn:
            self.assertEqual(tuple(conn.execute(text("SELECT id,name,team_id FROM brands")).one()),
                             (self.brand_id, "Audio", None))

    def test_existing_legacy_brief_and_missing_document_table_start_successfully(self):
        self.legacy_brand()
        self.legacy_brief()
        self.prepare()
        self.prepare()
        self.assert_complete_team_schema()
        with self.engine.connect() as conn:
            self.assertEqual(tuple(conn.execute(text("SELECT id,user_id,brand_id,goal,team_id FROM briefs")).one()),
                             (self.brief_id, self.old_user, self.brand_id, "legacy goal", None))
            self.assertEqual(conn.scalar(text("SELECT count(*) FROM knowledge_documents")), 0)

    def test_existing_legacy_document_and_missing_brief_table_preserve_file_bytes(self):
        self.legacy_brand()
        self.legacy_document()
        self.prepare()
        self.prepare()
        self.assert_complete_team_schema()
        with self.engine.connect() as conn:
            row = conn.execute(text("SELECT id,brand_id,body,file_bytes,team_id,version FROM knowledge_documents")).one()
            self.assertEqual(tuple(row), (self.doc_id, self.brand_id, "legacy document", b"unchanged legacy bytes", None, 1))
            self.assertEqual(conn.scalar(text("SELECT count(*) FROM briefs")), 0)

    def test_existing_legacy_resources_without_other_application_tables_start_successfully(self):
        self.legacy_brand()
        self.legacy_brief()
        self.legacy_document()
        self.prepare()
        self.prepare()
        self.assert_complete_team_schema()
        with self.engine.connect() as conn:
            self.assertEqual(conn.scalar(text("SELECT count(*) FROM brands WHERE team_id IS NULL")), 1)
            self.assertEqual(conn.scalar(text("SELECT count(*) FROM briefs WHERE team_id IS NULL")), 1)
            self.assertEqual(conn.scalar(text("SELECT count(*) FROM knowledge_documents WHERE team_id IS NULL")), 1)

    def test_team_default_seeding_does_not_adopt_or_merge_global_historical_resources(self):
        self.legacy_brand()
        self.legacy_brief()
        self.legacy_document()
        self.prepare()
        team_a, team_b = uuid4(), uuid4()
        with Session(self.engine) as db, db.begin():
            db.add_all([self.Team(id=team_a, name="New A"), self.Team(id=team_b, name="New B")])
            db.flush()
            self.seed_all_team_brands(db)
            self.seed_all_team_brands(db)
        self.prepare()
        with Session(self.engine) as db:
            legacy = db.get(self.Brand, self.brand_id)
            self.assertIsNone(legacy.team_id)
            for team in (team_a, team_b):
                rows = db.scalars(select(self.Brand).where(self.Brand.team_id == team)).all()
                self.assertEqual({row.name for row in rows}, {"Wearables", "Audio", "Smart home"})
                self.assertNotIn(self.brand_id, {row.id for row in rows})
            self.assertEqual(db.scalar(text("SELECT count(*) FROM brands")), 7)
            self.assertEqual(db.scalar(text("SELECT team_id FROM briefs WHERE id=:id"), {"id": self.brief_id}), None)
            self.assertEqual(db.scalar(text("SELECT team_id FROM knowledge_documents WHERE id=:id"), {"id": self.doc_id}), None)

    def assert_fallback_schema_is_untouched(self, *, upgraded):
        fallback = "team_fallback_" + uuid4().hex
        with self.engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{fallback}"'))

        def drop_fallback():
            with self.engine.begin() as conn:
                conn.execute(text(f'DROP SCHEMA "{fallback}" CASCADE'))

        self.addCleanup(drop_fallback)
        self.legacy_brand(schema=fallback)
        self.legacy_brief(schema=fallback)
        self.legacy_document(schema=fallback)
        if upgraded:
            with self.engine.begin() as conn:
                conn.execute(text(f'SET LOCAL search_path TO "{fallback}"'))
                self.main._prepare_schema(conn)

        def fallback_snapshot():
            with self.engine.connect() as conn:
                inspector = inspect(conn)
                tables = inspector.get_table_names(schema=fallback)
                return {
                    name: {
                        "columns": [(col["name"], str(col["type"]), col["nullable"], col.get("default"))
                                    for col in inspector.get_columns(name, schema=fallback)],
                        "foreign_keys": inspector.get_foreign_keys(name, schema=fallback),
                        "unique": inspector.get_unique_constraints(name, schema=fallback),
                        "indexes": inspector.get_indexes(name, schema=fallback),
                        "rows": [tuple(row) for row in conn.execute(text(f'SELECT * FROM "{fallback}"."{name}" ORDER BY 1'))],
                    }
                    for name in tables
                }

        before = fallback_snapshot()
        for _ in range(2):
            with self.engine.begin() as conn:
                conn.execute(text(f'SET LOCAL search_path TO "{self.schema}", "{fallback}"'))
                self.assertEqual(conn.scalar(text("SELECT current_schema()")), self.schema)
                self.main._prepare_schema(conn)
        self.assert_complete_team_schema()
        self.assertEqual(fallback_snapshot(), before)
        with self.engine.connect() as conn:
            for name in ("brands", "briefs", "knowledge_documents", "teams"):
                self.assertEqual(conn.scalar(text(f'SELECT count(*) FROM "{self.schema}"."{name}"')), 0, name)

    def test_private_startup_does_not_adopt_or_mutate_legacy_tables_in_search_path_fallback(self):
        self.assert_fallback_schema_is_untouched(upgraded=False)

    def test_private_startup_does_not_adopt_or_mutate_current_tables_in_search_path_fallback(self):
        self.assert_fallback_schema_is_untouched(upgraded=True)
