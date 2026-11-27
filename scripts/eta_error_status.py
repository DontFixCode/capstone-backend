"""ETA 오차 표본 현황 CLI(읽기 전용). DB에 쓰지 않고 외부 API도 호출하지 않는다.

    uv run python -m scripts.eta_error_status            # 전체 표본
    uv run python -m scripts.eta_error_status --days 14  # 최근 14일에 도착한 표본만

- groups: 예측 생성 방식(eta_source)·모델 버전·기준 시각 출처·잔여시간 구간별 표본 수(독립 운행 기준), 품질별 수
  (exact 점 시각 / bounded 범위), 오차(실제 도착 − 예측 도착, 초)의 중앙값·분위수, 가장 최근 도착·생성 시각.
  점 시각 표본의 오차(exact)와 전체 표본의 오차 하한(lower)·상한(upper) 분포를 따로 보여 준다. 범위 표본을 대표값으로 바꿔
  합치지 않는다. bounded_width_seconds는 범위 표본의 폭(상한 − 하한)으로, 수집 주기가 길수록 커진다.
- collection: 운행 키를 남긴 노선 위치 기록 수·운행 수·최근 수집 시각과, 도출한 도착 이벤트의 품질별 수. 위치 원천
  (by_source: daejeon / tago)별 수도 함께 준다.
- probability: 확률 계산에 쓰는 설정(.env.api)과, 구간별로 최소 표본 수를 채웠는지.

수집은 워커가 한다(BACKEND_ETA_ERROR_SAMPLING_ENABLED, docs/eta-error-probability.md).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from typing import Any

from app.core.config import Settings
from app.core.logging import configure_logging
from app.db.session import Database, DbConfigError, DbError
from app.modules.evaluation import eta_errors
from app.modules.evaluation import repository as evaluation_repository
from app.modules.transit import repository as transit_repository
from app.modules.transit.service import utc_now
from app.workers.main import to_jsonable


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.eta_error_status", description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=float, default=None, help="최근 며칠에 도착한 표본만 볼지(없으면 전체)")
    return parser


def report(settings: Settings, db: Database, days: float | None) -> dict[str, Any]:
    now = utc_now()
    since = None if days is None else now - timedelta(days=days)
    with db.transaction(isolation="REPEATABLE READ") as conn:
        collection = transit_repository.eta_error_collection_status(conn, since)
        ready = evaluation_repository.eta_error_samples_ready(conn)
        rows = evaluation_repository.eta_error_sample_status_rows(conn, since) if ready else []
    groups = eta_errors.summarize_samples(rows)
    minimum = settings.eval_min_marginal_samples
    for group in groups:
        # 확률 계산은 앞 차량·다음 차량 각각 이 구간의 표본이 최소 수 이상이어야 한다(설정한 창 안의 표본 기준).
        group["meets_min_marginal_samples"] = None if minimum is None else group["independent_runs"] >= minimum
    return {
        "checked_at": now, "window_days": days,
        "table_ready": ready,
        "note": None if ready else "app.eta_error_samples 테이블이 없습니다(migrations/14_eta_error_samples.sql 미적용).",
        "samples": {"total": len(rows), "independent_runs": len({row["run_group"] for row in rows}),
                    "latest_arrival_at": max((row["arrival_upper_at"] for row in rows), default=None),
                    "latest_created_at": max((row["created_at"] for row in rows), default=None)},
        "groups": groups,
        "collection": collection,
        "sampling": {"enabled": settings.eta_error_sampling_enabled,
                     "stops": list(settings.eta_error_sampling_stops),
                     "routes": list(settings.eta_error_sampling_routes),
                     "interval_seconds": settings.eta_error_sampling_interval_seconds,
                     "hours_kst": [list(hours) for hours in settings.eta_error_sampling_hours],
                     "position_source": settings.eta_error_position_source,
                     "arr_time_meaning": settings.daejeon_position_arr_time_meaning},
        "probability": {"enabled": settings.eval_marginal_probability_enabled,
                        "window_days": settings.eval_marginal_window_days, "min_samples": minimum,
                        "horizon_edges_seconds": list(settings.eta_error_horizon_edges_seconds)},
    }


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    if args.days is not None and args.days <= 0:
        print("오류: --days는 0보다 커야 합니다.", file=sys.stderr)
        return 2
    settings = Settings()
    configure_logging(settings.log_level)
    try:
        result = report(settings, Database.from_settings(settings), args.days)
    except (DbConfigError, DbError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
