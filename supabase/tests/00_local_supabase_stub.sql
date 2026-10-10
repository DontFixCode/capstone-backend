-- 로컬 순수 PostgreSQL에서 테스트할 때만 사용하는 Supabase 흉내 객체.
-- 실제 Supabase 프로젝트에는 실행하지 마세요 (auth 스키마는 Supabase가 관리합니다).
create schema if not exists auth;
create table if not exists auth.users (
  id uuid primary key default gen_random_uuid(),
  email text,
  raw_user_meta_data jsonb default '{}'::jsonb
);
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'anon') then create role anon nologin; end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then create role authenticated nologin; end if;
end $$;
