-- =====================================================================
-- 1차 MVP 스키마 (핵심 13개 테이블)
--   profiles, regions, categories, service_types, shops,
--   shop_business_hours, shop_closures, services, staff, staff_services,
--   staff_work_hours, reservations, reservation_logs
-- 기준 문서: Database Structure 보완안 (2026-10-03)
-- 2차 MVP 11개 테이블(staff_time_off, 평판, 채팅, 신고, 알림)은 별도 마이그레이션.
-- =====================================================================

-- 1차 MVP 스키마 (1/10): 확장 · 스키마 · 역할 · 공통 함수

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
