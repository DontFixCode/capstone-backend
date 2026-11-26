"""예상 혼잡도(Phase 8) 단위 시험(DB·네트워크 없음): 원본 파일 해석, 흐름 집계, 단계 변환, 설정.

원본 형식은 2026-11-26에 확인한 플랫폼 내려받기 파일(HTML 표 .xls)의 구조를 줄여 만든 합성 값이다.
"""

import unittest
from datetime import date, datetime, timedelta, timezone

from app.core.config import Settings
from app.modules.congestion import aggregation, levels, service, source
from app.modules.congestion.aggregation import CountRow

UTC = timezone.utc
ROUTE = "301번(기점~회차지)"
STOPS = [("기점", "44490"), ("가", "44480"), ("회차지", ""), ("나", "44500"), ("기점", "")]


def export_html(days, *, hour="09:00", route=ROUTE, stops=STOPS, header=source.HEADERS, break_total=False,
                grand_total=True) -> bytes:
    """days: [(날짜 글자, [(단순승차, 하차, 환승승차) × 정류장 수])]. 실제 파일처럼 묶음 칸은 첫 행에만 둔다."""
    out = ["<html><head><meta charset=\"UTF-8\"></head><body><table><thead><tr>",
           "".join(f"<th>{name}</th>" for name in header), "</tr></thead><tbody>"]
    grand = [0, 0, 0]
    for index, (label, counts) in enumerate(days):
        sums = [0, 0, 0]
        for order, ((name, number), values) in enumerate(zip(stops, counts), start=1):
            lead = ""
            if order == 1:
                lead = f"<td rowspan=\"{len(stops) + 1}\">{label}</td><td rowspan=\"{len(stops) + 1}\">{route}</td>"
                if index == 0:
                    lead += f"<td rowspan=\"{len(days) * len(stops)}\">{hour}</td>"
            out.append(f"<tr>{lead}<td>{order}</td><td>{name}({number})</td>"
                       + "".join(f"<td>{value:,}</td>" for value in values) + "</tr>")
            sums = [a + b for a, b in zip(sums, values)]
        grand = [a + b for a, b in zip(grand, sums)]
        shown = [sums[0] + 1, *sums[1:]] if break_total and index == 0 else sums
        out.append("<tr><td colspan=\"2\">합계</td>" + "".join(f"<td>{value:,}</td>" for value in shown) + "</tr>")
    if grand_total:
        out.append("<tr><td colspan=\"5\">합계</td>" + "".join(f"<td>{value:,}</td>" for value in grand) + "</tr>")
    out.append("</tbody></table></body></html>")
    return "".join(out).encode("utf-8")


DAY1 = [(1200, 0, 0), (5, 300, 2), (0, 500, 0), (400, 0, 100), (0, 200, 0)]
DAY2 = [(10, 0, 0), (0, 4, 0), (0, 6, 0), (8, 0, 2), (0, 8, 0)]


class SourceFileTest(unittest.TestCase):
    def parse(self, data):
        return source.parse_export(data, file_name="t.xls")

    def test_rows_are_read_without_trusting_rowspans(self):
        parsed = self.parse(export_html([("2026년 10월 01일", DAY1), ("2026년 10월 02일", DAY2)]))
        self.assertEqual((len(parsed.rows), parsed.checked_totals, parsed.unchecked_totals, parsed.warnings),
                         (10, 2, 1, []))
        first, last = parsed.rows[0], parsed.rows[-1]
        self.assertEqual((first.route_label, first.service_date, first.hour, first.stop_order, first.stop_name,
                          first.stop_no, first.board, first.alight, first.transfer),
                         (ROUTE, date(2026, 10, 1), 9, 1, "기점", "44490", 1200, 0, 0), "천 단위 쉼표를 읽는다")
        self.assertEqual((last.service_date, last.hour, last.stop_order, last.stop_no), (date(2026, 10, 2), 9, 5, None),
                         "시간 칸은 첫 행에만 있어도 이어서 쓰고, 빈 정류소 번호는 None")
        self.assertEqual(source.route_parts(ROUTE), ("301", "기점", "회차지"))
        self.assertEqual(source.route_parts("급행2번"), ("급행2", None, None))

    def test_bad_files_are_rejected_whole_with_reasons(self):
        cases = {
            "앞 5행의 합": export_html([("2026년 10월 01일", DAY1)], break_total=True),
            "머리글": export_html([("2026년 10월 01일", DAY1)], header=(*source.HEADERS[:5], "총승차", "하차", "환승승차")),
            "월별": export_html([("2026년 10월", DAY1)]),
            "1시간 단위": export_html([("2026년 10월 01일", DAY1)], hour="09:30"),
            "없는 날짜": export_html([("2026년 02월 30일", DAY1)]),
            "0 이상의 정수": export_html([("2026년 10월 01일", DAY1)]).replace(b"<td>300</td>", b"<td>-3</td>"),
            "두 번": export_html([("2026년 10월 01일", DAY1), ("2026년 10월 01일", DAY1)]),
            "실제 엑셀": b"PK\x03\x04 not really a workbook",
            "표가 1개": b"<html><body>no table</body></html>",
        }
        for expected, data in cases.items():
            with self.subTest(expected=expected):
                with self.assertRaises(source.SourceFileError) as ctx:
                    self.parse(data)
                self.assertTrue(any(expected in reason for reason in ctx.exception.reasons), ctx.exception.reasons)

    def test_script_and_formula_text_is_only_text(self):
        data = export_html([("2026년 10월 01일", DAY1)]).replace(
            "가(44480)".encode(), "=HYPERLINK(1)<script>alert(1)</script>(44480)".encode())
        parsed = self.parse(data)
        self.assertEqual(parsed.rows[1].stop_no, "44480")
        self.assertIn("=HYPERLINK", parsed.rows[1].stop_name, "수식처럼 보이는 글자도 실행하지 않고 글자로만 둔다")
        missing_total = self.parse(export_html([("2026년 10월 01일", DAY1)], grand_total=False)
                                   .replace("<tr><td colspan=\"2\">합계</td><td>1,605</td><td>1,000</td><td>102</td></tr>"
                                            .encode(), b""))
        self.assertEqual((missing_total.checked_totals, len(missing_total.warnings)), (0, 1))


class AggregationTest(unittest.TestCase):
    PATTERN = [(1, 11), (2, 12), (3, 13), (4, 14), (5, 15)]

    @staticmethod
    def day(day, hour, values, stop_ids=(11, 12, 13, 14, 15)):
        return [CountRow(day, hour, order, stop_id, board, alight)
                for order, (stop_id, (board, alight)) in enumerate(zip(stop_ids, values), start=1)]

    def test_alpha_corrects_missing_alight_tags_and_load_never_goes_negative(self):
        # 총승차 100, 하차 태그 50 → α = 2. 누적: 60 → 60+40-2×10=80 → 80-2×40=0 → 0 (음수는 0)
        flows = aggregation.link_flows([(1, 60, 0), (2, 40, 10), (3, 0, 40), (4, 0, 0)], alpha=2.0)
        self.assertEqual(flows, {1: 60.0, 2: 80.0, 3: 0.0, 4: 0.0})
        self.assertEqual(aggregation.link_flows([(1, 0, 5), (2, 3, 0)], 1.0), {1: 0.0, 2: 3.0})

    def test_day_types_window_and_percentile(self):
        holidays = [date(2026, 10, 9)]
        self.assertEqual([aggregation.day_type(date(2026, 10, d), holidays) for d in (8, 9, 10, 11)],
                         ["weekday", "holiday", "saturday", "holiday"], "평일에 낀 공휴일과 일요일은 holiday")
        self.assertEqual(aggregation.window_start(datetime(2026, 11, 26, 14, 0, tzinfo=UTC), 3), date(2026, 8, 26))
        self.assertEqual(aggregation.window_start(datetime(2026, 5, 31, 3, 0, tzinfo=UTC), 3), date(2026, 2, 28),
                         "같은 날짜가 없는 달은 그 달의 마지막 날")
        self.assertEqual(aggregation.window_start(datetime(2026, 1, 31, 16, 0, tzinfo=UTC), 1), date(2026, 1, 1),
                         "집계 시점의 날짜는 한국 시각(2월 1일) 기준")
        self.assertEqual(aggregation.percentile([10, 20, 30, 40, 50], 0.8), 42.0)
        self.assertEqual(aggregation.percentile([7], 0.8), 7)

    def test_profiles_by_day_type_with_mean_p80_and_timetable_trips(self):
        info = {"fields": {"intervaltime": {"value": 10}, "intervalsattime": {"value": 15},
                           "intervalsuntime": {"value": 0}}}
        thursday, friday, saturday = date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3)
        rows = [*self.day(thursday, 9, [(100, 0), (0, 20), (0, 60), (40, 0), (0, 20)]),   # α = 140 / 100
                *self.day(friday, 9, [(50, 0), (0, 10), (0, 30), (20, 0), (0, 20)]),      # α = 70 / 60
                # 같은 날 10시는 탄 사람보다 내린 사람이 많다(9시에 탄 승객). α는 시간대마다 따로 구한다: 30 / 60.
                *self.day(friday, 10, [(30, 0), (0, 20), (0, 40), (0, 0), (0, 0)]),
                *self.day(saturday, 9, [(10, 0), (0, 0), (0, 10), (0, 0), (0, 0)])]
        result = aggregation.build_profiles(rows, "p1:x", self.PATTERN, service_info=info, min_sample_days=2)
        self.assertEqual((result.status, result.days_used, result.matched_stops, result.source_period),
                         ("computed", 3, 5, "2026-10-01/2026-10-03"))
        by_key = {(p.day_type, p.stop_seq): p for p in result.profiles if p.hour == 9}
        first = by_key[("weekday", 1)]
        self.assertEqual((first.flow_mean, first.flow_p80, first.sample_days, first.hour), (75.0, 90.0, 2, 9))
        self.assertAlmostEqual(by_key[("weekday", 2)].flow_mean, ((100 - 1.4 * 20) + (50 - 70 / 60 * 10)) / 2)
        by_hour = {(p.hour, p.stop_seq): p.flow_mean for p in result.profiles if p.day_type == "weekday"}
        self.assertEqual((by_hour[(10, 1)], by_hour[(10, 2)], by_hour[(10, 3)]), (30.0, 20.0, 0.0),
                         "하루 합계가 아니라 그 시간대의 합으로 보정해 시간대 안에서 닫힌다")
        self.assertEqual((first.trips_per_hour, first.trips_source), (6.0, "timetable"))
        saturday_row = by_key[("saturday", 1)]
        self.assertEqual((saturday_row.flow_mean, saturday_row.flow_p80, saturday_row.trips_per_hour), (10.0, None, 4.0),
                         "표본 일수가 최소보다 적으면 p80을 만들지 않는다")
        self.assertNotIn(("holiday", 1), by_key)
        self.assertIsNone(aggregation.timetable_trips_per_hour(info, "holiday"), "범위를 벗어난 배차간격은 쓰지 않는다")

    def test_changed_stop_sequence_is_realigned_and_unknown_routes_are_held(self):
        day = date(2026, 10, 1)
        rows = self.day(day, 9, [(100, 0), (0, 50), (0, 50), (0, 0), (0, 0)])
        # 현재 패턴에 정류소가 하나 늘고(99) 원본의 두 번째 정류소(12)는 없어졌다.
        pattern = [(1, 11), (2, 99), (3, 13), (4, 14), (5, 15)]
        self.assertEqual(aggregation.align_stops([(1, 11), (2, 12), (3, 13)], [(1, 11), (2, 99), (3, 13)]),
                         {1: 1, 3: 3})
        held = aggregation.build_profiles(rows, "p", pattern, service_info=None)
        self.assertEqual((held.status, held.reason, held.matched_stops, held.profiles),
                         ("held", "stop_alignment_below_threshold", 4, []), "대응 비율이 기준 미만이면 보류")
        long_day = [CountRow(day, 9, order, 100 + order, 10 if order == 1 else 0, 1 if order == 20 else 0)
                    for order in range(1, 21)]
        longer = [(seq, 100 + seq) for seq in range(1, 11)] + [(11, 999)] + [(seq + 1, 100 + seq) for seq in range(11, 21)]
        result = aggregation.build_profiles(long_day, "p", longer, service_info=None)
        self.assertEqual((result.status, result.matched_stops, len(result.profiles)), ("computed", 20, 21))
        flows = {p.stop_seq: p.flow_mean for p in result.profiles}
        self.assertEqual((flows[10], flows[11], flows[12]), (10.0, 10.0, 10.0), "원본에 없는 새 정류소는 앞 구간 값을 잇는다")
        self.assertTrue(all(p.trips_per_hour is None and p.trips_source is None for p in result.profiles))

    def test_days_without_alight_tags_and_empty_windows(self):
        day = date(2026, 10, 1)
        no_tags = aggregation.build_profiles(self.day(day, 9, [(5, 0)] * 5), "p", self.PATTERN, service_info=None)
        self.assertEqual((no_tags.status, no_tags.reason, no_tags.hours_skipped),
                         ("no_data", "no_correctable_hour", 1))
        mixed = aggregation.build_profiles([*self.day(day, 9, [(5, 0)] * 5),
                                            *self.day(day, 10, [(4, 0), (0, 2), (0, 2), (0, 0), (0, 0)])],
                                           "p", self.PATTERN, service_info=None)
        self.assertEqual((mixed.status, mixed.hours_skipped, mixed.days_used, {p.hour for p in mixed.profiles}),
                         ("computed", 1, 1, {10}), "보정할 수 없는 시간대만 빼고 그날의 다른 시간대는 쓴다")
        self.assertEqual(aggregation.build_profiles([], "p", self.PATTERN, service_info=None).reason,
                         "no_rows_in_window")


class ObservedTripsTest(unittest.TestCase):
    """위치 관측으로 센 시간당 통과 횟수. 정류소 31개(구간 30개), 10분마다 수집, 시작은 한국 시각 목요일 09:00."""

    START = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)

    def times(self, count, step=600):
        return [self.START + timedelta(seconds=step * index) for index in range(count)]

    def track(self, vehicle, seqs, times=None):
        return [aggregation.VehiclePoint(at, vehicle, seq) for at, seq in zip(times or self.times(len(seqs)), seqs)
                if seq is not None]

    def test_only_vehicles_that_actually_pass_a_link_are_counted(self):
        snapshots = self.times(13)                                          # 09:00 ~ 11:00
        points = [
            *self.track("A", [1, 6, 11, 16, 21, 26, 31, 31, 31, 31, 31, 31, 31]),   # 9시대에 전 구간을 한 번 지남
            *self.track("B", [1, 1, 1, 1, 1, 1, 1, 6, 11, 16, 21, 26, 31]),         # 9시대는 기점 대기, 10시대에 운행
            *self.track("C", [21, 26, 31, 5, 10, 15, 20]),                          # 종점을 지나 기점에서 다시 나옴
        ]
        rates = aggregation.observed_link_rates(snapshots, points, 31, max_gap_seconds=1260)
        nine, ten = rates[(date(2026, 10, 1), 9)], rates[(date(2026, 10, 1), 10)]
        self.assertEqual((nine[1], nine[19], nine[20], nine[25], nine[30]), (2.0, 2.0, 1.0, 2.0, 2.0),
                         "대기 중인 B는 세지 않고, C는 종점까지와 기점부터의 구간을 센다")
        self.assertEqual(set(ten[1:31]), {1.0}, "10시대에는 B만 지나간다")

    def test_gaps_jumps_and_thin_coverage_are_not_used(self):
        # 수집이 20분만 있는 시간대는 값을 만들지 않는다.
        short = aggregation.observed_link_rates(self.times(3), self.track("A", [1, 6, 11]), 31, max_gap_seconds=1260)
        self.assertEqual(short, {})
        # 10분에 50구간을 뛴 관측(정류소당 20초보다 빠름)과 간격이 긴 쌍은 세지 않는다.
        snapshots = self.times(7)
        jump = self.track("J", [10, 60, 62, 64, 66, 68, 70])
        rates = aggregation.observed_link_rates(snapshots, jump, 101, max_gap_seconds=1260)[(date(2026, 10, 1), 9)]
        self.assertEqual((rates[30], rates[61], rates[69]), (0.0, 1.0, 1.0))
        sparse_times = [self.START, self.START + timedelta(seconds=3000), self.START + timedelta(seconds=3600)]
        sparse = aggregation.observed_link_rates(sparse_times, self.track("S", [1, 20, 25], sparse_times), 31,
                                                 max_gap_seconds=1260)
        self.assertEqual(sparse, {}, "간격이 긴 수집은 이어진 관측으로 보지 않는다")
        empty_hour = aggregation.observed_link_rates(self.times(7), [], 31, max_gap_seconds=1260)
        self.assertEqual(set(empty_hour[(date(2026, 10, 1), 9)]), {0.0}, "수집은 했지만 차량이 없던 시간대는 0이다")

    def test_profiles_use_observed_trips_and_fall_back_to_timetable(self):
        snapshots = self.times(7)
        points = [*self.track("A", [1, 2, 3, 4, 5, 5, 5]), *self.track("B", [1, 1, 1, 2, 3, 4, 5]),
                  *self.track("C", [1, 2, 2, 2, 2, 2, 2])]
        observed = aggregation.observed_trips(snapshots, points, 5, max_gap_seconds=1260)
        self.assertEqual(observed.days, {("weekday", 9): 1})
        self.assertEqual([observed.rates[("weekday", 9, seq)] for seq in range(1, 6)], [3.0, 2.0, 2.0, 2.0, 2.0],
                         "마지막 순번은 바로 앞 구간의 값을 쓴다")
        self.assertEqual(aggregation.observed_trips(snapshots, points, 5, max_gap_seconds=1260, min_days=2).days, {},
                         "관측 일수가 모자라면 쓰지 않는다")
        info = {"fields": {"intervaltime": {"value": 10}}}
        thursday = date(2026, 10, 1)
        rows = [CountRow(thursday, hour, order, 10 + order, board, alight)
                for hour in (9, 10) for order, (board, alight) in enumerate([(60, 0), (0, 30), (0, 30), (0, 0), (0, 0)], 1)]
        pattern = [(seq, 10 + seq) for seq in range(1, 6)]
        quiet = aggregation.ObservedTrips({("weekday", 9): 1}, {("weekday", 9, 1): 3.0, ("weekday", 9, 2): 0.5})
        result = aggregation.build_profiles(rows, "p", pattern, service_info=info, observed=quiet)
        by_key = {(p.hour, p.stop_seq): (p.trips_per_hour, p.trips_source) for p in result.profiles}
        self.assertEqual(by_key[(9, 1)], (3.0, "observed"))
        self.assertEqual(by_key[(9, 2)], (None, None), "시간당 1대 미만으로 지나간 구간은 1대당 재차를 만들지 않는다")
        self.assertEqual(by_key[(9, 3)], (None, None))
        self.assertEqual(by_key[(10, 1)], (6.0, "timetable"), "관측이 없는 시간대는 배차간격으로 대신한다")


class LevelTest(unittest.TestCase):
    POLICY = levels.LevelPolicy(seats=25, capacity=50, min_sample_days=5, holidays=frozenset({date(2026, 10, 9)}))
    THURSDAY_9 = datetime(2026, 10, 1, 0, 30, tzinfo=UTC)        # 한국 시각 09:30

    @staticmethod
    def row(flow_mean, flow_p80=None, trips=6.0, sample_days=20):
        return {"flow_mean": flow_mean, "flow_p80": flow_p80, "trips_per_hour": trips,
                "trips_source": "timetable" if trips else None, "sample_days": sample_days,
                "source_period": "2026-08-01/2026-10-31"}

    def test_five_levels_use_seat_and_capacity_bounds(self):
        loads = [5, 5.1, 25, 25.1, 37.5, 37.6, 50, 50.1]
        self.assertEqual([levels.LEVELS[levels.level_of(load, self.POLICY) - 1] for load in loads],
                         ["empty", "relaxed", "relaxed", "normal", "normal", "crowded", "crowded", "very_crowded"])

    def test_leg_uses_its_most_crowded_link_and_prefers_p80(self):
        profiles = {("weekday", 9, 3): self.row(60, 90), ("weekday", 9, 4): self.row(200, 240),
                    ("weekday", 9, 5): self.row(400), ("weekday", 9, 6): self.row(999)}
        result = levels.describe(profiles, self.POLICY, at=self.THURSDAY_9, from_seq=3, to_seq=6)
        self.assertEqual((result["status"], result["stop_seq"], result["load_per_vehicle"], result["basis"],
                          result["level"], result["level_index"]), ("available", 5, 66.7, "mean", "very_crowded", 5),
                         "하차 정류소를 출발한 뒤 구간(6)은 타지 않는다")
        one = levels.describe(profiles, self.POLICY, at=self.THURSDAY_9, from_seq=4)
        self.assertEqual((one["load_per_vehicle"], one["basis"], one["level"], one["fallback"], one["low_confidence"]),
                         (40.0, "p80", "crowded", None, False))
        self.assertEqual((one["day_type"], one["hour"], one["policy_version"]), ("weekday", 9, self.POLICY.version))
        self.assertEqual(levels.view(one)["loadPerVehicle"], 40.0)
        self.assertEqual(levels.view(one)["kind"], "typical_for_time")

    def test_missing_rows_fall_back_and_unknowns_are_reported(self):
        profiles = {("weekday", 8, 1): self.row(60), ("weekday", 10, 1): self.row(120, sample_days=3),
                    ("weekday", 13, 1): self.row(300), ("saturday", 15, 1): self.row(30, trips=None)}
        near = levels.describe(profiles, self.POLICY, at=self.THURSDAY_9, from_seq=1)
        self.assertEqual((near["fallback"], near["load_per_vehicle"], near["low_confidence"], near["sample_days"]),
                         ("adjacent_hours", 15.0, True, 3), "앞뒤 시간대 평균, 표본이 적은 쪽을 신뢰도로 쓴다")
        holiday = levels.describe(profiles, self.POLICY, at=datetime(2026, 10, 9, 4, 0, tzinfo=UTC), from_seq=1)
        self.assertEqual((holiday["day_type"], holiday["hour"], holiday["fallback"], holiday["load_per_vehicle"]),
                         ("holiday", 13, "weekday", 50.0), "공휴일 행이 없으면 평일 같은 시간대")
        cases = {
            levels.TIME_MISSING: dict(at=datetime(2026, 10, 1, 9, 0, tzinfo=UTC), from_seq=1),
            levels.TRIPS_UNKNOWN: dict(at=datetime(2026, 10, 3, 6, 0, tzinfo=UTC), from_seq=1),
            levels.TIME_UNKNOWN: dict(at=None, from_seq=1),
        }
        for reason, kwargs in cases.items():
            with self.subTest(reason=reason):
                result = levels.describe(profiles, self.POLICY, **kwargs)
                self.assertEqual((result["status"], result["level"], result["reason"]), ("unavailable", None, reason))
        self.assertEqual(levels.describe({}, self.POLICY, at=self.THURSDAY_9, from_seq=1)["reason"],
                         levels.PROFILE_MISSING)
        self.assertEqual(levels.describe(profiles, None, at=self.THURSDAY_9, from_seq=1)["reason"],
                         levels.NOT_CONFIGURED)
        self.assertIsNone(levels.view(None))

    def test_policy_needs_seats_and_capacity_from_settings(self):
        self.assertIsNone(levels.policy_from_settings(Settings(_env_file=None)), "근거 없는 기본값을 두지 않는다")
        self.assertIsNone(levels.policy_from_settings(Settings(_env_file=None, congestion_seats=50,
                                                               congestion_capacity=25)))
        settings = Settings(_env_file=None, congestion_seats="25", congestion_capacity="50",
                            congestion_min_sample_days="", congestion_holidays="2026-10-09, 2026-10-03,2026-10-09")
        policy = levels.policy_from_settings(settings)
        self.assertEqual((policy.seats, policy.capacity, policy.min_sample_days, sorted(policy.holidays)),
                         (25, 50, None, [date(2026, 10, 3), date(2026, 10, 9)]))
        other = levels.policy_from_settings(Settings(_env_file=None, congestion_seats=30, congestion_capacity=50))
        self.assertNotEqual(policy.version, other.version, "문턱값이 바뀌면 설정 버전이 바뀐다")
        with self.assertRaises(ValueError):
            Settings(_env_file=None, congestion_holidays="10월 9일")

    def test_ride_bias_is_ride_time_times_level_weight_minus_one(self):
        settings = Settings(_env_file=None, congestion_seats=25, congestion_capacity=50,
                            congestion_cost_weights="1.0, 1.0,1.2,1.5,2.0")
        policy = levels.policy_from_settings(settings)
        self.assertEqual(policy.cost_weights, (1.0, 1.0, 1.2, 1.5, 2.0))
        bias = [levels.ride_bias_seconds({"level_index": index}, 600.0, policy) for index in range(1, 6)]
        self.assertEqual([round(value, 6) for value in bias], [0.0, 0.0, 120.0, 300.0, 600.0])
        # 모르는 것은 0이 아니라 None이다: 단계 미확인, 탑승시간 없음, 가중치 설정 없음, 단계 정책 없음.
        self.assertIsNone(levels.ride_bias_seconds(levels.unavailable(levels.PROFILE_MISSING, policy), 600.0, policy))
        self.assertIsNone(levels.ride_bias_seconds({"level_index": 5}, None, policy))
        self.assertIsNone(levels.ride_bias_seconds({"level_index": 5}, 600.0, self.POLICY))
        self.assertIsNone(levels.ride_bias_seconds({"level_index": 5}, 600.0, None))
        self.assertEqual(self.POLICY.cost_weights, (), "가중치를 설정하지 않으면 순위에 반영하지 않는다")
        self.assertNotEqual(policy.version, levels.policy_from_settings(
            Settings(_env_file=None, congestion_seats=25, congestion_capacity=50)).version,
            "가중치가 바뀌면 설정 버전이 바뀐다")
        for wrong in ("1,1,1.2,1.5", "1,1,1.2,1.5,2,3", "0.9,1,1.2,1.5,2", "1,1,1.5,1.2,2", "1,1,1.2,1.5,inf"):
            with self.subTest(weights=wrong), self.assertRaises(ValueError):
                Settings(_env_file=None, congestion_cost_weights=wrong)
        self.assertEqual(Settings(_env_file=None, congestion_cost_weights="").congestion_cost_weights, ())


class StopMappingTest(unittest.TestCase):
    def test_stops_are_matched_in_route_order_by_number_then_name(self):
        parsed = source.parse_export(export_html([("2026년 10월 01일", DAY1)]), file_name="t.xls")
        pattern = [{"seq": 1, "stop_id": 501, "stop_no": "44490", "name": "기점"},
                   {"seq": 2, "stop_id": 502, "stop_no": "44480", "name": "가"},
                   {"seq": 3, "stop_id": 503, "stop_no": None, "name": "회차지"},
                   {"seq": 4, "stop_id": 504, "stop_no": "44500", "name": "나(이름 변경)"},
                   {"seq": 5, "stop_id": 505, "stop_no": None, "name": "기점"}]
        mapping, key, unmatched = service._map_stops(parsed.rows, {"p1:a": pattern})
        self.assertEqual((mapping, key, unmatched), ({1: 501, 2: 502, 3: 503, 4: 504, 5: 505}, "p1:a", []),
                         "번호가 없는 기점·회차지는 이름으로, 같은 이름의 처음과 끝은 순서로 구분")
        other = [{"seq": 1, "stop_id": 9, "stop_no": "99999", "name": "다른 노선"}]
        mapping, key, unmatched = service._map_stops(parsed.rows, {"p1:b": other, "p1:a": pattern[:2]})
        self.assertEqual((sorted(mapping), key, [u["order"] for u in unmatched]), ([1, 2], "p1:a", [3, 4, 5]))


if __name__ == "__main__":
    unittest.main()
