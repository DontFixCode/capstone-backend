"""좌표 기반 경로 검색 CLI: 정적 후보 → TMAP 도보 → 실시간 공유 수집 → 평가 → 추천 → route_searches·route_candidates 저장.

기본 실행은 계획만 출력한다(DFS는 저장된 정적 공개본만 쓰므로 외부 호출·DB 쓰기 없음). --execute를 지정해야 TMAP·TAGO를
호출하고 검색(source=cli)과 후보를 저장한다. 사용자·이동 세션 식별자는 만들지 않는다.

    uv run python -m scripts.search_routes run --origin 36.3326,127.4342 --destination 36.3504,127.3845
    uv run python -m scripts.search_routes run --origin 36.3326,127.4342 --destination 36.3504,127.3845 --execute
    uv run python -m scripts.search_routes show <search_id>
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from app.core.config import Settings
from app.core.logging import configure_logging
from app.db.session import Database, DbConfigError, DbError
from app.integrations.bus_api import TagoClient, TagoConfigError
from app.integrations.daejeon_bus_api import DaejeonBusClient, DaejeonBusConfigError
from app.integrations.walking_api import TmapConfigError, TmapWalkingClient
from app.modules.routing import repository as search_repository
from app.workers.main import to_jsonable
from app.workflows.search import (
    PlaceInput,
    SearchConfigError,
    SearchError,
    SearchRequest,
    SearchWorkflow,
    load_search_config,
    pattern_stop_index,
    required_targets,
    walk_pairs,
)


def _coordinate(text: str) -> tuple[float, float]:
    try:
        lat_text, lon_text = text.split(",")
        lat, lon = float(lat_text), float(lon_text)
    except ValueError:
        raise argparse.ArgumentTypeError("'위도,경도' 형식이 필요합니다(예: 36.3504,127.3845).") from None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        raise argparse.ArgumentTypeError("WGS84 범위의 유효한 좌표가 필요합니다.")
    return lat, lon


def _utc(text: str) -> datetime:
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise argparse.ArgumentTypeError("시간대가 있는 ISO 8601 시각이 필요합니다(예: 2026-11-07T21:00:00+09:00).")
    return value.astimezone(timezone.utc)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.search_routes", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="좌표로 검색(기본은 계획만 출력)")
    run.add_argument("--origin", type=_coordinate, required=True, help="출발지 '위도,경도'")
    run.add_argument("--destination", type=_coordinate, required=True, help="목적지 '위도,경도'")
    run.add_argument("--start-at", type=_utc, help="출발 기준시각(ISO 8601, 기본 현재)")
    for side in ("origin", "destination"):
        run.add_argument(f"--{side}-name")
        run.add_argument(f"--{side}-address")
        run.add_argument(f"--{side}-provider", help="장소를 고른 지도 API 제공기관(제공처 지정 전에는 비움)")
        run.add_argument(f"--{side}-external-id")
    run.add_argument("--execute", action="store_true", help="TMAP·TAGO를 호출하고 검색·후보를 DB에 저장합니다.")
    show = sub.add_parser("show", help="저장한 검색과 후보를 다시 읽어 출력")
    show.add_argument("search_id", type=UUID)
    return parser


def _place(args: argparse.Namespace, side: str) -> PlaceInput:
    lat, lon = getattr(args, side)
    return PlaceInput(lat, lon, getattr(args, f"{side}_name"), getattr(args, f"{side}_address"),
                      getattr(args, f"{side}_provider"), getattr(args, f"{side}_external_id"))


def describe_saved(search: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """저장 결과 요약: 추천 목록·제한 후보·적용 모드·조회 범위/잘림·도보 보존 상태."""
    summary = search["summary"]

    def walk_view(leg: dict[str, Any]) -> dict[str, Any]:
        return {"from": leg["from"].get("ref") if leg.get("from") else None,
                "to": leg["to"].get("ref") if leg.get("to") else None, "status": leg["status"],
                "distance_m": leg["distance_m"], "duration_seconds": leg["duration_seconds"],
                "geometry_points": len(leg["geometry"]["coordinates"]) if leg.get("geometry") else 0,
                "reason": leg.get("reason")}

    def bus_view(leg: dict[str, Any]) -> dict[str, Any]:
        return {"route_no": leg["route_no"], "route_id": leg["route_id"],
                "board": f"{leg['board'].get('name')}({leg['board'].get('stop_id')}) seq {leg['board']['seq']}",
                "alight": f"{leg['alight'].get('name')}({leg['alight'].get('stop_id')}) seq {leg['alight']['seq']}",
                "timed": leg["timed"], "reason_codes": leg["reason_codes"]}

    def view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "rank_no": row["rank_no"], "ranking_mode": row["ranking_mode"], "option_key": row["option_key"],
            "static_route_key": row["static_route_key"], "boarding_count": row["boarding_count"],
            "time_status": row["time_status"], "nominally_feasible": row["nominally_feasible"],
            "nominal_total_seconds": row["nominal_total_seconds"], "walking_seconds": row["walking_seconds"],
            "journey_probability": row["journey_probability"],
            "legs": [walk_view(leg) if leg["kind"] == "walk" else bus_view(leg) for leg in row["legs"]],
            "reason_codes": row["evaluation"].get("reason_codes", []),
        }

    ranked = [view(row) for row in candidates if row["rank_no"] is not None]
    limited = [view(row) for row in candidates if row["rank_no"] is None]
    return {
        "search_id": search["id"], "status": search["status"], "status_reason": summary.get("status_reason"),
        "ranking_mode": search["ranking_mode"], "source": search["source"], "static_version": search["static_version"],
        "config_version": search["config_version"], "start_at": search["start_at"], "expires_at": search["expires_at"],
        "place_info": search["place_info"],
        "counts": summary.get("counts"), "recommendation": summary.get("recommendation"),
        "search_coverage": summary.get("search_coverage"), "evaluation_coverage": summary.get("evaluation_coverage"),
        "walk_lookups": summary.get("walk_lookups"), "realtime_targets": summary.get("realtime_targets"),
        "realtime_collection": summary.get("realtime_collection"), "eta_reports": summary.get("eta_reports"),
        "history_inputs": summary.get("history_inputs"), "samples": summary.get("samples"),
        "scenes": summary.get("scenes"),
        "ranked": ranked, "limited": limited,
    }


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    settings = Settings()
    configure_logging(settings.log_level)
    try:
        db = Database.from_settings(settings)
        if args.command == "show":
            with db.transaction(isolation="REPEATABLE READ") as conn:
                search = search_repository.read_search(conn, args.search_id)
                if search is None:
                    print(f"검색 {args.search_id}이(가) 없습니다.", file=sys.stderr)
                    return 1
                candidates = search_repository.read_candidates(conn, args.search_id)
            print(json.dumps(to_jsonable(describe_saved(search, candidates)), ensure_ascii=False, indent=2))
            return 0
        config = load_search_config(settings)
        request = SearchRequest(_place(args, "origin"), _place(args, "destination"), args.start_at)
        if not args.execute:
            workflow = SearchWorkflow(db, config, tago_client=None, walking_client=None)
            network, context, result, _ = workflow.plan(request)
            pairs = walk_pairs(result.candidates)
            targets = required_targets(result.candidates, pattern_stop_index(network))
            print(json.dumps(to_jsonable({
                "mode": "plan (외부 호출·DB 쓰기 없음)", "static_version": network.data_version,
                "config_version": config.config_version, "start_at": context.start_at,
                "static_candidates": len(result.candidates), "reason_codes": result.reason_codes,
                "coverage": result.coverage, "access_stops": [s.stop_id for s in result.access_stops],
                "egress_stops": [s.stop_id for s in result.egress_stops],
                "walk_pairs_to_lookup": len(pairs), "tmap_limit": config.tmap_max_lookups,
                "realtime_targets": {"arrivals": sum(t.kind == "arrivals" for t in targets),
                                     "positions": sum(t.kind == "positions" for t in targets)},
            }), ensure_ascii=False, indent=2))
            return 0
        tago = TagoClient.from_settings(settings)
        tmap = TmapWalkingClient.from_settings(settings)
        daejeon = DaejeonBusClient.from_settings(settings)  # 도착정보 원천(대전시 버스 API)
        try:
            outcome = SearchWorkflow(db, config, tago_client=tago, walking_client=tmap,
                                     daejeon_client=daejeon).run(request)
        finally:
            tago.close()
            tmap.close()
            daejeon.close()
        with db.transaction(isolation="REPEATABLE READ") as conn:
            search = search_repository.read_search(conn, outcome.search_id)
            candidates = search_repository.read_candidates(conn, outcome.search_id)
        report = describe_saved(search, candidates)
        report["calls"] = {"tmap": tmap.request_count, "tago_http_attempts": tago.request_count}
        print(json.dumps(to_jsonable(report), ensure_ascii=False, indent=2))
        return 0 if outcome.status in ("ready", "data_limited", "no_path") else 1
    except (DbConfigError, DbError, TagoConfigError, TmapConfigError, DaejeonBusConfigError, SearchConfigError,
            SearchError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
