\set ON_ERROR_STOP on
BEGIN;
CREATE SCHEMA IF NOT EXISTS app;

CREATE TABLE app.api_fetches (
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 batch_key UUID NOT NULL,
 provider TEXT NOT NULL,
 api_kind TEXT NOT NULL CHECK (api_kind IN ('stops','routes','route_stops','positions','arrivals')),
 target_key TEXT NOT NULL,
 request_params JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(request_params)='object' AND NOT (request_params ?| ARRAY['serviceKey','appKey'])),
 page_no INTEGER NOT NULL CHECK (page_no > 0),
 attempt_no INTEGER NOT NULL CHECK (attempt_no > 0),
 requested_at TIMESTAMPTZ NOT NULL,
 received_at TIMESTAMPTZ,
 http_status INTEGER,
 provider_code TEXT,
 result_status TEXT NOT NULL CHECK (result_status IN ('success','empty','partial','error','timeout')),
 total_count INTEGER CHECK (total_count >= 0),
 raw_body TEXT,
 normalizer_version TEXT NOT NULL,
 published_at TIMESTAMPTZ,
 error_reason TEXT,
 UNIQUE (batch_key,page_no,attempt_no),
 CHECK (published_at IS NULL OR result_status IN ('success','empty'))
);
CREATE UNIQUE INDEX api_fetches_published_page_unique ON app.api_fetches(batch_key,page_no) WHERE published_at IS NOT NULL;
CREATE INDEX api_fetches_latest_idx ON app.api_fetches(provider,api_kind,target_key,published_at DESC);
CREATE INDEX api_fetches_attempt_idx ON app.api_fetches(provider,api_kind,target_key,requested_at DESC);
CREATE INDEX api_fetches_usage_idx ON app.api_fetches(provider,api_kind,requested_at);
CREATE INDEX api_fetches_batch_idx ON app.api_fetches(batch_key,page_no);

CREATE TABLE app.stops (
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 provider TEXT NOT NULL,
 external_id TEXT NOT NULL,
 name TEXT,
 stop_no TEXT,
 location extensions.geography(Point,4326),
 external_ids JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(external_ids)='object'),
 data_version UUID NOT NULL,
 source_fetch_id BIGINT REFERENCES app.api_fetches(id) ON DELETE SET NULL,
 is_active BOOLEAN NOT NULL DEFAULT TRUE,
 updated_at TIMESTAMPTZ NOT NULL,
 UNIQUE(provider,external_id),
 CHECK (location IS NULL OR (extensions.ST_X(location::extensions.geometry) BETWEEN -180 AND 180 AND extensions.ST_Y(location::extensions.geometry) BETWEEN -90 AND 90 AND NOT (extensions.ST_X(location::extensions.geometry)=0 AND extensions.ST_Y(location::extensions.geometry)=0)))
);
CREATE INDEX stops_location_gist ON app.stops USING GIST(location);

CREATE TABLE app.routes (
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 provider TEXT NOT NULL,
 external_id TEXT NOT NULL,
 route_no TEXT,
 route_type TEXT,
 start_name TEXT,
 end_name TEXT,
 service_info JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(service_info)='object'),
 shape_info JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(shape_info)='object'),
 external_ids JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(external_ids)='object'),
 data_version UUID NOT NULL,
 source_fetch_id BIGINT REFERENCES app.api_fetches(id) ON DELETE SET NULL,
 is_active BOOLEAN NOT NULL DEFAULT TRUE,
 updated_at TIMESTAMPTZ NOT NULL,
 UNIQUE(provider,external_id)
);

CREATE TABLE app.route_stops (
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 route_id BIGINT NOT NULL REFERENCES app.routes(id),
 pattern_key TEXT NOT NULL,
 direction_code TEXT,
 seq INTEGER NOT NULL CHECK (seq > 0),
 source_node_order TEXT,
 stop_id BIGINT NOT NULL REFERENCES app.stops(id),
 cumulative_distance_m DOUBLE PRECISION CHECK (cumulative_distance_m >= 0 AND cumulative_distance_m < 'Infinity'::double precision),
 data_version UUID NOT NULL,
 source_fetch_id BIGINT REFERENCES app.api_fetches(id) ON DELETE SET NULL,
 UNIQUE(route_id,pattern_key,seq)
);
CREATE INDEX route_stops_board_idx ON app.route_stops(stop_id,route_id,pattern_key,seq);

CREATE TABLE app.transit_observations (
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 fetch_id BIGINT REFERENCES app.api_fetches(id),
 item_index INTEGER CHECK (item_index >= 0),
 kind TEXT NOT NULL CHECK (kind IN ('position','arrival','event')),
 route_id BIGINT REFERENCES app.routes(id),
 stop_id BIGINT REFERENCES app.stops(id),
 pattern_key TEXT,
 stop_seq INTEGER CHECK (stop_seq > 0),
 vehicle_no TEXT,
 run_key TEXT,
 location extensions.geography(Point,4326),
 remaining_seconds INTEGER CHECK (remaining_seconds >= 0),
 eta_basis_at TIMESTAMPTZ,
 predicted_arrival_at TIMESTAMPTZ,
 source_generated_at TIMESTAMPTZ,
 event_type TEXT CHECK (event_type IN ('arrival','departure','pass')),
 quality TEXT CHECK (quality IN ('exact','bounded','insufficient')),
 event_key TEXT,
 event_at TIMESTAMPTZ,
 collected_at TIMESTAMPTZ NOT NULL,
 details JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(details)='object'),
 CHECK ((kind = 'event' AND event_type IS NOT NULL AND quality IS NOT NULL AND event_key IS NOT NULL) OR (kind <> 'event' AND fetch_id IS NOT NULL AND item_index IS NOT NULL AND event_type IS NULL AND quality IS NULL AND event_key IS NULL)),
 CHECK (quality IS DISTINCT FROM 'exact' OR event_at IS NOT NULL),
 CHECK (predicted_arrival_at IS NULL OR (remaining_seconds IS NOT NULL AND eta_basis_at IS NOT NULL AND details ? 'eta_basis_source' AND predicted_arrival_at = eta_basis_at + remaining_seconds * INTERVAL '1 second'))
);
CREATE UNIQUE INDEX transit_observations_fetch_item_unique ON app.transit_observations(fetch_id,item_index) WHERE kind <> 'event';
CREATE UNIQUE INDEX transit_observations_event_unique ON app.transit_observations(event_key) WHERE kind = 'event';
CREATE INDEX transit_observations_route_idx ON app.transit_observations(route_id,kind,collected_at DESC);
CREATE INDEX transit_observations_stop_idx ON app.transit_observations(stop_id,kind,collected_at DESC);
CREATE INDEX transit_observations_vehicle_idx ON app.transit_observations(vehicle_no,collected_at);
CREATE INDEX transit_observations_event_idx ON app.transit_observations(pattern_key,stop_seq,event_type,event_at) WHERE kind = 'event';
COMMIT;
