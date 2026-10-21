-- 1차 MVP 스키마 (3/10): 지역 · 업종 · 서비스 종류

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
