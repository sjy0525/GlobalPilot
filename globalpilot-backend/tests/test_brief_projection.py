"""Brief history preserves its response while avoiding unused generated payloads."""

import os
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.db.base import Base
from app.modules.brands.models import Brand
from app.modules.briefs.models import Brief
from app.modules.users.models import User
from app.modules.teams.models import Team, TeamMember
from app.modules.briefs.schemas import BriefListItem
from app.modules.briefs.service import list_briefs


class BriefProjectionTests(unittest.TestCase):
    def test_history_query_selects_response_fields_without_limit_or_large_outputs(self):
        # Capture the real ORM query without making a database connection.
        session = Session()
        query = session.query(Brief)
        database = Mock()
        database.query.return_value = query
        with unittest.mock.patch.object(type(query), "all", autospec=True, return_value=[]) as all_rows:
            with unittest.mock.patch("app.modules.briefs.authorization.require_active_team_member"):
                list_briefs(database, SimpleNamespace(id=uuid4()), team_id=uuid4())
        statement = all_rows.call_args.args[0].statement
        compiled = str(statement.compile(dialect=postgresql.dialect()))
        selected = compiled.split("\nFROM", 1)[0]
        for name in BriefListItem.model_fields:
            self.assertIn(f"briefs.{name}", selected)
        for name in ("strategy", "content", "final_output", "workflow_checkpoint_id", "workflow_version"):
            self.assertNotIn(f"briefs.{name}", selected)
        self.assertIn("briefs.workflow_status IS NOT NULL", compiled)
        self.assertIn("ORDER BY briefs.updated_at DESC", compiled)
        self.assertNotIn("LIMIT", compiled)
        session.close()


@unittest.skipUnless(os.getenv("WORKFLOW_INDEX_TEST_DATABASE_URL"), "requires isolated WORKFLOW_INDEX_TEST_DATABASE_URL")
class BriefProjectionPostgresTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(os.environ["WORKFLOW_INDEX_TEST_DATABASE_URL"])
        self.connection = self.engine.connect()
        self.transaction = self.connection.begin()
        self.schema = f"brief_projection_{uuid4().hex}"
        self.connection.exec_driver_sql(f'CREATE SCHEMA "{self.schema}"')
        self.connection = self.connection.execution_options(schema_translate_map={None: self.schema})
        Base.metadata.create_all(self.connection, tables=[User.__table__, Team.__table__, TeamMember.__table__, Brand.__table__, Brief.__table__])
        self.session = Session(bind=self.connection)
        self.addCleanup(self.cleanup_database)

    def cleanup_database(self):
        self.session.close()
        self.transaction.rollback()
        self.connection.close()
        self.engine.dispose()

    def test_serialization_uses_one_query_and_preserves_history_filtering_and_order(self):
        user, other = User(email="projection@example.invalid"), User(email="other@example.invalid")
        team = Team(name="Projection")
        self.session.add_all([user, other, team])
        self.session.flush()
        brand = Brand(name="Projection", team_id=team.id)
        self.session.add(brand)
        self.session.add(TeamMember(team_id=team.id, user_id=user.id, role="employee", status="active"))
        self.session.flush()
        user_id, team_id = user.id, team.id
        now = datetime(2026, 1, 1)
        rows = []
        for position, (owner, status) in enumerate([
            (user, "completed"), (user, None), (other, "completed"), (user, "awaiting_review"),
        ]):
            rows.append(Brief(
                team_id=team.id, user_id=owner.id, brand_id=brand.id, goal=f"Goal {position}", selling_points="Points",
                region="eu-na", user_persona="Readers", social_media=["linkedin"],
                purchase_channels=None, competitor_url=None, workflow_status=status,
                strategy="S" * 20000, content={"text": "C" * 20000}, final_output="F" * 20000,
                updated_at=now + timedelta(days=position),
            ))
        self.session.add_all(rows)
        self.session.flush()
        expected = [BriefListItem.model_validate(row).model_dump() for row in (rows[3], rows[0])]
        self.session.expunge_all()
        statements = []

        def capture(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(self.connection, "before_cursor_execute", capture)
        result = list_briefs(self.session, SimpleNamespace(id=user_id), team_id=team_id)
        serialized = [BriefListItem.model_validate(row).model_dump() for row in result]
        self.assertEqual(serialized, expected)
        self.assertEqual(len(statements), 2, statements)
        self.assertTrue(all({"strategy", "content", "final_output"} <= inspect(row).unloaded for row in result))
        self.assertTrue(all(name not in statements[-1] for name in ("briefs.strategy", "briefs.content", "briefs.final_output")))


if __name__ == "__main__":
    unittest.main()
