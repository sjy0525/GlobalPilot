"""Auth HTTP boundaries with real schemas/JWT and mocked repositories/network."""
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
from google.auth.exceptions import GoogleAuthError, TransportError
from google.oauth2 import id_token as google_id_token
import sqlalchemy.orm  # Load ORM once before suites replace sys.modules entries.
from sqlalchemy.exc import IntegrityError

from app.modules.users.models import User  # Keep ORM registration outside sys.modules stubs.


class ConstraintViolation(Exception):
    def __init__(self, constraint, sqlstate="23505", *, driver="psycopg2"):
        super().__init__("mock database constraint failure")
        self.diag = types.SimpleNamespace(constraint_name=constraint)
        if driver == "psycopg2":
            self.pgcode = sqlstate
        else:
            self.sqlstate = sqlstate


def integrity_error(constraint="users_email_key", sqlstate="23505", **kwargs):
    return IntegrityError("INSERT INTO users", {}, ConstraintViolation(constraint, sqlstate, **kwargs))


class AuthStatusCodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Do not import app.main or create/connect a database engine.
        database = types.ModuleType("app.db.database")
        database.get_db = lambda: None
        with patch.dict(sys.modules, {"app.db.database": database}):
            cls.routes = importlib.import_module("app.modules.auth.router")
            cls.users = importlib.import_module("app.modules.users.router")
            cls.security = importlib.import_module("app.modules.auth.security")
            cls.service = importlib.import_module("app.modules.auth.service")
            cls.user_service = importlib.import_module("app.modules.users.service")
            cls.errors = importlib.import_module("app.core.http_errors")
        cls.app = FastAPI()
        cls.errors.install_exception_handlers(cls.app)
        cls.app.include_router(cls.routes.router, prefix="/api")
        cls.app.include_router(cls.users.router, prefix="/api")

    def setUp(self):
        self.user = types.SimpleNamespace(
            id=uuid4(), email="test@example.com", nickname="Test", google_sub="google-sub",
            password_hash="mock-password-hash", created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc), is_active=True,
        )
        self.db = Mock()
        self.db.query.return_value.filter.return_value.first.return_value = self.user
        self.app.dependency_overrides[self.routes.get_db] = lambda: self.db
        self.app.dependency_overrides[self.security.get_db] = lambda: self.db
        self.client = TestClient(self.app, raise_server_exceptions=False)
        secret = patch.object(self.routes.settings, "jwt_secret_key", "test-only-secret-" * 4)
        secret.start()
        self.addCleanup(secret.stop)
        self.addCleanup(self.client.close)
        self.addCleanup(self.app.dependency_overrides.clear)

    def test_password_byte_limit_rejects_ascii_and_multibyte_before_service(self):
        for path, service_name, module in (
            ("/api/auth/login", "login_service", self.routes),
            ("/api/register", "register_user", self.users),
        ):
            for password in ("a" * 73, "界" * 25):
                with self.subTest(path=path, byte_count=len(password.encode("utf-8"))):
                    with patch.object(module, service_name) as service:
                        response = self.client.post(path, json={"email": self.user.email, "password": password})
                    self.assertEqual(response.status_code, 422)
                    service.assert_not_called()
                    self.assertNotIn(password, response.text)

    def test_password_exact_byte_limit_is_accepted_and_registration_returns_201(self):
        tokens = self.security.create_token_pair(self.user.id)
        for password in ("a" * 72, "界" * 24):
            with self.subTest(password_length=len(password)):
                with patch.object(self.routes, "login_service", return_value=(*tokens, self.user)):
                    login = self.client.post("/api/auth/login", json={"email": self.user.email, "password": password})
                self.assertEqual(login.status_code, 200)
                with patch.object(self.users, "register_user", return_value=self.user):
                    registered = self.client.post("/api/register", json={"email": self.user.email, "password": password})
                self.assertEqual(registered.status_code, 201)
                self.assertEqual(registered.json()["id"], str(self.user.id))

    def test_password_minimum_is_preserved(self):
        for path in ("/api/register", "/api/auth/login"):
            response = self.client.post(path, json={"email": self.user.email, "password": "short"})
            self.assertEqual(response.status_code, 422)

    def test_disabled_password_account_cannot_receive_tokens(self):
        self.user.is_active = False
        with patch.object(self.service, "verify_password", return_value=True), \
             patch.object(self.service, "create_token_pair") as issue_tokens:
            response = self.client.post("/api/auth/login", json={
                "email": self.user.email, "password": "password",
            })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "ACCOUNT_DISABLED")
        self.assertNotIn("access_token", response.json())
        self.assertNotIn("set-cookie", response.headers)
        self.assertNotIn("www-authenticate", response.headers)
        issue_tokens.assert_not_called()

    def test_missing_account_and_incorrect_password_remain_401(self):
        for exists in (False, True):
            with self.subTest(account_exists=exists):
                self.db.query.return_value.filter.return_value.first.return_value = self.user if exists else None
                with patch.object(self.service, "verify_password", return_value=False), \
                     patch.object(self.service, "create_token_pair") as issue_tokens:
                    response = self.client.post("/api/auth/login", json={
                        "email": self.user.email, "password": "password",
                    })
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["code"], "INVALID_CREDENTIALS")
                self.assertEqual(response.headers["www-authenticate"], "Bearer")
                issue_tokens.assert_not_called()

    def test_active_password_login_still_issues_access_and_refresh_tokens(self):
        with patch.object(self.service, "verify_password", return_value=True):
            response = self.client.post("/api/auth/login", json={
                "email": self.user.email, "password": "password",
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.security.decode_token(response.json()["access_token"], "access"), str(self.user.id),
        )
        self.assertEqual(
            self.security.decode_token(self.client.cookies.get("gp_refresh"), "refresh"), str(self.user.id),
        )

    def test_disabled_google_account_cannot_receive_tokens(self):
        self.user.is_active = False
        profile = {"sub": self.user.google_sub, "email": self.user.email}
        with patch.object(self.service, "verify_google_id_token", return_value=profile), \
             patch.object(self.service, "create_token_pair") as issue_tokens:
            response = self.client.post("/api/auth/google-login", json={"id_token": "placeholder"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "ACCOUNT_DISABLED")
        self.assertNotIn("access_token", response.json())
        self.assertNotIn("set-cookie", response.headers)
        issue_tokens.assert_not_called()

    def test_disabled_google_email_account_is_not_linked(self):
        self.user.is_active = False
        self.user.google_sub = None
        profile = {"sub": "new-google-sub", "email": self.user.email}
        with patch.object(self.service, "verify_google_id_token", return_value=profile), \
             patch.object(self.service, "get_user_by_google_sub", return_value=None), \
             patch.object(self.service, "get_user_by_email", return_value=self.user), \
             patch.object(self.service, "create_token_pair") as issue_tokens:
            response = self.client.post("/api/auth/google-login", json={"id_token": "placeholder"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "ACCOUNT_DISABLED")
        self.assertIsNone(self.user.google_sub)
        self.db.execute.assert_not_called()
        self.db.commit.assert_not_called()
        self.db.refresh.assert_not_called()
        self.assertNotIn("set-cookie", response.headers)
        issue_tokens.assert_not_called()

    def test_google_creation_race_does_not_link_disabled_email_account(self):
        self.user.is_active = False
        self.user.google_sub = None
        self.db.commit.side_effect = integrity_error()
        profile = {"sub": "new-google-sub", "email": self.user.email}
        with patch.object(self.service, "verify_google_id_token", return_value=profile), \
             patch.object(self.service, "get_user_by_google_sub", return_value=None), \
             patch.object(self.service, "get_user_by_email", side_effect=[None, self.user]), \
             patch.object(self.service, "create_token_pair") as issue_tokens:
            response = self.client.post("/api/auth/google-login", json={"id_token": "placeholder"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "ACCOUNT_DISABLED")
        self.assertIsNone(self.user.google_sub)
        self.db.execute.assert_not_called()
        self.db.commit.assert_called_once()
        self.db.rollback.assert_called_once()
        self.db.refresh.assert_not_called()
        issue_tokens.assert_not_called()

    def test_active_google_login_still_issues_access_and_refresh_tokens(self):
        profile = {"sub": self.user.google_sub, "email": self.user.email}
        with patch.object(self.service, "verify_google_id_token", return_value=profile):
            response = self.client.post("/api/auth/google-login", json={"id_token": "placeholder"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.security.decode_token(response.json()["access_token"], "access"), str(self.user.id),
        )
        self.assertEqual(
            self.security.decode_token(self.client.cookies.get("gp_refresh"), "refresh"), str(self.user.id),
        )

    def test_existing_access_token_observes_current_account_state(self):
        headers = {"Authorization": f"Bearer {self.security.create_access_token(self.user.id)}"}
        self.assertEqual(self.client.get("/api/auth/me", headers=headers).status_code, 200)
        self.user.is_active = False
        disabled = self.client.get("/api/auth/me", headers=headers)
        self.assertEqual(disabled.status_code, 403)
        self.assertEqual(disabled.json()["code"], "ACCOUNT_DISABLED")
        self.user.is_active = True
        self.assertEqual(self.client.get("/api/auth/me", headers=headers).status_code, 200)

    def test_disabled_account_cannot_refresh_and_cookie_is_preserved(self):
        refresh_token = self.security.create_refresh_token(self.user.id)
        self.client.cookies.set("gp_refresh", refresh_token)
        self.user.is_active = False
        with patch.object(self.service, "create_access_token") as issue_token:
            response = self.client.post("/api/auth/refresh")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "ACCOUNT_DISABLED")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["pragma"], "no-cache")
        self.assertNotIn("access_token", response.json())
        self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(self.client.cookies.get("gp_refresh"), refresh_token)
        issue_token.assert_not_called()

    def test_password_unpaired_surrogate_is_422_without_echoing_input(self):
        body = b'{"email":"test@example.com","password":"\\ud800\\ud800\\ud800\\ud800\\ud800\\ud800"}'
        for path in ("/api/register", "/api/auth/login"):
            response = self.client.post(path, content=body, headers={"Content-Type": "application/json"})
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()["code"], "VALIDATION_ERROR")
            self.assertIn(response.json()["detail"][0]["type"], {"string_unicode", "password_invalid_encoding"})
            self.assertNotIn("input", response.json()["detail"][0])

    def test_invalid_google_credentials_are_401_with_bearer_challenge(self):
        for error in (ValueError("invalid signature"), GoogleAuthError("wrong issuer")):
            with self.subTest(error_type=type(error).__name__):
                with patch.object(self.service, "verify_google_id_token", side_effect=error):
                    response = self.client.post("/api/auth/google-login", json={"id_token": "placeholder"})
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["www-authenticate"], "Bearer")
                self.assertEqual(response.json()["code"], "INVALID_GOOGLE_TOKEN")
        self.db.query.assert_not_called()

    def test_real_google_issuer_error_is_translated_to_401(self):
        # The real Google verifier performs its issuer check; no certificate fetch.
        with patch.object(google_id_token, "verify_token", return_value={"iss": "wrong-issuer"}):
            response = self.client.post("/api/auth/google-login", json={"id_token": "placeholder"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "INVALID_GOOGLE_TOKEN")

    def test_google_transport_failure_remains_an_upstream_error(self):
        error = TransportError("mock certificate outage")
        with patch.object(self.service, "verify_google_id_token", side_effect=error):
            with self.assertRaises(TransportError) as caught:
                self.service.login_with_google(self.db, "placeholder")
        self.assertIs(caught.exception, error)
        self.db.query.assert_not_called()
        with patch.object(self.service, "verify_google_id_token", side_effect=error), \
             self.assertLogs(self.errors.logger, level="ERROR"):
            response = self.client.post("/api/auth/google-login", json={"id_token": "placeholder"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "SERVICE_UNAVAILABLE")
        self.assertNotIn("www-authenticate", response.headers)
        self.assertNotIn("mock certificate outage", response.text)

    def test_protected_route_401s_always_include_bearer_challenge(self):
        expired = jwt.encode(
            {"sub": str(self.user.id), "token_type": "access",
             "exp": datetime.now(timezone.utc) - timedelta(seconds=10)},
            self.routes.settings.jwt_secret_key, algorithm=self.routes.settings.jwt_algorithm,
        )
        missing_sub = jwt.encode(
            {"token_type": "access", "exp": datetime.now(timezone.utc) + timedelta(minutes=1)},
            self.routes.settings.jwt_secret_key, algorithm=self.routes.settings.jwt_algorithm,
        )
        for authorization in (None, "Basic invalid", "Bearer invalid", f"Bearer {expired}",
                              f"Bearer {missing_sub}", f"Bearer {self.security.create_refresh_token(self.user.id)}"):
            with self.subTest(authorization_kind=(authorization or "missing").split(" ")[0]):
                headers = {"Authorization": authorization} if authorization else {}
                response = self.client.get("/api/auth/me", headers=headers)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["www-authenticate"], "Bearer")
        self.db.query.return_value.filter.return_value.first.return_value = None
        response = self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {self.security.create_access_token(self.user.id)}"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["www-authenticate"], "Bearer")

    def test_refresh_preserves_error_code_challenge_and_private_cache_headers(self):
        for cookie in (None, "gp_refresh=invalid"):
            with self.subTest(cookie=bool(cookie)):
                response = self.client.post("/api/auth/refresh", headers={"Cookie": cookie} if cookie else {})
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["www-authenticate"], "Bearer")
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(response.headers["pragma"], "no-cache")
                self.assertEqual(response.json()["code"], "INVALID_TOKEN" if cookie else "AUTHENTICATION_REQUIRED")
                self.assertNotIn("set-cookie", response.headers)

    def test_register_email_race_rolls_back_and_returns_409_for_both_postgres_drivers(self):
        self.db.query.return_value.filter.return_value.first.return_value = None
        for driver in ("psycopg2", "psycopg"):
            with self.subTest(driver=driver):
                self.db.reset_mock()
                self.db.commit.side_effect = integrity_error(driver=driver)
                with patch.object(self.user_service, "hash_password", return_value="mock-hash"):
                    response = self.client.post("/api/register", json={"email": self.user.email, "password": "password"})
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()["code"], "EMAIL_ALREADY_REGISTERED")
                self.db.rollback.assert_called_once()
                self.db.refresh.assert_not_called()

    def test_registration_does_not_mask_unrelated_integrity_errors(self):
        self.db.query.return_value.filter.return_value.first.return_value = None
        for constraint, sqlstate in (("users_pkey", "23505"), ("users_email_key", "23502"),
                                    ("users_google_sub_key", "23505"), (None, "23505")):
            with self.subTest(constraint=constraint, sqlstate=sqlstate):
                self.db.reset_mock()
                error = integrity_error(constraint, sqlstate)
                self.db.commit.side_effect = error
                with patch.object(self.user_service, "hash_password", return_value="mock-hash"):
                    with self.assertRaises(IntegrityError) as caught:
                        self.user_service.register_user(self.db, self.user.email, "password")
                self.assertIs(caught.exception, error)
                self.db.rollback.assert_called_once()

    def test_google_same_identity_creation_race_recovers_existing_user(self):
        for constraint in ("users_email_key", "users_google_sub_key"):
            with self.subTest(constraint=constraint):
                self.db.reset_mock()
                self.db.commit.side_effect = integrity_error(constraint)
                with patch.object(self.service, "get_user_by_google_sub", side_effect=[None, self.user]), \
                     patch.object(self.service, "get_user_by_email", return_value=None):
                    user = self.service.get_or_create_google_user(self.db, {"sub": "google-sub", "email": self.user.email})
                self.assertIs(user, self.user)
                self.db.rollback.assert_called_once()
                self.db.commit.assert_called_once()

    def test_google_email_race_with_other_identity_is_409(self):
        self.db.commit.side_effect = integrity_error()
        conflicting = types.SimpleNamespace(google_sub="different-google-account")
        with patch.object(self.service, "get_user_by_google_sub", return_value=None), \
             patch.object(self.service, "get_user_by_email", side_effect=[None, conflicting]):
            with self.assertRaises(self.errors.AppHTTPException) as caught:
                self.service.get_or_create_google_user(self.db, {"sub": "google-sub", "email": self.user.email})
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.code, "GOOGLE_ACCOUNT_CONFLICT")
        self.db.rollback.assert_called_once()

    def test_google_email_race_can_link_concurrently_registered_password_account(self):
        self.db.commit.side_effect = [integrity_error(), None]
        self.user.google_sub = None
        self.db.refresh.side_effect = lambda user: setattr(user, "google_sub", "google-sub")
        with patch.object(self.service, "get_user_by_google_sub", return_value=None), \
             patch.object(self.service, "get_user_by_email", side_effect=[None, self.user]):
            user = self.service.get_or_create_google_user(self.db, {"sub": "google-sub", "email": self.user.email})
        self.assertIs(user, self.user)
        self.assertEqual(user.google_sub, "google-sub")
        self.assertEqual(self.db.commit.call_count, 2)
        self.db.rollback.assert_called_once()
        self.db.refresh.assert_called_once_with(user)

    def test_google_link_recovery_is_bounded(self):
        self.db.commit.side_effect = integrity_error()
        unlinked = types.SimpleNamespace(id=uuid4(), google_sub=None, nickname="Test", is_active=True)
        still_unlinked = types.SimpleNamespace(id=unlinked.id, google_sub=None, nickname="Test", is_active=True)
        with patch.object(self.service, "get_user_by_google_sub", return_value=None), \
             patch.object(self.service, "get_user_by_email", side_effect=[None, unlinked, still_unlinked]):
            with self.assertRaises(self.errors.AppHTTPException) as caught:
                self.service.get_or_create_google_user(self.db, {"sub": "google-sub", "email": self.user.email})
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.db.commit.call_count, 2)
        self.assertEqual(self.db.rollback.call_count, 2)

    def test_google_does_not_mask_other_database_integrity_failures(self):
        error = integrity_error("users_pkey")
        self.db.commit.side_effect = error
        with patch.object(self.service, "get_user_by_google_sub", return_value=None) as lookup, \
             patch.object(self.service, "get_user_by_email", return_value=None):
            with self.assertRaises(IntegrityError) as caught:
                self.service.get_or_create_google_user(self.db, {"sub": "google-sub", "email": self.user.email})
        self.assertIs(caught.exception, error)
        self.db.rollback.assert_called_once()
        lookup.assert_called_once()


if __name__ == "__main__":
    unittest.main()
