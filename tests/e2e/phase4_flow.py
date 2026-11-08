"""Phase 4 E2E: API 서버·워커를 별도 프로세스로 띄우고 앱 흐름을 HTTP·WebSocket으로 재현한다.

검색 접수 → 후보 조회 → 이동 시작 → (스트림) 수동 탑승 → 연결 끊김 중 하차·위치 → 재접속 복구 → 도착.

    $env:TEST_POSTGIS_DSN = "postgresql://postgres:...@127.0.0.1:55437/postgres"
    uv run python tests/e2e/phase4_flow.py              # TAGO·TMAP 합성
    uv run python tests/e2e/phase4_flow.py --real-tmap  # 도보만 실제 TMAP(.env.api 앱키)

TEST_POSTGIS_DSN 서버에 임시 DB를 만들고 끝나면 지운다. 토큰은 시험용 비밀값으로 서명한 HS256 JWT이며 출력하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import jwt
import psycopg
from websockets.sync.client import connect as ws_connect

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

import phase3_world as w  # noqa: E402
from tago_world import FAKE_KEY, FakeTago  # noqa: E402

from app.db.session import Database, DbParams  # noqa: E402
from app.integrations.bus_api import TagoClient  # noqa: E402
from app.modules.transit.service import StaticImportService  # noqa: E402

ISSUER = "http://auth.e2e.local/auth/v1"
UTC = timezone.utc


def log(step: str, **data) -> None:
    print(json.dumps({"step": step, **data}, ensure_ascii=False, default=str), flush=True)


def ddl(path: Path) -> str:
    return "\n".join(line for line in path.read_text(encoding="utf-8").splitlines() if not line.startswith("\\"))


def prepare_db(admin_dsn: str) -> tuple[str, str, str]:
    name = f"phase4_e2e_{uuid4().hex[:10]}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    dsn = admin_dsn.rsplit("/", 1)[0] + f"/{name}"
    user = str(uuid4())
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("CREATE SCHEMA extensions")
        conn.execute("CREATE EXTENSION postgis SCHEMA extensions")
        conn.execute("CREATE SCHEMA auth")
        conn.execute("CREATE TABLE auth.users (id uuid PRIMARY KEY)")
        for path in sorted((ROOT / "migrations").glob("[0-9]*.sql")):
            conn.execute(ddl(path))
        conn.execute("ALTER TABLE app.route_searches ADD CONSTRAINT route_searches_journey_id_fkey "
                     "FOREIGN KEY (journey_id) REFERENCES app.journeys(id) ON DELETE CASCADE")
        conn.execute("INSERT INTO auth.users (id) VALUES (%s)", (user,))
    url = urlparse(dsn)
    db = Database(DbParams(url.hostname, url.port or 5432, name, url.username, url.password or "", 10))
    tago = FakeTago(w.world())
    report = StaticImportService(db, lambda **kw: TagoClient(FAKE_KEY, transport=tago.transport(), request_interval=0,
                                                             retry_backoff=0, sleep=lambda _s: None, max_retries=0,
                                                             **kw), rows=100).run()
    if report.status != "published":
        raise SystemExit(f"정적 적재 실패: {report.problems}")
    return name, dsn, user


def wait_until(predicate, timeout: float, interval: float = 0.3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise TimeoutError("시간 안에 조건을 만족하지 못함")


def receive_until(ws, predicate, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("WebSocket에서 기대한 메시지를 받지 못함")
        message = json.loads(ws.recv(timeout=remaining))
        if predicate(message):
            return message


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--real-tmap", action="store_true", help="도보 조회만 실제 TMAP(.env.api SK_OPEN_API_KEY)")
    parser.add_argument("--port", type=int, default=18765)
    args = parser.parse_args()
    admin_dsn = os.environ.get("TEST_POSTGIS_DSN")
    if not admin_dsn:
        raise SystemExit("TEST_POSTGIS_DSN이 필요합니다.")
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="backslashreplace")

    name, dsn, user = prepare_db(admin_dsn)
    secret = secrets.token_urlsafe(48)
    env = {**os.environ, "E2E_DSN": dsn, "E2E_JWT_SECRET": secret, "E2E_JWT_ISSUER": ISSUER,
           "E2E_PORT": str(args.port), "E2E_REAL_TMAP": "1" if args.real_tmap else "0", "PYTHONUTF8": "1"}
    logs = Path(os.environ.get("E2E_LOG_DIR") or os.environ.get("TEMP", ".")) / f"{name}"
    logs.mkdir(exist_ok=True)
    api = subprocess.Popen([sys.executable, str(ROOT / "tests/e2e/serve_api.py")], env=env, cwd=ROOT,
                           stdout=open(logs / "api.log", "w"), stderr=subprocess.STDOUT)
    worker = subprocess.Popen([sys.executable, str(ROOT / "tests/e2e/run_worker.py")], env=env, cwd=ROOT,
                              stdout=open(logs / "worker.log", "w"), stderr=subprocess.STDOUT)
    log("processes", api_pid=api.pid, worker_pid=worker.pid, separate_processes=api.pid != worker.pid,
        real_tmap=args.real_tmap)
    base = f"http://127.0.0.1:{args.port}"
    token = jwt.encode({"sub": user, "aud": "authenticated", "iss": ISSUER, "role": "authenticated",
                        "exp": int(time.time()) + 3600}, secret, algorithm="HS256")
    auth = {"Authorization": f"Bearer {token}"}
    ok = False
    try:
        http = httpx.Client(base_url=base, timeout=30)
        wait_until(lambda: _alive(http), 30)
        me = http.get("/api/v1/me", headers=auth)
        log("GET /me", status=me.status_code, displayName=me.json().get("displayName"))

        body = {"origin": {"latitude": w.ORIGIN[0], "longitude": w.ORIGIN[1], "name": "E2E 출발지"},
                "destination": {"latitude": w.DESTINATION[0], "longitude": w.DESTINATION[1], "name": "E2E 도착지"}}
        accepted = http.post("/api/v1/route-searches", headers=auth, json=body)
        search_id = accepted.json()["searchId"]
        log("POST /route-searches", status=accepted.status_code, searchStatus=accepted.json()["status"])
        started = time.monotonic()
        result = wait_until(lambda: (r := http.get(f"/api/v1/route-searches/{search_id}", headers=auth).json())
                            ["status"] != "processing" and r, 120)
        ranked = [c for c in result["candidates"] if c["rank"] is not None]
        walks = [leg for c in result["candidates"] for leg in c["legs"] if leg["kind"] == "walk"]
        log("GET /route-searches/{id}", status=result["status"], rankingMode=result["rankingMode"],
            seconds=round(time.monotonic() - started, 1), candidates=len(result["candidates"]), ranked=len(ranked),
            walkStatuses=sorted({leg["status"] for leg in walks}),
            walkProviders=sorted({leg["provider"] for leg in walks if leg["provider"]}),
            walkSamples=sorted({(leg["distanceM"], leg["durationSeconds"]) for leg in walks
                                if leg["status"] == "available"}),
            busGeometry=sorted({leg["geometryStatus"] for c in result["candidates"] for leg in c["legs"]
                                if leg["kind"] == "bus"}))
        chosen = next((c for c in ranked if c["boardingCount"] > 1), ranked[0])
        start = http.post("/api/v1/journeys", headers=auth,
                          json={"searchId": search_id, "candidateId": chosen["candidateId"]})
        journey = start.json()
        jid = journey["journeyId"]
        bus_legs = [leg["legIndex"] for leg in journey["selectedRoute"]["legs"] if leg["kind"] == "bus"]
        log("POST /journeys", status=start.status_code, rank=chosen["rank"], boardings=chosen["boardingCount"],
            stateVersion=journey["stateVersion"], movementState=journey["movementState"])

        ws_url = f"ws://127.0.0.1:{args.port}/api/v1/journeys/{jid}/stream"
        with ws_connect(ws_url, additional_headers=auth) as ws:
            first = json.loads(ws.recv(timeout=10))
            log("WS connect", type=first["type"], stateVersion=first["stateVersion"],
                resultVersion=first["resultVersion"], guidanceStatus=first["guidanceStatus"])
            current = receive_until(ws, lambda m: m["guidanceStatus"] == "current")
            nb = current["guidance"]["nextBoarding"]
            log("WS guidance (worker 계산)", resultVersion=current["resultVersion"],
                baseStateVersion=current["baseStateVersion"], waitSeconds=nb["waitSeconds"],
                predictedArrivalAt=nb["predictedArrivalAt"], reasonCodes=current["guidance"]["reasonCodes"])
            board = http.post(f"/api/v1/journeys/{jid}/events", headers={**auth, "Idempotency-Key": "e2e-board-1"},
                              json={"eventType": "board", "stateVersion": 1,
                                    "payload": {"legIndex": bus_legs[0], "vehicleId": None}})
            replay = http.post(f"/api/v1/journeys/{jid}/events", headers={**auth, "Idempotency-Key": "e2e-board-1"},
                               json={"eventType": "board", "stateVersion": 1,
                                     "payload": {"legIndex": bus_legs[0], "vehicleId": None}})
            log("POST events board", status=board.status_code, stateVersionAfter=board.json()["stateVersionAfter"],
                replayStatus=replay.status_code, replayed=replay.json()["replayed"],
                sameEventId=replay.json()["eventId"] == board.json()["eventId"])
            onboard = receive_until(ws, lambda m: m["stateVersion"] == 2 and m["guidanceStatus"] == "current")
            log("WS onboard guidance", movementState=onboard["movementState"],
                baseStateVersion=onboard["baseStateVersion"], reasonCodes=onboard["guidance"]["reasonCodes"])
        # 연결이 끊긴 동안(백그라운드) 하차·위치 입력 → 서버는 계속 처리
        alight = http.post(f"/api/v1/journeys/{jid}/events", headers={**auth, "Idempotency-Key": "e2e-alight-1"},
                           json={"eventType": "alight", "stateVersion": 2, "payload": {"legIndex": bus_legs[0]}})
        now = datetime.now(UTC)
        newer = http.put(f"/api/v1/journeys/{jid}/location", headers=auth,
                         json={"latitude": 36.3601, "longitude": 127.3805, "observedAt": now.isoformat(), "accuracyM": 10})
        older = http.put(f"/api/v1/journeys/{jid}/location", headers=auth,
                         json={"latitude": 36.3500, "longitude": 127.3800,
                               "observedAt": (now - timedelta(minutes=2)).isoformat()})
        detail = http.get(f"/api/v1/journeys/{jid}", headers=auth).json()
        log("disconnected: alight + location", alightStatus=alight.status_code, newer=newer.status_code,
            older=older.status_code, keptLatitude=detail["location"]["latitude"],
            connectionState=detail["connectionState"], lifecycle=detail["lifecycleStatus"],
            movementState=detail["movementState"])
        active = http.get("/api/v1/journeys", headers=auth, params={"status": "active"}).json()
        log("GET /journeys?status=active (앱 재시작 복구)", items=[i["journeyId"] == jid for i in active["items"]])
        with ws_connect(ws_url, additional_headers=auth) as ws:
            restored = receive_until(ws, lambda m: m["stateVersion"] == 3 and m["guidanceStatus"] == "current")
            log("WS reconnect 복구", stateVersion=restored["stateVersion"], resultVersion=restored["resultVersion"],
                movementState=restored["movementState"], nextBoardingLeg=(restored["guidance"].get("nextBoarding")
                                                                          or {}).get("legIndex"))
            version = restored["stateVersion"]
            for leg in bus_legs[1:]:
                for event in ("board", "alight"):
                    r = http.post(f"/api/v1/journeys/{jid}/events", headers={**auth, "Idempotency-Key": uuid4().hex},
                                  json={"eventType": event, "stateVersion": version, "payload": {"legIndex": leg}})
                    version = r.json()["stateVersionAfter"]
            done = http.post(f"/api/v1/journeys/{jid}/events", headers={**auth, "Idempotency-Key": "e2e-done"},
                             json={"eventType": "completed", "stateVersion": version, "payload": {}})
            final = receive_until(ws, lambda m: m["type"] == "journey_ended")
            log("도착(completed)", status=done.status_code, type=final["type"], lifecycle=final["lifecycleStatus"],
                movementState=final["movementState"], stateVersion=final["stateVersion"])
        with psycopg.connect(dsn) as conn:
            row = conn.execute("SELECT next_recalc_at, ended_at IS NOT NULL FROM app.journeys WHERE id = %s",
                               (jid,)).fetchone()
            snaps = conn.execute("SELECT count(*) FROM app.journey_events WHERE journey_id = %s AND "
                                 "event_type = 'evaluation_snapshot'", (jid,)).fetchone()[0]
            searches = conn.execute("SELECT status, summary->'walk_lookups' FROM app.route_searches").fetchall()
        log("DB after end", next_recalc_at=row[0], ended=row[1], evaluation_snapshots=snaps,
            transfers=chosen["transferCount"], walk_lookups=searches[0][1])
        ok = True
    finally:
        for proc in (api, worker):
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        log("cleanup", database_dropped=True, logs=str(logs), result="ok" if ok else "failed")
    return 0 if ok else 1


def _alive(http: httpx.Client) -> bool:
    try:
        return http.get("/health").status_code == 200
    except httpx.HTTPError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
