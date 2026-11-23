"""Phase 7 GPS-to-bus trajectory inference unit checks."""

import unittest
from datetime import datetime, timedelta, timezone

from app.modules.journeys.presence import estimate_boarding


UTC = timezone.utc


class BoardingEstimateTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 23, 10, 3, tzinfo=UTC)

    def moving_tracks(self):
        gps, buses = [], []
        for index in range(3):
            at = self.now - timedelta(minutes=2 - index)
            lon = 127.0 + index * 0.0008
            gps.append({"lat": 36.0, "lon": lon, "accuracy_m": 8, "observed_at": at})
            buses.append({"route_id": 42, "pattern_key": "route-pattern-a", "direction_code": "1",
                          "vehicle_id": "bus-7", "lat": 36.00001, "lon": lon,
                          "observed_at": at + timedelta(seconds=2)})
        return gps, buses

    def test_same_vehicle_trajectory_produces_confirmable_suggestion(self):
        gps, buses = self.moving_tracks()

        result = estimate_boarding(gps, buses, now=self.now, max_gps_age_seconds=120)

        self.assertEqual(result["status"], "suggested")
        self.assertEqual(result["confidence"], "high")
        self.assertEqual(result["suggestion"]["route_id"], "42")
        self.assertEqual(result["suggestion"]["pattern_key"], "route-pattern-a")
        self.assertEqual(result["suggestion"]["vehicle_id"], "bus-7")
        self.assertTrue(result["suggestion"]["requires_user_confirmation"])

    def test_stationary_user_track_does_not_suggest_boarding(self):
        gps, buses = self.moving_tracks()
        for point in gps:
            point["lon"] = 127.0

        result = estimate_boarding(gps, buses, now=self.now, max_gps_age_seconds=120)

        self.assertEqual(result["status"], "no_match")
        self.assertIsNone(result["suggestion"])

    def test_stale_or_missing_inputs_are_reported_without_suggestion(self):
        gps, buses = self.moving_tracks()

        stale = estimate_boarding(gps, buses, now=self.now + timedelta(minutes=10),
                                  max_gps_age_seconds=120)
        no_vehicle_positions = estimate_boarding(gps, [], now=self.now, max_gps_age_seconds=120)

        self.assertEqual(stale["reason_codes"], ["location_stale"])
        self.assertEqual(no_vehicle_positions["reason_codes"], ["vehicle_positions_unavailable"])
        self.assertIsNone(stale["suggestion"])
        self.assertIsNone(no_vehicle_positions["suggestion"])


if __name__ == "__main__":
    unittest.main()
