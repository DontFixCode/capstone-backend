-- 1차 MVP 스키마 (5/10): 메뉴 · 담당자 · 근무시간

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
