\set ON_ERROR_STOP on

-- 단일 차량 ETA 오차 표본: 버스 한 대의 '실제 도착 − 예측 도착'(초).
-- 판정된 짝 표본(app.transfer_samples)이 부족할 때, 앞 차량과 다음 차량의 오차 분포를 결합해 환승 확률을 내는 데 쓴다
-- (app/modules/evaluation/probability.py). 워커가 설정으로 켠 대상에서 모은다(app/workflows/eta_error_sampling.py).
--   - 도착 시각은 대전시 노선 위치 기록에서 도출한 도착 이벤트(app.transit_observations kind = 'event')다. 범위로만 확인된
--     도착은 오차도 하한·상한으로 남기고 대표값으로 바꾸지 않는다(quality = 'bounded').
--   - 같은 운행의 반복 예측을 독립 표본으로 세지 않도록 운행·생성 방식·모델 버전·잔여시간 구간마다 표본은 하나다.
--     제공처 예측은 모델 버전이 없어(NULL) 고유 키를 coalesce 식 인덱스로 둔다.
--   - 생성 방식(eta_source)·모델 버전·기준 시각 출처를 남겨, 다른 방식의 오차를 한 분포로 섞지 않는다.
-- 재실행해도 결과가 같다.

BEGIN;

CREATE TABLE IF NOT EXISTS app.eta_error_samples (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- 운행 키(노선·차량번호·그 운행의 첫 위치 기록 수집 시각). 정류소는 넣지 않는다.
    run_group TEXT NOT NULL,
    -- 예측 생성 방식(evaluation.schemas.EtaSource)·모델 버전(제공처 예측은 NULL)·기준 시각 출처.
    eta_source TEXT NOT NULL,
    model_version TEXT,
    basis_source TEXT,
    -- 잔여시간(예측 도착 − 기준 시각, 초)과 그 구간 이름(예: 120-300, 600+). 경계 설정이 바뀌면 이름이 달라져 섞이지 않는다.
    horizon_bucket TEXT NOT NULL,
    horizon_seconds DOUBLE PRECISION NOT NULL,
    -- 오차 = 도착 − 예측 도착(초). 점 시각이면 하한 = 상한이다.
    error_lower_seconds DOUBLE PRECISION NOT NULL,
    error_upper_seconds DOUBLE PRECISION NOT NULL,
    quality TEXT NOT NULL,
    route_id BIGINT REFERENCES app.routes(id),
    stop_id BIGINT REFERENCES app.stops(id),
    pattern_key TEXT,
    stop_seq INTEGER,
    vehicle_no TEXT,
    basis_at TIMESTAMPTZ NOT NULL,
    predicted_arrival_at TIMESTAMPTZ NOT NULL,
    arrival_lower_at TIMESTAMPTZ NOT NULL,
    arrival_upper_at TIMESTAMPTZ NOT NULL,
    -- 참조한 관측: 도착 예측(kind = 'arrival')과 도출한 도착 이벤트(kind = 'event').
    prediction_observation_id BIGINT REFERENCES app.transit_observations(id),
    event_observation_id BIGINT REFERENCES app.transit_observations(id),
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT eta_error_samples_quality_check CHECK (quality IN ('exact', 'bounded')),
    -- NaN은 모든 값보다 크게 비교되므로 '< Infinity'로 NaN·무한대를 함께 막는다.
    CONSTRAINT eta_error_samples_error_check
        CHECK (error_lower_seconds > '-Infinity'::double precision
               AND error_upper_seconds < 'Infinity'::double precision
               AND error_lower_seconds <= error_upper_seconds),
    CONSTRAINT eta_error_samples_exact_check CHECK (quality <> 'exact' OR error_lower_seconds = error_upper_seconds),
    CONSTRAINT eta_error_samples_horizon_check
        CHECK (horizon_seconds >= 0 AND horizon_seconds < 'Infinity'::double precision),
    CONSTRAINT eta_error_samples_time_check
        CHECK (arrival_lower_at <= arrival_upper_at AND basis_at <= predicted_arrival_at),
    CONSTRAINT eta_error_samples_details_check CHECK (jsonb_typeof(details) = 'object')
);

CREATE UNIQUE INDEX IF NOT EXISTS eta_error_samples_run_bucket_unique
    ON app.eta_error_samples (run_group, eta_source, (coalesce(model_version, '')), horizon_bucket);

CREATE INDEX IF NOT EXISTS eta_error_samples_select_idx
    ON app.eta_error_samples (eta_source, horizon_bucket, arrival_upper_at);

-- 실행 주체별 최소 권한. 마이그레이션 실행 시점에 있는 역할에만 부여한다(없으면 NOTICE).
--   app_api          API 서버: 표본 조회(확률 계산)
--   app_worker       워커: 표본 생성·조회
--   bus_transfer_api 현재 대상 DB에서 API·워커가 함께 쓰는 단일 접속 역할
DO $$
DECLARE
    grant_row RECORD;
BEGIN
    FOR grant_row IN
        SELECT * FROM (VALUES
            ('app_api', 'app.eta_error_samples', 'SELECT'),
            ('app_worker', 'app.eta_error_samples', 'SELECT, INSERT'),
            ('bus_transfer_api', 'app.eta_error_samples', 'SELECT, INSERT')
        ) AS g(role_name, table_name, privileges)
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = grant_row.role_name) THEN
            RAISE NOTICE '역할 %이 없어 % 권한을 건너뜀', grant_row.role_name, grant_row.table_name;
            CONTINUE;
        END IF;
        EXECUTE format('GRANT %s ON %s TO %I', grant_row.privileges, grant_row.table_name, grant_row.role_name);
    END LOOP;
END
$$;

COMMIT;
