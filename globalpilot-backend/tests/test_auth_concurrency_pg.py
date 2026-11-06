"""Google identity binding races on an explicitly configured disposable PostgreSQL DB.

AUTH_CONCURRENCY_TEST_DATABASE_URL must name a test_*, audit_* or refactor_* database.
Each class owns a random schema. No OAuth/provider calls or application DB access.
"""
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.core.http_errors import AppHTTPException
from app.modules.users.models import User
from app.modules.auth import service as auth_service


@unittest.skipUnless(os.environ.get("AUTH_CONCURRENCY_TEST_DATABASE_URL"), "requires disposable PostgreSQL URL")
class AuthConcurrencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = make_url(os.environ["AUTH_CONCURRENCY_TEST_DATABASE_URL"])
        if url.get_backend_name() != "postgresql" or not url.database.startswith(("test_", "audit_", "refactor_")):
            raise AssertionError("Refusing to run identity tests against a non-test database")
        cls.schema = "auth_concurrency_" + uuid4().hex
        cls.admin = create_engine(url)
        with cls.admin.begin() as connection:
            connection.execute(text(f"CREATE SCHEMA {cls.schema}"))
        cls.engine = create_engine(url, connect_args={
            "options": f"-csearch_path={cls.schema} -cstatement_timeout=10000 -clock_timeout=5000",
        })
        cls.sessions = sessionmaker(cls.engine, autoflush=False)
        User.__table__.create(cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        with cls.admin.begin() as connection:
            connection.execute(text(f"DROP SCHEMA {cls.schema} CASCADE"))
        cls.admin.dispose()

    def setUp(self):
        with self.engine.begin() as connection:
            connection.execute(User.__table__.delete())
        self.email = "identity-race@example.invalid"

    def create_unlinked(self, nickname=None):
        with self.sessions.begin() as db:
            user = User(email=self.email, password_hash="test-only", nickname=nickname)
            db.add(user)
            db.flush()
            return user.id

    def bind(self, subject):
        try:
            with self.sessions() as db:
                user = auth_service.get_or_create_google_user(db, {
                    "email": self.email, "sub": subject, "name": "Google name",
                })
                return (subject, user.id)
        except AppHTTPException as exc:
            return exc

    def final_user(self):
        with self.sessions() as db:
            users = db.scalars(select(User)).all()
            self.assertEqual(len(users), 1)
            user = users[0]
            return user.id, user.google_sub, user.nickname

    def concurrent_initial_reads(self, subjects):
        barrier = threading.Barrier(len(subjects))
        original = auth_service.get_user_by_email

        def both_read_before_writing(db, email):
            user = original(db, email)
            barrier.wait(5)
            return user

        with patch.object(auth_service, "get_user_by_email", side_effect=both_read_before_writing):
            with ThreadPoolExecutor(max_workers=len(subjects)) as workers:
                futures = [workers.submit(self.bind, subject) for subject in subjects]
                return [future.result(10) for future in futures]

    def assert_one_binding(self, outcomes):
        winners = [value for value in outcomes if isinstance(value, tuple)]
        conflicts = [value for value in outcomes if isinstance(value, AppHTTPException)]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].status_code, 409)
        self.assertEqual(conflicts[0].code, "GOOGLE_ACCOUNT_CONFLICT")
        identity, subject, _nickname = self.final_user()
        self.assertEqual(winners[0], (subject, identity))

    def test_different_subjects_cannot_overwrite_an_existing_unlinked_user(self):
        self.create_unlinked(nickname="Existing nickname")
        outcomes = self.concurrent_initial_reads(("subject-a", "subject-b"))
        self.assert_one_binding(outcomes)
        self.assertEqual(self.final_user()[2], "Existing nickname")

    def test_same_subject_can_link_concurrently_and_remains_idempotent(self):
        identity = self.create_unlinked()
        outcomes = self.concurrent_initial_reads(("same-subject", "same-subject"))
        self.assertEqual(outcomes, [("same-subject", identity)] * 2)
        self.assertEqual(self.final_user(), (identity, "same-subject", "Google name"))

    def test_same_identity_concurrent_creation_returns_one_user(self):
        outcomes = self.concurrent_initial_reads(("same-subject", "same-subject"))
        self.assertTrue(all(isinstance(value, tuple) for value in outcomes))
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual(self.final_user()[1], "same-subject")

    def test_integrity_error_retry_cannot_overwrite_a_concurrent_binding(self):
        original = auth_service.get_user_by_email
        initial_reads = threading.Barrier(3)
        retry_reads = threading.Barrier(2)
        registered = threading.Event()
        local = threading.local()

        def controlled_lookup(db, email):
            user = original(db, email)
            count = getattr(local, "lookups", 0)
            local.lookups = count + 1
            if count == 0:
                self.assertIsNone(user)
                initial_reads.wait(5)
                if not registered.wait(5):
                    raise AssertionError("Concurrent password registration was not released")
            elif count == 1:
                self.assertIsNotNone(user)
                self.assertIsNone(user.google_sub)
                retry_reads.wait(5)
            return user

        with patch.object(auth_service, "get_user_by_email", side_effect=controlled_lookup):
            with ThreadPoolExecutor(max_workers=2) as workers:
                futures = [workers.submit(self.bind, subject) for subject in ("subject-a", "subject-b")]
                try:
                    initial_reads.wait(5)
                    self.create_unlinked(nickname="Password account")
                finally:
                    registered.set()
                outcomes = [future.result(10) for future in futures]
        self.assert_one_binding(outcomes)
        self.assertEqual(self.final_user()[2], "Password account")

    def test_binding_preserves_a_nickname_committed_after_its_initial_read(self):
        identity = self.create_unlinked()
        original = auth_service.get_user_by_email

        def update_nickname_after_read(db, email):
            user = original(db, email)
            self.assertIsNone(user.nickname)
            with self.sessions.begin() as other:
                other.get(User, identity).nickname = "Newer nickname"
            return user

        with patch.object(auth_service, "get_user_by_email", side_effect=update_nickname_after_read):
            result = self.bind("subject-a")
        self.assertEqual(result, ("subject-a", identity))
        self.assertEqual(self.final_user(), (identity, "subject-a", "Newer nickname"))


if __name__ == "__main__":
    unittest.main()
