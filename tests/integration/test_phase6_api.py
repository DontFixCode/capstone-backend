"""Phase 6 격리 시험: 이동 기록 조회·삭제, 환승 결과·수정, 회원탈퇴(소프트 삭제·접근 차단·Auth 차단 재시도).

Phase 4 시험 기반(tests/integration/test_phase4_api.py)을 그대로 쓴다: TEST_POSTGIS_DSN의 임시 서버에서 시험마다 새 DB를
만들고 migrations/01~11을 적용한다. TAGO·TMAP은 합성 응답, Supabase Auth 관리자 API는 가짜(호출 기록)다. 실제 사용자 계정
삭제·운영 DB 변경·외부 과금 호출은 없다. Phase 5(대안 계산·수락)에 의존하는 흐름은 시험하지 않는다.

    $env:TEST_POSTGIS_DSN = "postgresql://postgres:...@127.0.0.1:55436/postgres"
    uv run python -m unittest tests.integration.test_phase6_api
"""

import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest import mock
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from starlette.websockets import WebSocketDisconnect

from app import main
from app.db.session import Database
from app.integrations.supabase_auth import AuthAdminConfigError, BlockResult
from app.modules.evaluation import user_results
from app.modules.journeys import repository as journey_repository
from app.modules.journeys import service as journey_service
from app.modules.journeys.schemas import EventBody
from app.modules.users import repository as user_repository
from app.modules.users import service as user_service
from app.workers.collection import collection_targets
from tests.integration.test_phase4_api import DESTINATION, ORIGIN, Phase4TestCase, synthetic_provider

UTC = timezone.utc


class FakeAuthAdmin:
    """Supabase Auth 관리자 API 대역. outcomes 순서대로 blocked/http_error/timeout/raise를 돌려준다(비면 blocked)."""

    def __init__(self, outcomes=None):
        self.calls: list[UUID] = []
        self.outcomes = list(outcomes or [])
        self.lock = threading.Lock()

    def block_new_logins(self, user_id):
        with self.lock:
            self.calls.append(user_id)
            outcome = self.outcomes.pop(0) if self.outcomes else "blocked"
        if outcome == "raise":
            raise RuntimeError("예상하지 못한 연동 오류")
        return BlockResult(outcome == "blocked", outcome, 200 if outcome == "blocked" else 500)

    def close(self):
        pass


class Phase6TestCase(Phase4TestCase):
    def setUp(self):
        super().setUp()
        self.fake_admin = FakeAuthAdmin()
        state = main.app.state
        saved = getattr(state, "auth_admin_factory", None)
        self.addCleanup(lambda: setattr(state, "auth_admin_factory", saved))
        state.auth_admin_factory = lambda: self.fake_admin

    def post(self, jid, event_type, version, payload, *, key=None, user=None, occurred_at=None):
        body = {"eventType": event_type, "stateVersion": version, "payload": payload}
        if occurred_at is not None:
            body["occurredAt"] = occurred_at.isoformat()
        return self.client.post(f"/api/v1/journeys/{jid}/events",
                                headers={**self.auth(user or self.user_a), "Idempotency-Key": key or uuid4().hex},
                                json=body)

    @staticmethod
    def result_payload(item, result, **extra):
        return {"snapshotKey": item["snapshotKey"], "selectionVersion": item["selectionVersion"],
                "transferIndex": item["transferIndex"], "result": result, **extra}

    def bus_legs(self, journey):
        return [leg["legIndex"] for leg in journey["selectedRoute"]["legs"] if leg["kind"] == "bus"]

    def count(self, sql, params=None):
        return self.query(sql, params)[0][0]

    def common_data_counts(self):
        return {t: self.count(f"SELECT count(*) FROM app.{t}") for t in
                ("api_fetches", "transit_observations", "transfer_samples", "journey_scenes", "stops", "routes")}


# ---------------------------------------------------------------- 4A. 이동 기록 조회

class JourneyHistoryTest(Phase6TestCase):
    def test_list_filters_cursor_pagination_and_ownership(self):
        journey, _, _ = self.start()
        jid = journey["journeyId"]
        base = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
        extra = []
        # 같은 시작 시각 둘(ID로 순서 결정)을 포함한 종료 기록을 만든다. 선택 스냅샷은 실제 이동 행에서 복사한다.
        for i, (status, started) in enumerate([("completed", base), ("cancelled", base),
                                               ("completed", base - timedelta(hours=1)),
                                               ("cancelled", base - timedelta(hours=2)),
                                               ("completed", base - timedelta(hours=3))]):
            extra.append(str(self.query(
                "INSERT INTO app.journeys (user_id, selected_snapshot, lifecycle_status, movement_state, "
                "connection_state, current_progress, next_recalc_at, started_at, ended_at) "
                "SELECT user_id, selected_snapshot, %s, 'arrived', 'unknown', current_progress, NULL, %s, %s "
                "FROM app.journeys WHERE id = %s RETURNING id",
                (status, started, started + timedelta(minutes=30), jid))[0][0]))
        expected = [r[0] for r in self.query("SELECT id::text FROM app.journeys WHERE user_id = %s "
                                             "ORDER BY started_at DESC, id DESC", (self.user_a,))]
        with mock.patch.object(journey_service, "PAGE_SIZE", 2):
            pages, cursor = [], None
            while True:
                r = self.client.get("/api/v1/journeys", headers=self.auth(self.user_a),
                                    params={"cursor": cursor} if cursor else {})
                self.assertEqual(r.status_code, 200, r.text)
                body = r.json()
                pages.append([i["journeyId"] for i in body["items"]])
                cursor = body["nextCursor"]
                if cursor is None:
                    break
            self.assertEqual([j for p in pages for j in p], expected, "시작 시각·ID 내림차순, 페이지 사이 중복·누락 없음")
            self.assertEqual([len(p) for p in pages], [2, 2, 2])
            pages, cursor = [], None
            while True:  # 상태 필터와 커서를 함께 쓴다.
                body = self.client.get("/api/v1/journeys", headers=self.auth(self.user_a),
                                       params={"status": "completed", **({"cursor": cursor} if cursor else {})}).json()
                pages.extend(body["items"])
                cursor = body["nextCursor"]
                if cursor is None:
                    break
            self.assertEqual([i["journeyId"] for i in pages],
                             [r[0] for r in self.query("SELECT id::text FROM app.journeys WHERE user_id = %s AND "
                                                       "lifecycle_status = 'completed' ORDER BY started_at DESC, "
                                                       "id DESC", (self.user_a,))])
            self.assertEqual(len(pages), 3)
        item = next(i for i in self.client.get("/api/v1/journeys", headers=self.auth(self.user_a),
                                               params={"status": "active"}).json()["items"])
        self.assertEqual(set(item), {"journeyId", "lifecycleStatus", "movementState", "origin", "destination",
                                     "startedAt", "endedAt"})
        self.assertEqual(item["destination"]["name"], DESTINATION["name"])
        empty = self.client.get("/api/v1/journeys", headers=self.auth(self.user_b)).json()
        self.assertEqual(empty, {"items": [], "nextCursor": None})
        # 다른 사용자의 기록 위치를 가리키는 커서를 써도 자기 기록만 본다.
        foreign = journey_service._encode_cursor({"started_at": base + timedelta(days=1), "id": UUID(extra[0])})
        other = self.client.get("/api/v1/journeys", headers=self.auth(self.user_b), params={"cursor": foreign}).json()
        self.assertEqual(other, {"items": [], "nextCursor": None}, "커서로 다른 사용자 기록에 접근하지 못함")
        self.assertEqual(self.client.get("/api/v1/journeys", headers=self.auth(self.user_a),
                                         params={"cursor": "not-a-cursor"}).status_code, 422)
        self.assertEqual(self.client.get("/api/v1/journeys", headers=self.auth(self.user_a),
                                         params={"status": "done"}).status_code, 422)
        for other_jid in (jid, extra[0]):
            r = self.client.get(f"/api/v1/journeys/{other_jid}", headers=self.auth(self.user_b))
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (404, "NOT_FOUND"))

    def test_detail_restores_from_snapshot_after_search_cleanup(self):
        journey, chosen, search_id = self.start(multi=True)
        jid = journey["journeyId"]
        self.assertEqual(len(journey["transferResults"]), chosen["transferCount"])
        self.assertTrue(all(t["evaluationStatus"] == "no_response" and t["result"] is None
                            for t in journey["transferResults"]))
        self.assertEqual(self.post(jid, "completed", 1, {}).status_code, 200)
        before = self.detail(jid)
        # 최초 검색·후보가 만료 후 정리된 상황: 시작 검색 FK는 SET NULL, 선택 스냅샷은 그대로.
        self.query("DELETE FROM app.route_searches WHERE id = %s RETURNING 1", (search_id,))
        after = self.detail(jid)
        self.assertIsNone(after["initialSearchId"])
        self.assertEqual(after["selectedRoute"], before["selectedRoute"], "검색이 없어도 선택 경로 복원")
        self.assertEqual(after["transferResults"], before["transferResults"], "고정 예측도 이벤트로 남아 있음")
        self.assertEqual((after["lifecycleStatus"], after["movementState"]), ("completed", "arrived"))
        self.assertIsNone(after["proposedRoute"], "Phase 5 대안이 없으면 null")
        self.assertEqual(self.query("SELECT selected_candidate_id FROM app.journeys WHERE id = %s", (jid,)),
                         [(None,)])


# ---------------------------------------------------------------- 4B. 환승 결과·수정

class TransferResultTest(Phase6TestCase):
    def test_results_link_to_fixed_snapshot_and_are_validated(self):
        journey, _, _ = self.start(multi=True)
        jid = journey["journeyId"]
        legs = self.bus_legs(journey)
        item = journey["transferResults"][0]
        self.assertEqual(item["snapshotKey"], f"{jid}:s1:t1")
        self.assertEqual((item["frontLegIndex"], item["nextLegIndex"]), (legs[0], legs[1]))
        r = self.post(jid, "transfer_result", 1, self.result_payload(item, "success"))
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "TRANSFER_NOT_REACHED"),
                         "앞 버스 하차 전에는 결과를 받지 않음")
        board = self.post(jid, "board", 1, {"legIndex": legs[0]})
        self.assertEqual(board.status_code, 200)
        wrong = self.post(jid, "correction", 2, {"supersedesEventId": board.json()["eventId"], "result": "success"})
        self.assertEqual(wrong.json()["error"]["code"], "INVALID_CORRECTION", "탑승 수정에 결과 값을 넣을 수 없음")
        self.assertEqual(self.post(jid, "alight", 2, {"legIndex": legs[0]}).status_code, 200)

        other, _, _ = self.start(self.user_b, multi=True)
        bad_cases = [
            ({"snapshotKey": "nope"}, 422, "SNAPSHOT_NOT_FOUND"),
            ({"snapshotKey": other["transferResults"][0]["snapshotKey"]}, 422, "SNAPSHOT_NOT_FOUND"),
            ({"selectionVersion": 2}, 422, "SNAPSHOT_MISMATCH"),
            ({"transferIndex": 9}, 422, "SNAPSHOT_MISMATCH"),
            ({"result": "late"}, 422, "INVALID_PAYLOAD"),
            ({"actualConnection": {"nextRouteId": "999999"}}, 422, "ROUTE_NOT_FOUND"),
        ]
        for patch_, status, code in bad_cases:
            with self.subTest(patch=patch_):
                r = self.post(jid, "transfer_result", 3, {**self.result_payload(item, "success"), **patch_})
                self.assertEqual((r.status_code, r.json()["error"]["code"]), (status, code), r.text)
        now = datetime.now(UTC)
        r = self.post(jid, "transfer_result", 3, self.result_payload(item, "success"), occurred_at=now + timedelta(hours=1))
        self.assertEqual(r.json()["error"]["code"], "OCCURRED_AT_IN_FUTURE")
        r = self.post(jid, "transfer_result", 3, self.result_payload(item, "success"), occurred_at=now - timedelta(days=1))
        self.assertEqual(r.json()["error"]["code"], "OCCURRED_BEFORE_SNAPSHOT")
        r = self.post(jid, "transfer_result", 2, self.result_payload(item, "success"))
        self.assertEqual(r.json()["error"]["code"], "STATE_VERSION_CONFLICT")
        self.assertEqual(self.client.get(f"/api/v1/journeys/{jid}", headers=self.auth(self.user_b)).status_code, 404)
        self.assertEqual(self.post(jid, "transfer_result", 3, self.result_payload(item, "success"),
                                   user=self.user_b).status_code, 404, "다른 사용자의 이동")
        self.assertEqual(self.count("SELECT count(*) FROM app.journey_events WHERE journey_id = %s "
                                    "AND event_type = 'transfer_result'", (jid,)), 0, "거부된 입력은 기록하지 않음")

        samples_before = self.query("SELECT id, label_status, time_feasible, observation_context, labeled_at FROM "
                                    "app.transfer_samples ORDER BY id")
        common_before = self.common_data_counts()
        ok = self.post(jid, "transfer_result", 3, self.result_payload(item, "success"), key="tr-1",
                       occurred_at=now - timedelta(seconds=5))
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual((ok.json()["stateVersionAfter"], ok.json()["replayed"]), (3, False),
                         "결과 기록은 이동 상태 버전을 올리지 않음")
        replay = self.post(jid, "transfer_result", 3, self.result_payload(item, "success"), key="tr-1",
                           occurred_at=now - timedelta(seconds=5))
        self.assertEqual((replay.json()["eventId"], replay.json()["replayed"]), (ok.json()["eventId"], True))
        reused = self.post(jid, "transfer_result", 3, self.result_payload(item, "failure"), key="tr-1")
        self.assertEqual((reused.status_code, reused.json()["error"]["code"]), (409, "IDEMPOTENCY_KEY_REUSED"))
        dup = self.post(jid, "transfer_result", 3, self.result_payload(item, "failure"), key="tr-2")
        self.assertEqual((dup.status_code, dup.json()["error"]["code"]), (409, "TRANSFER_RESULT_EXISTS"))
        self.assertEqual(dup.json()["error"]["details"]["eventId"], ok.json()["eventId"])

        detail = self.detail(jid)
        self.assertEqual(detail["stateVersion"], 3)
        row = detail["transferResults"][0]
        self.assertEqual((row["result"]["value"], row["result"]["eventId"], row["evaluationStatus"]),
                         ("success", ok.json()["eventId"], "counted"))
        self.assertEqual(row["result"]["connectionMatch"], "planned", "기록된 앞 구간 탑승이 계획 노선과 같음")
        stored = self.query("SELECT payload FROM app.journey_events WHERE id = %s", (int(ok.json()["eventId"]),))[0][0]
        snap_id = self.query("SELECT id FROM app.journey_events WHERE journey_id = %s AND event_type = "
                             "'evaluation_snapshot' AND payload->>'snapshot_key' = %s", (jid, item["snapshotKey"]))[0][0]
        self.assertEqual((stored["effect"]["snapshot_event_id"], stored["snapshot_key"]), (snap_id, item["snapshotKey"]))
        self.assertLess(snap_id, int(ok.json()["eventId"]), "예측 스냅샷이 결과보다 먼저 고정됨")
        self.assertTrue(samples_before, "합성 검색이 공통 환승 표본을 만든다")
        self.assertEqual(self.query("SELECT id, label_status, time_feasible, observation_context, labeled_at FROM "
                                    "app.transfer_samples ORDER BY id"), samples_before,
                         "사용자 결과가 공통 표본의 시간상 가능 판정을 바꾸지 않음")
        self.assertEqual(self.common_data_counts(), common_before)

    def test_corrections_on_ended_journey_and_single_effective_result(self):
        journey, _, _ = self.start(multi=True)
        jid = journey["journeyId"]
        item = journey["transferResults"][0]
        self.assertEqual(self.post(jid, "completed", 1, {}).status_code, 200)
        unknown = self.post(jid, "transfer_result", 2, self.result_payload(item, "unknown"))
        self.assertEqual(unknown.status_code, 200, "종료된 이동에도 결과 입력 허용(앞 구간 기록 없어도)")
        original_id = unknown.json()["eventId"]
        original_row = self.query("SELECT payload, received_at FROM app.journey_events WHERE id = %s",
                                  (int(original_id),))
        view = self.detail(jid)["transferResults"][0]
        self.assertEqual((view["result"]["value"], view["evaluationStatus"]), ("unknown", "unknown"),
                         "unknown은 실패로 집계하지 않음")
        for target, payload, code in [
            (original_id, {"vehicleId": "X"}, "INVALID_CORRECTION"),
            (original_id, {"reason": "값 없음"}, "INVALID_CORRECTION"),
            (str(self.query("SELECT id FROM app.journey_events WHERE journey_id = %s AND event_type = "
                            "'evaluation_snapshot' LIMIT 1", (jid,))[0][0]), {"result": "success"},
             "INVALID_CORRECTION_TARGET"),
            ("999999", {"result": "success"}, "INVALID_CORRECTION_TARGET"),
        ]:
            with self.subTest(payload=payload):
                r = self.post(jid, "correction", 2, {"supersedesEventId": target, **payload})
                self.assertEqual((r.status_code, r.json()["error"]["code"]), (422, code), r.text)
        fix = self.post(jid, "correction", 2, {"supersedesEventId": original_id, "result": "failure",
                                               "reason": "다시 확인"})
        self.assertEqual(fix.status_code, 200, fix.text)
        self.assertEqual(fix.json()["stateVersionAfter"], 2)
        stale = self.post(jid, "correction", 2, {"supersedesEventId": original_id, "result": "success"})
        self.assertEqual((stale.status_code, stale.json()["error"]["code"]), (409, "CORRECTION_TARGET_SUPERSEDED"))
        self.assertEqual(stale.json()["error"]["details"]["latestEventId"], fix.json()["eventId"])
        view = self.detail(jid)["transferResults"][0]
        self.assertEqual((view["result"]["value"], view["result"]["originalEventId"],
                          view["result"]["correctionEventIds"], view["evaluationStatus"]),
                         ("failure", original_id, [fix.json()["eventId"]], "counted"))
        other = self.post(jid, "correction", 2, {"supersedesEventId": fix.json()["eventId"],
                                                 "actualConnection": {"usedPlanned": False}})
        self.assertEqual(other.status_code, 200, other.text)
        view = self.detail(jid)["transferResults"][0]
        self.assertEqual((view["result"]["value"], view["evaluationStatus"], view["result"]["connectionMatch"]),
                         ("failure", "excluded", "different"), "다른 연결 이용은 대조에서 제외(결과 값은 유지)")
        self.assertIn("different_connection", view["result"]["excludedReasons"])
        self.assertEqual(self.query("SELECT payload, received_at FROM app.journey_events WHERE id = %s",
                                    (int(original_id),)), original_row, "원본 이벤트는 수정하지 않음")
        with self.db.transaction() as conn:
            events = journey_repository.result_events(conn, UUID(jid))
            snaps = [e["payload"] for e in journey_repository.events(conn, UUID(jid), "evaluation_snapshot")]
        summary = user_results.summarize(user_results.comparison_rows(snaps, user_results.resolve(events)))
        self.assertEqual((summary["responded"], summary["excluded"], summary["counted"]), (1, 1, 0),
                         "같은 환승의 결과·수정 3건이 한 번만 집계됨")
        mixed = self.post(jid, "correction", 2, {"supersedesEventId": other.json()["eventId"], "result": "success",
                                                 "stopId": "1"})
        self.assertEqual(mixed.json()["error"]["code"], "INVALID_CORRECTION", "결과 수정에 하차 값을 섞을 수 없음")

    def test_concurrent_results_and_corrections_keep_one_chain(self):
        journey, _, _ = self.start(multi=True)
        jid = UUID(journey["journeyId"])
        item = journey["transferResults"][0]
        self.assertEqual(self.post(str(jid), "cancelled", 1, {"reason": "시험"}).status_code, 200)

        def submit(args):
            key, event_type, payload = args
            body = EventBody(eventType=event_type, stateVersion=2, payload=payload)
            try:
                return journey_service.record_event(self.db, self.user_a, jid, key, body, now=datetime.now(UTC))
            except journey_service.JourneyError as exc:
                return exc.code

        same = self.result_payload(item, "success")
        with ThreadPoolExecutor(max_workers=4) as pool:
            outcomes = list(pool.map(submit, [("k-same", "transfer_result", same)] * 2 +
                                     [("k-other", "transfer_result", self.result_payload(item, "failure"))]))
        # 같은 환승의 결과는 하나만 저장된다: 같은 키 재전송은 같은 결과(replayed), 다른 키는 409. 어느 쪽이 먼저든 마찬가지다.
        self.assertTrue(all(o == "TRANSFER_RESULT_EXISTS" or isinstance(o, dict) for o in outcomes), outcomes)
        stored = self.query("SELECT id, idempotency_key FROM app.journey_events WHERE journey_id = %s AND "
                            "event_type = 'transfer_result'", (jid,))
        self.assertEqual(len(stored), 1, outcomes)
        head = str(stored[0][0])
        saved = [o for o in outcomes if isinstance(o, dict)]
        self.assertTrue(all(str(o["event_id"]) == head for o in saved), outcomes)
        if stored[0][1] == "k-same":
            self.assertEqual(sorted(o["replayed"] for o in saved), [False, True])
            self.assertEqual(outcomes[2], "TRANSFER_RESULT_EXISTS")
        else:
            self.assertEqual(outcomes[:2], ["TRANSFER_RESULT_EXISTS"] * 2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            fixes = list(pool.map(submit, [(f"fix-{i}", "correction", {"supersedesEventId": head, "result": r})
                                           for i, r in enumerate(("failure", "unknown"))]))
        self.assertEqual(sorted(type(f).__name__ for f in fixes), ["dict", "str"], fixes)
        self.assertIn("CORRECTION_TARGET_SUPERSEDED", fixes)
        self.assertEqual(self.count("SELECT count(*) FROM app.journey_events WHERE journey_id = %s AND "
                                    "event_type IN ('transfer_result', 'correction')", (jid,)), 2)


# ---------------------------------------------------------------- 4C. 이동 기록 삭제

class JourneyDeleteTest(Phase6TestCase):
    def add_reevaluation_search(self, jid, search_id):
        """Phase 5가 만들 재평가 검색(source=worker)과 후보를 DB에 직접 넣는다(대안 계산 로직은 만들지 않음)."""
        worker_search = self.query(
            "INSERT INTO app.route_searches (user_id, journey_id, source, origin, destination, place_info, start_at, "
            "static_version, config_version, params, ranking_mode, status, summary, expires_at) "
            "SELECT user_id, %s, 'worker', origin, destination, '{}'::jsonb, start_at, static_version, config_version, "
            "params, ranking_mode, 'ready', '{}'::jsonb, expires_at FROM app.route_searches WHERE id = %s RETURNING id",
            (jid, search_id))[0][0]
        candidate = self.query(
            "INSERT INTO app.route_candidates (search_id, static_route_key, option_key, legs, boarding_count, "
            "time_status, evaluation, evaluated_at) SELECT %s, static_route_key, option_key, legs, boarding_count, "
            "time_status, evaluation, evaluated_at FROM app.route_candidates WHERE search_id = %s LIMIT 1 "
            "RETURNING id", (worker_search, search_id))[0][0]
        self.query("UPDATE app.journeys SET proposed_candidate_id = %s WHERE id = %s RETURNING 1", (candidate, jid))
        return worker_search

    def test_delete_rules_and_cleanup_scope(self):
        journey, _, search_id = self.start()
        jid = journey["journeyId"]
        other, _, other_search = self.start(self.user_b)
        worker_search = self.add_reevaluation_search(jid, search_id)
        url = f"/api/v1/journeys/{jid}"
        r = self.client.delete(url, headers=self.auth(self.user_a))
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "JOURNEY_ACTIVE"))
        self.assertEqual(self.client.delete(url, headers=self.auth(self.user_b)).status_code, 404, "타인 기록")
        self.assertEqual(self.client.delete(f"/api/v1/journeys/{uuid4()}", headers=self.auth(self.user_a)).status_code,
                         404)
        self.assertEqual(self.client.delete("/api/v1/journeys/not-a-uuid", headers=self.auth(self.user_a)).status_code,
                         422)
        self.assertEqual(self.post(jid, "cancelled", 1, {"reason": "삭제 시험"}).status_code, 200)
        common = self.common_data_counts()
        b_rows = (self.count("SELECT count(*) FROM app.journey_events WHERE journey_id = %s", (other["journeyId"],)),
                  self.count("SELECT count(*) FROM app.route_candidates WHERE search_id = %s", (other_search,)))
        self.assertGreater(self.count("SELECT count(*) FROM app.journey_events WHERE journey_id = %s", (jid,)), 0)

        r = self.client.delete(url, headers=self.auth(self.user_a))
        self.assertEqual((r.status_code, r.content), (204, b""))
        self.assertEqual(self.count("SELECT count(*) FROM app.journeys WHERE id = %s", (jid,)), 0)
        self.assertEqual(self.count("SELECT count(*) FROM app.journey_events WHERE journey_id = %s", (jid,)), 0)
        self.assertEqual(self.count("SELECT count(*) FROM app.route_searches WHERE id IN (%s, %s)",
                                    (worker_search, search_id)), 0, "재평가 검색과 참조 없는 시작 검색 정리")
        self.assertEqual(self.count("SELECT count(*) FROM app.route_candidates WHERE search_id IN (%s, %s)",
                                    (worker_search, search_id)), 0)
        self.assertEqual(self.common_data_counts(), common, "공통 관측·표본은 보존")
        self.assertEqual((self.count("SELECT count(*) FROM app.journey_events WHERE journey_id = %s",
                                     (other["journeyId"],)),
                          self.count("SELECT count(*) FROM app.route_candidates WHERE search_id = %s",
                                     (other_search,))), b_rows, "다른 사용자 자료는 그대로")
        self.assertEqual(self.client.get(url, headers=self.auth(self.user_a)).status_code, 404)
        self.assertEqual(self.client.delete(url, headers=self.auth(self.user_a)).status_code, 404, "재삭제는 404")
        self.assertEqual(self.count("SELECT count(*) FROM app.profiles WHERE user_id = %s AND deleted_at IS NULL",
                                    (self.user_a,)), 1, "기록 삭제는 계정 탈퇴가 아님")

    def test_initial_search_shared_by_another_journey_is_preserved(self):
        search_id, body = self.ready_search()
        ranked = [c for c in body["candidates"] if c["rank"] is not None]
        first = self.client.post("/api/v1/journeys", headers=self.auth(self.user_a),
                                 json={"searchId": search_id, "candidateId": ranked[0]["candidateId"]}).json()
        self.assertEqual(self.post(first["journeyId"], "cancelled", 1, {}).status_code, 200)
        second = self.client.post("/api/v1/journeys", headers=self.auth(self.user_a),
                                  json={"searchId": search_id, "candidateId": ranked[0]["candidateId"]})
        self.assertEqual(second.status_code, 201, second.text)
        second = second.json()
        self.assertEqual(self.post(second["journeyId"], "completed", 1, {}).status_code, 200)
        self.assertEqual(self.client.delete(f"/api/v1/journeys/{first['journeyId']}",
                                            headers=self.auth(self.user_a)).status_code, 204)
        self.assertEqual(self.count("SELECT count(*) FROM app.route_searches WHERE id = %s", (search_id,)), 1,
                         "다른 이동이 참조하는 시작 검색은 보존")
        self.assertEqual(self.query("SELECT initial_search_id::text, selected_candidate_id::text FROM app.journeys "
                                    "WHERE id = %s", (second["journeyId"],)),
                         [(search_id, ranked[0]["candidateId"])])
        self.assertEqual(self.client.get(f"/api/v1/route-searches/{search_id}",
                                         headers=self.auth(self.user_a)).status_code, 200)
        self.assertEqual(self.client.delete(f"/api/v1/journeys/{second['journeyId']}",
                                            headers=self.auth(self.user_a)).status_code, 204)
        self.assertEqual(self.count("SELECT count(*) FROM app.route_searches WHERE id = %s", (search_id,)), 0,
                         "마지막 참조가 사라지면 정리")


# ---------------------------------------------------------------- 4D. 회원탈퇴

class AccountDeletionTest(Phase6TestCase):
    def test_soft_delete_preserves_data_and_blocks_access(self):
        journey, _, search_id = self.start(multi=True)
        jid = journey["journeyId"]
        pending_search = self.submit(self.user_a)  # 워커가 아직 계산하지 않은 검색
        token = self.auth(self.user_a)  # 탈퇴 뒤에도 만료 전인 같은 JWT
        before = {"searches": self.count("SELECT count(*) FROM app.route_searches WHERE user_id = %s", (self.user_a,)),
                  "candidates": self.count("SELECT count(*) FROM app.route_candidates c JOIN app.route_searches s "
                                           "ON s.id = c.search_id WHERE s.user_id = %s", (self.user_a,)),
                  "events": self.count("SELECT count(*) FROM app.journey_events WHERE journey_id = %s", (jid,))}
        with self.db.transaction() as conn:
            claim = journey_repository.claim_due_journeys(conn, datetime.now(UTC), lease_seconds=60, limit=5)[0]
        stream = f"/api/v1/journeys/{jid}/stream"
        with self.assertRaises(WebSocketDisconnect) as ctx:
            with self.client.websocket_connect(stream, headers=token) as ws:
                self.assertEqual(ws.receive_json()["type"], "journey_state")
                r = self.client.delete("/api/v1/me", headers=token)
                # DB 확인 간격(0.2초)보다 자주 ping을 보내도 다음 확인 때 기존 연결이 닫힌다(최대 5초 대기).
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    ws.send_json({"type": "ping"})
                    self.assertEqual(ws.receive_json()["type"], "pong")
                    time.sleep(0.05)
        self.assertEqual(ctx.exception.code, 4403, "기존 WebSocket 연결 종료")
        self.assertEqual(r.status_code, 202, r.text)
        body = r.json()
        self.assertEqual((body["displayUserId"], body["authBlockStatus"], body["cancelledJourneyIds"]),
                         (f"{self.user_a}_deleted", "blocked", [jid]))
        self.assertEqual(self.fake_admin.calls, [self.user_a])
        profile = self.query("SELECT user_id, deleted_at, auth_blocked_at FROM app.profiles WHERE user_id = %s",
                             (self.user_a,))
        self.assertEqual(len(profile), 1)
        self.assertEqual(profile[0][0], self.user_a, "실제 UUID는 바뀌지 않음")
        self.assertGreaterEqual(profile[0][2], profile[0][1])
        self.assertEqual(self.query("SELECT lifecycle_status, next_recalc_at, ended_at IS NOT NULL, state_version "
                                    "FROM app.journeys WHERE id = %s", (jid,)), [("cancelled", None, True, 2)])
        self.assertEqual(self.query("SELECT source, payload->>'reason' FROM app.journey_events WHERE journey_id = %s "
                                    "AND event_type = 'cancelled'", (jid,)), [("system", "account_deleted")])
        self.assertEqual(self.count("SELECT count(*) FROM app.route_searches WHERE user_id = %s", (self.user_a,)),
                         before["searches"], "검색 기록 보존")
        self.assertEqual(self.count("SELECT count(*) FROM app.route_candidates c JOIN app.route_searches s ON "
                                    "s.id = c.search_id WHERE s.user_id = %s", (self.user_a,)), before["candidates"])
        self.assertEqual(self.count("SELECT count(*) FROM app.journey_events WHERE journey_id = %s", (jid,)),
                         before["events"] + 1, "이벤트 보존 + 시스템 취소 1건")
        with self.db.transaction() as conn:
            saved = journey_repository.save_guidance(conn, UUID(jid), expected_state_version=claim["state_version"],
                                                     leased_until=claim["leased_until"], guidance={"x": 1},
                                                     next_recalc_at=datetime.now(UTC))
        self.assertIsNone(saved, "탈퇴 전에 시작한 재평가 결과는 저장하지 않음")

        bus = self.bus_legs(journey)[0]
        for method, url, kwargs in [
            ("get", "/api/v1/me", {}), ("patch", "/api/v1/me", {"json": {"displayName": "x"}}),
            ("get", "/api/v1/journeys", {}), ("get", f"/api/v1/journeys/{jid}", {}),
            ("post", f"/api/v1/journeys/{jid}/events",
             {"json": {"eventType": "board", "stateVersion": 2, "payload": {"legIndex": bus}},
              "headers": {**token, "Idempotency-Key": "after-delete"}}),
            ("put", f"/api/v1/journeys/{jid}/location",
             {"json": {"latitude": 36.35, "longitude": 127.38, "observedAt": datetime.now(UTC).isoformat()}}),
            ("delete", f"/api/v1/journeys/{jid}", {}),
            ("post", "/api/v1/journeys", {"json": {"searchId": search_id, "candidateId": str(uuid4())}}),
            ("post", "/api/v1/route-searches", {"json": {"origin": ORIGIN, "destination": DESTINATION}}),
            ("get", f"/api/v1/route-searches/{search_id}", {}),
            ("get", "/api/v1/stops/nearby?latitude=36.35&longitude=127.38", {}),
        ]:
            with self.subTest(method=method, url=url):
                headers = kwargs.pop("headers", token)
                r = getattr(self.client, method)(url, headers=headers, **kwargs)
                self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "ACCOUNT_DELETED"))
        with self.assertRaises(WebSocketDisconnect) as ctx:
            with self.client.websocket_connect(stream, headers=token) as ws:
                ws.receive_json()
        self.assertEqual(ctx.exception.code, 4403, "WebSocket 신규 연결 거부")
        self.assertEqual(self.query("SELECT count(*), min(deleted_at) = max(deleted_at) FROM app.profiles "
                                    "WHERE user_id = %s", (self.user_a,)), [(1, True)], "프로필 재생성 없음")
        again = self.client.delete("/api/v1/me", headers=token)
        self.assertEqual(again.status_code, 202)
        self.assertEqual((again.json()["deletedAt"], again.json()["authBlockStatus"]),
                         (body["deletedAt"], "blocked"), "재요청은 기존 탈퇴 건")
        self.assertEqual(len(self.fake_admin.calls), 1, "차단이 확인된 계정은 다시 호출하지 않음")

        conn = self.db.connect()
        targets = collection_targets(self.settings, conn, datetime.now(UTC))
        conn.close()
        sources = {s for t in targets for s in t.sources}
        self.assertFalse(any(s == f"journey:{jid}" for s in sources), "이동 수집 대상 해제")
        self.assertFalse(any(s.endswith(search_id) or s.endswith(pending_search) for s in sources),
                         "탈퇴 계정 검색의 수집 대상 제외")
        jobs = self.worker(synthetic_provider()).run_jobs_pass()
        self.assertIn({"search_id": pending_search, "action": "account_deleted"}, jobs["searches"])
        self.assertEqual(self.query("SELECT status, summary->>'error' FROM app.route_searches WHERE id = %s",
                                    (pending_search,)), [("error", "account_deleted")])
        self.assertEqual(jobs["journeys"], [], "탈퇴 계정 이동은 재평가하지 않음")

    def test_auth_block_failure_is_retried_and_survives_restart(self):
        self.fake_admin.outcomes = ["http_error", "timeout"]
        self.client.get("/api/v1/me", headers=self.auth(self.user_a))
        first = self.client.delete("/api/v1/me", headers=self.auth(self.user_a))
        self.assertEqual(first.status_code, 202)
        self.assertEqual((first.json()["authBlockStatus"], first.json()["authBlockedAt"]), ("pending", None))
        self.assertEqual(self.client.get("/api/v1/me", headers=self.auth(self.user_a)).status_code, 403,
                         "Auth 차단 실패와 무관하게 서버 접근은 차단")
        second = self.client.delete("/api/v1/me", headers=self.auth(self.user_a))
        self.assertEqual((second.status_code, second.json()["authBlockStatus"]), (202, "pending"))
        self.assertEqual(len(self.fake_admin.calls), 2, "재요청이 차단을 다시 시도")
        self.assertEqual(self.query("SELECT auth_blocked_at FROM app.profiles WHERE user_id = %s", (self.user_a,)),
                         [(None,)])

        def not_configured(_settings):
            raise AuthAdminConfigError("미설정")

        unconfigured = self.worker()
        unconfigured.auth_admin_factory = not_configured
        self.assertEqual(unconfigured.retry_auth_blocks(self.settings, self.db),
                         [{"user_id": str(self.user_a), "status": "not_configured"}])
        # 워커 재시작: 새 프로세스(메모리 상태 없음)가 DB의 대기 상태로 재시도한다.
        restarted = self.worker()
        restarted.auth_admin_factory = lambda _s: self.fake_admin
        done = restarted.run_jobs_pass()["auth_blocks"]
        self.assertEqual([(d["user_id"], d["status"]) for d in done], [(str(self.user_a), "blocked")])
        self.assertIsNotNone(self.query("SELECT auth_blocked_at FROM app.profiles WHERE user_id = %s",
                                        (self.user_a,))[0][0])
        self.assertEqual(restarted.run_jobs_pass()["auth_blocks"], [], "간격 안에는 다시 시도하지 않음")
        self.assertEqual(restarted.retry_auth_blocks(self.settings, self.db, force=True), [], "대기 계정 없음")
        # API 재시작(새 DB 객체): 탈퇴 상태는 DB에 있으므로 그대로 차단.
        main.app.state.db = Database(self.db.params)
        self.assertEqual(self.client.get("/api/v1/journeys", headers=self.auth(self.user_a)).status_code, 403)

    def test_recovery_when_steps_fail(self):
        # 1) 탈퇴 트랜잭션 안에서 실패하면 아무것도 저장되지 않고 외부 호출도 없다.
        journey, _, _ = self.start()
        with mock.patch.object(journey_service, "cancel_for_account_deletion", side_effect=RuntimeError("중단")):
            with self.assertRaises(RuntimeError):
                user_service.delete_account(self.db, self.user_a, now=datetime.now(UTC),
                                            admin_factory=lambda: self.fake_admin)
        self.assertEqual(self.query("SELECT deleted_at FROM app.profiles WHERE user_id = %s", (self.user_a,)),
                         [(None,)])
        self.assertEqual(self.fake_admin.calls, [])
        self.assertEqual(self.detail(journey["journeyId"])["lifecycleStatus"], "active")
        # 2) 외부 차단은 성공했지만 auth_blocked_at 기록이 실패: 탈퇴는 유지되고 대기 상태로 남아 재시도로 복구한다.
        with mock.patch.object(user_repository, "mark_auth_blocked", side_effect=RuntimeError("DB 일시 오류")):
            r = self.client.delete("/api/v1/me", headers=self.auth(self.user_a))
        self.assertEqual((r.status_code, r.json()["authBlockStatus"]), (202, "pending"))
        self.assertEqual(self.query("SELECT deleted_at IS NOT NULL, auth_blocked_at FROM app.profiles "
                                    "WHERE user_id = %s", (self.user_a,)), [(True, None)])
        self.assertEqual(self.client.get(f"/api/v1/journeys/{journey['journeyId']}",
                                         headers=self.auth(self.user_a)).status_code, 403)
        # 3) 연동이 예외를 던져도 접수는 202로 끝나고 이후 재시도가 복구한다.
        self.fake_admin.outcomes = ["raise"]
        self.assertEqual(self.client.delete("/api/v1/me", headers=self.auth(self.user_a)).json()["authBlockStatus"],
                         "pending")
        retried = user_service.retry_pending_auth_blocks(self.db, admin_factory=lambda: self.fake_admin,
                                                         clock=lambda: datetime.now(UTC), limit=10)
        self.assertEqual([r["status"] for r in retried], ["blocked"])
        self.assertEqual(len(self.fake_admin.calls), 3, "같은 사용자 차단 요청을 다시 보내도 안전(멱등)")

    def test_account_without_profile_and_concurrent_requests(self):
        # 프로필을 만든 적 없는 계정도 탈퇴 상태로 기록되고, 이후 자동 생성되지 않는다.
        r = self.client.delete("/api/v1/me", headers=self.auth(self.user_b))
        self.assertEqual((r.status_code, r.json()["displayUserId"]), (202, f"{self.user_b}_deleted"))
        self.assertEqual(self.client.get("/api/v1/me", headers=self.auth(self.user_b)).status_code, 403)
        self.assertEqual(self.count("SELECT count(*) FROM app.profiles WHERE user_id = %s", (self.user_b,)), 1)
        unknown = uuid4()
        self.assertEqual(self.client.delete("/api/v1/me", headers=self.auth(unknown)).status_code, 401,
                         "Auth 사용자가 없는 토큰")
        self.assertEqual(self.client.delete("/api/v1/me").status_code, 401)

        # 동시 탈퇴 요청: 접수 시각 하나, 시스템 취소 이벤트 하나.
        journey, _, _ = self.start(self.user_c)
        barrier = threading.Barrier(3)

        def delete(_):
            barrier.wait()
            return user_service.delete_account(self.db, self.user_c, now=datetime.now(UTC),
                                               admin_factory=lambda: self.fake_admin)

        with ThreadPoolExecutor(max_workers=3) as pool:
            outcomes = list(pool.map(delete, range(3)))
        self.assertEqual(sum(o.first_request for o in outcomes), 1)
        self.assertEqual(len({o.profile["deleted_at"] for o in outcomes}), 1)
        self.assertEqual(self.count("SELECT count(*) FROM app.journey_events WHERE journey_id = %s AND "
                                    "event_type = 'cancelled'", (journey["journeyId"],)), 1)
        self.assertEqual(self.count("SELECT count(*) FROM app.profiles WHERE user_id = %s AND "
                                    "auth_blocked_at IS NOT NULL", (self.user_c,)), 1)

    def test_start_racing_deletion_never_leaves_active_journey(self):
        search_id, body = self.ready_search(self.user_c)
        candidate = next(c for c in body["candidates"] if c["rank"] is not None)["candidateId"]
        barrier = threading.Barrier(2)

        def start():
            barrier.wait()
            try:
                return journey_service.start_journey(self.db, self.user_c, UUID(search_id), UUID(candidate),
                                                     now=datetime.now(UTC))["id"]
            except journey_service.JourneyError as exc:
                return exc.code

        def delete():
            barrier.wait()
            time.sleep(0.01)
            return user_service.delete_account(self.db, self.user_c, now=datetime.now(UTC),
                                               admin_factory=lambda: self.fake_admin)

        with ThreadPoolExecutor(max_workers=2) as pool:
            started, deleted = pool.submit(start), pool.submit(delete)
            started, deleted = started.result(), deleted.result()
        self.assertEqual(self.count("SELECT count(*) FROM app.journeys WHERE user_id = %s AND "
                                    "lifecycle_status = 'active'", (self.user_c,)), 0, (started, deleted))
        if isinstance(started, UUID):
            self.assertEqual(deleted.cancelled_journey_ids, [started], "먼저 시작한 이동은 탈퇴가 취소")
        else:
            self.assertEqual(started, "ACCOUNT_DELETED")


if __name__ == "__main__":
    unittest.main()
