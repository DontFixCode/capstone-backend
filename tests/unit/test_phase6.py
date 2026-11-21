"""Phase 6 단위 시험(DB·네트워크 없음): 환승 결과 대조 규칙, Supabase Auth 관리자 차단 요청, 요청 형식, 표시용 ID."""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
from pydantic import ValidationError

from app.core.config import Settings
from app.integrations.supabase_auth import AuthAdminConfigError, SupabaseAuthAdmin, load_auth_admin
from app.modules.evaluation import user_results as ur
from app.modules.journeys.schemas import CorrectionPayload, EventBody, TransferResultPayload
from app.modules.users.schemas import AccountDeletionResponse
from app.modules.users.service import display_user_id
from app.workflows.guidance import evaluation_snapshots

UTC = timezone.utc
SNAP = {"snapshot_key": "J:s1:t1", "selection_version": 1, "transfer_index": 1, "front_leg_index": 1,
        "next_leg_index": 3, "predicted_probability": 0.8, "slack_seconds": 120, "connection_status": "feasible",
        "front_route": {"route_internal_id": 11}, "next_route": {"route_internal_id": 22},
        "front_vehicle": {"vehicle_id": "V-1"}, "next_vehicle": None}


def effect(result, *, key="J:s1:t1", supersedes=None, match=ur.MATCH_UNVERIFIED, excluded=()):
    e = {"kind": ur.EFFECT_KIND, "snapshot_key": key, "result": result, "actual_connection": {},
         "connection_match": match, "excluded_reasons": list(excluded)}
    if supersedes is not None:
        e["supersedes_event_id"] = supersedes
    return e


def event(eid, etype, eff):
    return {"id": eid, "event_type": etype, "payload": {"effect": eff}}


class ConnectionMatchTest(unittest.TestCase):
    def test_no_evidence_is_unverified(self):
        self.assertEqual(ur.match_connection(SNAP, {}, None), (ur.MATCH_UNVERIFIED, None, []))
        self.assertEqual(ur.match_connection(SNAP, {}, {"front_route": None, "next_route": None})[0],
                         ur.MATCH_UNVERIFIED)

    def test_user_report_overrides_recorded_boarding(self):
        match, evidence, diffs = ur.match_connection(SNAP, {"nextRouteId": "22"}, {"next_route": 99})
        self.assertEqual((match, evidence, diffs), (ur.MATCH_PLANNED, "user_reported", []))
        match, evidence, diffs = ur.match_connection(SNAP, {"nextRouteId": "23"}, None)
        self.assertEqual((match, evidence), (ur.MATCH_DIFFERENT, "user_reported"))
        self.assertIn("next_route_differs", diffs)

    def test_recorded_boarding_and_unknown_planned_vehicle(self):
        match, evidence, _ = ur.match_connection(SNAP, {}, {"front_route": 11, "next_route": 22, "next_vehicle": "X"})
        self.assertEqual((match, evidence), (ur.MATCH_PLANNED, "recorded_boarding"), "계획 차량 미확인이면 차량 비교 안 함")
        match, _, diffs = ur.match_connection(SNAP, {}, {"front_vehicle": "V-2"})
        self.assertEqual((match, diffs), (ur.MATCH_DIFFERENT, ["front_vehicle_differs"]))

    def test_used_planned_flag(self):
        self.assertEqual(ur.match_connection(SNAP, {"usedPlanned": True}, None)[0], ur.MATCH_PLANNED)
        match, _, diffs = ur.match_connection(SNAP, {"usedPlanned": False}, None)
        self.assertEqual((match, diffs), (ur.MATCH_DIFFERENT, ["user_reported_other_connection"]))
        self.assertEqual(ur.exclusion_reasons("failure", match, diffs),
                         ["different_connection", "user_reported_other_connection"],
                         "다른 연결 이용은 실패로 채우지 않고 대조에서 제외")
        self.assertEqual(ur.exclusion_reasons("unknown", ur.MATCH_PLANNED, []), [])


class ResolveTest(unittest.TestCase):
    def test_latest_correction_wins_and_counts_once(self):
        events = [event(5, "transfer_result", effect("success")),
                  event(9, "correction", effect("failure", supersedes=5)),
                  event(12, "correction", effect("unknown", supersedes=9)),
                  event(7, "board", {})]
        found = ur.resolve(events)["J:s1:t1"]
        self.assertEqual((found.result, found.original_event_id, found.effective_event_id, found.chain),
                         ("unknown", 5, 12, (5, 9, 12)))
        self.assertFalse(found.counted, "unknown은 집계하지 않음")
        self.assertEqual(ur.chain_of(events, 9).effective_event_id, 12)
        self.assertIsNone(ur.chain_of(events, 7))

    def test_comparison_and_summary(self):
        snaps = [SNAP, {**SNAP, "snapshot_key": "J:s1:t2", "transfer_index": 2},
                 {**SNAP, "snapshot_key": "J:s1:t3", "transfer_index": 3},
                 {**SNAP, "snapshot_key": "J:s1:t4", "transfer_index": 4}]
        events = [event(1, "transfer_result", effect("failure")),
                  event(2, "correction", effect("success", supersedes=1)),
                  event(3, "transfer_result", effect("unknown", key="J:s1:t2")),
                  event(4, "transfer_result", effect("failure", key="J:s1:t3", match=ur.MATCH_DIFFERENT,
                                                     excluded=["different_connection"]))]
        rows = ur.comparison_rows(snaps, ur.resolve(events))
        self.assertEqual([r["status"] for r in rows], ["counted", "unknown", "excluded", "no_response"])
        self.assertEqual(rows[0]["result"], "success")
        self.assertEqual(ur.summarize(rows), {"snapshots": 4, "responded": 3, "counted": 1, "success": 1,
                                              "failure": 0, "unknown": 1, "excluded": 1, "no_response": 1},
                         "재전송·수정이 있어도 환승 하나는 한 번만, 미응답·unknown·다른 연결은 실패로 세지 않음")


class AuthAdminTest(unittest.TestCase):
    def client(self, handler):
        return SupabaseAuthAdmin("http://auth.test:9999/", "service-role-secret", ban_duration="876000h",
                                 timeout_seconds=1, transport=httpx.MockTransport(handler))

    def test_block_request_shape(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"id": "x", "email": "private@example.invalid"})

        uid = uuid4()
        with self.client(handler) as admin:
            result = admin.block_new_logins(uid)
            self.assertNotIn("service-role-secret", repr(admin))
        self.assertEqual((result.ok, result.status, result.http_status), (True, "blocked", 200))
        req = seen[0]
        self.assertEqual((req.method, str(req.url)), ("PUT", f"http://auth.test:9999/admin/users/{uid}"))
        self.assertEqual(json.loads(req.content), {"ban_duration": "876000h"}, "물리 삭제가 아니라 차단 기간만 건다")
        self.assertEqual(req.headers["authorization"], "Bearer service-role-secret")
        self.assertEqual(req.headers["apikey"], "service-role-secret")

    def test_failures_are_classified(self):
        def raise_timeout(request):
            raise httpx.ReadTimeout("slow", request=request)

        def raise_connect(request):
            raise httpx.ConnectError("down", request=request)

        cases = [(lambda r: httpx.Response(500), ("http_error", 500)),
                 (lambda r: httpx.Response(404), ("http_error", 404)),
                 (raise_timeout, ("timeout", None)), (raise_connect, ("network_error", None))]
        for handler, expected in cases:
            with self.subTest(expected=expected), self.client(handler) as admin:
                with self.assertLogs("app.integrations.supabase_auth", "WARNING") as logs:
                    result = admin.block_new_logins(uuid4())
                self.assertFalse(result.ok)
                self.assertEqual((result.status, result.http_status), expected)
                self.assertFalse(any("service-role-secret" in line for line in logs.output))

    def test_load_requires_url_and_key_with_root_env_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("SERVICE_ROLE_KEY=from-root-env\n", encoding="utf-8")
            with self.assertRaises(AuthAdminConfigError) as ctx:
                load_auth_admin(Settings(_env_file=None), env_file=env)
            self.assertIn("BACKEND_AUTH_ADMIN_URL", str(ctx.exception))
            admin = load_auth_admin(Settings(_env_file=None, auth_admin_url="http://auth:9999"), env_file=env)
            self.assertEqual(admin._key, "from-root-env")
            admin.close()
            empty = Path(tmp) / "empty.env"
            empty.write_text("", encoding="utf-8")
            with self.assertRaises(AuthAdminConfigError) as ctx:
                load_auth_admin(Settings(_env_file=None, auth_admin_url="http://auth:9999"), env_file=empty)
            self.assertIn("SERVICE_ROLE_KEY", str(ctx.exception))
            admin = load_auth_admin(Settings(_env_file=None, auth_admin_url="http://auth:9999",
                                             auth_service_role_key="from-settings"), env_file=empty)
            self.assertEqual(admin._key, "from-settings")
            admin.close()

    def test_ban_duration_setting_is_validated(self):
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, auth_ban_duration="forever")
        self.assertEqual(Settings(_env_file=None).auth_ban_duration, "876000h")


class PayloadTest(unittest.TestCase):
    def test_transfer_result_payload(self):
        ok = TransferResultPayload.model_validate({"snapshotKey": "k", "selectionVersion": 1, "transferIndex": 1,
                                                   "result": "unknown"}).model_dump(exclude_unset=True)
        self.assertNotIn("actualConnection", ok)
        full = TransferResultPayload.model_validate({
            "snapshotKey": "k", "selectionVersion": 1, "transferIndex": 2, "result": "success",
            "actualConnection": {"usedPlanned": True, "nextRouteId": "42"}}).model_dump(exclude_unset=True)
        self.assertEqual(full["actualConnection"], {"usedPlanned": True, "nextRouteId": "42"})
        for bad in [{"result": "late"}, {"transferIndex": 0}, {"selectionVersion": 0}, {"extra": 1},
                    {"actualConnection": {"nextRouteId": "abc"}}, {"actualConnection": {"note": "x"}}]:
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                TransferResultPayload.model_validate({"snapshotKey": "k", "selectionVersion": 1, "transferIndex": 1,
                                                      "result": "success", **bad})

    def test_correction_accepts_result_fields_and_event_type_list(self):
        c = CorrectionPayload.model_validate({"supersedesEventId": "7", "result": "failure"})
        self.assertEqual(c.model_dump(exclude_unset=True), {"supersedesEventId": "7", "result": "failure"})
        with self.assertRaises(ValidationError):
            CorrectionPayload.model_validate({"supersedesEventId": "7", "result": "maybe"})
        EventBody.model_validate({"eventType": "transfer_result", "stateVersion": 1, "payload": {}})
        with self.assertRaises(ValidationError):
            EventBody.model_validate({"eventType": "evaluation_snapshot", "stateVersion": 1, "payload": {}})


class DisplayAndSnapshotTest(unittest.TestCase):
    def test_display_id_suffix_only_for_deleted(self):
        uid = uuid4()
        self.assertEqual(display_user_id({"user_id": uid, "deleted_at": None}), str(uid))
        self.assertEqual(display_user_id({"user_id": uid, "deleted_at": datetime.now(UTC)}), f"{uid}_deleted")
        now = datetime.now(UTC)
        body = AccountDeletionResponse.from_outcome(SimpleNamespace(
            profile={"user_id": uid, "deleted_at": now, "auth_blocked_at": None}, cancelled_journey_ids=[uid]))
        self.assertEqual((body.displayUserId, body.authBlockStatus, body.cancelledJourneyIds),
                         (f"{uid}_deleted", "pending", [str(uid)]))

    def test_snapshot_keeps_planned_routes(self):
        legs = [{"leg_index": 0, "kind": "walk"},
                {"leg_index": 1, "kind": "bus", "route_id": "R1", "route_internal_id": 11, "route_no": "1",
                 "pattern_key": "p1", "alight_eta": None, "vehicle": None},
                {"leg_index": 2, "kind": "walk"},
                {"leg_index": 3, "kind": "bus", "route_id": "R2", "route_internal_id": 22, "route_no": "2",
                 "pattern_key": "p2", "board_eta": None, "vehicle": {"vehicle_id": "V"}}]
        cand = {"id": uuid4(), "search_id": uuid4(), "static_route_key": "s", "option_key": "o", "legs": legs,
                "evaluation": {"transfers": [{"transfer_index": 1, "front_leg_index": 1, "next_leg_index": 3}]},
                "evaluated_at": datetime.now(UTC), "journey_probability": None}
        [(key, payload)] = evaluation_snapshots(uuid4(), cand, selection_version=1, config_version="c",
                                                fixed_at=datetime.now(UTC))
        self.assertEqual(key, "evaluation_snapshot:s1:t1")
        self.assertEqual((payload["front_route"]["route_internal_id"], payload["next_route"]["route_internal_id"]),
                         (11, 22))


if __name__ == "__main__":
    unittest.main()
