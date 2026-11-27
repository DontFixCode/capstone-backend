"""버스 예상 혼잡도(Phase 8) 자료 입력·집계 CLI.

대전교통빅데이터플랫폼의 '노선별 정류장 승하차 인원'을 '엑셀저장'으로 받은 원본 파일(.xls, 내용은 HTML 표)을 그대로
입력한다. 기본 실행은 검증 결과와 예상 집계만 출력하고, --execute를 지정해야 DB에 쓴다. 외부 호출은 없다.

    uv run python -m scripts.import_ridership import data/ridership/*.xls            # 검증·대응 결과와 예상 집계(저장 안 함)
    uv run python -m scripts.import_ridership import data/ridership/*.xls --execute  # 원본 누적 후 해당 노선 다시 집계
    uv run python -m scripts.import_ridership aggregate                              # 원본이 있는 노선의 집계 결과 미리 보기
    uv run python -m scripts.import_ridership aggregate --route-no 301 --execute     # 한 노선만 다시 집계해 교체
    uv run python -m scripts.import_ridership status                                 # 노선별 입력·집계 현황

내려받는 조건과 절차는 docs/phase8-congestion.md를 따른다. 적용 DDL: migrations/13_congestion.sql.
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.logging import configure_logging
from app.db.session import Database, DbConfigError, DbError
from app.modules.congestion import levels, repository, service
from app.modules.congestion.source import SourceFileError, parse_export
from app.workers.main import to_jsonable


def _print(data: Any) -> None:
    print(json.dumps(to_jsonable(data), ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.import_ridership", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    load = sub.add_parser("import", help="원본 파일 입력(기본은 검증·미리 보기)")
    load.add_argument("files", nargs="+", help="원본 파일 경로(여러 개, 와일드카드 가능)")
    load.add_argument("--execute", action="store_true", help="원본을 DB에 누적하고 해당 노선을 다시 집계합니다.")
    aggregate = sub.add_parser("aggregate", help="집계(기본은 미리 보기)")
    aggregate.add_argument("--route-no", help="이 노선번호만 집계합니다(생략하면 원본이 있는 모든 노선).")
    aggregate.add_argument("--execute", action="store_true", help="집계 결과로 기존 행을 교체합니다.")
    sub.add_parser("status", help="노선별 원본 입력·집계 현황과 적용 설정")
    return parser


def _read(patterns: list[str]) -> list[tuple[str, bytes]]:
    paths: list[Path] = []
    for pattern in patterns:
        matched = sorted(glob.glob(pattern))
        paths.extend(Path(p) for p in (matched or [pattern]))
    files = []
    for path in dict.fromkeys(paths):
        files.append((path.name, path.read_bytes()))
    return files


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    settings = Settings()
    configure_logging(settings.log_level)
    now = datetime.now(timezone.utc)
    try:
        db = Database.from_settings(settings)
        if args.command == "status":
            with db.transaction(isolation="REPEATABLE READ") as conn:
                ready = repository.tables_ready(conn)
                routes = repository.status(conn) if ready else []
            policy = levels.policy_from_settings(settings)
            _print({"tables_ready": ready, "routes": routes, "settings": {
                "window_months": settings.congestion_window_months,
                "min_sample_days": settings.congestion_min_sample_days, "seats": settings.congestion_seats,
                "capacity": settings.congestion_capacity, "holidays": len(settings.congestion_holidays),
                "level_policy_version": policy.version if policy else None}})
            return 0
        if args.command == "import":
            files = _read(args.files)
            report = service.import_files(db, files, execute=args.execute)
            if args.execute:
                report["aggregation"] = [service.aggregate_route(db, settings, route_id, now=now, execute=True)
                                         for route_id in report["route_ids"]]
            else:
                # 저장하지 않았으므로 파일의 행만으로 집계 결과를 미리 본다(이미 쌓인 원본은 포함하지 않는다).
                previews = []
                for name, data in files:
                    try:
                        previews.extend(service.preview(db, settings, parse_export(data, file_name=name), now=now))
                    except SourceFileError:
                        continue
                report["aggregation_preview_from_these_files_only"] = previews
            _print(report)
            return 0 if all(entry["status"] != "rejected" for entry in report["files"]) else 1
        with db.transaction() as conn:
            if not repository.tables_ready(conn):
                print("오류: 혼잡도 테이블이 없습니다(migrations/13_congestion.sql 미적용).", file=sys.stderr)
                return 2
            route_ids = repository.routes_with_counts(conn)
            if args.route_no:
                wanted = {route["id"] for route in repository.routes_by_number(conn, args.route_no)}
                route_ids = [route_id for route_id in route_ids if route_id in wanted]
        _print([service.aggregate_route(db, settings, route_id, now=now, execute=args.execute)
                for route_id in route_ids])
        return 0
    except OSError as exc:
        print(f"오류: 파일을 읽을 수 없습니다: {exc}", file=sys.stderr)
        return 2
    except (DbConfigError, DbError, service.CongestionConfigError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
