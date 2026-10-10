-- =====================================================================
-- 1차 MVP 스키마 (핵심 13개 테이블)
--   profiles, regions, categories, service_types, shops,
--   shop_business_hours, shop_closures, services, staff, staff_services,
--   staff_work_hours, reservations, reservation_logs
-- 기준 문서: Database Structure 보완안 (2026-10-03)
-- 2차 MVP 11개 테이블(staff_time_off, 평판, 채팅, 신고, 알림)은 별도 마이그레이션.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 0. 확장 · 스키마 · 역할
-- ---------------------------------------------------------------------
create schema if not exists extensions;
create extension if not exists btree_gist with schema extensions;
create extension if not exists pgcrypto  with schema extensions;

-- 서버 전용 헬퍼·쓰기 함수 스키마
create schema if not exists app;
revoke all on schema app from public;

-- FastAPI가 사용하는 제한 역할 (테이블 소유자 아님, BYPASSRLS 아님)
-- 실제 접속 계정은 별도 LOGIN 역할을 만든 뒤 이 역할을 GRANT 합니다. (README 참고)
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'app_backend') then
    create role app_backend nologin;
  end if;
  -- 승인 만료·관리 작업 등 서버 내부 작업 전용 역할
  if not exists (select 1 from pg_roles where rolname = 'app_worker') then
    create role app_worker nologin;
  end if;
end $$;

grant usage on schema public, app, extensions to app_backend, app_worker;

-- ---------------------------------------------------------------------
-- 1. 공통 함수
-- ---------------------------------------------------------------------
create or replace function app.set_updated_at()
returns trigger language plpgsql set search_path = '' as $$
begin
  new.updated_at := now();
  return new;
end $$;

-- 공백이 아닌 문자열 여부
create or replace function app.is_filled(t text)
returns boolean language sql immutable set search_path = '' as $$
  select t is not null and btrim(t) <> ''
$$;

-- TIME → 하루 중 초 (배제 제약용)
create or replace function app.time_sec(t time)
returns integer language sql immutable set search_path = '' as $$
  select extract(epoch from t)::integer
$$;

-- ---------------------------------------------------------------------
-- 2. profiles
-- ---------------------------------------------------------------------
create table public.profiles (
  id            uuid primary key default gen_random_uuid(),
  auth_user_id  uuid unique references auth.users(id) on delete set null,
  name          varchar(100),
  phone         varchar(30),
  platform_role varchar(20) not null default 'USER',
  status        varchar(20) not null default 'ACTIVE',
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  deleted_at    timestamptz,
  constraint profiles_role_chk   check (platform_role in ('USER','ADMIN')),
  constraint profiles_status_chk check (status in ('ACTIVE','SUSPENDED','DELETED')),
  constraint profiles_active_required_chk
    check (status = 'DELETED' or (auth_user_id is not null and app.is_filled(name))),
  constraint profiles_deleted_at_chk
    check ((status = 'DELETED') = (deleted_at is not null))
);

create trigger profiles_updated_at before update on public.profiles
  for each row execute function app.set_updated_at();

-- Auth 가입 → 프로필 자동 생성. 권한은 메타데이터와 무관하게 USER 고정.
create or replace function app.handle_new_auth_user()
returns trigger language plpgsql security definer set search_path = '' as $$
declare
  v_name text := nullif(btrim(coalesce(new.raw_user_meta_data->>'name', '')), '');
begin
  insert into public.profiles (auth_user_id, name, phone, platform_role, status)
  values (
    new.id,
    left(coalesce(v_name, nullif(split_part(coalesce(new.email, ''), '@', 1), ''), '회원'), 100),
    left(nullif(btrim(coalesce(new.raw_user_meta_data->>'phone', '')), ''), 30),
    'USER',
    'ACTIVE'
  )
  on conflict (auth_user_id) do nothing;
  return new;
end $$;
revoke all on function app.handle_new_auth_user() from public;

create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function app.handle_new_auth_user();

-- ---------------------------------------------------------------------
-- 3. regions / categories / service_types
-- ---------------------------------------------------------------------
create table public.regions (
  id      uuid primary key default gen_random_uuid(),
  sido    varchar(50)  not null,
  sigungu varchar(100) not null,
  constraint regions_uq unique (sido, sigungu),
  constraint regions_name_chk check (app.is_filled(sido) and app.is_filled(sigungu))
);

create table public.categories (
  id   uuid primary key default gen_random_uuid(),
  code varchar(50)  not null unique,
  name varchar(100) not null,
  constraint categories_name_chk check (app.is_filled(code) and app.is_filled(name))
);

create table public.service_types (
  id          uuid primary key default gen_random_uuid(),
  category_id uuid not null references public.categories(id) on delete restrict,
  code        varchar(50)  not null unique,
  name        varchar(100) not null,
  is_active   boolean not null default true,
  constraint service_types_cat_id_uq unique (category_id, id),
  constraint service_types_name_chk check (app.is_filled(code) and app.is_filled(name))
);

-- ---------------------------------------------------------------------
-- 4. shops
-- ---------------------------------------------------------------------
create table public.shops (
  id                   uuid primary key default gen_random_uuid(),
  owner_id             uuid not null references public.profiles(id) on delete restrict,
  category_id          uuid not null references public.categories(id) on delete restrict,
  region_id            uuid not null references public.regions(id) on delete restrict,
  name                 varchar(200) not null,
  address              text not null,
  latitude             numeric(9,6),
  longitude            numeric(9,6),
  timezone             varchar(50) not null default 'Asia/Seoul',
  phone                varchar(30),
  description          text,
  slot_unit_min        integer not null default 15,
  min_lead_time_min    integer not null default 0,
  late_cancel_hours    integer not null default 3,
  approval_timeout_min integer not null default 30,
  min_trust_score      numeric(5,2),
  auto_confirm         boolean not null default false,
  status               varchar(20) not null default 'ACTIVE',
  source               varchar(20) not null default 'INTERNAL',
  external_ref         text,
  imported_at          timestamptz,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now(),
  constraint shops_id_category_uq unique (id, category_id),
  constraint shops_name_chk     check (app.is_filled(name) and app.is_filled(address)),
  constraint shops_policy_chk   check (slot_unit_min > 0 and min_lead_time_min >= 0
                                       and late_cancel_hours >= 0 and approval_timeout_min > 0),
  constraint shops_latlng_chk   check ((latitude is null) = (longitude is null)),
  constraint shops_lat_range    check (latitude  is null or latitude  between -90  and 90),
  constraint shops_lng_range    check (longitude is null or longitude between -180 and 180),
  constraint shops_trust_chk    check (min_trust_score is null or min_trust_score between 0 and 100),
  constraint shops_status_chk   check (status in ('ACTIVE','INACTIVE')),
  constraint shops_source_chk   check (source in ('INTERNAL','NAVER')),
  constraint shops_timezone_chk check (timezone = 'Asia/Seoul')
);
create index shops_owner_idx on public.shops (owner_id);
create index shops_search_idx on public.shops (region_id, category_id) where status = 'ACTIVE';

create trigger shops_updated_at before update on public.shops
  for each row execute function app.set_updated_at();

-- 소유자 임의 변경 금지
create or replace function app.shops_guard()
returns trigger language plpgsql set search_path = '' as $$
begin
  if new.owner_id is distinct from old.owner_id then
    raise exception 'shops.owner_id는 변경할 수 없습니다' using errcode = '42501';
  end if;
  return new;
end $$;
create trigger shops_guard before update on public.shops
  for each row execute function app.shops_guard();

-- ---------------------------------------------------------------------
-- 5. shop_business_hours / shop_closures
-- ---------------------------------------------------------------------
create table public.shop_business_hours (
  id          uuid primary key default gen_random_uuid(),
  shop_id     uuid not null references public.shops(id) on delete restrict,
  day_of_week smallint not null,
  open_time   time not null,
  close_time  time not null,
  constraint sbh_uq unique (shop_id, day_of_week, open_time),
  constraint sbh_dow_chk check (day_of_week between 0 and 6),
  constraint sbh_range_chk check (open_time < close_time and close_time < time '24:00'),
  constraint sbh_minute_chk check (extract(second from open_time) = 0
                                   and extract(second from close_time) = 0),
  constraint sbh_no_overlap exclude using gist (
    shop_id with =,
    day_of_week with =,
    int4range(app.time_sec(open_time), app.time_sec(close_time), '[)') with &&
  )
);

create table public.shop_closures (
  shop_id     uuid not null references public.shops(id) on delete restrict,
  closed_date date not null,
  reason      text,
  primary key (shop_id, closed_date)
);

-- ---------------------------------------------------------------------
-- 6. services
-- ---------------------------------------------------------------------
create table public.services (
  id              uuid primary key default gen_random_uuid(),
  shop_id         uuid not null,
  category_id     uuid not null,
  service_type_id uuid not null,
  name            varchar(200) not null,
  price           numeric(12,0) not null,
  duration_min    integer not null,
  buffer_min      integer not null default 0,
  is_active       boolean not null default true,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  constraint services_shop_fk foreign key (shop_id, category_id)
    references public.shops(id, category_id) on delete restrict,
  constraint services_type_fk foreign key (category_id, service_type_id)
    references public.service_types(category_id, id) on delete restrict,
  constraint services_shop_id_uq unique (shop_id, id),
  constraint services_values_chk check (price >= 0 and duration_min > 0 and buffer_min >= 0),
  constraint services_name_chk check (app.is_filled(name))
);
create index services_shop_idx on public.services (shop_id) where is_active;
create index services_type_idx on public.services (service_type_id, shop_id) where is_active;

create trigger services_updated_at before update on public.services
  for each row execute function app.set_updated_at();

-- ---------------------------------------------------------------------
-- 7. staff / staff_services / staff_work_hours
-- ---------------------------------------------------------------------
create table public.staff (
  id         uuid primary key default gen_random_uuid(),
  shop_id    uuid not null references public.shops(id) on delete restrict,
  name       varchar(100) not null,
  title      varchar(100),
  is_active  boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint staff_shop_id_uq unique (shop_id, id),
  constraint staff_name_chk check (app.is_filled(name))
);
create index staff_shop_idx on public.staff (shop_id) where is_active;

create trigger staff_updated_at before update on public.staff
  for each row execute function app.set_updated_at();

create table public.staff_services (
  shop_id               uuid not null,
  staff_id              uuid not null,
  service_id            uuid not null,
  price_override        numeric(12,0),
  duration_override_min integer,
  is_active             boolean not null default true,
  primary key (staff_id, service_id),
  constraint staff_services_triple_uq unique (shop_id, staff_id, service_id),
  constraint staff_services_staff_fk foreign key (shop_id, staff_id)
    references public.staff(shop_id, id) on delete restrict,
  constraint staff_services_service_fk foreign key (shop_id, service_id)
    references public.services(shop_id, id) on delete restrict,
  constraint staff_services_override_chk check (
    (price_override is null or price_override >= 0) and
    (duration_override_min is null or duration_override_min > 0))
);
create index staff_services_service_idx on public.staff_services (service_id, staff_id) where is_active;

create table public.staff_work_hours (
  id          uuid primary key default gen_random_uuid(),
  staff_id    uuid not null references public.staff(id) on delete restrict,
  day_of_week smallint not null,
  start_time  time not null,
  end_time    time not null,
  constraint swh_uq unique (staff_id, day_of_week, start_time),
  constraint swh_dow_chk check (day_of_week between 0 and 6),
  constraint swh_range_chk check (start_time < end_time and end_time < time '24:00'),
  constraint swh_minute_chk check (extract(second from start_time) = 0
                                   and extract(second from end_time) = 0),
  constraint swh_no_overlap exclude using gist (
    staff_id with =,
    day_of_week with =,
    int4range(app.time_sec(start_time), app.time_sec(end_time), '[)') with &&
  )
);

-- ---------------------------------------------------------------------
-- 8. reservations
-- ---------------------------------------------------------------------
create table public.reservations (
  id                  uuid primary key default gen_random_uuid(),
  user_id             uuid not null references public.profiles(id) on delete restrict,
  shop_id             uuid not null,
  staff_id            uuid not null,
  service_id          uuid not null,
  start_at            timestamptz not null,
  service_end_at      timestamptz not null,
  end_at              timestamptz not null,
  duration_min        integer not null,
  buffer_min          integer not null default 0,
  late_cancel_hours   integer not null,
  status              varchar(20) not null,
  requested_at        timestamptz not null default now(),
  approval_expires_at timestamptz,
  shop_name           varchar(200) not null,
  staff_name          varchar(100) not null,
  service_name        varchar(200) not null,
  price               numeric(12,0) not null,
  customer_memo       text,
  reject_reason       text,
  canceled_by         varchar(20),
  is_late_cancel      boolean not null default false,
  canceled_at         timestamptz,
  cancel_reason       text,
  source              varchar(30) not null default 'INTERNAL',
  external_ref        text,
  version             integer not null default 1,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now(),

  constraint reservations_staff_service_fk foreign key (shop_id, staff_id, service_id)
    references public.staff_services(shop_id, staff_id, service_id) on delete restrict,
  constraint reservations_id_user_shop_uq unique (id, user_id, shop_id),
  constraint reservations_values_chk check (duration_min > 0 and buffer_min >= 0
    and late_cancel_hours >= 0 and price >= 0 and version >= 1),
  constraint reservations_time_chk check (
    start_at < service_end_at and service_end_at <= end_at
    and service_end_at - start_at = duration_min * interval '1 minute'
    and end_at - service_end_at   = buffer_min   * interval '1 minute'),
  constraint reservations_status_chk check (status in
    ('PENDING','CONFIRMED','REJECTED','CANCELED','COMPLETED','NO_SHOW','EXPIRED')),
  constraint reservations_pending_chk check (status <> 'PENDING' or (
    approval_expires_at is not null
    and requested_at < approval_expires_at and approval_expires_at <= start_at)),
  constraint reservations_canceled_by_chk check (canceled_by is null or canceled_by in ('USER','SHOP','SYSTEM')),
  constraint reservations_cancel_chk check (
    (status = 'CANCELED' and canceled_by is not null and canceled_at is not null and app.is_filled(cancel_reason))
    or (status <> 'CANCELED' and canceled_by is null and canceled_at is null
        and cancel_reason is null and is_late_cancel = false)),
  constraint reservations_late_cancel_chk check (not is_late_cancel or (status = 'CANCELED' and canceled_by = 'USER')),
  constraint reservations_reject_chk check (status <> 'REJECTED' or app.is_filled(reject_reason)),
  constraint reservations_source_chk check (source in ('INTERNAL','NAVER','KAKAO','OTHER')),
  constraint reservations_external_chk check (source <> 'INTERNAL' or external_ref is null),
  constraint reservations_created_chk check (created_at <= requested_at),
  constraint reservations_names_chk check (app.is_filled(shop_name) and app.is_filled(staff_name) and app.is_filled(service_name)),
  -- 같은 담당자의 점유 구간 [start_at, end_at) 중첩 금지
  constraint reservations_no_overlap exclude using gist (
    staff_id with =,
    tstzrange(start_at, end_at, '[)') with &&
  ) where (status in ('PENDING','CONFIRMED','COMPLETED','NO_SHOW'))
);
create index reservations_user_idx    on public.reservations (user_id, start_at desc, id);
create index reservations_shop_idx    on public.reservations (shop_id, start_at, id);
create index reservations_expire_idx  on public.reservations (approval_expires_at) where status = 'PENDING';
create index reservations_service_idx on public.reservations (service_id);
create unique index reservations_external_uq on public.reservations (source, external_ref)
  where external_ref is not null;

create trigger reservations_updated_at before update on public.reservations
  for each row execute function app.set_updated_at();

-- ---------------------------------------------------------------------
-- 9. reservation_logs (append-only 감사 이력)
-- ---------------------------------------------------------------------
create table public.reservation_logs (
  id              uuid primary key default gen_random_uuid(),
  reservation_id  uuid not null references public.reservations(id) on delete restrict,
  action          varchar(30) not null,
  before_snapshot jsonb,
  after_snapshot  jsonb not null,
  actor_type      varchar(20) not null,
  actor_user_id   uuid references public.profiles(id) on delete restrict,
  idempotency_key uuid not null unique,
  request_hash    char(64) not null,
  reason          text,
  created_at      timestamptz not null default now(),
  constraint rlogs_action_chk check (action in
    ('CREATE','APPROVE','REJECT','RESCHEDULE','CANCEL','COMPLETE','NO_SHOW','EXPIRE')),
  constraint rlogs_actor_chk check (actor_type in ('CUSTOMER','SHOP','ADMIN','SYSTEM')),
  constraint rlogs_actor_user_chk check ((actor_type = 'SYSTEM') = (actor_user_id is null)),
  constraint rlogs_before_chk check ((action = 'CREATE') = (before_snapshot is null)),
  constraint rlogs_snapshot_type_chk check (
    jsonb_typeof(after_snapshot) = 'object'
    and (before_snapshot is null or jsonb_typeof(before_snapshot) = 'object')),
  constraint rlogs_snapshot_keys_chk check (
    after_snapshot ?& array['status','start_at','service_end_at','end_at','shop_id','staff_id',
      'service_id','shop_name','staff_name','service_name','price','duration_min','buffer_min',
      'late_cancel_hours','requested_at','approval_expires_at','canceled_by','canceled_at',
      'is_late_cancel','version']
    and (before_snapshot is null or before_snapshot ?& array['status','start_at','end_at','version'])),
  constraint rlogs_version_chk check (
    (action = 'CREATE' and (after_snapshot->>'version')::int = 1)
    or (action <> 'CREATE' and (after_snapshot->>'version')::int = (before_snapshot->>'version')::int + 1)),
  constraint rlogs_reason_chk check (action not in ('REJECT','CANCEL','NO_SHOW') or app.is_filled(reason)),
  constraint rlogs_hash_chk check (request_hash ~ '^[0-9a-f]{64}$')
);
create index rlogs_reservation_idx on public.reservation_logs (reservation_id, created_at, id);
create index rlogs_actor_idx on public.reservation_logs (actor_user_id) where actor_user_id is not null;

create or replace function app.reservation_logs_immutable()
returns trigger language plpgsql set search_path = '' as $$
begin
  raise exception 'reservation_logs는 수정·삭제할 수 없습니다' using errcode = '42501';
end $$;
create trigger reservation_logs_no_update before update or delete on public.reservation_logs
  for each row execute function app.reservation_logs_immutable();

-- =====================================================================
-- 10. 요청 문맥 · 권한 헬퍼
--   FastAPI는 트랜잭션마다 아래처럼 설정합니다 (JWT 검증 후):
--     select set_config('app.auth_user_id', '<jwt sub>', true);
-- =====================================================================
create or replace function app.current_profile_id()
returns uuid language sql stable security definer set search_path = '' as $$
  select p.id from public.profiles p
  where p.auth_user_id = nullif(current_setting('app.auth_user_id', true), '')::uuid
    and p.status = 'ACTIVE'
$$;

create or replace function app.is_admin()
returns boolean language sql stable security definer set search_path = '' as $$
  select coalesce((select p.platform_role = 'ADMIN' from public.profiles p
                   where p.id = app.current_profile_id()), false)
$$;

-- 서버 내부 작업(만료·관리) 문맥 여부. superuser/마이그레이션도 포함
create or replace function app.is_system()
returns boolean language sql stable set search_path = '' as $$
  select pg_catalog.pg_has_role(current_user, 'app_worker', 'MEMBER')
$$;

create or replace function app.owns_shop(p_shop_id uuid)
returns boolean language sql stable security definer set search_path = '' as $$
  select exists (select 1 from public.shops s
                 where s.id = p_shop_id and s.owner_id = app.current_profile_id())
$$;

create or replace function app.can_view_shop(p_shop_id uuid)
returns boolean language sql stable security definer set search_path = '' as $$
  select exists (select 1 from public.shops s
                 where s.id = p_shop_id
                   and (s.status = 'ACTIVE' or s.owner_id = app.current_profile_id()))
         or app.is_admin()
$$;

create or replace function app.staff_shop_id(p_staff_id uuid)
returns uuid language sql stable security definer set search_path = '' as $$
  select shop_id from public.staff where id = p_staff_id
$$;

-- 잠금 헬퍼: RLS와 무관하게 공통 순서로 행 잠금 (업체 → 담당자 ID 순)
create or replace function app.lock_shop(p_shop_id uuid)
returns public.shops language sql volatile security definer set search_path = '' as $$
  select * from public.shops where id = p_shop_id for update
$$;

create or replace function app.lock_staff(p_ids uuid[])
returns void language plpgsql volatile security definer set search_path = '' as $$
begin
  perform 1 from public.staff where id = any(p_ids) order by id for update;
end $$;

-- 예약 스냅샷(JSON): 로그 before/after에 사용. 개인정보(메모 등)는 제외
create or replace function app.reservation_snapshot(r public.reservations)
returns jsonb language sql stable set search_path = '' as $$
  select jsonb_build_object(
    'status', r.status,
    'start_at', to_char(r.start_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
    'service_end_at', to_char(r.service_end_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
    'end_at', to_char(r.end_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
    'shop_id', r.shop_id, 'staff_id', r.staff_id, 'service_id', r.service_id,
    'shop_name', r.shop_name, 'staff_name', r.staff_name, 'service_name', r.service_name,
    'price', r.price, 'duration_min', r.duration_min, 'buffer_min', r.buffer_min,
    'late_cancel_hours', r.late_cancel_hours,
    'requested_at', to_char(r.requested_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
    'approval_expires_at', to_char(r.approval_expires_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
    'canceled_by', r.canceled_by,
    'canceled_at', to_char(r.canceled_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
    'is_late_cancel', r.is_late_cancel,
    'version', r.version)
$$;

-- =====================================================================
-- 11. 예약 무결성 트리거
--   - 가격·시간·이름·정책 스냅샷은 DB가 계산 (클라이언트 값 무시)
--   - 업체 행 잠금 → 영업/근무/휴무/슬롯/사전시간 검증
--   - 상태 전이 · 실행 주체 검증
--   중복 점유는 reservations_no_overlap 배제 제약이 최종 차단 (HTTP 409로 변환)
-- =====================================================================
create or replace function app.check_reservation_slot(
  p_shop public.shops, p_staff_id uuid, p_start timestamptz, p_end timestamptz)
returns void language plpgsql stable set search_path = '' as $$
declare
  v_ls  timestamp := p_start at time zone p_shop.timezone;
  v_le  timestamp := p_end   at time zone p_shop.timezone;
  v_dow smallint  := extract(dow from v_ls)::smallint;
begin
  if v_ls::date <> v_le::date then
    raise exception '자정을 넘기는 예약은 지원하지 않습니다' using errcode = '23514';
  end if;
  if exists (select 1 from public.shop_closures c
             where c.shop_id = p_shop.id and c.closed_date = v_ls::date) then
    raise exception '업체 휴무일입니다' using errcode = '23514';
  end if;
  if not exists (
    select 1 from public.shop_business_hours h
    where h.shop_id = p_shop.id and h.day_of_week = v_dow
      and h.open_time <= v_ls::time and v_le::time <= h.close_time
      and (app.time_sec(v_ls::time) - app.time_sec(h.open_time)) % (p_shop.slot_unit_min * 60) = 0) then
    raise exception '영업시간 밖이거나 예약 단위에 맞지 않는 시각입니다' using errcode = '23514';
  end if;
  if not exists (
    select 1 from public.staff_work_hours w
    where w.staff_id = p_staff_id and w.day_of_week = v_dow
      and w.start_time <= v_ls::time and v_le::time <= w.end_time) then
    raise exception '담당자 근무시간 밖입니다' using errcode = '23514';
  end if;
end $$;

-- 현재 메뉴·담당·업체 정책으로 스냅샷 계산 + 활성 여부 검증
create or replace function app.fill_reservation_snapshot(r inout public.reservations, p_shop public.shops)
language plpgsql set search_path = '' as $$
declare
  v_svc   public.services;
  v_staff public.staff;
  v_ss    public.staff_services;
begin
  select * into v_svc   from public.services where id = r.service_id and shop_id = r.shop_id;
  perform app.lock_staff(array[r.staff_id]);
  select * into v_staff from public.staff where id = r.staff_id and shop_id = r.shop_id;
  select * into v_ss    from public.staff_services
    where staff_id = r.staff_id and service_id = r.service_id and shop_id = r.shop_id;

  if v_svc.id is null or not v_svc.is_active then
    raise exception '판매 중인 메뉴가 아닙니다' using errcode = '23514'; end if;
  if v_staff.id is null or not v_staff.is_active then
    raise exception '예약 가능한 담당자가 아닙니다' using errcode = '23514'; end if;
  if v_ss.staff_id is null or not v_ss.is_active then
    raise exception '담당자가 제공하지 않는 메뉴입니다' using errcode = '23514'; end if;

  r.duration_min      := coalesce(v_ss.duration_override_min, v_svc.duration_min);
  r.buffer_min        := v_svc.buffer_min;
  r.price             := coalesce(v_ss.price_override, v_svc.price);
  r.service_end_at    := r.start_at + r.duration_min * interval '1 minute';
  r.end_at            := r.service_end_at + r.buffer_min * interval '1 minute';
  r.late_cancel_hours := p_shop.late_cancel_hours;
  r.shop_name         := p_shop.name;
  r.staff_name        := v_staff.name;
  r.service_name      := v_svc.name;
end $$;

create or replace function app.reservations_before_write()
returns trigger language plpgsql set search_path = '' as $$
declare
  v_shop   public.shops;
  v_me     uuid    := app.current_profile_id();
  v_system boolean := app.is_system();
  v_owner  boolean;
  v_sched  boolean;
begin
  -- 공통 잠금 순서: 업체 행 → 담당자 행
  v_shop := app.lock_shop(coalesce(new.shop_id, old.shop_id));
  if v_shop.id is null then
    raise exception '업체가 존재하지 않습니다' using errcode = '23503';
  end if;
  v_owner := (v_me is not null and v_shop.owner_id = v_me);

  if not v_system and v_me is null then
    raise exception '인증된 활성 회원만 예약을 처리할 수 있습니다' using errcode = '42501';
  end if;

  -- ---------------- INSERT: 신규 예약 ----------------
  if tg_op = 'INSERT' then
    if not v_system and new.user_id is distinct from v_me then
      raise exception '본인 명의로만 예약할 수 있습니다' using errcode = '42501';
    end if;
    if not exists (select 1 from public.profiles where id = new.user_id and status = 'ACTIVE') then
      raise exception '활성 회원이 아닙니다' using errcode = '23514';
    end if;
    if v_shop.status <> 'ACTIVE' then
      raise exception '예약을 받지 않는 업체입니다' using errcode = '23514';
    end if;

    new := app.fill_reservation_snapshot(new, v_shop);
    if new.start_at < now() + v_shop.min_lead_time_min * interval '1 minute' then
      raise exception '예약 가능한 최소 사전 시간이 지났습니다' using errcode = '23514';
    end if;
    perform app.check_reservation_slot(v_shop, new.staff_id, new.start_at, new.end_at);

    new.created_at   := now();
    new.updated_at   := now();
    new.requested_at := now();
    new.version      := 1;
    new.reject_reason := null;
    new.canceled_by := null; new.canceled_at := null; new.cancel_reason := null;
    new.is_late_cancel := false;
    if v_shop.auto_confirm then
      new.status := 'CONFIRMED';
      new.approval_expires_at := null;
    else
      new.status := 'PENDING';
      new.approval_expires_at := least(now() + v_shop.approval_timeout_min * interval '1 minute', new.start_at);
    end if;
    return new;
  end if;

  -- ---------------- UPDATE ----------------
  if new.id <> old.id or new.user_id <> old.user_id or new.shop_id <> old.shop_id
     or new.created_at <> old.created_at or new.source <> old.source
     or new.external_ref is distinct from old.external_ref then
    raise exception '변경할 수 없는 예약 필드입니다' using errcode = '42501';
  end if;
  if old.status in ('REJECTED','CANCELED','COMPLETED','NO_SHOW','EXPIRED') then
    raise exception '종료된 예약은 변경할 수 없습니다 (%)', old.status using errcode = '23514';
  end if;

  v_sched := new.start_at   is distinct from old.start_at
          or new.staff_id   is distinct from old.staff_id
          or new.service_id is distinct from old.service_id;

  if v_sched then
    -- 일정 변경: 고객 본인(또는 시스템)만, 시작 전, 상태는 DB가 결정
    if not v_system and v_me <> old.user_id then
      raise exception '일정 변경은 예약 고객만 할 수 있습니다' using errcode = '42501';
    end if;
    if new.status <> old.status then
      raise exception '일정 변경과 상태 변경은 함께 요청할 수 없습니다' using errcode = '23514';
    end if;
    if now() >= old.start_at then
      raise exception '시작된 예약은 변경할 수 없습니다' using errcode = '23514';
    end if;
    if v_shop.status <> 'ACTIVE' then
      raise exception '예약을 받지 않는 업체입니다' using errcode = '23514';
    end if;
    if new.staff_id <> old.staff_id then  -- 기존 담당자도 ID 순서로 잠금
      perform app.lock_staff(array[old.staff_id, new.staff_id]);
    end if;
    new := app.fill_reservation_snapshot(new, v_shop);
    if new.start_at < now() + v_shop.min_lead_time_min * interval '1 minute' then
      raise exception '예약 가능한 최소 사전 시간이 지났습니다' using errcode = '23514';
    end if;
    perform app.check_reservation_slot(v_shop, new.staff_id, new.start_at, new.end_at);
    new.requested_at := now();
    if v_shop.auto_confirm then
      new.status := 'CONFIRMED';
      new.approval_expires_at := null;
    else
      new.status := 'PENDING';
      new.approval_expires_at := least(now() + v_shop.approval_timeout_min * interval '1 minute', new.start_at);
    end if;
  else
    -- 일정 외 스냅샷은 수정 불가
    if (new.price, new.duration_min, new.buffer_min, new.late_cancel_hours, new.service_end_at,
        new.end_at, new.shop_name, new.staff_name, new.service_name, new.requested_at)
       is distinct from
       (old.price, old.duration_min, old.buffer_min, old.late_cancel_hours, old.service_end_at,
        old.end_at, old.shop_name, old.staff_name, old.service_name, old.requested_at)
       or (new.approval_expires_at is distinct from old.approval_expires_at) then
      raise exception '예약 스냅샷은 직접 수정할 수 없습니다' using errcode = '42501';
    end if;

    if new.status <> old.status then
      case
        when old.status = 'PENDING' and new.status = 'CONFIRMED' then
          if not (v_owner or v_system) then raise exception '업체만 승인할 수 있습니다' using errcode = '42501'; end if;
          if now() >= old.approval_expires_at or now() >= old.start_at then
            raise exception '승인 가능 시간이 지났습니다' using errcode = '23514'; end if;

        when old.status = 'PENDING' and new.status = 'REJECTED' then
          if not (v_owner or v_system) then raise exception '업체만 거절할 수 있습니다' using errcode = '42501'; end if;

        when old.status = 'PENDING' and new.status = 'EXPIRED' then
          if now() < old.approval_expires_at then
            raise exception '아직 승인 마감 전입니다' using errcode = '23514'; end if;

        when old.status in ('PENDING','CONFIRMED') and new.status = 'CANCELED' then
          if new.canceled_by = 'USER' and not (v_me = old.user_id or v_system) then
            raise exception '고객 취소는 예약 고객만 할 수 있습니다' using errcode = '42501';
          elsif new.canceled_by = 'SHOP' and not (v_owner or v_system) then
            raise exception '업체 취소는 업체만 할 수 있습니다' using errcode = '42501';
          elsif new.canceled_by = 'SYSTEM' and not v_system then
            raise exception '시스템 취소 권한이 없습니다' using errcode = '42501';
          end if;
          -- 시작 후 예외 취소는 플랫폼 관리자/시스템만
          if now() >= old.start_at and not (v_system or app.is_admin()) then
            raise exception '시작된 예약은 취소할 수 없습니다' using errcode = '23514';
          end if;
          new.canceled_at    := now();
          new.is_late_cancel := (new.canceled_by = 'USER'
                                 and old.start_at - now() < old.late_cancel_hours * interval '1 hour');

        when old.status = 'CONFIRMED' and new.status = 'COMPLETED' then
          if not (v_owner or v_system) then raise exception '업체만 완료 처리할 수 있습니다' using errcode = '42501'; end if;
          if now() < old.service_end_at then
            raise exception '서비스 종료 후에 완료 처리할 수 있습니다' using errcode = '23514'; end if;

        when old.status = 'CONFIRMED' and new.status = 'NO_SHOW' then
          if not (v_owner or v_system) then raise exception '업체만 노쇼 처리할 수 있습니다' using errcode = '42501'; end if;
          if now() < old.start_at then
            raise exception '예약 시작 후에 노쇼 처리할 수 있습니다' using errcode = '23514'; end if;

        else
          raise exception '허용되지 않는 상태 전이입니다: % → %', old.status, new.status using errcode = '23514';
      end case;
    end if;
  end if;

  new.version := old.version + 1;
  return new;
end $$;

create trigger reservations_before_write
  before insert or update on public.reservations
  for each row execute function app.reservations_before_write();

-- 영업시간·근무시간·휴무 변경 시 기존 활성 예약 충돌 검사 (업체 잠금 후)
create or replace function app.schedule_change_guard()
returns trigger language plpgsql set search_path = '' as $$
declare
  v_shop_id uuid;
  v_bad     uuid;
begin
  if tg_table_name = 'staff_work_hours' then
    v_shop_id := app.staff_shop_id(coalesce(new.staff_id, old.staff_id));
  else
    v_shop_id := coalesce(new.shop_id, old.shop_id);
  end if;
  perform app.lock_shop(v_shop_id);

  if tg_table_name = 'shop_closures' then
    if tg_op = 'DELETE' then return old; end if;
    select r.id into v_bad from public.reservations r
      where r.shop_id = new.shop_id and r.status in ('PENDING','CONFIRMED')
        and (r.start_at at time zone 'Asia/Seoul')::date = new.closed_date
      limit 1;
    if v_bad is not null then
      raise exception '해당 날짜에 진행 중인 예약이 있어 휴무를 등록할 수 없습니다' using errcode = '23P01';
    end if;
    return new;
  end if;

  -- 영업/근무시간: 변경 결과 기준으로 모든 활성 미래 예약이 여전히 들어가는지 확인
  -- (AFTER 트리거에서 실행되므로 새 데이터가 반영된 상태로 검사)
  select r.id into v_bad
  from public.reservations r
  where r.shop_id = v_shop_id and r.status in ('PENDING','CONFIRMED') and r.end_at > now()
    and (
      not exists (select 1 from public.shop_business_hours h
        where h.shop_id = r.shop_id
          and h.day_of_week = extract(dow from r.start_at at time zone 'Asia/Seoul')
          and h.open_time <= (r.start_at at time zone 'Asia/Seoul')::time
          and (r.end_at at time zone 'Asia/Seoul')::time <= h.close_time)
      or not exists (select 1 from public.staff_work_hours w
        where w.staff_id = r.staff_id
          and w.day_of_week = extract(dow from r.start_at at time zone 'Asia/Seoul')
          and w.start_time <= (r.start_at at time zone 'Asia/Seoul')::time
          and (r.end_at at time zone 'Asia/Seoul')::time <= w.end_time))
  limit 1;
  if v_bad is not null then
    raise exception '기존 예약(%)과 충돌하여 시간을 변경할 수 없습니다', v_bad using errcode = '23P01';
  end if;
  return null;
end $$;

create trigger shop_closures_guard before insert or update on public.shop_closures
  for each row execute function app.schedule_change_guard();
create trigger sbh_guard after update or delete on public.shop_business_hours
  for each row execute function app.schedule_change_guard();
create trigger swh_guard after update or delete on public.staff_work_hours
  for each row execute function app.schedule_change_guard();

-- =====================================================================
-- 12. 승인 만료 작업 (app_worker 전용, 1분 간격 실행 권장)
-- =====================================================================
create or replace function app.expire_pending_reservations(p_limit integer default 200)
returns integer language plpgsql set search_path = '' as $$
declare
  r       public.reservations;
  v_new   public.reservations;
  v_count integer := 0;
begin
  if not app.is_system() then
    raise exception 'app_worker 전용 함수입니다' using errcode = '42501';
  end if;
  for r in
    select * from public.reservations
    where status = 'PENDING' and approval_expires_at <= now()
    order by approval_expires_at
    limit p_limit
    for update skip locked
  loop
    update public.reservations set status = 'EXPIRED'
      where id = r.id and status = 'PENDING'
      returning * into v_new;
    if v_new.id is not null then
      insert into public.reservation_logs
        (reservation_id, action, before_snapshot, after_snapshot, actor_type,
         idempotency_key, request_hash, reason)
      values
        (r.id, 'EXPIRE', app.reservation_snapshot(r), app.reservation_snapshot(v_new), 'SYSTEM',
         md5('EXPIRE:' || r.id || ':' || r.version)::uuid,
         encode(extensions.digest('EXPIRE:' || r.id || ':' || r.version, 'sha256'), 'hex'),
         '승인 대기 시간 만료');
      v_count := v_count + 1;
    end if;
  end loop;
  return v_count;
end $$;

-- =====================================================================
-- 13. 권한 (GRANT) · RLS
--   - 클라이언트(anon/authenticated)의 Data API 직접 접근 차단
--   - app_backend: RLS 적용, 요청 회원 문맥으로 제한
--   - app_worker : 서버 내부 작업, 전체 허용 정책
-- =====================================================================
do $$
declare t text;
begin
  foreach t in array array['profiles','regions','categories','service_types','shops',
    'shop_business_hours','shop_closures','services','staff','staff_services',
    'staff_work_hours','reservations','reservation_logs']
  loop
    execute format('alter table public.%I enable row level security', t);
    execute format('revoke all on public.%I from public', t);
    if exists (select 1 from pg_roles where rolname = 'anon') then
      execute format('revoke all on public.%I from anon', t);
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
      execute format('revoke all on public.%I from authenticated', t);
    end if;
    execute format('grant select, insert, update on public.%I to app_worker', t);
    execute format('create policy worker_all on public.%I for all to app_worker using (true) with check (true)', t);
  end loop;
end $$;

revoke all on all functions in schema app from public;
grant execute on all functions in schema app to app_backend, app_worker;
revoke execute on function app.expire_pending_reservations(integer) from app_backend;
revoke execute on function app.handle_new_auth_user() from app_backend, app_worker;

-- profiles: 본인 조회, 이름·연락처만 수정. 역할·상태 변경은 app_worker 경로
grant select on public.profiles to app_backend;
grant update (name, phone) on public.profiles to app_backend;
create policy profiles_select on public.profiles for select to app_backend
  using (id = app.current_profile_id() or app.is_admin());
create policy profiles_update on public.profiles for update to app_backend
  using (id = app.current_profile_id()) with check (id = app.current_profile_id());

-- 기본 분류: 조회만
grant select on public.regions, public.categories, public.service_types to app_backend;
create policy regions_select       on public.regions       for select to app_backend using (true);
create policy categories_select    on public.categories    for select to app_backend using (true);
create policy service_types_select on public.service_types for select to app_backend using (true);

-- shops
grant select, insert on public.shops to app_backend;
grant update (category_id, region_id, name, address, latitude, longitude, phone, description,
  slot_unit_min, min_lead_time_min, late_cancel_hours, approval_timeout_min, auto_confirm, status)
  on public.shops to app_backend;
create policy shops_select on public.shops for select to app_backend
  using (status = 'ACTIVE' or owner_id = app.current_profile_id() or app.is_admin());
create policy shops_insert on public.shops for insert to app_backend
  with check (owner_id = app.current_profile_id() and source = 'INTERNAL');
create policy shops_update on public.shops for update to app_backend
  using (owner_id = app.current_profile_id()) with check (owner_id = app.current_profile_id());

-- 업체 하위 데이터: 공개 업체는 조회, 쓰기는 소유자만 (삭제는 영업시간·휴무만 허용)
grant select, insert, update, delete on public.shop_business_hours, public.shop_closures to app_backend;
grant select, insert, update on public.services, public.staff, public.staff_services to app_backend;
grant select, insert, update, delete on public.staff_work_hours to app_backend;

create policy sbh_select on public.shop_business_hours for select to app_backend using (app.can_view_shop(shop_id));
create policy sbh_write  on public.shop_business_hours for all    to app_backend
  using (app.owns_shop(shop_id)) with check (app.owns_shop(shop_id));
create policy closures_select on public.shop_closures for select to app_backend using (app.can_view_shop(shop_id));
create policy closures_write  on public.shop_closures for all    to app_backend
  using (app.owns_shop(shop_id)) with check (app.owns_shop(shop_id));
create policy services_select on public.services for select to app_backend
  using ((is_active and app.can_view_shop(shop_id)) or app.owns_shop(shop_id));
create policy services_write  on public.services for all to app_backend
  using (app.owns_shop(shop_id)) with check (app.owns_shop(shop_id));
create policy staff_select on public.staff for select to app_backend
  using ((is_active and app.can_view_shop(shop_id)) or app.owns_shop(shop_id));
create policy staff_write  on public.staff for all to app_backend
  using (app.owns_shop(shop_id)) with check (app.owns_shop(shop_id));
create policy staff_services_select on public.staff_services for select to app_backend
  using ((is_active and app.can_view_shop(shop_id)) or app.owns_shop(shop_id));
create policy staff_services_write  on public.staff_services for all to app_backend
  using (app.owns_shop(shop_id)) with check (app.owns_shop(shop_id));
create policy swh_select on public.staff_work_hours for select to app_backend
  using (app.can_view_shop(app.staff_shop_id(staff_id)));
create policy swh_write  on public.staff_work_hours for all to app_backend
  using (app.owns_shop(app.staff_shop_id(staff_id))) with check (app.owns_shop(app.staff_shop_id(staff_id)));

-- reservations: 고객 본인 / 업체 소유자 / 관리자 조회. 전이 규칙은 트리거가 검증
grant select, insert on public.reservations to app_backend;
grant update (staff_id, service_id, start_at, status, reject_reason, canceled_by, cancel_reason, customer_memo)
  on public.reservations to app_backend;
create policy reservations_select on public.reservations for select to app_backend
  using (user_id = app.current_profile_id() or app.owns_shop(shop_id) or app.is_admin());
create policy reservations_insert on public.reservations for insert to app_backend
  with check (user_id = app.current_profile_id() and source = 'INTERNAL');
create policy reservations_update on public.reservations for update to app_backend
  using (user_id = app.current_profile_id() or app.owns_shop(shop_id) or app.is_admin())
  with check (user_id = app.current_profile_id() or app.owns_shop(shop_id) or app.is_admin());

-- reservation_logs: 조회 + 추가만 (수정·삭제 불가)
grant select, insert on public.reservation_logs to app_backend;
create policy rlogs_select on public.reservation_logs for select to app_backend
  using (exists (select 1 from public.reservations r where r.id = reservation_id));
create policy rlogs_insert on public.reservation_logs for insert to app_backend
  with check (actor_user_id = app.current_profile_id()
              and exists (select 1 from public.reservations r where r.id = reservation_id));
