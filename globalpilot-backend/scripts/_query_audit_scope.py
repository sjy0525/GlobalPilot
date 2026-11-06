"""Compile the real Brief SELECT without database settings or authentication IO.

Only diagnostic query construction uses this helper. Callers own a synthetic
database or have already checked the explicitly selected actor/team in their
read-only transaction. These timings do not measure endpoint authorization.
"""

from functools import lru_cache
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from sqlalchemy.orm import Query


@lru_cache(maxsize=1)
def _service_and_authorization():
    authorization = ModuleType("app.modules.teams.authorization")

    def forbidden(*args, **kwargs):
        raise AssertionError("Query compilation must not execute writes")

    authorization.require_active_team_member = lambda *args, **kwargs: None
    authorization.lock_team_and_require_member = forbidden
    with patch.dict(sys.modules, {"app.modules.teams.authorization": authorization}):
        root = Path(__file__).resolve().parents[1]
        helper_spec = importlib.util.spec_from_file_location(
            "audit_brief_authorization", root / "app/modules/briefs/authorization.py",
        )
        helper = importlib.util.module_from_spec(helper_spec)
        helper_spec.loader.exec_module(helper)
        # Keep the compile-only stub in private module objects. An official
        # authorization helper imported later must receive the real functions.
        with patch.dict(sys.modules, {"app.modules.briefs.authorization": helper}):
            spec = importlib.util.spec_from_file_location(
                "audit_brief_list_service", root / "app/modules/briefs/service.py",
            )
            service = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(service)
    return service, helper


def brief_list_statement(session, *, team_id, actor_user_id):
    service, authorization = _service_and_authorization()
    with patch.object(authorization, "require_active_team_member", lambda *args, **kwargs: None), \
            patch.object(Query, "all", lambda query: query.statement):
        return service.list_briefs(
            session, SimpleNamespace(id=actor_user_id), team_id=team_id,
        )
