\set ON_ERROR_STOP on

-- 혼잡도(Phase 8): 승하차 원본 누적 테이블과 조회용 집계 테이블.
-- 설계 초안은 Notion "혼잡도 테이블 설계 (ridership_counts · congestion_profiles)"이며, 2026-11-26에 플랫폼 원본 파일
-- (노선별 정류장 승하차 인원, HTML 표 형식 .xls)과 서버 DB의 301번 경유 순서를 대조해 다음을 초안과 다르게 정했다.
--   - 원본에는 총승차 열이 없다(단순승차·하차·환승승차). 세 값을 NOT NULL로 저장하고 총승차는 생성 열로 둔다.
--   - 원본의 순번은 항상 있고 정류소 번호는 비어 있는 행이 있다(301번 51·100번째). 고유 키는 순번을 쓰고
--     정류소 번호는 NULL을 허용한다.
-- 재실행해도 결과가 같다.

BEGIN;

CREATE TABLE IF NOT EXISTS app.ridership_counts (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    route_id BIGINT NOT NULL REFERENCES app.routes(id),
    -- 운행 일자(Asia/Seoul)와 시간대의 시작 시(0~23). 원본 파일의 구분을 그대로 따른다.
    service_date DATE NOT NULL,
    hour SMALLINT NOT NULL,
    -- 원본의 순번(노선 전체 경유 순서, 1부터)·정류소 번호·이름. 정류소 번호는 원본에 비어 있으면 NULL.
    source_stop_order INTEGER NOT NULL,
    source_stop_id TEXT,
    source_stop_name TEXT,
    -- 입력 시점의 경유 순서에 대응시킨 내부 정류소. 대응하지 못하면 NULL.
    stop_id BIGINT REFERENCES app.stops(id),
    board_count INTEGER NOT NULL,
    alight_count INTEGER NOT NULL,
    transfer_board_count INTEGER NOT NULL,
    total_board_count INTEGER GENERATED ALWAYS AS (board_count + transfer_board_count) STORED,
    source_file TEXT NOT NULL,
    uploaded_by UUID REFERENCES app.profiles(user_id),
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ridership_counts_key UNIQUE (route_id, service_date, hour, source_stop_order),
    CONSTRAINT ridership_counts_hour_check CHECK (hour BETWEEN 0 AND 23),
    CONSTRAINT ridership_counts_order_check CHECK (source_stop_order > 0),
    CONSTRAINT ridership_counts_nonnegative_check
        CHECK (board_count >= 0 AND alight_count >= 0 AND transfer_board_count >= 0)
);

CREATE TABLE IF NOT EXISTS app.congestion_profiles (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    route_id BIGINT NOT NULL REFERENCES app.routes(id),
    -- 집계 당시의 패턴 서명과 경유 순번(= route_stops.seq). 값은 그 경유 위치를 출발한 직후 구간의 것이다.
    pattern_key TEXT NOT NULL,
    stop_seq INTEGER NOT NULL,
    day_type TEXT NOT NULL,
    hour SMALLINT NOT NULL,
    -- 그 시간대에 이 구간을 지난 모든 차량의 재차 합(추정)의 일별 평균과 상위 80퍼센타일(표본 부족이면 NULL).
    flow_mean DOUBLE PRECISION NOT NULL,
    flow_p80 DOUBLE PRECISION,
    trips_per_hour DOUBLE PRECISION,
    trips_source TEXT,
    sample_days INTEGER NOT NULL,
    source_period TEXT NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT congestion_profiles_key UNIQUE (route_id, pattern_key, day_type, hour, stop_seq),
    CONSTRAINT congestion_profiles_seq_check CHECK (stop_seq > 0),
    CONSTRAINT congestion_profiles_hour_check CHECK (hour BETWEEN 0 AND 23),
    CONSTRAINT congestion_profiles_sample_check CHECK (sample_days > 0),
    CONSTRAINT congestion_profiles_day_type_check CHECK (day_type IN ('weekday', 'saturday', 'holiday')),
    CONSTRAINT congestion_profiles_trips_source_check CHECK (trips_source IN ('observed', 'timetable')),
    -- NaN은 모든 값보다 크게 비교되므로 '< Infinity'로 NaN·무한대를 함께 막는다.
    CONSTRAINT congestion_profiles_flow_check
        CHECK (flow_mean >= 0 AND flow_mean < 'Infinity'::double precision
               AND (flow_p80 IS NULL OR (flow_p80 >= 0 AND flow_p80 < 'Infinity'::double precision))),
    CONSTRAINT congestion_profiles_trips_check
        CHECK (trips_per_hour IS NULL OR (trips_per_hour > 0 AND trips_per_hour < 'Infinity'::double precision)),
    CONSTRAINT congestion_profiles_trips_pair_check CHECK ((trips_per_hour IS NULL) = (trips_source IS NULL))
);

-- 실행 주체별 최소 권한. 마이그레이션 실행 시점에 있는 역할에만 부여한다(없으면 NOTICE).
--   app_api          API 서버: 집계 조회
--   app_worker       워커·입력 CLI: 원본 누적, 집계 교체
--   bus_transfer_api 현재 대상 DB에서 API·워커가 함께 쓰는 단일 접속 역할
DO $$
DECLARE
    grant_row RECORD;
BEGIN
    FOR grant_row IN
        SELECT * FROM (VALUES
            ('app_api', 'app.congestion_profiles', 'SELECT'),
            ('app_worker', 'app.ridership_counts', 'SELECT, INSERT'),
            ('app_worker', 'app.congestion_profiles', 'SELECT, INSERT, DELETE'),
            ('bus_transfer_api', 'app.ridership_counts', 'SELECT, INSERT'),
            ('bus_transfer_api', 'app.congestion_profiles', 'SELECT, INSERT, DELETE')
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
