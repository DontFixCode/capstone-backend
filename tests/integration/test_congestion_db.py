"""예상 혼잡도(Phase 8) 격리 시험: 임시 PostgreSQL/PostGIS에서 DDL 제약 → 원본 입력·재입력 → 집계 교체 → 검색·도착 응답.

    $env:TEST_POSTGIS_DSN = "postgresql://postgres:...@127.0.0.1:55433/postgres"
    uv run python -m unittest tests.integration.test_congestion_db

정적·실시간 값은 Phase 3·4 합성 자료이고, 승하차 파일은 플랫폼 원본 형식을 줄여 만든 합성 값이다.
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "integration"))
sys.path.insert(0, str(ROOT / "tests" / "unit"))

import test_phase4_api as base  # noqa: E402
from test_congestion import export_html  # noqa: E402

from app import main  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.modules.congestion import equilibrium, levels, service  # noqa: E402
from app.workers.collection import collection_targets  # noqa: E402

UTC = timezone.utc
KST = timezone(timedelta(hours=9))
w = base.w
ROUTE_10 = "TSTR000010"
# 합성 노선 10: A → B → C → D → E. 정류소 번호는 합성 정적 자료의 nodeno(90000 + 등록 순서).
STOPS_10 = [("가상A", "90000"), ("가상B", "90001"), ("가상C", "90002"), ("가상D", "90004"), ("가상E", "90005")]
# 정류소별 (단순승차, 하차, 환승승차). 총승차 300, 하차 태그 200 → α = 1.5. 구간 흐름: 150, 240, 150, 0, 0.
COUNTS = [(150, 0, 0), (120, 20, 0), (0, 60, 0), (20, 120, 10), (0, 0, 0)]
CONGESTION = dict(congestion_window_months=3, congestion_min_sample_days=2, congestion_seats=25,
                  congestion_capacity=50)


def files_for(days, *, counts=COUNTS, prefix="ridership"):
    """날짜마다 0~23시 파일 하나씩(플랫폼은 조회 조건의 시간대 하나를 내려준다)."""
    label = f"10번({STOPS_10[0][0]}~{STOPS_10[-1][0]})"
    return [(f"{prefix}-{hour:02d}.xls",
             export_html([(f"{d.year}년 {d.month:02d}월 {d.day:02d}일", counts) for d in days], hour=f"{hour:02d}:00",
                         route=label, stops=STOPS_10))
            for hour in range(24)]


class CongestionFlowTest(base.Phase4TestCase):
    def use_settings(self, **extra):
        self.settings = Settings(_env_file=None, **base.SETTINGS, **self.boundary_setting(), **extra)
        main.app.state.settings = self.settings

    def test_import_aggregate_and_expose_in_search_and_arrivals(self):
        route_id = self.query("SELECT id FROM app.routes WHERE external_id = %s", (ROUTE_10,))[0][0]
        # 합성 노선 정보에는 평일 배차간격(10분)만 있다. 요일과 무관하게 시험하도록 토·일 값도 넣는다.
        self.query("UPDATE app.routes SET service_info = jsonb_set(jsonb_set(service_info, "
                   "'{fields,intervalsattime}', service_info #> '{fields,intervaltime}'), "
                   "'{fields,intervalsuntime}', service_info #> '{fields,intervaltime}') WHERE id = %s RETURNING 1",
                   (route_id,))
        today = datetime.now(KST).date()
        days = [today - timedelta(days=n) for n in range(14, 0, -1)]     # 지난 14일(모든 요일유형 포함)

        # ---- 제약: 범위 밖 시간대, 음수 인원, 통과 횟수와 근거의 한쪽만 NULL, 허용값 밖 요일유형
        bad = [
            ("INSERT INTO app.ridership_counts (route_id, service_date, hour, source_stop_order, board_count, "
             "alight_count, transfer_board_count, source_file) VALUES (%s, current_date, 24, 1, 0, 0, 0, 'x')"),
            ("INSERT INTO app.ridership_counts (route_id, service_date, hour, source_stop_order, board_count, "
             "alight_count, transfer_board_count, source_file) VALUES (%s, current_date, 9, 1, -1, 0, 0, 'x')"),
            ("INSERT INTO app.congestion_profiles (route_id, pattern_key, stop_seq, day_type, hour, flow_mean, "
             "trips_per_hour, sample_days, source_period) VALUES (%s, 'p', 1, 'weekday', 9, 1, 6, 1, 'a/b')"),
            ("INSERT INTO app.congestion_profiles (route_id, pattern_key, stop_seq, day_type, hour, flow_mean, "
             "sample_days, source_period) VALUES (%s, 'p', 1, 'sunday', 9, 1, 1, 'a/b')"),
            ("INSERT INTO app.congestion_profiles (route_id, pattern_key, stop_seq, day_type, hour, flow_mean, "
             "sample_days, source_period) VALUES (%s, 'p', 1, 'weekday', 9, 'NaN', 1, 'a/b')"),
        ]
        for sql in bad:
            with self.subTest(sql=sql[:70]), self.assertRaises(psycopg.errors.CheckViolation):
                self.query(sql, (route_id,))

        # ---- 입력: 미리 보기는 저장하지 않고, 실행하면 누적한다. 같은 자료를 다시 올려도 행이 늘지 않는다.
        files = files_for(days)
        planned = service.import_files(self.db, files)
        self.assertEqual({f["status"] for f in planned["files"]}, {"valid"})
        self.assertEqual(self.query("SELECT count(*) FROM app.ridership_counts"), [(0,)])
        done = service.import_files(self.db, files, execute=True)
        first = done["files"][0]["routes"][0]
        self.assertEqual((done["route_ids"], first["inserted"], first["unmatched_stops"], first["missing_dates"],
                          first["board_per_alight"]), ([route_id], 14 * 5, [], [], 1.5))
        total = 14 * 24 * 5
        self.assertEqual(self.query("SELECT count(*), count(stop_id), min(total_board_count), max(total_board_count) "
                                    "FROM app.ridership_counts"), [(total, total, 0, 150)])
        # 원본이 들어온 노선은 실제 통과 횟수를 세도록 위치를 상시 수집 대상에 넣는다.
        with self.db.transaction() as conn:
            targets = collection_targets(self.settings, conn, datetime.now(UTC))
        self.assertIn(("positions", ROUTE_10, frozenset({"congestion"})),
                      [(t.kind, t.external_id, t.sources) for t in targets])
        again = service.import_files(self.db, files, execute=True)["files"][0]["routes"][0]
        self.assertEqual((again["inserted"], again["skipped_existing"], again["skipped_with_different_values"]),
                         (0, 14 * 5, 0))
        changed = [(150, 0, 0), (999, 20, 0), *COUNTS[2:]]
        differing = service.import_files(self.db, files_for(days[:1], counts=changed)[:1], execute=True)
        self.assertEqual(differing["files"][0]["routes"][0]["skipped_with_different_values"], 1)
        rejected = service.import_files(self.db, [("broken.xls", files[0][1].replace("순번".encode(), b"seq")),
                                                  ("unknown.xls", export_html([("2026년 10월 01일", COUNTS)],
                                                                              route="9999번(없음~없음)", stops=STOPS_10))],
                                        execute=True)
        self.assertEqual([f["status"] for f in rejected["files"]], ["rejected", "rejected"])
        self.assertEqual(self.query("SELECT count(*), max(board_count) FROM app.ridership_counts"), [(total, 150)],
                         "값이 다른 중복·거부된 파일은 원본을 바꾸지 않는다")

        # ---- 집계: 창 안의 원본만 쓰고, 다시 집계하면 같은 키의 행을 교체한다.
        self.use_settings(**CONGESTION)
        now = datetime.now(UTC)
        preview = service.aggregate_route(self.db, self.settings, route_id, now=now)
        self.assertEqual((preview["status"], preview["saved"], preview["days_used"], preview["matched_stops"]),
                         ("computed", None, 14, 5))
        self.assertEqual(self.query("SELECT count(*) FROM app.congestion_profiles"), [(0,)])
        saved = service.aggregate_route(self.db, self.settings, route_id, now=now, execute=True)
        self.assertEqual(saved["saved"], 3 * 24 * 5)
        row = self.query("SELECT flow_mean, flow_p80, trips_per_hour, trips_source, source_period FROM "
                         "app.congestion_profiles WHERE route_id = %s AND day_type = 'weekday' AND hour = 9 "
                         "AND stop_seq = 2", (route_id,))[0]
        self.assertEqual(row, (240.0, 240.0, 6.0, "timetable", f"{days[0].isoformat()}/{days[-1].isoformat()}"))
        old = service.aggregate_route(self.db, self.settings, route_id, now=now + timedelta(days=200), execute=True)
        self.assertEqual((old["status"], old["reason"], old["saved"]), ("no_data", "no_rows_in_window", None))
        self.assertEqual(self.query("SELECT count(*) FROM app.congestion_profiles"), [(3 * 24 * 5,)],
                         "집계할 자료가 없으면 기존 행을 유지한다")
        self.assertEqual(service.aggregate_all(self.db, self.settings, now=now)[0]["saved"], 3 * 24 * 5)
        self.assertEqual(self.query("SELECT count(*) FROM app.congestion_profiles"), [(3 * 24 * 5,)])

        # ---- 조회: 검색 후보의 버스 구간과 정류소 도착 목록에 '평소 이 시간대 예상'으로 나간다.
        _, body = self.ready_search()
        legs = [leg for c in body["candidates"] for leg in c["legs"] if leg["kind"] == "bus"]
        mine = [leg for leg in legs if leg["route"]["externalId"] == ROUTE_10]
        others = [leg["congestion"] for leg in legs if leg["route"]["externalId"] != ROUTE_10]
        self.assertTrue(mine and others)
        # 구간별 1대당 재차 = 흐름(150, 240, 150, 0) ÷ 시간당 6회. 좌석 25·정원 50이면 40명은 혼잡, 25명은 여유다.
        per_vehicle = {1: 25.0, 2: 40.0, 3: 25.0, 4: 0.0}
        names = {40.0: "crowded", 25.0: "relaxed", 0.0: "empty"}
        for leg in mine:
            congestion = leg["congestion"]
            self.assertEqual((congestion["status"], congestion["kind"], congestion["tripsSource"],
                              congestion["sampleDays"] >= 2, congestion["policyVersion"] is not None),
                             ("available", "typical_for_time", "timetable", True, True), congestion)
            # 타는 구간(승차 순번 ~ 하차 직전) 가운데 가장 붐비는 구간의 값이다.
            worst = max(per_vehicle[seq] for seq in range(leg["board"]["seq"], leg["alight"]["seq"]))
            self.assertEqual((congestion["loadPerVehicle"], congestion["level"]), (worst, names[worst]), leg)
        self.assertIn(40.0, [leg["congestion"]["loadPerVehicle"] for leg in mine], "A→E 전 구간을 타는 후보가 있다")
        self.assertEqual({c["status"] for c in others}, {"unavailable"})
        self.assertEqual({c["reason"] for c in others}, {"congestion_profile_missing"})
        stored = self.query("SELECT legs FROM app.route_candidates LIMIT 1")[0][0]
        self.assertIn("congestion", next(leg for leg in stored if leg["kind"] == "bus"), "당시 값을 후보에 복사")

        stop_b = self.query("SELECT id FROM app.stops WHERE external_id = %s", (w.B,))[0][0]
        arrivals = self.client.get(f"/api/v1/stops/{stop_b}/arrivals", headers=self.auth(self.user_a)).json()
        self.assertTrue(all("congestion" in item for item in arrivals["items"]))

        # ---- 순위: 가중치 설정이 없으면 비용은 시간 그대로다(내시균형 bias 0).
        ranked = [c for c in body["candidates"] if c["rank"] is not None]
        self.assertTrue(ranked)
        self.assertEqual({(c["cost"]["equilibriumBiasSeconds"], c["cost"]["costSeconds"] == c["cost"]["timeSeconds"])
                          for c in ranked}, {(0.0, True)})
        self.assertTrue(all("congestion_bias_applied" not in c["rankingReasonCodes"] for c in ranked))

        # ---- 순위: 가중치가 있으면 버스 탑승시간 × (단계 가중치 − 1)을 내시균형 bias로 비용에 더해 정렬한다.
        weights = (1.0, 1.0, 1.2, 1.5, 2.0)
        self.use_settings(**CONGESTION, congestion_cost_weights=",".join(str(value) for value in weights))
        weighted_id, body = self.ready_search(self.user_c)
        ranked = sorted((c for c in body["candidates"] if c["rank"] is not None), key=lambda c: c["rank"])
        self.assertTrue(ranked)
        for candidate in ranked:
            expected = sum(leg["rideSeconds"] * (weights[leg["congestion"]["levelIndex"] - 1] - 1)
                           for leg in candidate["legs"]
                           if leg["kind"] == "bus" and leg["congestion"]["status"] == "available")
            cost = candidate["cost"]
            self.assertAlmostEqual(cost["equilibriumBiasSeconds"], expected, places=6, msg=candidate["legs"])
            self.assertAlmostEqual(cost["costSeconds"], cost["timeSeconds"] + expected, places=6)
            self.assertEqual("congestion_bias_applied" in candidate["rankingReasonCodes"], expected > 0)
        self.assertTrue(any(c["cost"]["equilibriumBiasSeconds"] > 0 for c in ranked),
                        "혼잡 구간(B→C, 1대당 40명)을 타는 후보에 비용이 붙는다")
        # 배차간격 예상 후보는 실시간 후보 뒤에 두고, 그 안에서는 시간이 아니라 비용 순이다.
        order = [(c["estimated"], c["cost"]["costSeconds"]) for c in ranked]
        self.assertEqual(order, sorted(order))
        self.assertTrue(all(c["cost"] is None for c in body["candidates"] if c["rank"] is None))
        summary = self.query("SELECT summary -> 'congestion_bias', params -> 'bias' ->> 'equilibrium_seconds' "
                             "FROM app.route_searches WHERE id = %s", (weighted_id,))[0]
        self.assertEqual((summary[0]["applied"], summary[0]["candidates_with_bias"] > 0, summary[1]),
                         (True, True, "congestion_ride_time_weight"))

        # ---- 균형 시뮬레이션 입력: 저장된 검색의 순위 후보를 정적 경로마다 하나씩 읽는다(DB에 쓰지 않는다).
        policy = levels.policy_from_settings(self.settings)
        with self.db.transaction() as conn:
            options = service.equilibrium_options(conn, weighted_id, policy)
        static_keys = {r[0] for r in self.query("SELECT static_route_key FROM app.route_candidates "
                                                "WHERE search_id = %s AND rank_no IS NOT NULL", (weighted_id,))}
        self.assertEqual(len(options), len(static_keys))
        rides = [ride for option in options for ride in option.rides]
        known = [ride for ride in rides if not ride.assumed]
        self.assertTrue(known and all(link.trips_per_hour == 6.0 for ride in known for link in ride.links))
        self.assertEqual({link.base_load for ride in known for link in ride.links} - {25.0, 40.0, 0.0}, set())
        self.assertTrue(all(ride.assumed == ("base_load_unknown", "trips_from_timetable")
                            for ride in rides if ride.assumed), "집계가 없는 노선은 배차간격으로 채우고 가정을 남긴다")
        simulated = equilibrium.simulate(options, policy, demand_per_hour=300, theta=0.01, max_iterations=500,
                                         tolerance=0.001)
        self.assertEqual(len(simulated.options), len(options))
        self.assertAlmostEqual(sum(o.share_at_equilibrium for o in simulated.options), 1.0)
        self.assertLessEqual(simulated.max_share("share_at_equilibrium"), simulated.max_share("share_all_to_best"))

        # ---- 안내 인원: 다른 사용자가 같은 차량으로 안내받고 있으면 그 구간의 1대당 재차에 1명씩 더한다.
        def route_10_leg(candidate):
            return next((leg for leg in candidate["legs"] if leg["kind"] == "bus"
                         and leg["route"]["externalId"] == ROUTE_10 and leg["vehicle"]
                         and leg["vehicle"]["vehicleId"]), None)

        self.assertEqual({leg["congestion"]["guidedRiders"] for c in ranked for leg in c["legs"]
                          if leg["kind"] == "bus" and leg["congestion"]["status"] == "available"}, {0.0})
        chosen = next(c for c in ranked if route_10_leg(c))
        ridden = route_10_leg(chosen)
        started = self.client.post("/api/v1/journeys", headers=self.auth(self.user_c),
                                   json={"searchId": weighted_id, "candidateId": chosen["candidateId"]})
        self.assertEqual(started.status_code, 201, started.text)
        _, body = self.ready_search(self.user_a)
        same = [leg for c in body["candidates"] for leg in c["legs"]
                if leg["kind"] == "bus" and leg["route"]["externalId"] == ROUTE_10 and leg["vehicle"]
                and leg["vehicle"]["vehicleId"] == ridden["vehicle"]["vehicleId"]
                and (leg["board"]["seq"], leg["alight"]["seq"]) == (ridden["board"]["seq"], ridden["alight"]["seq"])]
        self.assertTrue(same, "같은 차량·같은 구간을 타는 후보가 다른 사용자 검색에도 있다")
        for leg in same:
            self.assertEqual((leg["congestion"]["guidedRiders"], leg["congestion"]["loadPerVehicle"]),
                             (1.0, ridden["congestion"]["loadPerVehicle"] + 1.0), leg["congestion"])
        guided_summary = self.query("SELECT summary -> 'congestion_bias' ->> 'guided_rides' FROM app.route_searches "
                                    "WHERE user_id = %s ORDER BY created_at DESC LIMIT 1", (self.user_a,))[0][0]
        self.assertEqual(guided_summary, "1")
        # 안내받는 사용자 자신의 검색에는 자기 탑승을 더하지 않는다.
        _, own = self.ready_search(self.user_c)
        self.assertEqual({leg["congestion"]["guidedRiders"] for c in own["candidates"] for leg in c["legs"]
                          if leg["kind"] == "bus" and leg["congestion"]["status"] == "available"}, {0.0})

        # ---- 단계 설정(좌석 수·정원)이 없으면 단계를 만들지 않고 사유를 준다.
        self.use_settings()
        _, body = self.ready_search(self.user_b)
        reasons = {leg["congestion"]["reason"] for c in body["candidates"] for leg in c["legs"] if leg["kind"] == "bus"}
        self.assertEqual(reasons, {"congestion_not_configured"})


if __name__ == "__main__":
    unittest.main()
