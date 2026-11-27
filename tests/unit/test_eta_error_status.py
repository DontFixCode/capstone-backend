"""ETA 오차 표본 현황 집계(scripts.eta_error_status가 쓰는 계산)의 DB 없는 시험."""

import unittest
from datetime import datetime, timedelta, timezone

from app.modules.evaluation.eta_errors import quantile, summarize_samples
from scripts import eta_error_status

UTC = timezone.utc
T0 = datetime(2026, 11, 27, 0, 0, tzinfo=UTC)


def row(run: str, bucket: str, lower: float, upper: float | None = None, *, source: str = "provider",
        model: str | None = None, basis: str | None = "daejeon_info_offer_tm", minutes: int = 0) -> dict:
    upper = lower if upper is None else upper
    return {"run_group": run, "eta_source": source, "model_version": model, "basis_source": basis,
            "horizon_bucket": bucket, "quality": "exact" if lower == upper else "bounded",
            "error_lower_seconds": lower, "error_upper_seconds": upper,
            "arrival_upper_at": T0 + timedelta(minutes=minutes), "created_at": T0 + timedelta(minutes=minutes + 1)}


class SummaryTest(unittest.TestCase):
    def test_quantile_interpolates(self):
        values = [0.0, 10.0, 20.0, 30.0, 40.0]
        self.assertEqual([quantile(values, q) for q in (0, 0.25, 0.5, 0.9, 1)], [0.0, 10.0, 20.0, 36.0, 40.0])
        self.assertEqual(quantile([7.0], 0.9), 7.0)
        self.assertIsNone(quantile([], 0.5))

    def test_groups_by_generation_method_and_bucket_without_merging_ranges(self):
        rows = [
            row("run-1", "120-300", -30), row("run-2", "120-300", 0), row("run-3", "120-300", 90, minutes=30),
            row("run-4", "120-300", -100, 60), row("run-5", "120-300", 20, 200),
            row("run-1", "600+", 45),
            row("run-9", "120-300", 300, source="leading_vehicle_gap", model="gap/1", basis=None),
        ]
        summary = summarize_samples(rows)
        self.assertEqual([(g["eta_source"], g["horizon_bucket"]) for g in summary],
                         [("leading_vehicle_gap", "120-300"), ("provider", "120-300"), ("provider", "600+")],
                         "생성 방식이 다른 표본은 다른 묶음이고, 구간은 잔여시간 순이다")
        group = summary[1]
        self.assertEqual((group["samples"], group["independent_runs"], group["by_quality"]),
                         (5, 5, {"exact": 3, "bounded": 2}))
        errors = group["error_seconds"]
        self.assertEqual(errors["exact"]["median"], 0.0, "점 시각 표본(−30, 0, 90)만의 중앙값")
        self.assertEqual(errors["lower"]["median"], 0.0, "하한(−100, −30, 0, 20, 90)의 중앙값")
        self.assertEqual(errors["upper"]["median"], 60.0, "상한(−30, 0, 60, 90, 200)의 중앙값")
        self.assertEqual(set(errors["lower"]), {"p10", "p25", "median", "p75", "p90"})
        self.assertEqual(group["bounded_width_seconds"]["median"], 170.0, "범위 표본의 폭 160, 180")
        self.assertEqual((group["latest_arrival_at"], group["latest_created_at"]),
                         (T0 + timedelta(minutes=30), T0 + timedelta(minutes=31)))
        self.assertIsNone(summary[2]["bounded_width_seconds"])
        self.assertEqual(summary[2]["error_seconds"]["exact"], {"p10": 45.0, "p25": 45.0, "median": 45.0, "p75": 45.0,
                                                               "p90": 45.0})
        self.assertEqual(summarize_samples([]), [])

    def test_cli_rejects_a_non_positive_window(self):
        self.assertEqual(eta_error_status.main(["--days", "0"]), 2)
        self.assertEqual(eta_error_status.build_parser().parse_args([]).days, None)


if __name__ == "__main__":
    unittest.main()
