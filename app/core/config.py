"""실행 위치와 무관하게 저장소 루트의 .env.api에서 실행 설정을 읽는다.

우선순위는 프로세스 환경변수 → .env.api → 기본값이다. 루트 .env는 Supabase 설정이므로 이 클래스로 읽지 않는다.
DB 이름·비밀번호는 app/db/session.py가 루트 .env에서 직접 읽고, 접속 위치·계정은 여기(BACKEND_DB_*)서 읽는다.
"""

from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# 쉼표로 구분한 외부 ID 목록. JSON 배열 대신 사람이 편집하기 쉬운 형식을 쓴다.
IdList = Annotated[tuple[str, ...], NoDecode]


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
    # 장소 검색 제공처. 프론트엔드와 지도 제공처를 확정하기 전이라 비어 있고, 비어 있으면 /places/search는 503이다.
    places_provider: str | None = None
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
    # 위치 최신성 기준(초). 비우면 평가 자료 최신성 기준 BACKEND_EVAL_MAX_DATA_AGE_SECONDS를 쓴다.
    journey_location_max_age_seconds: float | None = Field(default=None, gt=0)
    # 기기 시계 오차로 허용하는 미래 관측 시각(초). 이보다 미래인 위치는 422로 거부한다.
    location_max_future_skew_seconds: float = Field(default=60.0, ge=0)
    # WebSocket: DB에 저장된 버전을 확인하는 간격, 헤더 없이 연결한 경우 첫 인증 메시지를 기다리는 시간.
    ws_poll_seconds: float = Field(default=2.0, gt=0)
    ws_auth_timeout_seconds: float = Field(default=10.0, gt=0)

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
        "odsay_daily_call_limit",
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

    @field_validator("realtime_position_route_ids", "realtime_arrival_stop_ids", mode="before")
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
