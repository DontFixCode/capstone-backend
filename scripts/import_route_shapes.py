"""버스 노선 지도 형상(ODsay) 수집·확인 CLI.

워커가 정적 갱신 뒤에 하는 수집과 같은 흐름(app.workflows.route_shapes)을 쓴다. 기본 실행은 계획만 출력하고,
--execute를 지정해야 실제 ODsay 호출과 DB 쓰기를 한다. 일일 호출 한도가 작으므로 남은 한도만큼만 진행하며,
다음 실행이 이어서 한다.

    uv run python -m scripts.import_route_shapes                          # 계획만 출력
    uv run python -m scripts.import_route_shapes run --execute            # 남은 일일 한도만큼 수집
    uv run python -m scripts.import_route_shapes run --execute --max-calls 5
    uv run python -m scripts.import_route_shapes run --execute --refresh-all   # 저장된 형상도 모두 다시 받기
    uv run python -m scripts.import_route_shapes status                   # 확보 현황·최근 24시간 호출 수(외부 호출 없음)
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from typing import Any

from app.core.config import Settings
from app.core.logging import configure_logging
from app.db.session import Database, DbConfigError, DbError
from app.integrations import route_shapes_api
from app.integrations.odsay_api import OdsayClient, OdsayConfigError
from app.modules.transit import repository
from app.workers.main import to_jsonable
from app.workflows import route_shapes

PLAN = """버스 노선 형상 수집 계획 (--execute 없이 실행해 외부 호출·DB 쓰기를 하지 않았습니다)
  제공처: {provider}   일일 호출 한도: {limit}   묶음 크기: {batch}   갱신 간격: {refresh_days:.0f}일
  1. 노선 ID 연결: 제공처 노선 ID가 없는 노선을 노선번호 첫 글자별로 searchBusLane 1회씩 조회(localBusID = TAGO 노선 ID)
  2. 형상 조회: 형상이 없거나 갱신 간격이 지난 노선을 {batch}개씩 loadLane 1회로 조회
  3. 패턴마다 정류소를 형상 위에 대응시켜 app.routes.shape_info에 저장(대응 실패는 저장하지 않고 사유를 남김)
  모든 HTTP 시도는 app.api_fetches(provider=odsay)에 기록하고, 최근 24시간 기록 수로 남은 한도를 계산합니다.
  현재 상태는 status 명령으로 확인합니다.
"""


def _print(data: Any) -> None:
    print(json.dumps(to_jsonable(data), ensure_ascii=False, indent=2))


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("1 이상의 정수가 필요합니다.")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.import_route_shapes", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("run", help="형상 수집(기본은 계획만 출력)")
    run.add_argument("--execute", action="store_true", help="실제 ODsay를 호출하고 DB에 저장합니다.")
    run.add_argument("--max-calls", type=_positive_int,
                     help="이번 실행의 최대 호출 수(일일 한도가 설정돼 있으면 남은 한도를 넘지 않음)")
    run.add_argument("--refresh-all", action="store_true",
                     help="갱신 간격과 무관하게 연결된 노선의 형상을 모두 다시 받습니다(한 요청에 최대한 넣음).")
    sub.add_parser("status", help="형상 확보 현황과 최근 24시간 호출 수")
    return parser


def status(db: Database, settings: Settings) -> dict[str, Any]:
    now = route_shapes.utc_now()
    with db.transaction(isolation="REPEATABLE READ") as conn:
        summary = repository.shape_summary(conn)
        used = repository.provider_call_count(conn, route_shapes.PROVIDER, now - route_shapes.USAGE_WINDOW)
    limit = settings.odsay_daily_call_limit
    return {"provider": route_shapes_api.configured_provider(settings.route_shape_provider), **summary,
            "calls_last_24h": used, "daily_call_limit": limit,
            "remaining_calls": None if limit is None else max(0, limit - used)}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    settings = Settings()
    configure_logging(settings.log_level)
    command = args.command or "run"
    try:
        if command == "run" and not getattr(args, "execute", False):
            print(PLAN.format(provider=route_shapes_api.configured_provider(settings.route_shape_provider) or "미설정",
                              limit=settings.odsay_daily_call_limit or "미확인", batch=settings.odsay_lane_batch_size,
                              refresh_days=settings.route_shape_refresh_interval_seconds / 86400))
            return 0
        db = Database.from_settings(settings)
        if command == "status":
            _print(status(db, settings))
            return 0
        with OdsayClient.from_settings(settings) as client:
            report = route_shapes.collect(db, settings, client, max_calls=args.max_calls,
                                          refresh_all=args.refresh_all)
        _print(asdict(report))
        return 0 if report.stopped in (None, "budget_exhausted") else 1
    except (DbConfigError, DbError, OdsayConfigError, route_shapes.ShapeConfigError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
