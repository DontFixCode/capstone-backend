"""설정 파일 경로·우선순위와 인증키 없는 앱 실행을 검증한다. 실제 .env.api는 읽거나 바꾸지 않는다."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from app.core.config import Settings

ROOT = Path(__file__).resolve().parents[2]
CLEARED = ("TAGO_SERVICE_KEY", "BACKEND_LOG_LEVEL", "BACKEND_TAGO_TIMEOUT_SECONDS", "BACKEND_TAGO_REQUEST_INTERVAL_SECONDS")


def clean_environ():
    return {name: value for name, value in os.environ.items() if name.upper() not in CLEARED}


class SettingsTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, clean_environ(), clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.env_file = Path(directory.name) / ".env.api"

    def test_env_file_is_fixed_to_repository_root(self):
        self.assertEqual(Path(Settings.model_config["env_file"]), ROOT / ".env.api")

    def test_precedence_is_process_env_then_env_api_then_default(self):
        self.env_file.write_text(
            "TAGO_SERVICE_KEY=from-file\nBACKEND_TAGO_TIMEOUT_SECONDS=7\nBACKEND_LOG_LEVEL=DEBUG\n", encoding="utf-8"
        )
        from_file = Settings(_env_file=self.env_file)
        self.assertEqual(from_file.tago_service_key.get_secret_value(), "from-file")
        self.assertEqual((from_file.tago_timeout_seconds, from_file.log_level), (7.0, "DEBUG"))
        self.assertEqual(from_file.tago_request_interval_seconds, 0.5, "파일에 없으면 기본값")

        os.environ.update({"TAGO_SERVICE_KEY": "from-env", "BACKEND_TAGO_TIMEOUT_SECONDS": "3"})
        from_env = Settings(_env_file=self.env_file)
        self.assertEqual(from_env.tago_service_key.get_secret_value(), "from-env")
        self.assertEqual((from_env.tago_timeout_seconds, from_env.log_level), (3.0, "DEBUG"))

    def test_missing_or_blank_key_is_none_and_never_in_repr(self):
        self.assertIsNone(Settings(_env_file=self.env_file).tago_service_key, "파일이 없어도 생성")
        self.env_file.write_text("TAGO_SERVICE_KEY=   \n", encoding="utf-8")
        self.assertIsNone(Settings(_env_file=self.env_file).tago_service_key)
        self.env_file.write_text("TAGO_SERVICE_KEY=visible-secret\n", encoding="utf-8")
        self.assertNotIn("visible-secret", repr(Settings(_env_file=self.env_file)))

    def test_working_directory_does_not_change_env_file(self):
        # 다른 실행 디렉터리에 둔 .env.api는 읽지 않아야 한다.
        decoy = "decoy-key-from-working-directory"
        self.env_file.write_text(f"TAGO_SERVICE_KEY={decoy}\n", encoding="utf-8")
        code = (
            "from pathlib import Path; from app.core.config import Settings; s = Settings(); "
            "print(Path(Settings.model_config['env_file']).resolve()); "
            f"print(s.tago_service_key is not None and s.tago_service_key.get_secret_value() == {decoy!r})"
        )
        environ = {**clean_environ(), "PYTHONPATH": str(ROOT)}
        output = subprocess.run(
            [sys.executable, "-c", code], cwd=self.env_file.parent, env=environ,
            capture_output=True, text=True, check=True,
        ).stdout.split()
        self.assertEqual(Path(output[0]), (ROOT / ".env.api").resolve())
        self.assertEqual(output[1], "False")


class HealthWithoutKeyTest(unittest.TestCase):
    def test_health_runs_without_tago_key(self):
        with mock.patch.dict(os.environ, clean_environ(), clear=True):
            from app.main import app

            response = TestClient(app).get("/health")
        self.assertEqual((response.status_code, response.json()), (200, {"status": "ok"}))


if __name__ == "__main__":
    unittest.main()
