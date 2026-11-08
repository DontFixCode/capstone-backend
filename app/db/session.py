"""애플리케이션 DB 연결과 트랜잭션 수명을 관리한다.

드라이버는 기존 의존성인 psycopg 3를 쓰고 ORM은 도입하지 않는다.

현재 대상 DB는 서버의 bus-transfer-supabase 스택이다. 이 스택에는 Supavisor 풀러가 없고 DB 포트를 외부에 열지
않으므로, SSH 터널로 DB 컨테이너(bus-transfer-supabase-db-1:5432)에 직접 접속한다.
- 접속 위치: .env.api의 BACKEND_DB_HOST·BACKEND_DB_PORT(터널의 로컬 주소). 필수다.
- 계정: BACKEND_DB_USER, 비어 있으면 Supabase 기본 계정 postgres.
- DB 이름·비밀번호: 저장소 루트 .env(서버 배포 .env와 같은 값)의 POSTGRES_DB·POSTGRES_PASSWORD.
루트 .env의 POSTGRES_HOST(=db, 컨테이너 내부 이름)·POOLER_TENANT_ID는 이 경로에서 쓰지 않는다.
비밀번호·접속 문자열은 로그·예외에 남기지 않는다.

풀러를 다시 도입해도 동작하도록 prepared statement를 쓰지 않고, 잠금은 트랜잭션 단위(pg_advisory_xact_lock)만 쓴다.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg
from dotenv import dotenv_values

if TYPE_CHECKING:
    from app.core.config import Settings

SUPABASE_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
DEFAULT_DB_USER = "postgres"


class DbConfigError(RuntimeError):
    """DB 접속에 필요한 설정이 없다. 메시지에는 설정 이름만 담는다."""


class DbError(RuntimeError):
    """DB 연결·실행 오류. 메시지에서 비밀번호·호스트·계정을 가린다."""


@dataclass(frozen=True)
class DbParams:
    host: str
    port: int
    dbname: str
    user: str
    password: str = field(repr=False)
    connect_timeout: float = 10.0

    def describe(self) -> dict[str, Any]:
        """설정 출처 확인용. 호스트·계정·비밀번호는 존재 여부만 반환한다."""
        return {"host": "set", "port": self.port, "dbname": self.dbname, "user": "set", "password": "set"}


def load_params(settings: Settings, env_file: Path | None = None) -> DbParams:
    """.env.api의 접속 위치와 루트 .env의 DB 이름·비밀번호로 접속 정보를 만든다. 없으면 누락된 설정 이름을 알려준다."""
    env = {k: (v or "").strip() for k, v in dotenv_values(env_file or SUPABASE_ENV_FILE).items()}
    missing: list[str] = []
    if not settings.db_host:
        missing.append(".env.api BACKEND_DB_HOST")
    if not settings.db_port:
        missing.append(".env.api BACKEND_DB_PORT")
    for name in ("POSTGRES_DB", "POSTGRES_PASSWORD"):
        if not env.get(name):
            missing.append(f".env {name}")
    if missing:
        raise DbConfigError("DB 접속 설정이 없습니다: " + ", ".join(missing))
    return DbParams(
        host=settings.db_host,
        port=settings.db_port,
        dbname=env["POSTGRES_DB"],
        user=settings.db_user or DEFAULT_DB_USER,
        password=env["POSTGRES_PASSWORD"],
        connect_timeout=settings.db_connect_timeout_seconds,
    )


class Database:
    """연결 생성기. 연결마다 UTC 시간대를 쓰고 prepared statement를 끈다."""

    def __init__(self, params: DbParams) -> None:
        self.params = params
        self._secrets = tuple(s for s in (params.password, params.host, params.user) if s)

    @classmethod
    def from_settings(cls, settings: Settings) -> Database:
        return cls(load_params(settings))

    def redact(self, text: str) -> str:
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, "<redacted>")
        return text

    def connect(self, *, autocommit: bool = False) -> psycopg.Connection:
        p = self.params
        try:
            conn = psycopg.connect(
                host=p.host, port=p.port, dbname=p.dbname, user=p.user, password=p.password,
                connect_timeout=max(1, int(p.connect_timeout)), autocommit=autocommit, prepare_threshold=None,
                application_name="capstone-backend",
            )
        except psycopg.Error as exc:
            raise DbError(f"DB 연결 실패 ({type(exc).__name__}): {self.redact(str(exc))}") from None
        conn.execute("SET TIME ZONE 'UTC'")
        if not autocommit:
            conn.commit()
        return conn

    @contextmanager
    def transaction(self, *, isolation: str | None = None) -> Iterator[psycopg.Connection]:
        """한 트랜잭션을 연다. 예외가 나면 되돌리고, 정상 종료하면 커밋한 뒤 연결을 닫는다."""
        conn = self.connect()
        try:
            if isolation:
                conn.execute(f"SET TRANSACTION ISOLATION LEVEL {isolation}")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


def advisory_key(name: str) -> int:
    """잠금 이름을 PostgreSQL bigint 범위의 안정적인 키로 바꾼다(프로세스·실행 간 동일)."""
    return int.from_bytes(hashlib.sha256(name.encode("utf-8")).digest()[:8], "big", signed=True)


def try_xact_lock(conn: psycopg.Connection, name: str) -> bool:
    """현재 트랜잭션이 끝날 때까지 유지되는 잠금을 기다리지 않고 시도한다."""
    row = conn.execute("SELECT pg_try_advisory_xact_lock(%s)", (advisory_key(name),)).fetchone()
    return bool(row and row[0])
