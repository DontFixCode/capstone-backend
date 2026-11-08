"""현재 대상 DB 접속 설정(app/db/session.py)과 /db-test를 DB 없이 검증한다. 실제 .env·.env.api는 읽지 않는다."""

import logging
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db import session
from app.db.session import Database, DbConfigError, DbError, DbParams, load_params

SECRET = "synthetic-db-password"


def settings(**values):
    return Settings(_env_file=None, **values)


class LoadParamsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.env_file = Path(directory.name) / ".env"
        # 이전 주제의 Supavisor 설정이 남아 있어도 쓰지 않아야 한다.
        self.env_file.write_text(
            f"POSTGRES_HOST=db\nPOSTGRES_HOST_PORT=5433\nPOOLER_TENANT_ID=old-tenant\n"
            f"POSTGRES_DB=postgres\nPOSTGRES_PASSWORD={SECRET}\n",
            encoding="utf-8",
        )

    def test_uses_tunnel_location_and_direct_postgres_account(self):
        params = load_params(settings(db_host="127.0.0.1", db_port=15432), self.env_file)
        self.assertEqual((params.host, params.port, params.dbname, params.user), ("127.0.0.1", 15432, "postgres", "postgres"))
        self.assertEqual(params.password, SECRET)
        self.assertNotIn(SECRET, repr(params))
        self.assertNotIn("127.0.0.1", str(params.describe()))

    def test_explicit_user_overrides_default(self):
        params = load_params(settings(db_host="127.0.0.1", db_port=15432, db_user="bus_transfer_api"), self.env_file)
        self.assertEqual(params.user, "bus_transfer_api")

    def test_missing_location_is_reported_without_falling_back_to_env_host(self):
        with self.assertRaises(DbConfigError) as raised:
            load_params(settings(), self.env_file)
        message = str(raised.exception)
        self.assertIn("BACKEND_DB_HOST", message)
        self.assertIn("BACKEND_DB_PORT", message)
        self.assertNotIn(SECRET, message)

    def test_missing_password_is_reported_by_name(self):
        self.env_file.write_text("POSTGRES_DB=postgres\n", encoding="utf-8")
        with self.assertRaises(DbConfigError) as raised:
            load_params(settings(db_host="127.0.0.1", db_port=15432), self.env_file)
        self.assertIn(".env POSTGRES_PASSWORD", str(raised.exception))


class FakeDb:
    def __init__(self, error=None):
        self.error = error
        self.params = DbParams("127.0.0.1", 15432, "postgres", "postgres", SECRET)

    def redact(self, text):
        return Database(self.params).redact(text)

    @contextmanager
    def transaction(self, **_):
        if self.error:
            raise self.error
        conn = mock.MagicMock()
        conn.execute.return_value.fetchone.return_value = (1,)
        yield conn


class DbTestEndpointTest(unittest.TestCase):
    def setUp(self):
        from app.main import app

        self.client = TestClient(app)

    def test_ok_uses_current_db_layer(self):
        with mock.patch.object(session.Database, "from_settings", return_value=FakeDb()):
            response = self.client.get("/db-test")
        self.assertEqual((response.status_code, response.json()), (200, {"status": "ok", "result": 1}))

    def test_missing_settings_is_503(self):
        with mock.patch.object(session.Database, "from_settings", side_effect=DbConfigError("x")):
            response = self.client.get("/db-test")
        self.assertEqual((response.status_code, response.json()["detail"]), (503, "DB connection settings are missing."))

    def test_connection_failure_is_503_and_log_is_redacted(self):
        failing = FakeDb(DbError(f"connection to 127.0.0.1 failed password={SECRET}"))
        with mock.patch.object(session.Database, "from_settings", return_value=failing), \
                self.assertLogs("app.main", level=logging.ERROR) as logs:
            response = self.client.get("/db-test")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(SECRET, "\n".join(logs.output))
        self.assertNotIn(SECRET, response.text)


if __name__ == "__main__":
    unittest.main()
