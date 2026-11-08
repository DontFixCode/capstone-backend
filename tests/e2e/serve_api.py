"""E2E용 API 서버 프로세스: 실제 app.main 앱을 uvicorn으로 띄우되 DB·토큰 검증만 E2E 임시 환경으로 주입한다.

환경변수: E2E_DSN(임시 DB 접속 문자열), E2E_JWT_SECRET·E2E_JWT_ISSUER(시험용 토큰 검증값), E2E_PORT.
API 서버는 TAGO·TMAP을 호출하지 않는다(검색 계산·수집은 워커 프로세스).
"""

import os
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "e2e"))

import uvicorn  # noqa: E402

from app import main  # noqa: E402
from app.core.security import TokenVerifier  # noqa: E402
from app.db.session import Database, DbParams  # noqa: E402
from e2e_settings import e2e_settings  # noqa: E402

url = urlparse(os.environ["E2E_DSN"])
main.app.state.settings = e2e_settings()
main.app.state.db = Database(DbParams(url.hostname, url.port or 5432, url.path.lstrip("/"), url.username,
                                      url.password or "", 10))
main.app.state.token_verifier = TokenVerifier(os.environ["E2E_JWT_SECRET"], issuer=os.environ["E2E_JWT_ISSUER"],
                                              audience="authenticated")
uvicorn.run(main.app, host="127.0.0.1", port=int(os.environ["E2E_PORT"]), log_level="warning")
