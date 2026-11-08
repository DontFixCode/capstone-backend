"""정적 교통정보(대전 정류소·노선·노선별 기본정보·경유 목록) 초기 적재와 확인 CLI.

워커의 하루 1회 갱신과 같은 서비스(app.modules.transit.service.StaticImportService)를 사용한다.
기본 실행은 계획만 출력하고, --execute를 지정해야 실제 TAGO 호출과 DB 쓰기를 한다.

    uv run python -m scripts.import_transit                          # 계획만 출력
    uv run python -m scripts.import_transit run --execute --max-requests 400
    uv run python -m scripts.import_transit status                   # 현재 공개본·보류 여부(외부 호출 없음)
    uv run python -m scripts.import_transit verify                   # 저장 원문과 적재 결과 대조(외부 호출 없음)
    uv run python -m scripts.import_transit usage --hours 24         # 서비스별 실제 호출 수와 승인 한도
    uv run python -m scripts.import_transit nearby --lat 36.3504 --lon 127.3845 --radius-m 300
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.logging import configure_logging
from app.db.session import Database, DbConfigError, DbError
from app.integrations.bus_api import TagoClient, TagoConfigError
from app.modules.transit import service
from app.workers.main import to_jsonable

PLAN = """정적 초기 적재 계획 (--execute 없이 실행해 외부 호출·DB 쓰기를 하지 않았습니다)
  1. getSttnNoList cityCode={city} 전체 페이지 (numOfRows={rows})
  2. getRouteNoList cityCode={city} 전체 페이지
  3. 노선마다 getRouteInfoIem 1회 + getRouteAcctoThrghSttnList 전체 페이지
  모든 HTTP 시도(재시도·실패 포함)는 app.api_fetches에 즉시 기록합니다.
  전체 수집·검증이 끝난 경우에만 stops·routes·route_stops를 같은 data_version으로 한 트랜잭션에서 공개합니다.
  한 노선이라도 실패하면 공개를 보류하고 마지막 정상본을 유지합니다.
  Phase 1 산정(numOfRows=100): 정류소 31 + 노선 목록 2 + 노선 정보 123 + 경유 목록 ≥123 호출(재시도 제외).
  승인 한도: {limits}
"""


def _limits(settings: Settings) -> str:
    parts = []
    for svc, name in service.LIMIT_SETTING_BY_SERVICE.items():
        value = getattr(settings, name)
        parts.append(f"{svc}={value if value is not None else '미확인'}")
    return ", ".join(parts)


def _print(data: Any) -> None:
    print(json.dumps(to_jsonable(data), ensure_ascii=False, indent=2))


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("1 이상의 정수가 필요합니다.")
    return value


def _utc(text: str) -> datetime:
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise argparse.ArgumentTypeError("시간대가 있는 ISO 8601 시각이 필요합니다(예: 2026-11-07T00:00:00+09:00).")
    return value.astimezone(timezone.utc)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.import_transit", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("run", help="정적 정보 적재(기본은 계획만 출력)")
    run.add_argument("--execute", action="store_true", help="실제 TAGO를 호출하고 DB에 적재합니다.")
    run.add_argument("--max-requests", type=_positive_int, help="이번 실행의 최대 HTTP 시도 수(초과 시 보류)")
    run.add_argument("--report", type=Path, help="실행 보고서를 저장할 JSON 경로(지정할 때만 저장)")
    sub.add_parser("status", help="현재 정적 공개본 버전·건수·보류 여부")
    sub.add_parser("verify", help="마지막 공개에 채택된 원문(raw_body)을 다시 해석해 활성 정적 행과 대조")
    usage = sub.add_parser("usage", help="서비스별 실제 HTTP 시도 수와 승인 한도 대조")
    usage.add_argument("--hours", type=_positive_int, default=24, help="현재부터 거슬러 올라갈 시간 (기본 %(default)s)")
    usage.add_argument("--since", type=_utc, help="시작 시각(ISO 8601, 지정 시 --hours 무시)")
    usage.add_argument("--until", type=_utc, help="끝 시각(ISO 8601, 기본 현재)")
    nearby = sub.add_parser("nearby", help="저장 좌표 기준 미터 반경 인근 정류소(도보 가능 확정 아님)")
    nearby.add_argument("--lat", type=float, required=True)
    nearby.add_argument("--lon", type=float, required=True)
    nearby.add_argument("--radius-m", type=float, required=True, help="반경(미터). 기본값을 두지 않습니다.")
    nearby.add_argument("--limit", type=_positive_int, default=50)
    return parser


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
            print(PLAN.format(city=service.CITY_CODE, rows=service.PAGE_ROWS, limits=_limits(settings)))
            return 0
        db = Database.from_settings(settings)
        if command == "run":
            options: dict[str, Any] = {}
            if args.max_requests:
                options["max_requests"] = args.max_requests
            TagoClient.from_settings(settings).close()  # 인증키 설정을 DB 쓰기 전에 확인한다.
            importer = service.StaticImportService(db, lambda **kw: TagoClient.from_settings(settings, **options, **kw))
            report = importer.run()
            if args.report:
                args.report.parent.mkdir(parents=True, exist_ok=True)
                args.report.write_text(json.dumps(to_jsonable(report), ensure_ascii=False, indent=2), encoding="utf-8")
            _print(report)
            return 0 if report.status == "published" else 1
        if command == "status":
            with db.transaction(isolation="REPEATABLE READ") as conn:
                _print(service.static_status(conn))
            return 0
        if command == "verify":
            with db.transaction(isolation="REPEATABLE READ") as conn:
                result = service.verify_published_static(conn)
            _print(result)
            return 0 if result["state"] == "verified" else 1
        if command == "usage":
            until = args.until or service.utc_now()
            since = args.since or until - timedelta(hours=args.hours)
            _print(service.usage_report(db, settings, since, until))
            return 0
        if command == "nearby":
            _print(service.find_nearby_stops(db, args.lat, args.lon, args.radius_m, limit=args.limit))
            return 0
    except (DbConfigError, DbError, TagoConfigError, ValueError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
