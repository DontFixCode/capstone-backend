"""Phase 3 검색 흐름의 PostgreSQL+PostGIS 격리 시험(정적 적재 → 검색 → TMAP → 실시간 공유 수집 → 평가·추천 → 저장).

대상 DB가 아니라 TEST_POSTGIS_DSN의 임시 서버에서 시험마다 새 데이터베이스를 만들고, 대상 DB에 적용된 DDL 사본
migrations/01~08과 실제 DB에만 있는 route_searches.journey_id FK를 적용한다. TAGO·TMAP 응답은 합성 데이터다.

    $env:TEST_POSTGIS_DSN = "postgresql://postgres:...@127.0.0.1:55433/postgres"
    uv run python -m unittest discover -s tests/integration
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import psycopg

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

import phase3_world as w  # noqa: E402
from tago_world import FAKE_KEY, FakeTago  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.db.session import Database, DbParams  # noqa: E402
from app.integrations.bus_api import TagoClient  # noqa: E402
from app.integrations.walking_api import TmapWalkingClient  # noqa: E402
from app.modules.evaluation.schemas import EtaReport  # noqa: E402
from app.modules.routing import repository as search_repository  # noqa: E402
from app.modules.transit import service as transit_service  # noqa: E402
from app.workers.main import Worker  # noqa: E402
from app.workflows import search as flow  # noqa: E402
from app.workflows.labeling import label_due  # noqa: E402

DSN = os.environ.get("TEST_POSTGIS_DSN")
UTC = timezone.utc
SETTINGS = dict(
    search_max_boardings=3, search_access_radius_meters=300, search_transfer_radius_meters=150,
    search_egress_radius_meters=300, search_max_candidates=50, search_max_states=100000, search_budget_ms=60000,
    eval_search_horizon_seconds=3600, eval_budget_ms=60000, eval_max_timed_candidates=1000,
    eval_boarding_buffer_seconds=60, eval_max_data_age_seconds=900, eval_min_segment_samples=1,
    eval_min_probability_samples=1, eval_min_joint_scenes=1, eval_min_improvement_seconds=120,
    eval_max_horizon_difference_seconds=300, eval_max_recovery_attempts=2, eval_recovery_search_enabled=False,
    eval_timing_policy_version="timing-v1", eval_sample_policy_version="sample-v1",
    eval_recovery_policy_version="recovery-v1", eval_segment_model_version="segment-v1",
    search_expires_seconds=1800, tmap_search_option="0", tmap_max_lookups_per_search=60,
    sample_label_deadline_seconds=7200,
)
REQUEST = flow.SearchRequest(flow.PlaceInput(*w.ORIGIN, name="합성 출발지"), flow.PlaceInput(*w.DESTINATION))


def ddl_text(path: Path) -> str:
    return "\n".join(line for line in path.read_text(encoding="utf-8").splitlines() if not line.startswith("\\"))


@unittest.skipUnless(DSN, "TEST_POSTGIS_DSN이 없어 DB 격리 시험을 건너뜀")
class SearchDbTestCase(unittest.TestCase):
    def setUp(self):
        url = urlparse(DSN)
        self.dbname = f"phase3_{uuid4().hex[:12]}"
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
            # 실제 DB 카탈로그에 있으나 DDL 파일에는 없는 FK(2026-11-07 확인).
            conn.execute("ALTER TABLE app.route_searches ADD CONSTRAINT route_searches_journey_id_fkey "
                         "FOREIGN KEY (journey_id) REFERENCES app.journeys(id) ON DELETE CASCADE")
        self.db = Database(DbParams(url.hostname, url.port or 5432, self.dbname, url.username, url.password or "", 10))
        self.tago = FakeTago(w.world())
        self.tmap = w.FakeTmap()
        report = transit_service.StaticImportService(self.db, lambda **kw: self.tago_client(**kw), rows=100).run()
        self.assertEqual(report.status, "published", report.problems)
        self.static_version = report.data_version
        self.config = flow.load_search_config(Settings(_env_file=None, **SETTINGS))

    def _drop(self):
        self.admin.execute(f'DROP DATABASE IF EXISTS "{self.dbname}" WITH (FORCE)')
        self.admin.close()

    def tago_client(self, **kw):
        kw.setdefault("max_retries", 0)
        return TagoClient(FAKE_KEY, transport=self.tago.transport(), request_interval=0, retry_backoff=0,
                          sleep=lambda _s: None, **kw)

    def workflow(self, *, provider=None, config=None, collect=True, clock=None):
        return flow.SearchWorkflow(
            self.db, config or self.config, tago_client=self.tago_client(),
            walking_client=TmapWalkingClient(self.tmap.app_key, transport=self.tmap.transport()),
            clock=clock or (lambda: datetime.now(UTC)), realtime_provider=provider, collect=collect)

    def query(self, sql, params=None):
        with self.db.transaction() as conn:
            return conn.execute(sql, params).fetchall()

    def saved(self, search_id):
        with self.db.transaction() as conn:
            return search_repository.read_search(conn, search_id), search_repository.read_candidates(conn, search_id)


def synthetic_provider(basis_at=None, **kwargs):
    """기준시각·차량·회차가 확인된 제공 ETA를 주는 합성 입력(실제 TAGO 기본 도착정보는 기준시각·차량이 없다).
    basis_at을 고정하면 같은 입력 수집 배치를 다시 평가하는 경우가 된다."""
    def provide(conn, targets, network, now):
        rows = w.synthetic_eta_rows(network.patterns, basis_at=basis_at or now - timedelta(seconds=30), **kwargs)
        return flow.RealtimeInputs([EtaReport(**row) for row in rows], frozenset(w.STOPS), {},
                                   [("synthetic", "fixture")], {})
    return provide


class SearchFlowTest(SearchDbTestCase):
    def test_tago_path_saves_limited_candidates_and_reuses_shared_batches(self):
        outcome = self.workflow().run(REQUEST)
        self.assertEqual(outcome.status, "data_limited")
        search, rows = self.saved(outcome.search_id)
        self.assertEqual((search["source"], search["user_id"], search["journey_id"]), ("cli", None, None))
        self.assertEqual(search["static_version"], self.static_version)
        self.assertEqual(search["ranking_mode"], "nominal")
        self.assertEqual(search["place_info"]["origin"]["name"], "합성 출발지")
        self.assertEqual(search["params"]["evaluation_policy"]["max_boardings"], 3)
        self.assertGreater(search["expires_at"], search["created_at"])
        targets = search["summary"]["realtime_targets"]
        self.assertTrue(targets["arrivals"] and targets["positions"])
        self.assertEqual(len(rows), outcome.summary["counts"]["timed_candidates"])
        self.assertTrue(rows and all(r["rank_no"] is None and r["ranking_mode"] is None for r in rows))
        reasons = [set(r["evaluation"]["reason_codes"]) for r in rows]
        eta_reasons = {"eta_basis_unconfirmed", "eta_position_unconfirmed"}
        self.assertTrue(all(reason & eta_reasons for reason in reasons),
                        [sorted(r) for r in reasons if not r & eta_reasons][:3])
        self.assertTrue(any("eta_basis_unconfirmed" in r for r in reasons))
        self.assertTrue(all(r["time_status"] != "available" for r in rows))
        walk = next(leg for leg in rows[0]["legs"] if leg["kind"] == "walk" and leg["status"] == "available")
        self.assertEqual(walk["provider"], "TMAP")
        self.assertGreater(walk["distance_m"], 0)
        self.assertEqual(walk["geometry"]["type"], "LineString")
        self.assertEqual(len(self.tmap.requests), outcome.summary["walk_lookups"]["tmap_calls"])
        self.assertEqual(len(self.tmap.requests), outcome.summary["walk_lookups"]["pairs"], "구간마다 한 번")
        states = search["summary"]["data_inputs"]
        self.assertTrue(all(s["state"] == "fresh" for s in states.values()))
        tago_calls = self.tago.count()
        second = self.workflow().run(REQUEST)
        self.assertEqual(self.tago.count(), tago_calls, "600초 이내 공개 배치를 재사용해 TAGO를 다시 부르지 않음")
        actions = {v["action"] for v in second.summary["realtime_collection"].values()}
        self.assertEqual(actions, {"reused"})
        self.assertEqual(self.query("SELECT count(*) FROM app.route_searches"), [(2,)])

    def test_ranked_candidates_round_trip_and_samples_reused(self):
        batch_at = datetime.now(UTC) - timedelta(seconds=30)
        outcome = self.workflow(provider=synthetic_provider(batch_at)).run(REQUEST)
        self.assertEqual((outcome.status, outcome.ranking_mode), ("ready", "nominal"))
        search, rows = self.saved(outcome.search_id)
        ranked = [r for r in rows if r["rank_no"] is not None]
        self.assertEqual([r["rank_no"] for r in ranked], list(range(1, len(ranked) + 1)))
        self.assertTrue(all(r["ranking_mode"] == search["ranking_mode"] for r in ranked))
        totals = [r["nominal_total_seconds"] for r in ranked]
        self.assertEqual(totals, sorted(totals))
        self.assertTrue(all(r["nominally_feasible"] and r["time_status"] == "available" for r in ranked))
        top = ranked[0]
        self.assertEqual(top["evaluation"]["ranking"]["rank"], 1)
        bus = next(leg for leg in top["legs"] if leg["kind"] == "bus")
        self.assertIsNotNone(bus["board_eta"]["predicted_arrival_at"])
        self.assertEqual(bus["vehicle"]["match_status"], "confirmed")
        limited = [r for r in rows if r["rank_no"] is None]
        self.assertTrue(all(r["ranking_mode"] is None for r in limited))
        by_static = {}
        for r in rows:
            by_static.setdefault(r["static_route_key"], set()).add(r["option_key"])
        self.assertGreater(max(len(v) for v in by_static.values()), 1, "정적 경로별 복수 차량 조합 저장")
        samples = self.query("SELECT count(*), count(DISTINCT sample_key) FROM app.transfer_samples")[0]
        scenes = self.query("SELECT count(*) FROM app.journey_scenes")[0][0]
        self.assertGreater(samples[0], 0)
        self.assertGreater(scenes, 0)
        transfer = next(t for r in rows for t in r["evaluation"]["transfers"] if t["sample_id"])
        self.assertTrue(self.query("SELECT 1 FROM app.transfer_samples WHERE id = %s", (transfer["sample_id"],)))
        # 같은 입력 배치를 1분 뒤 다시 평가: 평가 시각만 다르므로 표본·묶음을 새로 만들지 않고 재사용한다.
        again = self.workflow(provider=synthetic_provider(batch_at),
                              clock=lambda: datetime.now(UTC) + timedelta(minutes=1)).run(REQUEST)
        self.assertEqual(again.status, "ready")
        self.assertEqual(self.query("SELECT count(*) FROM app.transfer_samples")[0][0], samples[0],
                         "같은 조건의 표본은 새로 만들지 않고 재사용")
        self.assertEqual(self.query("SELECT count(*) FROM app.journey_scenes")[0][0], scenes)
        reused = {t["sample_id"] for r in self.saved(again.search_id)[1] for t in r["evaluation"]["transfers"]
                  if t["sample_id"]}
        first_ids = {t["sample_id"] for r in rows for t in r["evaluation"]["transfers"] if t["sample_id"]}
        self.assertEqual(reused, first_ids)
        # 새 입력 배치(다른 기준시각)는 새 예측 정보시점이므로 새 표본이 된다.
        self.workflow(provider=synthetic_provider()).run(REQUEST)
        self.assertGreater(self.query("SELECT count(*) FROM app.transfer_samples")[0][0], samples[0])
        personal = self.query("SELECT count(*) FROM app.transfer_samples WHERE prediction_context::text LIKE %s",
                              (f"%{w.ORIGIN[0]}%",))
        self.assertEqual(personal, [(0,)], "개인 출발지 좌표를 공통 표본에 넣지 않음")

    def test_missing_alight_eta_is_saved_as_partial(self):
        outcome = self.workflow(provider=synthetic_provider(with_alight=False)).run(REQUEST)
        self.assertEqual(outcome.status, "data_limited")
        _, rows = self.saved(outcome.search_id)
        partial = [r for r in rows if r["time_status"] == "partial"]
        self.assertTrue(partial)
        bus = next(leg for leg in partial[0]["legs"] if leg["kind"] == "bus")
        self.assertIsNotNone(bus["board_eta"])
        self.assertIsNone(bus["alight_eta"])
        self.assertIsNone(partial[0]["nominal_total_seconds"])

    def test_walk_failure_and_lookup_limit_are_preserved(self):
        self.tmap.fail_pairs.add((w.STOPS[w.C][1:], w.STOPS[w.C2][1:]))
        outcome = self.workflow(provider=synthetic_provider()).run(REQUEST)
        _, rows = self.saved(outcome.search_id)
        via_c2 = [r for r in rows if any(leg.get("to", {}) and leg["kind"] == "walk"
                                         and leg["to"].get("ref") == w.C2 for leg in r["legs"])]
        self.assertTrue(via_c2)
        for row in via_c2:
            leg = next(leg for leg in row["legs"] if leg["kind"] == "walk" and leg["to"].get("ref") == w.C2)
            self.assertEqual((leg["status"], leg["duration_seconds"]), ("error", None))
            self.assertIsNone(row["rank_no"], "조회 실패 도보는 0초로 채워 순위에 넣지 않음")
            self.assertIn("walk_lookup_failed", row["evaluation"]["reason_codes"])
        limited_cfg = flow.load_search_config(Settings(_env_file=None, **{**SETTINGS, "tmap_max_lookups_per_search": 1}))
        small = self.workflow(provider=synthetic_provider(), config=limited_cfg).run(REQUEST)
        _, rows = self.saved(small.search_id)
        statuses = {leg["status"] for r in rows for leg in r["legs"] if leg["kind"] == "walk"}
        self.assertIn("unknown", statuses)
        self.assertEqual(small.summary["walk_lookups"]["tmap_calls"], 1)

    def test_static_version_stays_consistent_when_publish_happens_mid_search(self):
        base = synthetic_provider()

        def publish_then_provide(conn, targets, network, now):
            changed = w.world()
            del changed["routes"]["TSTR000010"]
            self.tago.world = changed
            report = transit_service.StaticImportService(self.db, lambda **kw: self.tago_client(**kw), rows=100).run()
            self.assertEqual(report.status, "published")
            return base(conn, targets, network, now)

        outcome = self.workflow(provider=publish_then_provide).run(REQUEST)
        search, rows = self.saved(outcome.search_id)
        self.assertEqual(search["static_version"], self.static_version, "검색은 시작한 정적 공개본 하나만 사용")
        self.assertNotEqual(self.query("SELECT DISTINCT data_version FROM app.routes WHERE is_active")[0][0],
                            self.static_version)
        routes = {leg["route_id"] for r in rows for leg in r["legs"] if leg["kind"] == "bus"}
        self.assertIn("TSTR000010", routes, "검색 도중 새 공개본이 생겨도 같은 검색의 경로 구성은 섞이지 않음")

    def test_failure_marks_search_error(self):
        def broken(conn, targets, network, now):
            raise RuntimeError("synthetic input failure")

        with self.assertRaises(RuntimeError):
            self.workflow(provider=broken).run(REQUEST)
        status, summary = self.query("SELECT status, summary FROM app.route_searches")[0]
        self.assertEqual(status, "error")
        self.assertIn("synthetic input failure", summary["error"])
        self.assertEqual(self.query("SELECT count(*) FROM app.route_candidates"), [(0,)])


class HistoryChainTest(SearchDbTestCase):
    def test_labeled_history_enables_probabilities_and_expected_mode(self):
        first = self.workflow(provider=synthetic_provider()).run(REQUEST)
        self.assertEqual(first.ranking_mode, "nominal")
        # 합성 사후 관측: 묶음의 모든 예측 위치·회차에 예측과 같은 시각의 도착 점 이벤트를 넣는다.
        with self.db.transaction() as conn:
            for (predictions,) in conn.execute("SELECT predictions FROM app.journey_scenes").fetchall():
                for p in predictions:
                    for side in ("board", "alight"):
                        ctx = p[side]
                        conn.execute(
                            "INSERT INTO app.transit_observations (kind, event_type, quality, event_key, event_at, "
                            "collected_at, pattern_key, stop_seq, run_key) VALUES ('event', 'arrival', 'exact', %s, "
                            "%s, now(), %s, %s, %s) ON CONFLICT (event_key) WHERE kind = 'event' DO NOTHING",
                            (f"{ctx['pattern_id']}|{ctx['stop_order']}|{ctx['run_key']}|arrival",
                             ctx["predicted_arrival_at"], ctx["pattern_id"], ctx["stop_order"], ctx["run_key"]))
            counts = label_due(conn, datetime.now(UTC))
        self.assertGreater(counts["samples_labeled"], 0)
        self.assertGreater(counts["scenes_complete"], 0)
        self.assertEqual(self.query("SELECT count(*) FROM app.transfer_samples WHERE label_status = 'labeled' "
                                    "AND time_feasible IS NOT NULL"), [(counts["samples_labeled"],)])
        second = self.workflow(provider=synthetic_provider()).run(REQUEST)
        _, rows = self.saved(second.search_id)
        self.assertEqual(second.ranking_mode, "expected", second.recommendation.mode_reason_codes)
        ranked = [r for r in rows if r["rank_no"] is not None]
        self.assertTrue(all(r["ranking_mode"] == "expected" and r["expected_total_seconds"] is not None
                            for r in ranked))
        with_transfer = [r for r in ranked if r["boarding_count"] > 1]
        self.assertTrue(any(t["probability"] is not None for r in with_transfer for t in r["evaluation"]["transfers"]))
        self.assertTrue(all(r["journey_probability"] is not None for r in ranked))
        history = second.summary["history_inputs"]
        self.assertGreater(history["transfer_error_samples"], 0)
        self.assertGreater(history["joint_scenes"], 0)

    def test_pending_samples_expire_to_insufficient(self):
        self.workflow(provider=synthetic_provider()).run(REQUEST)
        with self.db.transaction() as conn:
            before = label_due(conn, datetime.now(UTC))
        self.assertEqual((before["samples_labeled"], before["samples_insufficient"]), (0, 0))
        with self.db.transaction() as conn:
            after = label_due(conn, datetime.now(UTC) + timedelta(hours=3))
        self.assertGreater(after["samples_insufficient"], 0)
        self.assertEqual(self.query("SELECT count(*) FROM app.transfer_samples WHERE label_status = 'pending'"),
                         [(0,)])


class WorkerTargetsTest(SearchDbTestCase):
    def test_worker_collects_search_targets_until_expiry(self):
        outcome = self.workflow(collect=False).run(REQUEST)
        expires = self.saved(outcome.search_id)[0]["expires_at"]
        clock = {"now": datetime.now(UTC)}
        worker = Worker(settings_factory=lambda: Settings(_env_file=None), db_factory=lambda _s: self.db,
                        client_factory=lambda _s, **kw: self.tago_client(**kw), clock=lambda: clock["now"],
                        static_enabled=False)
        first = worker.run_pass()
        expected = sum(len(v) for v in outcome.summary["realtime_targets"].values())
        self.assertEqual(first.targets, expected)
        self.assertEqual(len(first.outcomes), expected, "등록된 검색 대상만 최초 조회")
        clock["now"] = expires + timedelta(seconds=1)
        after = worker.run_pass()
        self.assertEqual((after.targets, after.outcomes), (0, []), "유효기간이 지나면 수집 대상에서 해제")


if __name__ == "__main__":
    unittest.main()
