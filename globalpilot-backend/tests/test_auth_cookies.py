"""Real JWT + HTTP cookie roundtrips; mocked user repository, no database/network."""
import importlib
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
from uuid import uuid4

import jwt
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.modules.users.models import User  # Keep ORM registration outside sys.modules stubs.


class AuthCookieTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Import only the auth router, avoiding app.main's DB startup/migrations.
        database = types.ModuleType("app.db.database")
        database.get_db = lambda: None
        with patch.dict(sys.modules, {"app.db.database": database}):
            cls.routes = importlib.import_module("app.modules.auth.router")
            cls.security = importlib.import_module("app.modules.auth.security")
            cls.service = importlib.import_module("app.modules.auth.service")
        cls.app = FastAPI()
        cls.app.include_router(cls.routes.router, prefix="/api")

    def setUp(self):
        self.user = types.SimpleNamespace(id=uuid4(), email="test@example.com", nickname="Test",
                                         created_at=datetime.now(), updated_at=datetime.now(), is_active=True)
        self.db = Mock()
        self.db.query.return_value.filter.return_value.first.return_value = self.user
        self.app.dependency_overrides[self.routes.get_db] = lambda: self.db
        self.app.dependency_overrides[self.security.get_db] = lambda: self.db
        self.client = TestClient(self.app)
        self.settings = self.routes.settings
        self.patches = [
            patch.object(self.settings, "jwt_secret_key", "test-only-secret-" * 4),
            patch.object(self.settings, "refresh_cookie_secure", False),
            patch.object(self.settings, "auth_allowed_origins", ["http://localhost:5173"]),
        ]
        for item in self.patches:
            item.start()
        self.addCleanup(self.client.close)
        self.addCleanup(self.app.dependency_overrides.clear)
        for item in self.patches:
            self.addCleanup(item.stop)

    def login(self, google=False):
        name = "google_login_service" if google else "login_service"
        tokens = self.security.create_token_pair(self.user.id)
        with patch.object(self.routes, name, return_value=(*tokens, self.user)):
            return self.client.post("/api/auth/google-login" if google else "/api/auth/login",
                                    json={"id_token": "fake"} if google else {"email": self.user.email, "password": "password"},
                                    headers={"Origin": "http://localhost:5173"})

    def test_login_and_google_only_expose_access_token(self):
        for google in (False, True):
            response = self.login(google)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(set(response.json()), {"access_token", "token_type", "user"})
            cookie = response.headers["set-cookie"]
            for flag in ("HttpOnly", "SameSite=lax", "Path=/api/auth", "Max-Age=1209600"):
                self.assertIn(flag, cookie)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertNotIn(self.client.cookies.get("gp_refresh"), response.text)

    def test_secure_cookie_configuration(self):
        with patch.object(self.settings, "refresh_cookie_secure", True):
            self.assertIn("Secure", self.login().headers["set-cookie"])

    def test_refresh_uses_cookie_without_json_and_keeps_absolute_expiry(self):
        login = self.login()
        response = self.client.post("/api/auth/refresh")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"]["id"], str(self.user.id))
        self.assertNotIn("refresh_token", response.json())
        self.assertNotIn("set-cookie", response.headers)
        me = self.client.get("/api/auth/me", headers={"Authorization": "Bearer " + response.json()["access_token"]})
        self.assertEqual(me.status_code, 200)
        self.assertEqual(login.status_code, 200)

    def test_body_refresh_token_is_not_accepted(self):
        response = self.client.post("/api/auth/refresh", json={"refresh_token": self.security.create_refresh_token(self.user.id)})
        self.assertEqual(response.status_code, 401)

    def test_invalid_expired_and_access_tokens_rejected_as_cookie(self):
        expired = jwt.encode({"sub": str(self.user.id), "token_type": "refresh",
                              "exp": datetime.now(timezone.utc) - timedelta(seconds=10)},
                             self.settings.jwt_secret_key, algorithm=self.settings.jwt_algorithm)
        for token in ("invalid", expired, self.security.create_access_token(self.user.id)):
            response = self.client.post("/api/auth/refresh", headers={"Cookie": f"gp_refresh={token}"})
            self.assertEqual(response.status_code, 401)
            self.assertNotIn("set-cookie", response.headers)

    def test_refresh_cookie_does_not_authorize_protected_route(self):
        self.login()
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)
        token = self.client.cookies.get("gp_refresh")
        self.assertEqual(self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code, 401)

    def test_logout_deletes_cookie_and_next_restore_fails(self):
        self.login()
        response = self.client.post("/api/auth/logout")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        self.assertIn("Path=/api/auth", response.headers["set-cookie"])
        self.assertIn("Max-Age=0", response.headers["set-cookie"])
        self.assertEqual(self.client.post("/api/auth/refresh").status_code, 401)
        self.assertEqual(self.client.post("/api/auth/logout").status_code, 204)

    def test_cross_origin_csrf_is_rejected_including_login_and_logout(self):
        for path in ("login", "google-login", "refresh", "logout"):
            response = self.client.post(f"/api/auth/{path}", json={"email": "a@b.com", "password": "password", "id_token": "fake"},
                                        headers={"Origin": "https://attacker.example"})
            self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.post("/api/auth/logout", headers={"Sec-Fetch-Site": "cross-site"}).status_code, 403)

    def test_deleted_user_cannot_refresh(self):
        self.login()
        self.db.query.return_value.filter.return_value.first.return_value = None
        self.assertEqual(self.client.post("/api/auth/refresh").status_code, 401)

    def test_legacy_session_endpoint_removed(self):
        self.assertEqual(self.client.post("/api/auth/session").status_code, 404)


if __name__ == "__main__":
    unittest.main()
