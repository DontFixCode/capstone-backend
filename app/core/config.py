"""실행 위치와 무관하게 저장소 루트의 .env.api에서 실행 설정을 읽는다.

우선순위는 프로세스 환경변수 → .env.api → 기본값이다. 루트 .env는 Supabase 설정이므로 이 클래스로 읽지 않는다.
DB 이름·비밀번호는 app/db/session.py가 루트 .env에서 직접 읽고, 접속 위치·계정은 여기(BACKEND_DB_*)서 읽는다.
"""

from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# 쉼표로 구분한 외부 ID 목록. JSON 배열 대신 사람이 편집하기 쉬운 형식을 쓴다.
IdList = Annotated[tuple[str, ...], NoDecode]


def _split_numbers(value: object) -> tuple[float, ...]:
    """쉼표 구분 숫자 문자열을 순서를 지킨 실수 목록으로 바꾼다. 비어 있으면 빈 목록(미설정)이다."""
    if value is None:
        return ()
    parts = value.split(",") if isinstance(value, str) else list(value)
    return tuple(float(str(part).strip()) for part in parts if str(part).strip())


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[2] / ".env.api",
        env_file_encoding="utf-8",
        env_prefix="BACKEND_",
        extra="ignore",
    )

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # 실행 환경. 지정하지 않으면 운영으로 보고 개발용 기능을 등록하지 않는다.
    env: Literal["development", "production"] = "production"
    # 개발용 확인 URL(/dev/...) 활성화. 개발 환경에서만 효과가 있다.
    enable_devtools: bool = Field(default=False, validation_alias="ENABLE_DEVTOOLS")

    # 공공데이터포털 일반 인증키(Decoding). 접두사 없이 읽고 실제 TAGO 호출 시점에만 확인한다.
    tago_service_key: SecretStr | None = Field(default=None, validation_alias="TAGO_SERVICE_KEY")
    tago_timeout_seconds: float = Field(default=10.0, gt=0)
    tago_request_interval_seconds: float = Field(default=0.5, ge=0)
    tago_max_retries: int = Field(default=2, ge=0, le=5)
    tago_retry_backoff_seconds: float = Field(default=1.0, ge=0)
    # 대전광역시 버스 도착정보(apis.data.go.kr/6300000/arrive) 인증키(Decoding). 비우면 같은 공공데이터포털 계정의
    # TAGO_SERVICE_KEY를 쓴다(해당 API 활용신청이 승인돼 있어야 한다). 현재는 devtools 확인 URL에서만 쓴다.
    daejeon_bus_service_key: SecretStr | None = Field(default=None, validation_alias="DAEJEON_BUS_SERVICE_KEY")
    daejeon_bus_timeout_seconds: float = Field(default=10.0, gt=0)
    # 대전시 노선 위치(getBusPosByRtid) ARR_TIME의 뜻. 도착 시각인지 출발 시각인지 확인하지 못했으므로 기본은 unverified다.
    # unverified면 그 정류소의 ARR_TIME을 도착 시각으로 쓰지 않고 앞뒤 정류소 기록 사이의 범위로만 도착을 남긴다.
    # arrival은 점 시각으로, departure는 [직전 정류소 ARR_TIME, 그 정류소 ARR_TIME] 범위로 쓴다(app.modules.transit.arrival_events).
    daejeon_position_arr_time_meaning: Literal["unverified", "arrival", "departure"] = "unverified"

    # 서비스별 승인 호출 한도. 계정에서 확인한 값이 없으므로 기본값을 두지 않으며 None은 '미확인'이다.
    # 공개 포털의 개발 트래픽 표기를 이 계정의 승인값으로 사용하지 않는다.
    tago_limit_stops: int | None = Field(default=None, gt=0)
    tago_limit_routes: int | None = Field(default=None, gt=0)
    tago_limit_positions: int | None = Field(default=None, gt=0)
    tago_limit_arrivals: int | None = Field(default=None, gt=0)

    # DB 접속 위치. 현재 대상 스택은 풀러 없이 SSH 터널로 DB 컨테이너에 직접 접속하므로 터널의 로컬 주소를 넣는다(필수).
    # 계정이 비어 있으면 postgres를 쓴다. 비밀번호는 여기에 두지 않고 루트 .env의 POSTGRES_PASSWORD를 쓴다.
    db_host: str | None = None
    db_port: int | None = Field(default=None, gt=0, le=65535)
    db_user: str | None = None
    db_connect_timeout_seconds: float = Field(default=10.0, gt=0)

    # 정적 정보는 초기 적재 후 하루 1회 갱신한다(Development Phase·Public Data).
    static_refresh_interval_seconds: float = Field(default=86400.0, gt=0)
    # 실시간 위치·도착정보의 대상별 수집 간격. 개발 단계 기준 600초(Development Phase·Repository Structure).
    realtime_interval_seconds: float = Field(default=600.0, gt=0)
    # 개발 설정으로 지정하는 실시간 수집 대상. 워커가 주기마다 다시 읽으므로 수정하면 등록·해제된다.
    realtime_position_route_ids: IdList = ()
    realtime_arrival_stop_ids: IdList = ()
    # 노선·정류소 실시간 화면 조회(GET .../positions, .../arrivals)가 등록하는 수집 수요의 유효기간(Phase 4A 후속 보완: 30분).
    # 같은 대상을 다시 조회하면 그 시점부터 다시 이 기간으로 연장된다. 공공 API 수집 간격(600초)과 별개다.
    realtime_view_target_ttl_seconds: float = Field(default=1800.0, gt=0)
    # TAGO 도착정보 잔여초(arrtime)의 기준 시각 정책. 원천이 기준 시각을 주지 않으므로 기본은 unconfirmed(절대 ETA 미산출).
    # server_received_at은 서버 응답 수신 시각을 기준으로 근사한다. Notion DB-C04는 "검증한 근사 정책"만 허용하므로
    # 검증(Backend > 검증 대기 항목) 전에는 검증 실행에만 켠다. 결과에는 정책 버전과 기준 출처가 함께 남는다.
    tago_eta_basis_policy: Literal["unconfirmed", "server_received_at"] = "unconfirmed"
    # 워커가 설정을 다시 읽고 만기 대상을 확인하는 최대 대기 간격. 수집 간격이 아니라 실행 루프의 반응 시간이다.
    worker_poll_seconds: float = Field(default=30.0, gt=0)

    # TMAP(SK open API) 보행자 경로 앱키. 키 이름은 기존 .env.api의 SK_OPEN_API_KEY를 쓴다.
    tmap_app_key: SecretStr | None = Field(default=None, validation_alias="SK_OPEN_API_KEY")
    tmap_timeout_seconds: float = Field(default=10.0, gt=0)

    # ---- 경로 검색·평가 실행 설정 (Phase 3) ----
    # Phase 3 Algorithms: 자료에 근거한 공통 수치가 없으므로 기본값을 두지 않는다. 검색 실행 시 하나라도 비어 있으면
    # 설정 오류로 멈춘다(app/workflows/search.py의 load_search_config). 적용 값은 검색마다 route_searches.params에 복사한다.
    search_max_boardings: int | None = None
    search_access_radius_meters: float | None = None
    search_transfer_radius_meters: float | None = None
    search_egress_radius_meters: float | None = None
    search_max_candidates: int | None = None
    search_round_pool_size: int | None = None
    search_walk_cost_weight: float | None = None
    search_max_states: int | None = None
    search_budget_ms: int | None = None
    eval_search_horizon_seconds: float | None = None
    eval_budget_ms: int | None = None
    eval_max_timed_candidates: int | None = None
    eval_boarding_buffer_seconds: float | None = None
    eval_max_data_age_seconds: float | None = None
    eval_min_segment_samples: int | None = None
    eval_min_probability_samples: int | None = None
    eval_min_joint_scenes: int | None = None
    eval_min_improvement_seconds: float | None = None
    eval_max_horizon_difference_seconds: float | None = None
    eval_max_recovery_attempts: int | None = None
    # true면 실패 대응에서 같은 후보 생성 함수로 대체 경로를 재탐색한다(검색과 같은 반경·한도). false면 다음 차량 대기만.
    eval_recovery_search_enabled: bool | None = None
    eval_timing_policy_version: str | None = None
    eval_sample_policy_version: str | None = None
    eval_recovery_policy_version: str | None = None
    eval_segment_model_version: str | None = None
    # 환승 여유시간 등급의 경계(초, "하한,상한"). 여유 m이 0 미만이면 missed, 0 이상 하한 미만 tight, 하한 이상 상한 미만
    # moderate, 상한 이상 comfortable이다. 근거 있는 공통 수치가 없어 기본값을 두지 않으며, 비우면 등급을 만들지 않는다(null).
    eval_slack_grade_thresholds_seconds: Annotated[tuple[float, ...], NoDecode] = ()
    # 환승 확률의 두 번째 단계: 짝지은 두 차량의 판정 표본이 부족하면 단일 차량 ETA 오차 표본(app.eta_error_samples)의 두
    # 분포를 결합해 확률 범위를 낸다. 기본은 꺼짐이다. 켜면 아래 두 값과 BACKEND_ETA_ERROR_HORIZON_EDGES_SECONDS가 있어야
    # 하고, 비어 있으면 검색 설정 오류다. 최근 며칠의 표본을 쓸지와 앞·다음 차량 각각의 최소 표본 수(독립 운행 기준)다.
    eval_marginal_probability_enabled: bool = False
    eval_marginal_window_days: float | None = Field(default=None, gt=0)
    eval_min_marginal_samples: int | None = Field(default=None, gt=0)
    # 최초 검색 결과·필요 실시간 대상의 유효기간(초). 이 동안 워커가 대상을 공유 수집한다.
    search_expires_seconds: float | None = None
    # TMAP 보행자 경로 searchOption(0 추천, 4 추천+대로우선, 10 최단, 30 최단+계단제외)과 검색당 최대 조회 수.
    tmap_search_option: str | None = None
    tmap_max_lookups_per_search: int | None = None
    # 환승 표본·공동 관측 묶음의 사후 판정 기한(평가 시각 기준 초).
    sample_label_deadline_seconds: float | None = None

    # ---- 앱 API·이동 세션 (Phase 4) ----
    # Supabase Auth 액세스 토큰 검증 값. 비우면 루트 .env(Supabase 설정)의 JWT_SECRET·API_EXTERNAL_URL을 쓴다.
    # 이 저장소의 docker-compose.yml에서 GoTrue는 GOTRUE_JWT_SECRET(HS256)으로 서명하고 iss=API_EXTERNAL_URL,
    # aud=GOTRUE_JWT_AUD(authenticated)다. 비밀값은 로그·응답에 남기지 않는다.
    auth_jwt_secret: SecretStr | None = None
    auth_jwt_issuer: str | None = None
    auth_jwt_audience: str = "authenticated"
    # GET /stops/nearby 반경(m). 배포 설정 값이 없으면 기본은 검색 접근 반경, 최대는 접근·목적지 반경 중 큰 값이다.
    stops_nearby_default_radius_meters: float | None = Field(default=None, gt=0)
    stops_nearby_max_radius_meters: float | None = Field(default=None, gt=0)
    # 앱 검색 지원 범위 판정용 대전광역시 행정경계 GeoJSON(features[0].geometry, EPSG:4326). 비우면 저장소의
    # data/service_area/daejeon.geojson을 쓴다. 파일이 없거나 읽을 수 없으면 경계 자료 없음으로 보고 정류소 기준만 적용한다.
    service_area_geojson: str | None = None
    # 장소 검색 제공처. 지원 값은 kakao(카카오 로컬 API: 키워드 장소 검색 + 주소 검색)이며 비우면 /places/search는 503이다.
    places_provider: str | None = None
    # 카카오 REST API 키(developers.kakao.com 앱 키). 접두사 없이 읽는다. 앱의 카카오맵 사용 설정이 켜져 있어야 한다.
    kakao_rest_api_key: SecretStr | None = Field(default=None, validation_alias="KAKAO_REST_API_KEY")
    kakao_timeout_seconds: float = Field(default=5.0, gt=0)
    # 장소 검색 범위(카카오 rect 형식: 최소 경도,최소 위도,최대 경도,최대 위도). 넣으면 이 사각형 안의 결과만 반환하고
    # 비우면 전국 결과를 그대로 반환한다.
    places_search_rect: str | None = None
    # 버스 노선 지도 형상 제공처. 지원 값은 odsay이며 비우면 형상을 수집하지 않는다(기존 저장분은 그대로 반환).
    route_shape_provider: str | None = None
    # ODsay API 키(lab.odsay.com, 서버 플랫폼 키는 호출하는 서버의 IP를 등록해야 한다). 접두사 없이 읽는다.
    odsay_api_key: SecretStr | None = Field(default=None, validation_alias="ODSAY_API_KEY")
    odsay_timeout_seconds: float = Field(default=60.0, gt=0)
    # ODsay 계정의 하루 호출 한도(확인한 값). 최근 24시간 호출 수와 비교해 남은 만큼만 호출한다. 근거 없는 기본값을
    # 두지 않으며, 비우면 워커는 형상을 자동 수집하지 않는다(scripts.import_route_shapes의 --max-calls로만 실행).
    odsay_daily_call_limit: int | None = Field(default=None, gt=0)
    # ODsay 도시 코드(대전 3000)와 노선 그래픽 조회 1회에 넣는 최대 노선 수. 호출 수를 아끼려고 받아야 할 노선을 한 요청에
    # 최대한 넣는다. 요청이 실패하면 수집 흐름이 반으로 나눠 다시 시도한다.
    odsay_city_code: str = "3000"
    odsay_lane_batch_size: int = Field(default=200, gt=0, le=1000)
    # 수동 실행에서 저장한 형상을 다시 받는 간격과 ODsay에 없던 노선을 다시 찾는 간격(초, 기본 30일). 워커는 하루 1회 정적
    # 갱신 뒤 형상 전체를 다시 받으므로 이 간격과 무관하다. 경유 정류소가 바뀌어 패턴이 달라진 노선은 항상 다시 받는다.
    route_shape_refresh_interval_seconds: float = Field(default=2_592_000.0, gt=0)
    # 워커 작업 대기열(검색 접수·이동 재평가)을 확인하는 간격. 공공 API 수집 주기와 별개다.
    job_poll_seconds: float = Field(default=2.0, gt=0)
    # 검색 작업 임대 시간·최대 시도 수. 워커가 중단돼 임대가 지나면 다시 가져가고, 시도를 다 쓰면 error로 끝낸다.
    search_job_lease_seconds: float = Field(default=180.0, gt=0)
    search_job_max_attempts: int = Field(default=2, ge=1)
    # 활성 이동 재평가 간격(Development Phase·Phase 3 Algorithms: 1분, 공유 수집 600초와 별개)과 계산 임대 시간.
    journey_recalc_interval_seconds: float = Field(default=60.0, gt=0)
    journey_recalc_lease_seconds: float = Field(default=120.0, gt=0)
    # 이동 안내에 바로 쓰이는 도착정보(다음 승차 정류소, 탑승 중이면 하차 정류소)를 재계산 직전에 새로 받는 기준(초).
    # 이 시간 안에 받은 값은 다시 받지 않는다. 공유 수집 600초만 쓰면 이미 지나간 차량이 안내에 남는다.
    journey_realtime_refresh_seconds: float = Field(default=60.0, gt=0)
    # 사용자 입력·위치·스트림 연결이 이 시간(초) 넘게 없는 이동은 위 주기 갱신을 멈춘다(상태가 바뀌면 다시 받는다).
    journey_realtime_refresh_idle_seconds: float = Field(default=1800.0, gt=0)
    # 위치 최신성 기준(초). 비우면 평가 자료 최신성 기준 BACKEND_EVAL_MAX_DATA_AGE_SECONDS를 쓴다.
    journey_location_max_age_seconds: float | None = Field(default=None, gt=0)
    # 기기 시계 오차로 허용하는 미래 관측 시각(초). 이보다 미래인 위치는 422로 거부한다.
    location_max_future_skew_seconds: float = Field(default=60.0, ge=0)
    # WebSocket: DB에 저장된 버전을 확인하는 간격, 헤더 없이 연결한 경우 첫 인증 메시지를 기다리는 시간.
    ws_poll_seconds: float = Field(default=2.0, gt=0)
    ws_auth_timeout_seconds: float = Field(default=10.0, gt=0)

    # ---- 회원탈퇴·기록 관리 (Phase 6) ----
    # Supabase Auth 관리자 API 주소(GoTrue). 서버 Docker 네트워크에서는 예: http://auth:9999, 게이트웨이 경유는 .../auth/v1.
    # 근거 없는 기본값을 두지 않는다. 비우면 탈퇴 접수 후 Auth 신규 로그인 차단을 보류하고(auth_blocked_at NULL) 설정되면 재시도한다.
    auth_admin_url: str | None = None
    # service_role 키. 비우면 루트 .env의 SERVICE_ROLE_KEY를 쓴다. 로그·응답·업무 DB에 남기지 않는다.
    auth_service_role_key: SecretStr | None = None
    auth_admin_timeout_seconds: float = Field(default=5.0, gt=0)
    # 신규 로그인 차단 기간(GoTrue ban_duration, Go duration 형식). 기본 876000h(약 100년)는 사실상 영구 차단이다.
    auth_ban_duration: str = Field(default="876000h", pattern=r"^[0-9]+h$")
    # 워커가 차단 대기 계정(deleted_at 있음·auth_blocked_at 없음)을 다시 시도하는 간격과 한 번에 처리할 최대 계정 수.
    auth_block_retry_interval_seconds: float = Field(default=300.0, gt=0)
    auth_block_retry_batch_size: int = Field(default=20, ge=1, le=500)

    # ---- 예상 혼잡도 (Phase 8) ----
    # 근거 없는 기본값을 두지 않는다. 비어 있으면 그 값이 필요한 기능이 사유와 함께 멈춘다(.env.api.example의 가정값 참고).
    # 집계 창: 집계 시점 기준 직전 n개월의 승하차 원본만 쓴다. 비우면 집계를 실행하지 않는다.
    congestion_window_months: int | None = Field(default=None, gt=0, le=36)
    # 표본 일수가 이 값보다 적으면 상위 80퍼센타일을 만들지 않고 응답에 신뢰도 낮음(lowConfidence)을 표시한다.
    congestion_min_sample_days: int | None = Field(default=None, gt=0)
    # 1대당 좌석 수·정원(단계 문턱값의 경계). 둘 다 있어야 혼잡 단계를 만든다. 실제 차량 제원으로 교체한다.
    congestion_seats: int | None = Field(default=None, gt=0)
    congestion_capacity: int | None = Field(default=None, gt=0)
    # 공휴일(YYYY-MM-DD, 쉼표 구분). 일요일과 함께 holiday 요일유형으로 집계·조회한다.
    congestion_holidays: Annotated[tuple[date, ...], NoDecode] = ()
    # 혼잡 단계별 탑승시간 가중치 5개(버스 전세, 여유, 보통, 혼잡, 매우 혼잡 순, 쉼표 구분, 각 1 이상).
    # 추천 비용의 내시균형 bias = Σ 버스 탑승시간 × (가중치 − 1). 비우면 혼잡을 순위에 반영하지 않는다(bias 0).
    congestion_cost_weights: Annotated[tuple[float, ...], NoDecode] = ()

    # ---- ETA 오차 표본 수집 (docs/eta-error-probability.md) ----
    # 버스 한 대씩의 '실제 도착 − 예측 도착' 오차 표본을 워커가 모은다(app/workflows/eta_error_sampling.py). 기본은 꺼짐이다.
    # 켜면 대상·주기·시간대·연결 창·잔여시간 구간 경계·운행 분리 간격이 모두 있어야 하고, 하나라도 비면 설정 오류로 표본
    # 수집만 멈춘다(기존 수집은 계속). 근거 없는 수치를 코드 기본값으로 두지 않는다.
    eta_error_sampling_enabled: bool = False
    # 대상 정류소·노선의 원천 ID(쉼표 구분, 'DJB' 접두사 포함). 정류소는 도착정보를, 노선은 대전시 노선 위치를 아래 주기로 받는다.
    eta_error_sampling_stops: IdList = ()
    eta_error_sampling_routes: IdList = ()
    # 표본 수집 대상의 수집 주기(초). 나머지 대상은 BACKEND_REALTIME_INTERVAL_SECONDS를 그대로 쓴다.
    eta_error_sampling_interval_seconds: float | None = Field(default=None, gt=0)
    # 수집 시간대(KST 시, "시작-끝"을 쉼표로 구분, 끝 시각 제외. 예: 6-23 또는 7-9,17-20). 시간대 밖에서는 호출하지 않는다.
    eta_error_sampling_hours: Annotated[tuple[tuple[int, int], ...], NoDecode] = ()
    # 서비스(도착정보·노선 위치)별로 그날(KST) 대전시 API 호출 수가 이 값에 닿으면 표본 수집 호출을 멈춘다. 검색·안내용
    # 호출은 계속하므로 계정 한도보다 낮게 잡아 여유를 남긴다. 비우면 제한하지 않는다.
    eta_error_sampling_daily_call_limit: int | None = Field(default=None, gt=0)
    # 예측 기준 시각부터 도착까지가 이 시간(초)을 넘으면 예측과 도착을 잇지 않는다(같은 차량의 다른 운행을 잇지 않게 한다).
    eta_error_match_window_seconds: float | None = Field(default=None, gt=0)
    # 잔여시간(예측 도착 − 기준 시각) 구간의 경계(초, 오름차순 쉼표 구분. 예: 120,300,600). 표본 생성과 확률 계산이 같은 값을 쓴다.
    eta_error_horizon_edges_seconds: Annotated[tuple[float, ...], NoDecode] = ()
    # 같은 차량의 위치 기록 간격이 이 시간(초)을 넘으면 다른 운행으로 나눈다.
    eta_error_run_gap_seconds: float | None = Field(default=None, gt=0)

    @field_validator("eta_error_sampling_enabled", "eval_marginal_probability_enabled", mode="before")
    @classmethod
    def blank_flag_is_off(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return False
        return value

    @field_validator("eta_error_sampling_interval_seconds", "eta_error_sampling_daily_call_limit",
                     "eta_error_match_window_seconds", "eta_error_run_gap_seconds", "eval_marginal_window_days",
                     "eval_min_marginal_samples", mode="before")
    @classmethod
    def blank_eta_error_setting_is_missing(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("eta_error_sampling_hours", mode="before")
    @classmethod
    def split_sampling_hours(cls, value: object) -> object:
        """"6-23" 또는 "7-9,17-20" 같은 KST 시간대 목록. 각 구간은 0 ≤ 시작 < 끝 ≤ 24다."""
        if value is None:
            return ()
        parts = value.split(",") if isinstance(value, str) else list(value)
        ranges = []
        for part in parts:
            if isinstance(part, (tuple, list)):
                start, end = part
            elif str(part).strip():
                start, _, end = str(part).strip().partition("-")
            else:
                continue
            try:
                start, end = int(str(start).strip()), int(str(end).strip())
            except ValueError:
                raise ValueError("수집 시간대는 '시작-끝' 형식의 KST 시(예: 6-23)여야 합니다.") from None
            if not 0 <= start < end <= 24:
                raise ValueError("수집 시간대는 0 ≤ 시작 < 끝 ≤ 24여야 합니다.")
            ranges.append((start, end))
        return tuple(ranges)

    @field_validator("eta_error_horizon_edges_seconds", mode="before")
    @classmethod
    def split_horizon_edges(cls, value: object) -> object:
        numbers = _split_numbers(value)
        if any(not 0 < number < float("inf") for number in numbers) or list(numbers) != sorted(set(numbers)):
            raise ValueError("잔여시간 구간 경계는 0보다 크고 서로 다른 오름차순 초 값이어야 합니다.")
        return numbers

    @field_validator("eval_slack_grade_thresholds_seconds", mode="before")
    @classmethod
    def split_slack_grade_thresholds(cls, value: object) -> object:
        numbers = _split_numbers(value)
        if numbers and (len(numbers) != 2 or not 0 < numbers[0] < numbers[1] < float("inf")):
            raise ValueError("환승 여유시간 등급 경계는 0 < 하한 < 상한인 초 값 2개여야 합니다.")
        return numbers

    @field_validator("congestion_cost_weights", mode="before")
    @classmethod
    def split_cost_weights(cls, value: object) -> object:
        if value is None:
            return ()
        parts = value.split(",") if isinstance(value, str) else list(value)
        weights = tuple(float(str(part).strip()) for part in parts if str(part).strip())
        if weights and (len(weights) != 5 or any(not 1.0 <= weight < float("inf") for weight in weights)
                        or list(weights) != sorted(weights)):
            raise ValueError("혼잡 단계별 가중치는 1 이상이고 단계가 높을수록 크거나 같은 값 5개여야 합니다.")
        return weights

    @field_validator("congestion_holidays", mode="before")
    @classmethod
    def split_holidays(cls, value: object) -> object:
        """쉼표 구분 날짜 문자열을 중복 없는 날짜 목록으로 바꾼다. 형식이 틀리면 설정 오류로 알린다."""
        if value is None:
            return ()
        parts = value.split(",") if isinstance(value, str) else list(value)
        days = {date.fromisoformat(str(part).strip()) if not isinstance(part, date) else part
                for part in parts if str(part).strip()}
        return tuple(sorted(days))

    @field_validator(
        "congestion_window_months", "congestion_min_sample_days", "congestion_seats", "congestion_capacity",
        mode="before",
    )
    @classmethod
    def blank_congestion_setting_is_missing(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator(
        "search_max_boardings", "search_access_radius_meters", "search_transfer_radius_meters",
        "search_egress_radius_meters", "search_max_candidates", "search_round_pool_size", "search_walk_cost_weight",
        "search_max_states", "search_budget_ms",
        "eval_search_horizon_seconds", "eval_budget_ms", "eval_max_timed_candidates", "eval_boarding_buffer_seconds",
        "eval_max_data_age_seconds", "eval_min_segment_samples", "eval_min_probability_samples",
        "eval_min_joint_scenes", "eval_min_improvement_seconds", "eval_max_horizon_difference_seconds",
        "eval_max_recovery_attempts", "eval_recovery_search_enabled", "eval_timing_policy_version",
        "eval_sample_policy_version", "eval_recovery_policy_version", "eval_segment_model_version",
        "search_expires_seconds", "tmap_search_option", "tmap_max_lookups_per_search",
        "sample_label_deadline_seconds", "tmap_app_key", "auth_jwt_secret", "auth_jwt_issuer",
        "stops_nearby_default_radius_meters", "stops_nearby_max_radius_meters", "places_provider",
        "journey_location_max_age_seconds", "service_area_geojson", "route_shape_provider", "odsay_api_key",
        "odsay_daily_call_limit", "auth_admin_url", "auth_service_role_key", "kakao_rest_api_key",
        "places_search_rect",
        mode="before",
    )
    @classmethod
    def blank_search_setting_is_missing(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("tago_service_key", "daejeon_bus_service_key", mode="before")
    @classmethod
    def blank_key_is_missing(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("db_host", "db_user", mode="before")
    @classmethod
    def blank_text_is_missing(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("tago_limit_stops", "tago_limit_routes", "tago_limit_positions", "tago_limit_arrivals",
                     "db_port", mode="before")
    @classmethod
    def blank_number_is_missing(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("realtime_position_route_ids", "realtime_arrival_stop_ids", "eta_error_sampling_stops",
                     "eta_error_sampling_routes", mode="before")
    @classmethod
    def split_id_list(cls, value: object) -> object:
        """쉼표 구분 문자열을 순서를 지킨 중복 없는 ID 목록으로 바꾼다. ID는 문자열 그대로 다룬다."""
        if value is None:
            return ()
        parts = value.split(",") if isinstance(value, str) else list(value)
        seen: dict[str, None] = {}
        for part in parts:
            text = str(part).strip()
            if text:
                seen.setdefault(text, None)
        return tuple(seen)

    @property
    def devtools_enabled(self) -> bool:
        return self.env == "development" and self.enable_devtools
