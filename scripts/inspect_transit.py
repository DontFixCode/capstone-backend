"""TAGO 교통 데이터 확인용 수동 실행 CLI.

DB 없이 정류소·노선·위치·도착 응답의 식별자·필드·누락 정보를 확인한다. 기본 실행은 호출 계획만
출력하고, --execute를 지정해야 실제 API를 호출한다. 결과와 요약은 실행별 디렉터리에 JSON으로 저장한다.

    uv run python -m scripts.inspect_transit --help
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from app.core.config import Settings
from app.core.logging import configure_logging
from app.integrations import bus_api
from app.integrations.bus_api import Collection, TagoClient, TagoConfigError
from scripts import inspect_transit_report as report

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "artifacts" / "transit-inspection"
DEFAULT_ROWS = 100
DEFAULT_MAX_REQUESTS = 30
PLAN_PREVIEW = 10
# 인증 실패·호출 한도 초과 뒤의 요청은 같은 이유로 실패하므로 남은 대상을 호출하지 않는다.
ABORT_CATEGORIES = frozenset({"auth", "rate_limit"})
OBSERVED_AT_NOTE = "공식 명세의 응답 항목에 원천 관측 시각이 없어 미제공(null)으로 둔다. pages[].received_at은 서버 수신 시각이다."
EPILOG = """\
예시 (저장소 루트에서 실행, --execute가 없으면 계획만 출력):
  uv run python -m scripts.inspect_transit stations
  uv run python -m scripts.inspect_transit stations --all-pages --max-requests 60 --execute
  uv run python -m scripts.inspect_transit routes --route-no 102 --execute
  uv run python -m scripts.inspect_transit route-detail --route-id DJB30300004 --execute
  uv run python -m scripts.inspect_transit route-detail --all-routes --routes-from artifacts/transit-inspection/20261026T010203Z_routes --max-requests 800 --execute
  uv run python -m scripts.inspect_transit nearby --lat 36.3504 --lon 127.3845 --execute
  uv run python -m scripts.inspect_transit locations --route-id DJB30300004 --execute
  uv run python -m scripts.inspect_transit arrivals --node-id DJB8001793 --execute
  uv run python -m scripts.inspect_transit analyze
"""


@dataclass
class Step:
    """호출 대상 하나. max_pages가 None이면 전체 페이지 수를 첫 응답 전에는 알 수 없다."""

    endpoint: bus_api.Endpoint
    target: str
    call: Callable[[TagoClient], Collection]
    max_pages: int | None = 1
    expand: Callable[[Collection], list[Step]] | None = None


def main(argv: list[str] | None = None) -> int:
    return run_cli(sys.argv[1:] if argv is None else argv)


def run_cli(
    argv: list[str],
    *,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] | None = None,
) -> int:
    """transport·sleep은 네트워크 없는 테스트에서만 주입한다."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.mode is None:
        parser.print_help()
        return 0
    try:
        settings = settings or Settings()
    except ValidationError as error:
        print(f"오류: 저장소 루트 .env.api 또는 환경변수의 설정값을 확인하세요.\n{error}", file=sys.stderr)
        return 2
    configure_logging(settings.log_level)
    if args.mode == "analyze":
        return analyze(args, settings)
    try:
        steps, notes = build_steps(args)
    except ValueError as error:
        print(f"오류: {error}", file=sys.stderr)
        return 2
    print_plan(args, steps, notes, settings)
    if not args.execute:
        print("\n계획만 출력했습니다. 실제로 호출하려면 같은 명령에 --execute를 추가하세요.")
        return 0
    options: dict[str, Any] = {"max_requests": args.max_requests, "transport": transport}
    if sleep is not None:
        options["sleep"] = sleep
    try:
        client = TagoClient.from_settings(settings, **options)
    except TagoConfigError as error:
        print(f"오류: {error}", file=sys.stderr)
        return 2
    with client:
        return execute(args, steps, notes, client, settings)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.inspect_transit",
        description="TAGO 버스 API 응답·식별자·누락 정보를 DB 없이 확인합니다. 기본 실행은 호출 계획만 출력합니다.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    modes = parser.add_subparsers(dest="mode", metavar="MODE")

    api = argparse.ArgumentParser(add_help=False)
    api.add_argument("--execute", action="store_true", help="실제 TAGO API를 호출합니다(미지정 시 계획만 출력).")
    api.add_argument(
        "--max-requests", type=_positive_int, default=DEFAULT_MAX_REQUESTS,
        help="실행당 최대 HTTP 요청 수. 재시도 포함 (기본 %(default)s).",
    )
    api.add_argument("--rows", type=_positive_int, default=DEFAULT_ROWS, help="페이지당 결과 수 numOfRows (기본 %(default)s).")
    api.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="결과 저장 위치 (기본 %(default)s).")
    city = argparse.ArgumentParser(add_help=False)
    city.add_argument("--city-code", default=bus_api.DAEJEON_CITY_CODE, help="도시코드 (기본 %(default)s: 대전).")
    paging = argparse.ArgumentParser(add_help=False)
    pages = paging.add_mutually_exclusive_group()
    pages.add_argument("--all-pages", action="store_true", help="전체 목록을 끝 페이지까지 조회합니다(명시적 옵션).")
    pages.add_argument("--max-pages", type=_positive_int, default=1, help="조회할 최대 페이지 수 (기본 %(default)s).")

    stations = modes.add_parser(
        "stations", parents=[api, city, paging], help="정류소 목록 조회 (getSttnNoList)",
        description="정류소 목록을 조회합니다. 기본은 첫 페이지만 조회하고 전체 목록은 --all-pages로 조회합니다.",
    )
    stations.add_argument("--node-name", help="정류소명 검색어 nodeNm")
    stations.add_argument("--node-no", help="정류소번호 nodeNo")

    routes = modes.add_parser(
        "routes", parents=[api, city, paging], help="노선 목록 조회 (getRouteNoList)",
        description="노선 목록을 조회합니다. 기본은 첫 페이지만 조회하고 전체 목록은 --all-pages로 조회합니다.",
    )
    routes.add_argument("--route-no", help="노선번호 routeNo")

    detail = modes.add_parser(
        "route-detail", parents=[api, city],
        help="지정 노선 기본정보·경유 정류소 조회 (getRouteInfoIem, getRouteAcctoThrghSttnList)",
        description="노선별 기본정보와 경유 정류소 전체 페이지를 조회합니다. 전체 노선은 --all-routes로만 조회합니다.",
    )
    targets = detail.add_mutually_exclusive_group(required=True)
    targets.add_argument("--route-id", action="append", help="노선 ID routeId (여러 번 지정 가능)")
    targets.add_argument("--all-routes", action="store_true", help="전체 노선 상세를 조회합니다(명시적 옵션).")
    detail.add_argument(
        "--routes-from", type=Path,
        help="--all-routes에서 노선 목록을 다시 조회하지 않고 저장된 routes 실행 디렉터리의 노선 ID를 사용합니다.",
    )

    nearby = modes.add_parser(
        "nearby", parents=[api], help="좌표 주변 정류소 조회 (getCrdntPrxmtSttnList)",
        description="WGS84 좌표 주변(명세상 반경 500m) 정류소를 조회합니다. 도보 경로·도보시간이 아닙니다.",
    )
    nearby.add_argument("--lat", type=_coordinate(90), required=True, help="위도 gpsLati")
    nearby.add_argument("--lon", type=_coordinate(180), required=True, help="경도 gpsLong")

    locations = modes.add_parser(
        "locations", parents=[api, city], help="지정 노선 버스 위치 조회 (getRouteAcctoBusLcList)",
        description="지정한 노선의 운행 차량 위치를 조회합니다. 전체 노선 실시간 순회는 지원하지 않습니다.",
    )
    locations.add_argument("--route-id", action="append", required=True, help="노선 ID routeId (여러 번 지정 가능)")

    arrivals = modes.add_parser(
        "arrivals", parents=[api, city], help="지정 정류소 도착정보 조회 (getSttnAcctoArvlPrearngeInfoList)",
        description="지정한 정류소의 도착예정정보를 조회합니다. 전체 정류소 실시간 순회는 지원하지 않습니다.",
    )
    arrivals.add_argument("--node-id", action="append", required=True, help="정류소 ID nodeId (여러 번 지정 가능)")

    analysis = modes.add_parser(
        "analyze", help="저장 결과의 연결 검사·호출량 요약 (API 호출 없음)",
        description="저장된 실행 결과로 ID 연결·필드·순서·좌표·도착시간을 검사하고 호출량을 요약합니다.",
    )
    analysis.add_argument("runs", nargs="*", type=Path, help="실행 디렉터리 (미지정 시 출력 위치의 모든 조회 실행)")
    analysis.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="결과 위치 (기본 %(default)s).")
    analysis.add_argument(
        "--active-hours", type=_bounded_float(0, 24), default=24.0, help="대상별 하루 활성 시간, 0 초과 24 이하 (기본 %(default)s)."
    )
    analysis.add_argument(
        "--interval-minutes", type=_bounded_float(0, None), default=10.0, help="실시간 갱신 주기(분) (기본 %(default)s)."
    )
    analysis.add_argument("--location-targets", type=_non_negative_int, help="사용자 필요 위치 대상 노선 수")
    analysis.add_argument("--arrival-targets", type=_non_negative_int, help="사용자 필요 도착 대상 정류소 수")
    analysis.add_argument("--daily-limit", type=_positive_int, help="서비스별 일일 호출 한도(계정에서 확인한 값)")
    return parser


def build_steps(args: argparse.Namespace) -> tuple[list[Step], list[str]]:
    rows = args.rows
    if args.mode in ("stations", "routes"):
        max_pages = None if args.all_pages else args.max_pages
        if args.mode == "stations":
            target = _target(cityCode=args.city_code, nodeNm=args.node_name, nodeNo=args.node_no)
            call = partial(
                TagoClient.station_list, city_code=args.city_code, node_name=args.node_name, node_no=args.node_no,
                rows=rows, max_pages=max_pages,
            )
            return [Step(bus_api.STATION_LIST, target, call, max_pages)], []
        target = _target(cityCode=args.city_code, routeNo=args.route_no)
        call = partial(TagoClient.route_list, city_code=args.city_code, route_no=args.route_no, rows=rows, max_pages=max_pages)
        return [Step(bus_api.ROUTE_LIST, target, call, max_pages)], []
    if args.mode == "nearby":
        call = partial(TagoClient.nearby_stations, latitude=args.lat, longitude=args.lon, rows=rows)
        note = "근접 정류소는 좌표 반경 조회 결과이며 실제 도보 경로·도보시간이 아닙니다."
        return [Step(bus_api.NEARBY_STATIONS, _target(gpsLati=args.lat, gpsLong=args.lon), call, None)], [note]
    if args.mode == "locations":
        return [
            Step(
                bus_api.BUS_LOCATIONS, _target(routeId=route_id),
                partial(TagoClient.bus_locations, route_id=route_id, city_code=args.city_code, rows=rows), None,
            )
            for route_id in dict.fromkeys(args.route_id)
        ], []
    if args.mode == "arrivals":
        return [
            Step(
                bus_api.ARRIVALS, _target(nodeId=node_id),
                partial(TagoClient.arrivals, node_id=node_id, city_code=args.city_code, rows=rows), None,
            )
            for node_id in dict.fromkeys(args.node_id)
        ], []

    if args.routes_from and not args.all_routes:
        raise ValueError("--routes-from은 --all-routes와 함께 사용합니다.")
    if args.route_id:
        return route_detail_steps(list(dict.fromkeys(args.route_id)), args), []
    if args.routes_from:
        route_ids, notes = report.route_ids_from(args.routes_from)
        return route_detail_steps(route_ids, args), notes

    def expand(collection: Collection) -> list[Step]:
        return route_detail_steps(report.route_ids(collection.items), args)

    call = partial(TagoClient.route_list, city_code=args.city_code, rows=rows, max_pages=None)
    notes = ["노선 목록을 끝 페이지까지 조회한 뒤 노선마다 기본정보 1회 + 경유 정류소 1회 이상을 호출합니다(노선 수는 조회 전 미확인)."]
    return [Step(bus_api.ROUTE_LIST, _target(cityCode=args.city_code), call, None, expand)], notes


def route_detail_steps(route_ids: list[str], args: argparse.Namespace) -> list[Step]:
    steps = []
    for route_id in route_ids:
        target = _target(routeId=route_id)
        steps.append(Step(bus_api.ROUTE_INFO, target, partial(TagoClient.route_info, route_id=route_id, city_code=args.city_code)))
        steps.append(Step(
            bus_api.ROUTE_STOPS, target,
            partial(TagoClient.route_stops, route_id=route_id, city_code=args.city_code, rows=args.rows), None,
        ))
    return steps


def print_plan(args: argparse.Namespace, steps: list[Step], notes: list[str], settings: Settings) -> None:
    deferred = any(step.expand for step in steps)
    print(f"[계획] {args.mode}: 호출 대상 {len(steps)}건" + (" (+노선 목록 조회 후 노선별 상세 추가)" if deferred else ""))
    for step in steps[:PLAN_PREVIEW]:
        print(f"  - {step.endpoint.path} {step.target}: {_describe(step)}")
    if len(steps) > PLAN_PREVIEW:
        print(f"  - … 외 {len(steps) - PLAN_PREVIEW}건")
    minimum = len(steps)
    if deferred or any(step.endpoint.paged and step.max_pages is None for step in steps):
        unknown = "노선 수와 전체 페이지 수" if deferred else "전체 페이지 수"
        print(f"예상 요청 수: 최소 {minimum}회. {unknown}를 호출 전에는 알 수 없어 상한을 확정하지 않습니다.")
    else:
        maximum = sum(step.max_pages if step.endpoint.paged else 1 for step in steps)
        span = f"{minimum}회" if minimum == maximum else f"{minimum}~{maximum}회"
        print(f"예상 요청 수: {span} (페이지 기준, 재시도 제외)")
    print(
        f"재시도: 시간초과·연결 오류·HTTP 5xx·게이트웨이 01/05에만 요청당 최대 {settings.tago_max_retries}회. "
        "인증 실패·잘못된 요청·호출 한도 초과는 재시도하지 않습니다."
    )
    print(f"실행당 최대 요청 수(재시도 포함): {args.max_requests}회. 소진되면 추가 호출을 멈추고 미완료 범위를 기록합니다.")
    if minimum > args.max_requests:
        print(f"주의: 최소 요청 수 {minimum}회가 최대 요청 수보다 많아 일부 대상은 호출되지 않습니다.")
    print(
        f"요청 간격 {settings.tago_request_interval_seconds}s, 제한시간 {settings.tago_timeout_seconds}s, "
        f"numOfRows {args.rows}"
    )
    key_state = "설정됨" if settings.tago_service_key is not None else "미설정 (--execute 전에 저장소 루트 .env.api에 입력)"
    print(f"인증키 TAGO_SERVICE_KEY: {key_state}")
    print(f"결과 저장 위치: {args.output_dir / ('<UTC시각>_' + args.mode)}")
    for note in notes:
        print(f"참고: {note}")


def execute(
    args: argparse.Namespace, steps: list[Step], notes: list[str], client: TagoClient, settings: Settings,
) -> int:
    recorder = RunRecorder(args, settings, notes)
    print(f"\n[실행] 결과 저장 위치: {recorder.dir}")
    queue = list(steps)
    abort: str | None = None
    status = "crashed"
    try:
        while queue:
            step = queue.pop(0)
            if abort:
                recorder.skip(step, f"aborted:{abort}")
                continue
            if client.remaining_requests == 0:
                recorder.skip(step, "budget_exhausted")
                continue
            collection = step.call(client)
            entry = recorder.add(step, collection, client)
            print(_progress(entry))
            error = collection.error
            if error and error.category in ABORT_CATEGORIES:
                abort = error.category
                print(f"중단: {error.message}. 남은 대상은 호출하지 않습니다.")
            if step.expand and collection.items:
                if collection.status != "success":
                    recorder.notes.append(
                        f"노선 목록이 {collection.status}({collection.stop_reason}) 상태라 확보한 노선만 상세 조회했습니다."
                    )
                queue[:0] = step.expand(collection)
        status = recorder.overall_status(abort)
    except KeyboardInterrupt:
        status = "interrupted"
        raise
    finally:
        recorder.finish(status, client, abort)

    runs, collections = report.load_runs([recorder.dir])
    summary = report.summarize(runs, collections, max_retries=settings.tago_max_retries)
    _write_json(recorder.dir / "summary.json", summary)
    if recorder.data["incomplete"]:
        print(f"미완료 대상 {len(recorder.data['incomplete'])}건 (run.json의 incomplete 참고)")
    print("\n".join(report.render(summary)))
    print(f"\n실행 상태: {status}. 저장 위치: {recorder.dir}")
    return 1 if abort or recorder.data["errors"] else 0


def analyze(args: argparse.Namespace, settings: Settings) -> int:
    paths = args.runs or report.find_runs(args.output_dir)
    if not paths:
        print(f"분석할 실행 결과가 없습니다: {args.output_dir}", file=sys.stderr)
        return 1
    try:
        runs, collections = report.load_runs(paths)
    except ValueError as error:
        print(f"오류: {error}", file=sys.stderr)
        return 2
    summary = report.summarize(
        runs, collections, active_hours=args.active_hours, interval_minutes=args.interval_minutes,
        location_targets=args.location_targets, arrival_targets=args.arrival_targets,
        daily_limit=args.daily_limit, max_retries=settings.tago_max_retries,
    )
    output = create_run_dir(args.output_dir, "analyze")
    _write_json(output / "summary.json", summary)
    print("\n".join(report.render(summary)))
    print(f"\n저장: {output / 'summary.json'}")
    return 0


class RunRecorder:
    """실행 디렉터리에 run.json과 응답별 JSON을 바로 기록해 중간 실패에도 확보한 결과를 남긴다."""

    def __init__(self, args: argparse.Namespace, settings: Settings, notes: list[str]) -> None:
        self.dir = create_run_dir(args.output_dir, args.mode)
        (self.dir / "responses").mkdir()
        self.notes = list(notes)
        self.data: dict[str, Any] = {
            "schema": "transit-inspection/run@1",
            "mode": args.mode,
            "status": "running",
            "started_at": _utc_now(),
            "finished_at": None,
            "options": {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()},
            "settings": {
                "service_key_configured": settings.tago_service_key is not None,
                "timeout_seconds": settings.tago_timeout_seconds,
                "request_interval_seconds": settings.tago_request_interval_seconds,
                "max_retries": settings.tago_max_retries,
                "retry_backoff_seconds": settings.tago_retry_backoff_seconds,
            },
            "budget": {"max_requests": args.max_requests, "used": 0, "exhausted": False},
            "abort_reason": None,
            "call_stats": {},
            "collections": [],
            "incomplete": [],
            "errors": [],
            "notes": self.notes,
        }
        self._write()

    def add(self, step: Step, collection: Collection, client: TagoClient) -> dict[str, Any]:
        index = len(self.data["collections"]) + 1
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", step.target.replace("=", "-"))[:60]
        name = f"{index:04d}_{step.endpoint.operation}_{slug}.json"
        _write_json(self.dir / "responses" / name, collection_record(collection, step.target))
        error = collection.error
        entry = {
            "index": index,
            "file": f"responses/{name}",
            "operation": step.endpoint.operation,
            "target": step.target,
            "status": collection.status,
            "stop_reason": collection.stop_reason,
            "items": len(collection.items),
            "total_count": collection.total_count,
            "pages": sum(1 for page in collection.pages if page.attempts),
            "http_attempts": sum(len(page.attempts) for page in collection.pages),
            "warnings": collection.warnings,
            "error": None if error is None else {"category": error.category, "code": error.code, "message": error.message},
        }
        self.data["collections"].append(entry)
        if error and error.category != "budget":
            self.data["errors"].append({"operation": entry["operation"], "target": step.target, **entry["error"]})
        if collection.stop_reason in ("budget_exhausted", "error"):
            self.data["incomplete"].append({
                "operation": entry["operation"], "target": step.target, "reason": collection.stop_reason,
                "items_collected": len(collection.items), "total_count": collection.total_count,
            })
        self._sync(client)
        return entry

    def skip(self, step: Step, reason: str) -> None:
        self.data["incomplete"].append({"operation": step.endpoint.operation, "target": step.target, "reason": reason})

    def overall_status(self, abort: str | None) -> str:
        statuses = [entry["status"] for entry in self.data["collections"]]
        if abort:
            return "aborted"
        if not statuses or all(status == "failed" for status in statuses):
            return "failed"
        if self.data["incomplete"] or any(status in ("partial", "failed") for status in statuses):
            return "partial"
        return "success"

    def finish(self, status: str, client: TagoClient, abort: str | None) -> None:
        self.data.update(status=status, finished_at=_utc_now(), abort_reason=abort)
        self._sync(client)

    def _sync(self, client: TagoClient) -> None:
        self.data["call_stats"] = client.stats
        self.data["budget"].update(used=client.request_count, exhausted=client.remaining_requests == 0)
        self._write()

    def _write(self) -> None:
        _write_json(self.dir / "run.json", self.data)


def collection_record(collection: Collection, target: str) -> dict[str, Any]:
    """응답 원본 항목과 페이지별 메타데이터를 저장 형식으로 만든다. 인증키는 포함되지 않는다."""
    endpoint = collection.endpoint
    pages = []
    for page in collection.pages:
        record = asdict(page)
        record.pop("items")
        if page.error is None:
            # 정상 페이지의 원본은 items와 header·meta로 보존하므로 오류 페이지에만 남긴다.
            record.pop("raw")
        record["item_count"] = len(page.items)
        pages.append(record)
    return {
        "schema": "transit-inspection/collection@1",
        "service": endpoint.service,
        "operation": endpoint.operation,
        "endpoint": endpoint.path,
        "target": target,
        "request": {"params": collection.params, "numOfRows": collection.rows, "paged": endpoint.paged},
        "status": collection.status,
        "stop_reason": collection.stop_reason,
        "total_count": collection.total_count,
        "item_count": len(collection.items),
        "warnings": collection.warnings,
        "error": asdict(collection.error) if collection.error else None,
        "source_observed_at": None,
        "source_observed_at_note": OBSERVED_AT_NOTE,
        "pages": pages,
        "items": collection.items,
    }


def create_run_dir(output_dir: Path, mode: str) -> Path:
    """UTC 시각과 모드로 새 디렉터리를 만든다. 같은 이름이 있으면 번호를 붙여 이전 결과를 덮어쓰지 않는다."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir.mkdir(parents=True, exist_ok=True)
    for number in range(1, 1000):
        path = output_dir / (f"{stamp}_{mode}" + (f"-{number}" if number > 1 else ""))
        try:
            path.mkdir()
        except FileExistsError:
            continue
        return path
    raise RuntimeError(f"실행 디렉터리를 만들 수 없습니다: {output_dir}")


def _describe(step: Step) -> str:
    if not step.endpoint.paged:
        return "1회"
    if step.max_pages == 1:
        return "1회 (첫 페이지)"
    if step.max_pages:
        return f"1~{step.max_pages}회"
    text = "1회 이상 (전체 페이지, 수는 첫 응답의 totalCount로 결정)"
    return text + (" → 이후 노선별 상세" if step.expand else "")


def _progress(entry: dict[str, Any]) -> str:
    text = (
        f"[{entry['index']}] {entry['operation']} {entry['target']}: {entry['status']}, 항목 {entry['items']}개"
        f" (totalCount {entry['total_count']}), 페이지 {entry['pages']}, HTTP 시도 {entry['http_attempts']}회"
    )
    if entry["stop_reason"]:
        text += f", 중단 사유 {entry['stop_reason']}"
    if entry["error"]:
        text += f", 오류 {entry['error']['category']}: {entry['error']['message']}"
    return text


def _target(**params: Any) -> str:
    return " ".join(f"{name}={value}" for name, value in params.items() if value not in (None, ""))


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("1 이상의 정수여야 합니다.")
    return value


def _non_negative_int(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError("0 이상의 정수여야 합니다.")
    return value


def _bounded_float(low: float, high: float | None) -> Callable[[str], float]:
    """low 초과, high 이하의 유한한 실수만 허용한다."""

    def parse(text: str) -> float:
        value = float(text)
        if not (low < value and (high is None or value <= high)):
            raise argparse.ArgumentTypeError(f"{low:g}보다 커야 합니다" + (f"({high:g} 이하)." if high else "."))
        return value

    return parse


def _coordinate(limit: float) -> Callable[[str], str]:
    """요청에는 입력한 문자열을 그대로 쓰고 범위만 검사한다."""

    def parse(text: str) -> str:
        try:
            value = float(text)
        except ValueError:
            raise argparse.ArgumentTypeError("숫자여야 합니다.") from None
        if not -limit <= value <= limit:
            raise argparse.ArgumentTypeError(f"-{limit:g}~{limit:g} 범위여야 합니다.")
        return text.strip()

    return parse


if __name__ == "__main__":
    sys.exit(main())
