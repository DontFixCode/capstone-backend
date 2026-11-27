"""혼잡 균형 시뮬레이션 CLI(Phase 8, 오프라인·읽기 전용).

저장된 검색 하나의 순위 후보를 대상으로, 같은 출발·도착을 한 시간에 N명이 검색한다고 두고 로짓 선택과 연속평균법으로
균형 분포를 구해 지금 분포와 비교한다. DB에는 쓰지 않고 외부 API도 호출하지 않는다.

    uv run python -m scripts.simulate_equilibrium --search-id <검색 ID> --demand-per-hour 200 --theta 0.01
    uv run python -m scripts.simulate_equilibrium --search-id <검색 ID> --demand-per-hour 200 --theta 0.01 \\
        --assume-trips-per-hour 6

- --demand-per-hour(N, 명/시간)와 --theta(로짓 민감도, 1/초)는 근거 있는 기본값이 없어 반드시 준다. θ = 0.01이면 비용이
  100초 낮은 후보를 약 2.7배 더 고른다.
- 혼잡 집계가 없는 노선은 기준 재차를 0으로 두고 통과 횟수를 노선 배차간격(또는 --assume-trips-per-hour)으로 채운다. 어떤
  값을 가정했는지는 후보별 assumed에 나온다.
- 좌석 수·정원·단계별 가중치는 .env.api의 BACKEND_CONGESTION_* 값을 쓴다. 계산식은 app/modules/congestion/equilibrium.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from typing import Any
from uuid import UUID

from app.core.config import Settings
from app.core.logging import configure_logging
from app.db.session import Database, DbConfigError, DbError
from app.modules.congestion import equilibrium, levels, service
from app.workers.main import to_jsonable


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.simulate_equilibrium", description=__doc__.splitlines()[0])
    parser.add_argument("--search-id", type=UUID, required=True, help="순위 후보가 저장된 검색 ID")
    parser.add_argument("--demand-per-hour", type=float, required=True, help="같은 출발·도착을 한 시간에 검색하는 인원 N")
    parser.add_argument("--theta", type=float, required=True, help="로짓 민감도(1/초). 클수록 가장 싼 후보로 몰린다")
    parser.add_argument("--max-iterations", type=int, default=2000, help="연속평균법 반복 상한(계산 한도)")
    parser.add_argument("--tolerance", type=float, default=0.001, help="수렴 판정: max|y − x| ÷ N이 이 값보다 작으면 멈춘다")
    parser.add_argument("--assume-trips-per-hour", type=float, default=None,
                        help="혼잡 집계가 없는 노선의 시간당 통과 횟수(없으면 노선 배차간격)")
    return parser


def report(result: equilibrium.Result, policy: levels.LevelPolicy) -> dict[str, Any]:
    rounded = []
    for option in result.options:
        row = asdict(option)
        for key, value in row.items():
            if isinstance(value, float):
                row[key] = round(value, 4 if key.startswith("share") else 1)
        rounded.append(row)
    return {
        "demand_per_hour": result.demand_per_hour, "theta": result.theta, "iterations": result.iterations,
        "converged": result.converged, "gap": round(result.gap, 5),
        "policy": {"seats": policy.seats, "capacity": policy.capacity, "cost_weights": list(policy.cost_weights),
                   "version": policy.version},
        "max_share": {name: round(result.max_share(name), 4)
                      for name in ("share_all_to_best", "share_before", "share_at_equilibrium")},
        "options": rounded,
    }


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    settings = Settings()
    configure_logging(settings.log_level)
    policy = levels.policy_from_settings(settings)
    if policy is None or not policy.cost_weights:
        print("오류: BACKEND_CONGESTION_SEATS·CAPACITY·COST_WEIGHTS가 모두 있어야 합니다.", file=sys.stderr)
        return 2
    try:
        db = Database.from_settings(settings)
        with db.transaction(isolation="REPEATABLE READ") as conn:
            options = service.equilibrium_options(conn, args.search_id, policy,
                                                  assume_trips_per_hour=args.assume_trips_per_hour)
        if not options:
            print("오류: 그 검색에 순위 후보가 없습니다.", file=sys.stderr)
            return 1
        result = equilibrium.simulate(options, policy, demand_per_hour=args.demand_per_hour, theta=args.theta,
                                      max_iterations=args.max_iterations, tolerance=args.tolerance)
    except ValueError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    except (DbConfigError, DbError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(to_jsonable(report(result, policy)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
