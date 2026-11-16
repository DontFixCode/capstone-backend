"""Phase 4 앱 API 격리 시험: FastAPI(TestClient) + 임시 PostgreSQL/PostGIS + 워커 작업(검색 실행·안내 재계산).

TEST_POSTGIS_DSN의 임시 서버에서 시험마다 새 DB를 만들고 migrations/01~09를 적용한다. TAGO·TMAP은 합성 응답, 토큰은 시험용
비밀값으로 서명한 HS256 JWT(GoTrue와 같은 iss·aud·role·sub 형식)다. 실제 Supabase Auth·외부 서비스는 쓰지 않는다.

    $env:TEST_POSTGIS_DSN = "postgresql://postgres:...@127.0.0.1:55433/postgres"
    uv run python -m unittest tests.integration.test_phase4_api
"""

import json
import os
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID, uuid4

import jwt
import psycopg
from psycopg.types.json import Jsonb
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

import phase3_world as w  # noqa: E402
from tago_world import FAKE_KEY, FakeTago, Fault  # noqa: E402

from app import main  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.core.security import TokenVerifier  # noqa: E402
from app.db.session import Database, DbParams, try_xact_lock  # noqa: E402
from app.integrations.bus_api import TagoClient  # noqa: E402
from app.integrations.walking_api import TmapWalkingClient  # noqa: E402
from app.modules.evaluation.schemas import EtaReport  # noqa: E402
from app.modules.journeys import repository as journey_repository  # noqa: E402
from app.modules.routing import service_area  # noqa: E402
from app.modules.transit import service as transit_service  # noqa: E402
from app.modules.transit.schemas import RealtimeTarget  # noqa: E402
from app.modules.users import service as user_service  # noqa: E402
from app.workers.collection import collection_targets  # noqa: E402
from app.workers.main import Worker  # noqa: E402
from app.workflows import search as flow  # noqa: E402

DSN = os.environ.get("TEST_POSTGIS_DSN")
UTC = timezone.utc
SECRET = "phase4-integration-secret-not-real-0000000000"
ISSUER = "http://auth.phase4.test/auth/v1"
SETTINGS = dict(
    search_max_boardings=3, search_access_radius_meters=300, search_transfer_radius_meters=150,
    search_egress_radius_meters=300, search_max_candidates=50, search_max_states=100000, search_budget_ms=60000,
    search_round_pool_size=50, search_walk_cost_weight=3,
    eval_search_horizon_seconds=3600, eval_budget_ms=60000, eval_max_timed_candidates=1000,
    eval_boarding_buffer_seconds=60, eval_max_data_age_seconds=900, eval_min_segment_samples=1,
    eval_min_probability_samples=1, eval_min_joint_scenes=1, eval_min_improvement_seconds=120,
    eval_max_horizon_difference_seconds=300, eval_max_recovery_attempts=2, eval_recovery_search_enabled=False,
    eval_timing_policy_version="timing-v1", eval_sample_policy_version="sample-v1",
    eval_recovery_policy_version="recovery-v1", eval_segment_model_version="segment-v1",
    search_expires_seconds=1800, tmap_search_option="0", tmap_max_lookups_per_search=60,
    sample_label_deadline_seconds=7200, ws_poll_seconds=0.2, ws_auth_timeout_seconds=2, job_poll_seconds=0.1,
)
ORIGIN = {"latitude": w.ORIGIN[0], "longitude": w.ORIGIN[1], "name": "합성 출발지"}
DESTINATION = {"latitude": w.DESTINATION[0], "longitude": w.DESTINATION[1], "name": "합성 도착지",
               "provider": "contract-test", "externalId": "D-1"}
# 합성 지원 경계(경도 127.3795~127.388, 위도 36.345~36.375): 합성 정류소 A~F를 포함하고, 서쪽 변은 A에서 약 45 m.
SYNTH_WEST, SYNTH_EAST, SYNTH_SOUTH, SYNTH_NORTH = 127.3795, 127.388, 36.345, 36.375
SYNTH_BOUNDARY = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"NAME": "합성경계"},
                  "geometry": {"type": "MultiPolygon", "coordinates": [[[[SYNTH_WEST, SYNTH_SOUTH],
                               [SYNTH_EAST, SYNTH_SOUTH], [SYNTH_EAST, SYNTH_NORTH], [SYNTH_WEST, SYNTH_NORTH],
                               [SYNTH_WEST, SYNTH_SOUTH]]]]}}]}


def ddl_text(path: Path) -> str:
    return "\n".join(line for line in path.read_text(encoding="utf-8").splitlines() if not line.startswith("\\"))


def synthetic_provider(**kwargs):
    """기준시각·차량·회차가 확인된 합성 ETA(실제 TAGO 기본 도착정보에는 없다). 정상 순위 흐름 검증용."""
    def provide(conn, targets, network, now):
        rows = w.synthetic_eta_rows(network.patterns, basis_at=now - timedelta(seconds=30), **kwargs)
        return flow.RealtimeInputs([EtaReport(**row) for row in rows], frozenset(w.STOPS), {},
                                   [("synthetic", "fixture")], {})
    return provide


@unittest.skipUnless(DSN, "TEST_POSTGIS_DSN이 없어 DB 격리 시험을 건너뜀")
class Phase4TestCase(unittest.TestCase):
    def setUp(self):
        url = urlparse(DSN)
        self.dbname = f"phase4_{uuid4().hex[:12]}"
        self.admin = psycopg.connect(DSN, autocommit=True)
        self.admin.execute(f'CREATE DATABASE "{self.dbname}"')
        self.addCleanup(self._drop)
        with psycopg.connect(DSN.rsplit("/", 1)[0] + f"/{self.dbname}", autocommit=True) as conn:
            conn.execute("CREATE SCHEMA extensions")
            conn.execute("CREATE EXTENSION postgis SCHEMA extensions")
            conn.execute("CREATE SCHEMA auth")
            conn.execute("CREATE TABLE auth.users (id uuid PRIMARY KEY)")
            for path in sorted((ROOT / "migrations").glob("[0-9]*.sql")):
                conn.execute(ddl_text(path))
            conn.execute("ALTER TABLE app.route_searches ADD CONSTRAINT route_searches_journey_id_fkey "
                         "FOREIGN KEY (journey_id) REFERENCES app.journeys(id) ON DELETE CASCADE")
            self.user_a, self.user_b, self.user_c = uuid4(), uuid4(), uuid4()
            for uid in (self.user_a, self.user_b, self.user_c):
                conn.execute("INSERT INTO auth.users (id) VALUES (%s)", (uid,))
        self.db = Database(DbParams(url.hostname, url.port or 5432, self.dbname, url.username, url.password or "", 10))
        self.tago = FakeTago(w.world())
        self.tmap = w.FakeTmap()
        report = transit_service.StaticImportService(self.db, lambda **kw: self.tago_client(**kw), rows=100).run()
        self.assertEqual(report.status, "published", report.problems)
        boundary_dir = tempfile.TemporaryDirectory()
        self.addCleanup(boundary_dir.cleanup)
        self.synthetic_boundary = Path(boundary_dir.name) / "synthetic-boundary.geojson"
        self.synthetic_boundary.write_text(json.dumps(SYNTH_BOUNDARY), encoding="utf-8")
        self.settings = Settings(_env_file=None, **SETTINGS, **self.boundary_setting())
        state = main.app.state
        saved = (state.settings, state.db, state.token_verifier, state.places_provider)
        self.addCleanup(lambda: (setattr(state, "settings", saved[0]), setattr(state, "db", saved[1]),
                                 setattr(state, "token_verifier", saved[2]), setattr(state, "places_provider", saved[3])))
        state.settings, state.db = self.settings, self.db
        state.token_verifier = TokenVerifier(SECRET, issuer=ISSUER, audience="authenticated")
        self.client = TestClient(main.app)

    def boundary_setting(self):
        return {"service_area_geojson": str(self.synthetic_boundary)}

    def _drop(self):
        self.admin.execute(f'DROP DATABASE IF EXISTS "{self.dbname}" WITH (FORCE)')
        self.admin.close()

    # ---- 도구
    def tago_client(self, **kw):
        kw.setdefault("max_retries", 0)
        return TagoClient(FAKE_KEY, transport=self.tago.transport(), request_interval=0, retry_backoff=0,
                          sleep=lambda _s: None, **kw)

    def token(self, user, **overrides):
        claims = {"sub": str(user), "aud": "authenticated", "iss": ISSUER, "role": "authenticated",
                  "exp": int(time.time()) + 3600}
        claims.update(overrides)
        return jwt.encode(claims, SECRET, algorithm="HS256")

    def auth(self, user, **overrides):
        return {"Authorization": f"Bearer {self.token(user, **overrides)}"}

    def worker(self, provider=None):
        return Worker(settings_factory=lambda: self.settings, db_factory=lambda _s: self.db,
                      client_factory=lambda _s, **kw: self.tago_client(**kw),
                      walking_client_factory=lambda _s: TmapWalkingClient(self.tmap.app_key,
                                                                         transport=self.tmap.transport()),
                      realtime_provider=provider, static_enabled=False)

    def query(self, sql, params=None):
        with self.db.transaction() as conn:
            return conn.execute(sql, params).fetchall()

    def submit(self, user=None, destination=None, **extra):
        r = self.client.post("/api/v1/route-searches", headers=self.auth(user or self.user_a),
                             json={"origin": ORIGIN, "destination": destination or DESTINATION, **extra})
        self.assertEqual(r.status_code, 202, r.text)
        return r.json()["searchId"]

    def ready_search(self, user=None, provider="synthetic"):
        search_id = self.submit(user)
        jobs = self.worker(synthetic_provider() if provider == "synthetic" else provider).run_jobs_pass()
        mine = [j["action"] for j in jobs["searches"] if j["search_id"] == search_id]
        self.assertEqual(mine, ["finished"], jobs)
        body = self.client.get(f"/api/v1/route-searches/{search_id}", headers=self.auth(user or self.user_a)).json()
        return search_id, body

    def start(self, user=None, *, multi=False):
        search_id, body = self.ready_search(user)
        ranked = [c for c in body["candidates"] if c["rank"] is not None]
        chosen = next(c for c in ranked if c["boardingCount"] > 1) if multi else ranked[0]
        r = self.client.post("/api/v1/journeys", headers=self.auth(user or self.user_a),
                             json={"searchId": search_id, "candidateId": chosen["candidateId"]})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json(), chosen, search_id

    def event(self, journey_id, event_type, version, payload, key=None, user=None):
        return self.client.post(f"/api/v1/journeys/{journey_id}/events",
                                headers={**self.auth(user or self.user_a), "Idempotency-Key": key or uuid4().hex},
                                json={"eventType": event_type, "stateVersion": version, "payload": payload})

    def detail(self, journey_id, user=None):
        return self.client.get(f"/api/v1/journeys/{journey_id}", headers=self.auth(user or self.user_a)).json()


# ---------------------------------------------------------------- 1. 인증·프로필·탈퇴 계정

class AuthProfileTest(Phase4TestCase):
    def test_tokens_profile_init_and_patch(self):
        self.assertEqual(self.client.get("/api/v1/me").status_code, 401)
        self.assertEqual(self.client.get("/api/v1/me", headers={"Authorization": "Bearer x.y.z"}).status_code, 401)
        expired = self.client.get("/api/v1/me", headers=self.auth(self.user_a, exp=int(time.time()) - 10))
        self.assertEqual((expired.status_code, expired.json()["error"]["code"]), (401, "TOKEN_EXPIRED"))
        self.assertEqual(expired.headers["www-authenticate"], "Bearer")
        unknown = self.client.get("/api/v1/me", headers=self.auth(uuid4()))
        self.assertEqual(unknown.status_code, 401, "auth.users에 없는 사용자")
        me = self.client.get("/api/v1/me", headers=self.auth(self.user_a))
        self.assertEqual(me.status_code, 200)
        self.assertEqual((me.json()["userId"], me.json()["displayName"]), (str(self.user_a), None))
        named = self.client.patch("/api/v1/me", headers=self.auth(self.user_a), json={"displayName": "대전버스 이용자"})
        self.assertEqual(named.json()["displayName"], "대전버스 이용자")
        self.assertGreaterEqual(named.json()["updatedAt"], me.json()["updatedAt"])
        kept = self.client.patch("/api/v1/me", headers=self.auth(self.user_a), json={})
        self.assertEqual((kept.json()["displayName"], kept.json()["updatedAt"]),
                         ("대전버스 이용자", named.json()["updatedAt"]), "생략은 유지, 값이 같으면 updatedAt 유지")
        self.assertEqual(self.client.patch("/api/v1/me", headers=self.auth(self.user_a),
                                           json={"displayName": "   "}).status_code, 400)
        self.assertEqual(self.client.patch("/api/v1/me", headers=self.auth(self.user_a),
                                           json={"displayName": 3}).status_code, 422)
        cleared = self.client.patch("/api/v1/me", headers=self.auth(self.user_a), json={"displayName": None})
        self.assertIsNone(cleared.json()["displayName"])

    def test_concurrent_initialization_leaves_one_row(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(lambda _: user_service.ensure_profile(self.db, self.user_b), range(8)))
        self.assertEqual(len({r["created_at"] for r in rows}), 1)
        self.assertEqual(self.query("SELECT count(*) FROM app.profiles WHERE user_id = %s", (self.user_b,)), [(1,)])

    def test_deleted_account_is_blocked_everywhere(self):
        self.client.get("/api/v1/me", headers=self.auth(self.user_c))
        journey_search = self.submit(self.user_c)
        self.query("UPDATE app.profiles SET deleted_at = now() WHERE user_id = %s RETURNING 1", (self.user_c,))
        headers = self.auth(self.user_c)
        for method, url, body in [("get", "/api/v1/me", None), ("patch", "/api/v1/me", {"displayName": "x"}),
                                  ("get", "/api/v1/stops/nearby?latitude=36.35&longitude=127.38", None),
                                  ("post", "/api/v1/route-searches", {"origin": ORIGIN, "destination": DESTINATION}),
                                  ("get", f"/api/v1/route-searches/{journey_search}", None),
                                  ("get", "/api/v1/journeys", None)]:
            with self.subTest(url=url):
                r = getattr(self.client, method)(url, headers=headers, **({"json": body} if body else {}))
                self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "ACCOUNT_DELETED"))
        self.assertEqual(self.query("SELECT count(*) FROM app.profiles WHERE user_id = %s", (self.user_c,)), [(1,)],
                         "프로필을 다시 만들지 않고 UUID·기록 보존")


# ---------------------------------------------------------------- 2. 정류소·노선·실시간 자료 상태

class TransitApiTest(Phase4TestCase):
    def stop_id(self, external):
        return str(self.query("SELECT id FROM app.stops WHERE external_id = %s", (external,))[0][0])

    def route_id(self, external):
        return str(self.query("SELECT id FROM app.routes WHERE external_id = %s", (external,))[0][0])

    def test_static_lookups_repeated_visits_and_shapes(self):
        h = self.auth(self.user_a)
        version = self.client.get("/api/v1/static-data/version", headers=h).json()["dataVersion"]
        near = self.client.get("/api/v1/stops/nearby", headers=h, params={"latitude": 36.3500, "longitude": 127.3800})
        self.assertEqual(near.status_code, 200)
        self.assertEqual(near.json()["items"][0]["externalId"], w.A)
        self.assertEqual((near.json()["radiusM"], near.json()["dataVersion"]), (300.0, version))
        self.assertEqual(self.client.get("/api/v1/stops/nearby", headers=h, params={
            "latitude": 36.35, "longitude": 127.38, "radiusM": 5000}).status_code, 400)
        self.assertEqual(self.client.get("/api/v1/stops/nearby", headers=h, params={
            "latitude": 91, "longitude": 127.38}).status_code, 422)
        self.query("UPDATE app.stops SET is_active = false WHERE external_id = %s RETURNING 1", (w.F,))
        inactive = self.client.get("/api/v1/stops", headers=h, params={"provider": "tago", "externalId": w.F})
        self.assertEqual((inactive.status_code, inactive.json()["isActive"]), (200, False))
        self.assertEqual(self.client.get("/api/v1/stops", headers=h,
                                         params={"provider": "tago", "externalId": "NOPE"}).status_code, 404)
        self.assertEqual(self.client.get("/api/v1/stops/abc", headers=h).status_code, 422)
        self.assertEqual(self.client.get("/api/v1/stops/999999999", headers=h).status_code, 404)
        b = self.stop_id(w.B)
        visits = self.client.get(f"/api/v1/stops/{b}/routes", headers=h).json()["items"]
        r20 = [v for v in visits if v["route"]["externalId"] == "TSTR000020"]
        self.assertEqual(sorted(v["seq"] for v in r20), [2, 4], "같은 패턴의 반복 경유를 seq별로 보존")
        self.assertTrue(all(isinstance(v["route"]["id"], str) for v in visits))
        found = self.client.get("/api/v1/routes", headers=h, params={"routeNo": "20"}).json()["items"]
        self.assertEqual([r["externalId"] for r in found], ["TSTR000020"])
        self.assertEqual(self.client.get("/api/v1/routes", headers=h).status_code, 422)
        rid = self.route_id("TSTR000020")
        detail = self.client.get(f"/api/v1/routes/{rid}", headers=h).json()
        self.assertEqual(detail["serviceInfo"]["headway"]["status"], "UNIT_UNCONFIRMED")
        self.assertIsNone(detail["serviceInfo"]["headway"]["seconds"])
        self.assertEqual({s["status"] for s in detail["shapes"]}, {"UNAVAILABLE"})
        patterns = self.client.get(f"/api/v1/routes/{rid}/patterns", headers=h).json()["items"]
        self.assertEqual(patterns[0]["stopCount"], 5)
        key = patterns[0]["patternKey"]
        after = self.client.get(f"/api/v1/routes/{rid}/patterns/{key}/stops", headers=h, params={"afterSeq": 3})
        self.assertEqual([i["seq"] for i in after.json()["items"]], [4, 5])
        self.assertEqual(self.client.get(f"/api/v1/routes/{rid}/patterns/{key}/stops", headers=h,
                                         params={"afterSeq": 5}).json()["items"], [])
        self.assertEqual(self.client.get(f"/api/v1/routes/{rid}/patterns/nope/stops", headers=h).status_code, 404)
        shape = self.client.get(f"/api/v1/routes/{rid}/shapes/{key}", headers=h)
        self.assertEqual((shape.status_code, shape.json()["status"], shape.json()["geometry"]),
                         (200, "UNAVAILABLE", None))
        self.assertEqual(shape.json()["reason"], "shape_provider_not_configured")
        again = self.client.get(f"/api/v1/routes/{rid}/shapes/{key}",
                                headers={**h, "If-None-Match": shape.headers["etag"]})
        self.assertEqual(again.status_code, 304)
        self.assertEqual(self.client.get(f"/api/v1/routes/{rid}/shapes/nope", headers=h).status_code, 404)
        self.assertEqual(self.client.get("/api/v1/routes/99999999/shapes/x", headers=h).status_code, 404)

    def test_realtime_status_distinguishes_missing_empty_stale_and_failure(self):
        h = self.auth(self.user_a)
        a = self.stop_id(w.A)
        missing = self.client.get(f"/api/v1/stops/{a}/arrivals", headers=h).json()
        self.assertEqual((missing["items"], missing["dataStatus"]["availability"], missing["dataStatus"]["isStale"]),
                         ([], "unavailable", None))
        collector = transit_service.RealtimeCollector(self.db, self.tago_client(), interval_seconds=600)
        collector.collect(RealtimeTarget("arrivals", w.A), force=True)
        got = self.client.get(f"/api/v1/stops/{a}/arrivals", headers=h).json()
        self.assertEqual(got["dataStatus"]["availability"], "available")
        self.assertFalse(got["dataStatus"]["isStale"])
        item = got["items"][0]
        self.assertEqual(item["remainingSeconds"], 300)
        self.assertIsNone(item["predictedArrivalAt"], "TAGO 기준 시각 미확인: 절대 ETA를 만들지 않음")
        self.assertIsNone(item["vehicle"], "차량 연결 근거 없음")
        self.query("UPDATE app.api_fetches SET received_at = received_at - interval '1000 seconds' RETURNING 1")
        self.assertTrue(self.client.get(f"/api/v1/stops/{a}/arrivals", headers=h).json()["dataStatus"]["isStale"])
        self.tago.faults.append(Fault("getSttnAcctoArvlPrearngeInfoList", "http500", match={"nodeId": w.A}))
        collector.collect(RealtimeTarget("arrivals", w.A), force=True)
        failed = self.client.get(f"/api/v1/stops/{a}/arrivals", headers=h).json()["dataStatus"]
        self.assertEqual((failed["availability"], failed["lastAttemptFailed"]), ("available", True),
                         "실패한 수집은 이전 공개본을 덮어쓰지 않음")
        self.assertIn(failed["lastAttemptStatus"], ("error", "unpublished"))
        self.tago.faults.clear()
        self.tago.world["arrivals"][w.C2] = []
        collector.collect(RealtimeTarget("arrivals", w.C2), force=True)
        empty = self.client.get(f"/api/v1/stops/{self.stop_id(w.C2)}/arrivals", headers=h).json()
        self.assertEqual((empty["items"], empty["dataStatus"]["availability"]), ([], "empty"))
        rid = self.route_id("TSTR000010")
        collector.collect(RealtimeTarget("positions", "TSTR000010"), force=True)
        positions = self.client.get(f"/api/v1/routes/{rid}/positions", headers=h).json()
        self.assertEqual(positions["dataStatus"]["availability"], "available")
        self.assertEqual(positions["items"][0]["vehicleNo"], "대전70자9000")


# ---------------------------------------------------------------- 3. 최초 경로 검색

class RouteSearchApiTest(Phase4TestCase):
    def test_accept_then_worker_ranks_and_shapes(self):
        line = {"type": "LineString", "coordinates": [[127.38, 36.35 + i * 0.005] for i in range(5)]}
        self.query("UPDATE app.routes SET shape_info = %s WHERE external_id = 'TSTR000010' RETURNING 1",
                   (Jsonb({"patterns": {}}),))
        key = self.query("SELECT DISTINCT pattern_key FROM app.route_stops rs JOIN app.routes r ON r.id = rs.route_id "
                         "WHERE r.external_id = 'TSTR000010'")[0][0]
        self.query("UPDATE app.routes SET shape_info = %s WHERE external_id = 'TSTR000010' RETURNING 1",
                   (Jsonb({"patterns": {key: {
                       "status": "available", "geometry": line, "source": "verified-test-provider", "version": "t1",
                       "fetched_at": "2026-11-08T00:00:00+00:00",
                       "stop_vertex_index": {str(i + 1): i for i in range(5)}}}}),))
        search_id = self.submit()
        pending = self.client.get(f"/api/v1/route-searches/{search_id}", headers=self.auth(self.user_a)).json()
        self.assertEqual((pending["status"], pending["candidates"], pending["rankingMode"]), ("processing", [], None))
        self.assertEqual(self.client.get(f"/api/v1/route-searches/{search_id}",
                                         headers=self.auth(self.user_b)).status_code, 404, "타인 검색")
        self.assertEqual(self.tmap.requests, [], "API 접수는 외부 호출 없음")
        self.assertEqual(self.tago.count("getSttnAcctoArvlPrearngeInfoList"), 0)
        self.worker(synthetic_provider()).run_jobs_pass()
        body = self.client.get(f"/api/v1/route-searches/{search_id}", headers=self.auth(self.user_a)).json()
        self.assertEqual((body["status"], body["rankingMode"]), ("ready", "nominal"))
        ranked = [c for c in body["candidates"] if c["rank"] is not None]
        self.assertEqual([c["rank"] for c in ranked], list(range(1, len(ranked) + 1)))
        self.assertEqual(body["recommendedCandidateId"], ranked[0]["candidateId"])
        self.assertTrue(all(c["startable"] and c["startMode"] == "ranked" for c in ranked))
        self.assertTrue(all(c["rankingMode"] == "nominal" for c in ranked), "한 검색의 순위에 모드를 섞지 않음")
        top = ranked[0]
        bus = next(leg for leg in top["legs"] if leg["kind"] == "bus")
        self.assertTrue(bus["route"]["id"].isdigit())
        self.assertIsNotNone(bus["boardEta"]["predictedArrivalAt"])
        self.assertIsNotNone(top["evaluation"]["nominalTotalSeconds"])
        self.assertIn("probability", top["evaluation"]["transfers"][0] if top["evaluation"]["transfers"] else
                      {"probability": None})
        r10 = [leg for c in body["candidates"] for leg in c["legs"] if leg["kind"] == "bus"
               and leg["route"]["externalId"] == "TSTR000010"]
        self.assertTrue(r10)
        self.assertTrue(all(leg["geometryStatus"] == "available" and leg["geometrySource"] == "verified-test-provider"
                            for leg in r10))
        seg = r10[0]
        self.assertEqual(len(seg["geometry"]["coordinates"]), seg["alight"]["seq"] - seg["board"]["seq"] + 1,
                         "승하차 구간만 잘라 붙임")
        other = [leg for c in body["candidates"] for leg in c["legs"] if leg["kind"] == "bus"
                 and leg["route"]["externalId"] != "TSTR000010"]
        self.assertTrue(all(leg["geometryStatus"] == "unavailable" for leg in other))
        self.assertEqual(self.query("SELECT user_id, source FROM app.route_searches WHERE id = %s", (search_id,)),
                         [(self.user_a, "app")])

    def test_data_limited_no_path_and_validation(self):
        search_id, body = self.ready_search(provider=None)  # 합성 TAGO: 기준 시각 미확인 → 순위 없음
        self.assertEqual(body["status"], "data_limited")
        self.assertIn("no_rankable_candidate", body["reasonCodes"])
        self.assertTrue(body["candidates"])
        self.assertTrue(all(c["rank"] is None for c in body["candidates"]))
        self.assertTrue(any("eta_basis_unconfirmed" in c["evaluation"]["reasonCodes"] for c in body["candidates"]))
        # 자료 부족(시간 미확인)일 뿐인 후보는 제한 시작, 기준 ETA·도보 결과로 연결 불가가 판정된 후보는 차단
        for c in body["candidates"]:
            blocked = c["evaluation"]["connectionStatus"] in ("confirmed_infeasible", "negative_slack")
            self.assertEqual((c["startable"], c["startMode"]), (False, None) if blocked else (True, "data_limited"))
        self.assertTrue(any(c["startMode"] == "data_limited" for c in body["candidates"]))
        far = self.submit(destination={"latitude": 36.374, "longitude": 127.387})  # 합성 경계 안, 반경 내 정류소 없음
        self.worker(synthetic_provider()).run_jobs_pass()
        none = self.client.get(f"/api/v1/route-searches/{far}", headers=self.auth(self.user_a)).json()
        self.assertEqual((none["status"], none["candidates"]), ("no_path", []))
        for bad in [{"origin": {**ORIGIN, "latitude": 91}, "destination": DESTINATION},
                    {"origin": ORIGIN, "destination": DESTINATION, "startAt": "2026-11-08T10:00:00"},
                    {"origin": ORIGIN}]:
            self.assertEqual(self.client.post("/api/v1/route-searches", headers=self.auth(self.user_a),
                                              json=bad).status_code, 422)

    def test_interrupted_jobs_do_not_stay_processing(self):
        stuck = self.submit()
        # 워커가 임대한 뒤 중단된 상황: 임대가 지난 검색은 다시 가져가 끝낸다.
        self.query("UPDATE app.route_searches SET summary = summary || %s WHERE id = %s RETURNING 1",
                   (Jsonb({"job": {"state": "running", "attempts": 1,
                                                      "lease_until": "2000-01-01T00:00:00+00:00"}}), stuck))
        exhausted = self.submit()
        self.query("UPDATE app.route_searches SET summary = summary || %s WHERE id = %s RETURNING 1",
                   (Jsonb({"job": {"state": "running", "attempts": 2,
                                                      "lease_until": "2000-01-01T00:00:00+00:00"}}), exhausted))
        expired = self.submit()
        self.query("UPDATE app.route_searches SET expires_at = now() - interval '1 second' WHERE id = %s RETURNING 1",
                   (expired,))
        leased = self.submit()
        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        self.query("UPDATE app.route_searches SET summary = summary || %s WHERE id = %s RETURNING 1",
                   (Jsonb({"job": {"state": "running", "attempts": 1, "lease_until": future}}),
                    leased))
        result = self.worker(synthetic_provider()).run_jobs_pass()
        actions = {j["search_id"]: j["action"] for j in result["searches"]}
        self.assertEqual(actions, {stuck: "finished", exhausted: "exhausted", expired: "expired"})
        statuses = dict(self.query("SELECT id::text, status FROM app.route_searches"))
        self.assertEqual((statuses[stuck], statuses[exhausted], statuses[expired], statuses[leased]),
                         ("ready", "error", "error", "processing"), "임대 중인 검색은 건드리지 않음")
        body = self.client.get(f"/api/v1/route-searches/{exhausted}", headers=self.auth(self.user_a)).json()
        self.assertEqual(body["reasonCodes"], ["search_job_attempts_exhausted"])

    def test_search_collection_is_not_blocked_by_concurrent_collection(self):
        search_id = self.submit()
        key = transit_service.realtime_target_key(RealtimeTarget("arrivals", w.A))
        holder = self.db.connect()
        self.addCleanup(holder.close)
        self.assertTrue(try_xact_lock(holder, f"transit:realtime:{key}"), "다른 실행이 같은 대상을 수집 중")
        started = time.monotonic()
        result = self.worker(None).run_jobs_pass()
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual(result["searches"][0]["action"], "finished")
        summary = self.query("SELECT summary FROM app.route_searches WHERE id = %s", (search_id,))[0][0]
        self.assertEqual(summary["realtime_collection"][key]["action"], "in_progress",
                         "잠금을 기다리지 않고 마지막 공개값으로 평가")
        holder.rollback()


# ---------------------------------------------------------------- 4. 이동 세션

class JourneyApiTest(Phase4TestCase):
    def test_start_restore_events_and_snapshots(self):
        journey, chosen, search_id = self.start(multi=True)
        jid = journey["journeyId"]
        self.assertEqual((journey["lifecycleStatus"], journey["movementState"]), ("active", "walking"))
        self.assertEqual((journey["stateVersion"], journey["selectionVersion"], journey["resultVersion"]), (1, 1, 0))
        self.assertEqual(journey["selectedRoute"]["candidateId"], chosen["candidateId"])
        self.assertEqual((journey["guidance"], journey["guidanceStatus"]), (None, "pending"))
        again = self.client.post("/api/v1/journeys", headers=self.auth(self.user_a),
                                 json={"searchId": search_id, "candidateId": chosen["candidateId"]})
        self.assertEqual((again.status_code, again.json()["error"]["code"]), (409, "ACTIVE_JOURNEY_EXISTS"))
        self.assertEqual(again.json()["error"]["details"]["activeJourneyId"], jid)
        snaps = self.query("SELECT idempotency_key, payload FROM app.journey_events WHERE journey_id = %s "
                           "AND event_type = 'evaluation_snapshot' ORDER BY id", (jid,))
        self.assertEqual(len(snaps), chosen["transferCount"], "환승별 예측을 결과 전에 고정")
        self.assertEqual(snaps[0][1]["selection_version"], 1)
        self.assertEqual(snaps[0][1]["transfer_index"], 1)
        self.assertIsNotNone(snaps[0][1]["slack_seconds"])
        # 재실행해도 중복 생성·덮어쓰기 없음
        with self.db.transaction() as conn:
            again_id = journey_repository.insert_event(
                conn, journey_id=UUID(jid), idempotency_key=snaps[0][0], event_type="evaluation_snapshot",
                source="system", occurred_at=None, received_at=datetime.now(UTC), state_version_after=1,
                payload={"overwrite": True}, skip_existing=True)
        self.assertIsNone(again_id)
        self.assertEqual(self.query("SELECT payload FROM app.journey_events WHERE idempotency_key = %s",
                                    (snaps[0][0],))[0][0], snaps[0][1])
        listed = self.client.get("/api/v1/journeys", headers=self.auth(self.user_a), params={"status": "active"})
        self.assertEqual([i["journeyId"] for i in listed.json()["items"]], [jid])
        self.assertEqual(self.client.get(f"/api/v1/journeys/{jid}", headers=self.auth(self.user_b)).status_code, 404)
        self.assertEqual(self.event(jid, "board", 1, {"legIndex": 1}, user=self.user_b).status_code, 404)

        worker = self.worker(synthetic_provider())
        worker.run_jobs_pass()
        restored = self.detail(jid)
        self.assertEqual((restored["resultVersion"], restored["guidanceStatus"]), (1, "current"))
        nb = restored["guidance"]["nextBoarding"]
        self.assertIsNotNone(nb["predictedArrivalAt"])
        self.assertGreaterEqual(nb["waitSeconds"], 0)
        self.assertIn("location_not_received", restored["guidance"]["reasonCodes"])

        bus_legs = [leg["legIndex"] for leg in restored["selectedRoute"]["legs"] if leg["kind"] == "bus"]
        self.assertEqual(self.event(jid, "board", 2, {"legIndex": bus_legs[0]}).json()["error"]["code"],
                         "STATE_VERSION_CONFLICT")
        self.assertEqual(self.event(jid, "missed_bus", 1, {}).json()["error"]["code"], "UNSUPPORTED_EVENT_TYPE")
        self.assertEqual(self.client.post(f"/api/v1/journeys/{jid}/events", headers=self.auth(self.user_a),
                                          json={"eventType": "board", "stateVersion": 1,
                                                "payload": {"legIndex": 1}}).status_code, 422, "키 필수")
        board = self.event(jid, "board", 1, {"legIndex": bus_legs[0], "vehicleId": None}, key="board-1")
        self.assertEqual(board.status_code, 200, board.text)
        self.assertEqual((board.json()["stateVersionAfter"], board.json()["replayed"]), (2, False))
        replay = self.event(jid, "board", 1, {"legIndex": bus_legs[0], "vehicleId": None}, key="board-1")
        self.assertEqual((replay.json()["eventId"], replay.json()["replayed"]), (board.json()["eventId"], True))
        reused = self.event(jid, "board", 1, {"legIndex": bus_legs[0], "vehicleId": "X"}, key="board-1")
        self.assertEqual(reused.json()["error"]["code"], "IDEMPOTENCY_KEY_REUSED")
        self.assertEqual(self.event(jid, "board", 2, {"legIndex": bus_legs[0]}).json()["error"]["code"],
                         "INVALID_TRANSITION")
        onboard = self.detail(jid)
        self.assertEqual((onboard["movementState"], onboard["guidanceStatus"]), ("onboard", "pending"),
                         "이전 상태의 안내는 현재 안내로 내보내지 않음")
        self.assertIsNone(onboard["currentProgress"]["boarded"]["vehicleId"])
        worker.run_jobs_pass()
        onboard = self.detail(jid)
        self.assertEqual((onboard["guidanceStatus"], onboard["guidance"]["baseStateVersion"]), ("current", 2))
        self.assertIn("onboard_vehicle_unconfirmed", onboard["guidance"]["reasonCodes"])
        fix = self.event(jid, "correction", 2, {"supersedesEventId": board.json()["eventId"],
                                                "vehicleId": f"V-{chosen['legs'][1]['route']['externalId']}-0"})
        self.assertEqual(fix.status_code, 200, fix.text)
        self.assertEqual(self.detail(jid)["currentProgress"]["boarded"]["correctedBy"], [fix.json()["eventId"]])
        alight = self.event(jid, "alight", 3, {"legIndex": bus_legs[0]})
        self.assertEqual(alight.status_code, 200, alight.text)
        self.assertEqual(self.detail(jid)["movementState"], "transferring")

        # 위치: 최신만 반영, 역순 위치는 무시, 미래 시각은 거부
        now = datetime.now(UTC)
        url = f"/api/v1/journeys/{jid}/location"
        put = lambda lat, at: self.client.put(url, headers=self.auth(self.user_a), json={  # noqa: E731
            "latitude": lat, "longitude": 127.38, "observedAt": at.isoformat(), "accuracyM": 8})
        self.assertEqual(put(36.3600, now - timedelta(seconds=5)).status_code, 204)
        self.assertEqual(put(36.3500, now - timedelta(seconds=60)).status_code, 204)
        self.assertEqual(self.detail(jid)["location"]["latitude"], 36.36, "역순 위치가 최신값을 덮어쓰지 않음")
        self.assertEqual(put(36.37, now + timedelta(hours=1)).status_code, 422)

        targets = collection_targets(self.settings, conn := self.db.connect(), datetime.now(UTC))
        conn.close()
        self.assertTrue(any(s.startswith(f"journey:{jid}") for t in targets for s in t.sources))
        done = self.event(jid, "completed", 4, {})
        self.assertEqual(done.json()["stateVersionAfter"], 5)
        ended = self.detail(jid)
        self.assertEqual((ended["lifecycleStatus"], ended["movementState"]), ("completed", "arrived"))
        self.assertIsNotNone(ended["endedAt"])
        self.assertEqual(self.query("SELECT next_recalc_at FROM app.journeys WHERE id = %s", (jid,)), [(None,)])
        conn = self.db.connect()
        targets = collection_targets(self.settings, conn, datetime.now(UTC) + timedelta(hours=1))
        conn.close()
        self.assertFalse(any(s.startswith("journey:") for t in targets for s in t.sources), "종료 후 수집 대상 해제")
        self.assertEqual(self.event(jid, "board", 5, {"legIndex": bus_legs[-1]}).json()["error"]["code"],
                         "JOURNEY_ENDED")
        self.assertEqual(put(36.37, datetime.now(UTC)).status_code, 409)
        late_fix = self.event(jid, "correction", 5, {"supersedesEventId": alight.json()["eventId"], "seq": 3})
        self.assertEqual(late_fix.status_code, 200, "종료 뒤에도 수정 입력은 허용")
        self.assertEqual(self.client.get("/api/v1/journeys", headers=self.auth(self.user_a),
                                         params={"status": "active"}).json()["items"], [])

    def test_data_limited_candidate_starts_with_limits(self):
        search_id, body = self.ready_search(provider=None)  # 합성 TAGO: 기준 시각 미확인 → 순위 없음
        cand = next(c for c in body["candidates"] if c["startMode"] == "data_limited"
                    and "eta_basis_unconfirmed" in c["evaluation"]["reasonCodes"])
        r = self.client.post("/api/v1/journeys", headers=self.auth(self.user_a),
                             json={"searchId": search_id, "candidateId": cand["candidateId"]})
        self.assertEqual(r.status_code, 201, r.text)
        jid, route = r.json()["journeyId"], r.json()["selectedRoute"]
        self.assertEqual((route["startMode"], route["rank"], route["rankingMode"]), ("data_limited", None, None))
        self.assertIn("eta_basis_unconfirmed", route["startLimitReasons"])
        self.assertIsNone(route["evaluation"]["nominalTotalSeconds"], "미산출 시간은 null로 유지")
        self.assertEqual(self.query("SELECT count(*) FROM app.journey_events WHERE journey_id = %s "
                                    "AND event_type = 'evaluation_snapshot'", (jid,)),
                         [(cand["transferCount"],)], "확률·ETA가 null이어도 환승별 예측을 결과 전에 고정")
        self.worker(None).run_jobs_pass()
        detail = self.detail(jid)
        self.assertEqual(detail["guidanceStatus"], "current", "제한 시작한 이동도 재평가가 돈다")
        g = detail["guidance"]
        self.assertEqual((g["status"], g["etaBasisPolicy"]), ("data_limited", "tago-arrival-basis-unconfirmed/1"))
        self.assertIsNone(g["nextBoarding"]["predictedArrivalAt"])
        self.assertIn("next_boarding_eta_unavailable", g["reasonCodes"])
        bus = next(leg["legIndex"] for leg in route["legs"] if leg["kind"] == "bus")
        board = self.event(jid, "board", 1, {"legIndex": bus, "vehicleId": None})
        self.assertEqual(board.status_code, 200, "수동 탑승 확인은 시간 미확인 상태에서도 동작")
        self.assertEqual(self.detail(jid)["movementState"], "onboard")

    def test_received_at_policy_builds_board_eta_without_inventing_rank(self):
        self.settings = Settings(_env_file=None, **SETTINGS, **self.boundary_setting(),
                                 tago_eta_basis_policy="server_received_at")
        main.app.state.settings = self.settings
        search_id, body = self.ready_search(provider=None)
        etas = [leg["boardEta"] for c in body["candidates"] for leg in c["legs"]
                if leg["kind"] == "bus" and leg["boardEta"]]
        self.assertTrue(etas, ("수신 시각 근사로 승차 ETA가 생김",
                               sorted({r for c in body["candidates"] for r in c["evaluation"]["reasonCodes"]})))
        self.assertTrue(all(e["basisSource"] == "server_received_at_approx" and e["basisAt"] == e["collectedAt"]
                            and e["sourceGeneratedAt"] is None for e in etas), "수신 시각을 원천 생성 시각으로 쓰지 않음")
        reasons = {r for c in body["candidates"] for r in c["evaluation"]["reasonCodes"]}
        self.assertNotIn("eta_basis_unconfirmed", reasons)
        self.assertTrue(all(c["rank"] is None for c in body["candidates"]),
                        "차량번호·도착 이벤트가 없어 하차 ETA가 없으므로 순위를 만들지 않음")
        self.assertEqual(body["status"], "data_limited")
        self.assertEqual(self.query("SELECT params->'realtime'->>'eta_basis_policy', summary->>'eta_basis_policy' "
                                    "FROM app.route_searches WHERE id = %s", (search_id,)),
                         [("tago-arrival-basis-server-received-at/1",) * 2])
        stop_id = next(leg["board"]["stopId"] for c in body["candidates"] for leg in c["legs"]
                       if leg["kind"] == "bus" and leg["boardEta"])
        items = self.client.get(f"/api/v1/stops/{stop_id}/arrivals", headers=self.auth(self.user_a)).json()["items"]
        timed = [i for i in items if i["remainingSeconds"] is not None]
        self.assertTrue(timed)
        for item in timed:
            self.assertEqual((item["etaBasisSource"], item["etaBasisAt"]), ("server_received_at_approx",
                                                                             item["collectedAt"]))
            self.assertEqual(datetime.fromisoformat(item["predictedArrivalAt"]),
                             datetime.fromisoformat(item["etaBasisAt"]) + timedelta(seconds=item["remainingSeconds"]))
            self.assertIsNone(item["sourceGeneratedAt"])
        self.settings = Settings(_env_file=None, **SETTINGS, **self.boundary_setting())
        main.app.state.settings = self.settings
        default = self.client.get(f"/api/v1/stops/{stop_id}/arrivals", headers=self.auth(self.user_a)).json()["items"]
        self.assertTrue(all(i["predictedArrivalAt"] is None and i["etaBasisSource"] is None for i in default),
                        "기본 정책에서는 절대 ETA를 만들지 않음(DB 원값은 바꾸지 않음)")

    def test_start_rules_and_stale_recalculation_is_discarded(self):
        search_id, body = self.ready_search()
        top = next(c for c in body["candidates"] if c["rank"] == 1)
        other = self.submit(self.user_b)
        self.assertEqual(self.client.post("/api/v1/journeys", headers=self.auth(self.user_a),
                                          json={"searchId": other, "candidateId": top["candidateId"]}).status_code,
                         404, "타인 검색")
        self.query("UPDATE app.route_searches SET expires_at = now() - interval '1 second' WHERE id = %s "
                   "RETURNING 1", (search_id,))
        expired = self.client.post("/api/v1/journeys", headers=self.auth(self.user_a),
                                   json={"searchId": search_id, "candidateId": top["candidateId"]})
        self.assertEqual(expired.json()["error"]["code"], "SEARCH_EXPIRED")
        journey, _, _ = self.start()
        jid = UUID(journey["journeyId"])
        now = datetime.now(UTC)
        with self.db.transaction() as conn:
            claim = journey_repository.claim_due_journeys(conn, now, lease_seconds=60, limit=5)[0]
        self.assertEqual(self.event(str(jid), "cancelled", 1, {"reason": "시험"}).status_code, 200)
        with self.db.transaction() as conn:
            saved = journey_repository.save_guidance(conn, jid, expected_state_version=claim["state_version"],
                                                     leased_until=claim["leased_until"], guidance={"x": 1},
                                                     next_recalc_at=now)
        self.assertIsNone(saved, "계산 중 상태가 바뀌면 이전 상태의 결과를 저장하지 않음")
        self.assertEqual(self.query("SELECT result_version, lifecycle_status FROM app.journeys WHERE id = %s",
                                    (jid,)), [(0, "cancelled")])


# ---------------------------------------------------------------- 5. WebSocket 복구·연결 단절

class StreamTest(Phase4TestCase):
    def url(self, jid):
        return f"/api/v1/journeys/{jid}/stream"

    def test_stream_sync_reconnect_and_end(self):
        journey, _, _ = self.start()
        jid = journey["journeyId"]
        bus_leg = next(leg["legIndex"] for leg in journey["selectedRoute"]["legs"] if leg["kind"] == "bus")
        worker = self.worker(synthetic_provider())
        with self.assertRaises(WebSocketDisconnect) as ctx:
            with self.client.websocket_connect(self.url(jid), headers={"Authorization": "Bearer bad.token.x"}):
                pass
        self.assertEqual(ctx.exception.code, 4401)
        with self.assertRaises(WebSocketDisconnect) as ctx:
            with self.client.websocket_connect(self.url(jid), headers=self.auth(self.user_b)) as ws:
                ws.receive_json()
        self.assertEqual(ctx.exception.code, 4404, "다른 사용자의 이동")
        with self.client.websocket_connect(self.url(jid)) as ws:  # 헤더 없이 첫 메시지 인증
            ws.send_json({"type": "auth", "accessToken": self.token(self.user_a)})
            first = ws.receive_json()
            self.assertEqual((first["type"], first["stateVersion"], first["resultVersion"]), ("journey_state", 1, 0))
        with self.assertRaises(WebSocketDisconnect) as ctx:
            with self.client.websocket_connect(self.url(jid)) as ws:
                ws.send_json({"type": "hello"})
                ws.receive_json()
        self.assertEqual(ctx.exception.code, 4401, "인증 전에는 세션 데이터를 보내지 않음")

        with self.client.websocket_connect(self.url(jid), headers=self.auth(self.user_a)) as ws:
            first = ws.receive_json()
            self.assertEqual(first["guidanceStatus"], "pending")
            self.assertEqual(self.query("SELECT connection_state FROM app.journeys WHERE id = %s", (jid,)),
                             [("connected",)])
            worker.run_jobs_pass()
            update = ws.receive_json()
            self.assertEqual((update["resultVersion"], update["baseStateVersion"], update["guidanceStatus"]),
                             (1, 1, "current"))
            self.assertEqual(self.event(jid, "board", 1, {"legIndex": bus_leg}).status_code, 200)
            moved = ws.receive_json()
            self.assertEqual((moved["stateVersion"], moved["movementState"], moved["guidanceStatus"]),
                             (2, "onboard", "pending"))
        # 연결이 끊겨도 이동·재평가 예약·수집 대상은 유지된다.
        time.sleep(0.3)
        row = self.query("SELECT lifecycle_status, connection_state, next_recalc_at IS NOT NULL FROM app.journeys "
                         "WHERE id = %s", (jid,))
        self.assertEqual(row, [("active", "disconnected", True)])
        conn = self.db.connect()
        targets = collection_targets(self.settings, conn, datetime.now(UTC) + timedelta(hours=1))
        conn.close()
        self.assertTrue(any(s == f"journey:{jid}" for t in targets for s in t.sources))
        worker.run_jobs_pass()  # 연결 없이(백그라운드) 계산
        with self.client.websocket_connect(self.url(jid), headers=self.auth(self.user_a)) as ws:
            restored = ws.receive_json()
            self.assertEqual((restored["stateVersion"], restored["resultVersion"], restored["guidanceStatus"]),
                             (2, 2, "current"), "재접속하면 최신 안내 복구")
            ws.send_json({"type": "ping"})
            self.assertEqual(ws.receive_json()["type"], "pong")
            self.assertEqual(self.event(jid, "cancelled", 2, {"reason": "시험 종료"}).status_code, 200)
            final = ws.receive_json()
            self.assertEqual((final["type"], final["lifecycleStatus"]), ("journey_ended", "cancelled"))
        self.assertEqual(self.query("SELECT next_recalc_at FROM app.journeys WHERE id = %s", (jid,)), [(None,)])


# ---------------------------------------------------------------- 6. 지원 범위(out_of_scope) — 합성 경계

OUTSIDE_ORIGIN = {"latitude": 36.30, "longitude": 127.30}        # 경계 밖, 반경 안 정류소 없음
OUTSIDE_DESTINATION = {"latitude": 36.40, "longitude": 127.45}   # 경계 밖, 반경 안 정류소 없음
OUT_STOP = "TST000OUT1"                                          # 정류소 목록에 없는 시외 경유 정류소(경계 밖)
NEAR_OUT_STOP = {"latitude": 36.3651, "longitude": 127.3925}     # OUT_STOP에서 약 45 m


class ServiceAreaTest(Phase4TestCase):
    def finish(self, search_id, provider="synthetic"):
        self.worker(synthetic_provider() if provider == "synthetic" else provider).run_jobs_pass()
        return self.client.get(f"/api/v1/route-searches/{search_id}", headers=self.auth(self.user_a))

    def submit_points(self, origin=None, destination=None):
        r = self.client.post("/api/v1/route-searches", headers=self.auth(self.user_a),
                             json={"origin": origin or ORIGIN, "destination": destination or DESTINATION})
        self.assertEqual(r.status_code, 202, r.text)
        return r.json()["searchId"]

    def add_intercity_route(self, active=True):
        world = w.world()
        if active:
            world["routes"]["TSTR000070"] = {
                "list": {"routeid": "TSTR000070", "routeno": "70", "routetp": "가상버스", "startnodenm": "가상F",
                         "endnodenm": "시외경유"},
                "info": {"routeid": "TSTR000070", "routeno": "70", "routetp": "가상버스"},
                "stops": [{"routeid": "TSTR000070", "nodeord": 1, "nodeid": w.F, "nodenm": "가상F",
                           "gpslati": w.STOPS[w.F][1], "gpslong": w.STOPS[w.F][2], "updowncd": 0},
                          {"routeid": "TSTR000070", "nodeord": 2, "nodeid": OUT_STOP, "nodenm": "시외경유",
                           "gpslati": 36.3650, "gpslong": 127.3920, "updowncd": 0}]}
        self.tago.world = world
        report = transit_service.StaticImportService(self.db, lambda **kw: self.tago_client(**kw), rows=100).run()
        self.assertEqual(report.status, "published", report.problems)

    def test_inside_edge_and_intercity_stop_continue_to_search(self):
        inside = self.finish(self.submit_points())
        self.assertEqual(inside.json()["status"], "ready")
        self.assertEqual({p["basis"] for p in inside.json()["serviceArea"]["points"]}, {"boundary"})
        edge = self.finish(self.submit_points(origin={"latitude": 36.3500, "longitude": SYNTH_WEST}))
        body = edge.json()
        self.assertEqual(body["status"], "ready", "경계선 위(ST_Covers) 출발지는 지원 범위")
        origin = next(p for p in body["serviceArea"]["points"] if p["role"] == "origin")
        self.assertEqual((origin["insideBoundary"], origin["basis"]), (True, "boundary"))
        self.add_intercity_route()
        stop_row = self.query("SELECT is_active FROM app.stops WHERE external_id = %s", (OUT_STOP,))
        self.assertEqual(stop_row, [(True,)], "정류소 목록에 없는 시외 경유 정류소도 활성 노선과 연결돼 보존")
        near = self.finish(self.submit_points(destination=NEAR_OUT_STOP)).json()
        self.assertNotEqual(near["status"], "out_of_scope")
        self.assertIn(near["status"], ("ready", "data_limited", "no_path"))
        dest = next(p for p in near["serviceArea"]["points"] if p["role"] == "destination")
        self.assertEqual((dest["insideBoundary"], dest["basis"], dest["nearestServedStop"]["name"]),
                         (False, "served_stop_within_radius", "시외경유"))
        self.assertIsNotNone(self.query("SELECT summary->'search_coverage' FROM app.route_searches "
                                        "WHERE id = %s", (near["searchId"],))[0][0], "기존 후보 생성으로 이어짐")
        self.add_intercity_route(active=False)  # 노선이 공개본에서 빠지면 그 정류소만으로는 지원 범위가 아님
        gone = self.finish(self.submit_points(destination=NEAR_OUT_STOP)).json()
        self.assertEqual((gone["status"], gone["reasonCodes"]), ("out_of_scope", ["destination_out_of_scope"]))

    def test_out_of_scope_points_end_without_candidates_or_external_calls(self):
        cases = [(OUTSIDE_ORIGIN, None, ["origin_out_of_scope"], ["origin"]),
                 (None, OUTSIDE_DESTINATION, ["destination_out_of_scope"], ["destination"]),
                 (OUTSIDE_ORIGIN, OUTSIDE_DESTINATION, ["origin_out_of_scope", "destination_out_of_scope"],
                  ["origin", "destination"])]
        for origin, destination, reasons, points in cases:
            with self.subTest(reasons=reasons):
                tago_before, tmap_before = self.tago.count(), len(self.tmap.requests)
                search_id = self.submit_points(origin, destination)
                accepted = self.client.get(f"/api/v1/route-searches/{search_id}", headers=self.auth(self.user_a))
                self.assertEqual(accepted.json()["status"], "processing")
                r = self.finish(search_id)
                self.assertEqual(r.status_code, 200)
                body = r.json()
                self.assertEqual((body["status"], body["candidates"], body["recommendedCandidateId"]),
                                 ("out_of_scope", [], None))
                self.assertEqual(body["reasonCodes"], reasons)
                self.assertEqual(body["serviceArea"]["outOfScopePoints"], points)
                self.assertIsNone(body["rankingMode"])
                self.assertEqual((self.tago.count(), len(self.tmap.requests)), (tago_before, tmap_before),
                                 "범위 밖 검색은 TAGO·TMAP을 호출하지 않음")
                summary = self.query("SELECT summary FROM app.route_searches WHERE id = %s", (search_id,))[0][0]
                self.assertNotIn("search_coverage", summary, "후보 생성(DFS)을 하지 않음")
                self.assertNotIn("realtime_targets", summary, "공유 수집 대상도 등록하지 않음")
                self.assertEqual(self.query("SELECT count(*) FROM app.route_candidates WHERE search_id = %s",
                                            (search_id,)), [(0,)])
        # 재처리: 확정된 검색은 다시 가져가거나 덮어쓰지 않는다.
        again = self.worker(synthetic_provider()).run_jobs_pass()
        self.assertEqual(again["searches"], [])
        workflow = flow.SearchWorkflow(self.db, flow.load_search_config(self.settings), tago_client=None,
                                       walking_client=None)
        with self.assertRaises(flow.SearchError):
            workflow.run_queued(UUID(search_id))
        self.assertEqual(self.query("SELECT status FROM app.route_searches WHERE id = %s", (search_id,)),
                         [("out_of_scope",)])
        cand = self.client.post("/api/v1/journeys", headers=self.auth(self.user_a),
                                json={"searchId": search_id, "candidateId": str(uuid4())})
        self.assertEqual(cand.status_code, 404)

    def test_no_path_inside_scope_and_data_limited_without_boundary(self):
        no_path = self.finish(self.submit_points(destination={"latitude": 36.374, "longitude": 127.387})).json()
        self.assertEqual((no_path["status"], no_path["candidates"]), ("no_path", []))
        self.assertEqual(no_path["serviceArea"]["status"], "in_scope")
        # 경계 자료가 없으면: 정류소로 확인되는 지점은 범위 안, 확인할 수 없는 지점은 data_limited.
        self.settings = Settings(_env_file=None, **SETTINGS,
                                 service_area_geojson=str(self.synthetic_boundary) + ".missing")
        main.app.state.settings = self.settings
        near_stops = self.finish(self.submit_points()).json()
        self.assertEqual(near_stops["status"], "ready")
        self.assertEqual({p["basis"] for p in near_stops["serviceArea"]["points"]}, {"served_stop_within_radius"})
        self.assertEqual(near_stops["serviceArea"]["boundary"],
                         {"available": False, "name": None, "reason": "boundary_file_missing"})
        tmap_before = len(self.tmap.requests)
        unknown = self.finish(self.submit_points(destination=OUTSIDE_DESTINATION)).json()
        self.assertEqual((unknown["status"], unknown["reasonCodes"], unknown["candidates"]),
                         ("data_limited", ["service_area_unavailable"], []))
        self.assertIsNone(unknown["rankingMode"])
        self.assertEqual(len(self.tmap.requests), tmap_before)
        invalid = Path(str(self.synthetic_boundary) + ".bad")
        invalid.write_text('{"type": "FeatureCollection", "features": []}', encoding="utf-8")
        self.settings = Settings(_env_file=None, **SETTINGS, service_area_geojson=str(invalid))
        broken = self.finish(self.submit_points(destination=OUTSIDE_DESTINATION)).json()
        self.assertEqual((broken["status"], broken["serviceArea"]["boundary"]["reason"]),
                         ("data_limited", "boundary_file_invalid"))


# ---------------------------------------------------------------- 7. 지원 범위 — 공식 대전광역시 경계

class OfficialBoundaryTest(Phase4TestCase):
    def boundary_setting(self):
        return {}  # 저장소 기본 파일 data/service_area/daejeon.geojson

    def test_official_boundary_points_and_app_flow(self):
        state = service_area.load_boundary(None)
        self.assertIsNotNone(state.boundary, state.reason)
        self.assertEqual(state.boundary.name, "대전광역시")
        geometry = json.loads(state.boundary.geometry_json)
        self.assertEqual(geometry["type"], "MultiPolygon")
        vertex = geometry["coordinates"][0][0][0]
        version = self.query("SELECT DISTINCT data_version FROM app.routes WHERE is_active")[0][0]
        with self.db.transaction() as conn:
            valid = conn.execute("SELECT extensions.ST_IsValid(extensions.ST_GeomFromGeoJSON(%s)), "
                                 "extensions.ST_GeometryType(extensions.ST_GeomFromGeoJSON(%s))",
                                 (state.boundary.geometry_json, state.boundary.geometry_json)).fetchone()
            decision = service_area.assess(conn, state, version, [
                ("origin", 36.3504, 127.3845, 300.0), ("destination", 37.5, 127.0, 300.0)])
            edge = service_area.assess(conn, state, version, [
                ("origin", vertex[1], vertex[0], 300.0), ("destination", 36.3504, 127.3845, 300.0)])
            contains = conn.execute(
                "SELECT extensions.ST_Contains(g, p), extensions.ST_Covers(g, p) FROM (SELECT "
                "extensions.ST_SetSRID(extensions.ST_GeomFromGeoJSON(%s), 4326) g, "
                "extensions.ST_SetSRID(extensions.ST_MakePoint(%s, %s), 4326) p) x",
                (state.boundary.geometry_json, vertex[0], vertex[1])).fetchone()
        self.assertEqual(valid, (True, "ST_MultiPolygon"))
        inside, outside = decision.points
        self.assertEqual((inside.inside_boundary, inside.status), (True, "in_scope"))
        self.assertEqual((outside.inside_boundary, outside.served_stops, outside.status), (False, 0, "out_of_scope"))
        self.assertEqual(decision.reason_codes, ["destination_out_of_scope"])
        self.assertEqual((edge.points[0].inside_boundary, edge.status), (True, "in_scope"))
        self.assertEqual(contains, (False, True), "경계선 위 꼭짓점: Contains는 거짓, Covers는 참")

        ready = self.client.post("/api/v1/route-searches", headers=self.auth(self.user_a),
                                 json={"origin": ORIGIN, "destination": DESTINATION}).json()["searchId"]
        seoul = self.client.post("/api/v1/route-searches", headers=self.auth(self.user_a),
                                 json={"origin": ORIGIN, "destination": {"latitude": 37.5, "longitude": 127.0}}
                                 ).json()["searchId"]
        self.worker(synthetic_provider()).run_jobs_pass()
        a = self.client.get(f"/api/v1/route-searches/{ready}", headers=self.auth(self.user_a)).json()
        b = self.client.get(f"/api/v1/route-searches/{seoul}", headers=self.auth(self.user_a)).json()
        self.assertEqual(a["status"], "ready")
        self.assertEqual((b["status"], b["reasonCodes"], b["candidates"], b["recommendedCandidateId"]),
                         ("out_of_scope", ["destination_out_of_scope"], [], None))

# ---------------------------------------------------------------- 8. 화면 조회 기반 실시간 수집 (Phase 4A 후속 보완)

class ViewTargetTest(Phase4TestCase):
    def ids(self):
        route = str(self.query("SELECT id FROM app.routes WHERE external_id = 'TSTR000010'")[0][0])
        stop = str(self.query("SELECT id FROM app.stops WHERE external_id = %s", (w.A,))[0][0])
        return route, stop

    def keys(self):
        return (transit_service.realtime_target_key(RealtimeTarget("positions", "TSTR000010")),
                transit_service.realtime_target_key(RealtimeTarget("arrivals", w.A)))

    def views(self):
        return dict(self.query("SELECT target_key, expires_at FROM app.realtime_view_targets"))

    def targets(self, now=None):
        conn = self.db.connect()
        try:
            return {transit_service.realtime_target_key(t): t
                    for t in collection_targets(self.settings, conn, now or datetime.now(UTC))}
        finally:
            conn.close()

    def calls(self, operation):
        return self.tago.count(operation)

    def test_first_view_registers_then_worker_publishes(self):
        route_id, stop_id = self.ids()
        pos_key, arr_key = self.keys()
        h = self.auth(self.user_a)
        before = datetime.now(UTC)
        first = self.client.get(f"/api/v1/routes/{route_id}/positions", headers=h).json()
        self.assertEqual((first["items"], first["dataStatus"]["availability"]), ([], "unavailable"))
        arrivals = self.client.get(f"/api/v1/stops/{stop_id}/arrivals", headers=h).json()
        self.assertEqual(arrivals["dataStatus"]["availability"], "unavailable")
        views = self.views()
        self.assertEqual(set(views), {pos_key, arr_key}, "응답 전에 공유 저장소에 등록")
        ttl = (views[pos_key] - before).total_seconds()
        self.assertTrue(1790 <= ttl <= 1830, ttl)
        self.assertEqual(self.calls("getRouteAcctoBusLcList") + self.calls("getSttnAcctoArvlPrearngeInfoList"), 0,
                         "API는 TAGO 실시간 조회를 호출하지 않음")
        self.assertEqual(self.targets()[pos_key].sources, frozenset({"realtime_view"}))
        result = self.worker().run_pass()  # 별도 워커 루프가 합성 제공처로 최초 수집
        actions = {o["target_key"]: o["action"] for o in result.outcomes}
        self.assertEqual((actions[pos_key], actions[arr_key]), ("published", "published"))
        self.assertEqual(self.client.get(f"/api/v1/routes/{route_id}/positions", headers=h).json()
                         ["dataStatus"]["availability"], "available")
        got = self.client.get(f"/api/v1/stops/{stop_id}/arrivals", headers=h).json()
        self.assertEqual(got["dataStatus"]["availability"], "available")
        self.assertTrue(got["items"])
        positions, arrival_calls = (self.calls("getRouteAcctoBusLcList"),
                                    self.calls("getSttnAcctoArvlPrearngeInfoList"))
        for _ in range(3):  # 최신 공개본이 유효한 동안 재조회·재실행이 외부 호출을 늘리지 않음
            self.client.get(f"/api/v1/routes/{route_id}/positions", headers=h)
            self.client.get(f"/api/v1/stops/{stop_id}/arrivals", headers=h)
        self.worker().run_pass()
        self.assertEqual((self.calls("getRouteAcctoBusLcList"), self.calls("getSttnAcctoArvlPrearngeInfoList")),
                         (positions, arrival_calls))

    def test_concurrent_views_and_overlap_with_search_share_one_key(self):
        route_id, _ = self.ids()
        pos_key, _ = self.keys()
        from app.modules.transit import repository as transit_repository

        def register(_):
            with self.db.transaction() as conn:
                return transit_repository.register_view_target(conn, pos_key, 1800)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(register, range(16)))
        self.client.get(f"/api/v1/routes/{route_id}/positions", headers=self.auth(self.user_a))
        self.assertEqual(self.query("SELECT count(*) FROM app.realtime_view_targets"), [(1,)], "동시 조회도 한 행")
        search_id = self.submit()
        self.worker(synthetic_provider()).run_jobs_pass()  # 검색이 같은 노선을 수집 대상으로 등록·검색 시점 수집
        summary = self.query("SELECT summary FROM app.route_searches WHERE id = %s", (search_id,))[0][0]
        self.assertIn("TSTR000010", summary["realtime_targets"]["positions"])
        merged = self.targets()[pos_key]
        self.assertIn("realtime_view", merged.sources)
        self.assertTrue(any(s.startswith("route_search:") for s in merged.sources), "같은 수집 키로 합쳐짐")
        calls = self.calls("getRouteAcctoBusLcList")
        self.worker().run_pass()
        self.worker().run_pass()
        self.assertEqual(self.calls("getRouteAcctoBusLcList"), calls, "겹친 수요도 갱신 주기 안에 호출을 늘리지 않음")

    def test_ttl_extension_expiry_and_remaining_demand(self):
        route_id, stop_id = self.ids()
        pos_key, arr_key = self.keys()
        h = self.auth(self.user_a)
        self.client.get(f"/api/v1/routes/{route_id}/positions", headers=h)
        self.query("UPDATE app.realtime_view_targets SET expires_at = now() + interval '60 seconds' "
                   "WHERE target_key = %s RETURNING 1", (pos_key,))
        self.client.get(f"/api/v1/routes/{route_id}/positions", headers=h)
        extended = (self.views()[pos_key] - datetime.now(UTC)).total_seconds()
        self.assertGreater(extended, 1700, "재조회가 마지막 조회 시점부터 30분으로 연장")
        self.query("UPDATE app.realtime_view_targets SET expires_at = now() + interval '5000 seconds' "
                   "WHERE target_key = %s RETURNING 1", (pos_key,))
        self.client.get(f"/api/v1/routes/{route_id}/positions", headers=h)
        self.assertGreater((self.views()[pos_key] - datetime.now(UTC)).total_seconds(), 4900, "만료를 앞당기지 않음")
        # 만료: 다른 수요가 없으면 집계에서 빠지고 워커가 행을 정리한다. 공개본 조회는 계속 가능하다.
        self.worker().run_pass()
        self.query("UPDATE app.realtime_view_targets SET expires_at = now() - interval '1 second' RETURNING 1")
        self.assertNotIn(pos_key, self.targets())
        after = self.worker().run_pass()
        self.assertEqual((after.targets, after.outcomes), (0, []))
        self.assertEqual(self.views(), {})
        self.assertEqual(self.client.get(f"/api/v1/routes/{route_id}/positions", headers=h).status_code, 200)
        self.query("DELETE FROM app.realtime_view_targets RETURNING 1")
        # 검색 수요가 남은 대상은 화면 수요가 만료돼도 수집이 계속된다.
        self.submit()
        self.worker(synthetic_provider()).run_jobs_pass()
        self.client.get(f"/api/v1/stops/{stop_id}/arrivals", headers=h)
        self.query("UPDATE app.realtime_view_targets SET expires_at = now() - interval '1 second' RETURNING 1")
        remaining = self.targets()
        self.assertIn(arr_key, remaining)
        self.assertNotIn("realtime_view", remaining[arr_key].sources)
        self.assertTrue(any(s.startswith("route_search:") for s in remaining[arr_key].sources))
        clock = datetime.now(UTC) + timedelta(hours=1)  # 검색 유효기간(30분)도 지나면 다른 수요가 없어 해제
        self.assertNotIn(arr_key, self.targets(clock))

    def test_rejected_requests_do_not_register(self):
        route_id, stop_id = self.ids()
        self.assertEqual(self.client.get(f"/api/v1/routes/{route_id}/positions").status_code, 401)
        self.assertEqual(self.client.get(f"/api/v1/stops/{stop_id}/arrivals",
                                         headers={"Authorization": "Bearer bad.token.x"}).status_code, 401)
        h = self.auth(self.user_a)
        self.assertEqual(self.client.get("/api/v1/routes/abc/positions", headers=h).status_code, 422)
        self.assertEqual(self.client.get("/api/v1/routes/999999/positions", headers=h).status_code, 404)
        self.assertEqual(self.client.get("/api/v1/stops/999999/arrivals", headers=h).status_code, 404)
        self.assertEqual(self.views(), {}, "인증 실패·잘못된 ID는 등록하지 않음")
        f_id = str(self.query("SELECT id FROM app.stops WHERE external_id = %s", (w.F,))[0][0])
        self.query("UPDATE app.stops SET is_active = false WHERE external_id = %s RETURNING 1", (w.F,))
        inactive = self.client.get(f"/api/v1/stops/{f_id}/arrivals", headers=h)
        self.assertEqual((inactive.status_code, inactive.json()["dataStatus"]["availability"]), (200, "unavailable"))
        self.assertEqual(self.views(), {}, "비활성 대상은 기존 응답을 주되 수요로 등록하지 않음")
        self.client.get(f"/api/v1/stops/{stop_id}/arrivals", headers=h)
        _, arr_key = self.keys()
        self.query("UPDATE app.realtime_view_targets SET expires_at = now() + interval '60 seconds' RETURNING 1")
        self.query("UPDATE app.stops SET is_active = false WHERE external_id = %s RETURNING 1", (w.A,))
        self.client.get(f"/api/v1/stops/{stop_id}/arrivals", headers=h)
        self.assertLess((self.views()[arr_key] - datetime.now(UTC)).total_seconds(), 120, "비활성 조회는 연장하지 않음")

if __name__ == "__main__":
    unittest.main()
