-- 1차 MVP 스키마 (4/10): 업체 · 영업시간 · 휴무

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
