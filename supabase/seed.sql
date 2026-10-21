-- 기본 분류 시드 데이터 (지역 · 업종 · 공통 서비스 종류)
-- 여러 번 실행해도 중복되지 않습니다.

insert into public.regions (sido, sigungu) values
  ('서울특별시', '강남구'), ('서울특별시', '마포구'), ('서울특별시', '송파구'),
  ('서울특별시', '종로구'), ('서울특별시', '관악구'),
  ('세종특별자치시', '세종시'),
  ('대전광역시', '유성구'), ('대전광역시', '서구'),
  ('충청남도', '공주시'), ('충청남도', '천안시'), ('충청남도', '아산시')
on conflict (sido, sigungu) do nothing;

insert into public.categories (code, name) values
  ('HAIR', '미용실'),
  ('NAIL', '네일숍')
on conflict (code) do nothing;

insert into public.service_types (category_id, code, name)
select c.id, v.code, v.name
from (values
  ('HAIR', 'HAIR_CUT',    '커트'),
  ('HAIR', 'HAIR_PERM',   '펌'),
  ('HAIR', 'HAIR_COLOR',  '염색'),
  ('HAIR', 'HAIR_CLINIC', '클리닉'),
  ('NAIL', 'NAIL_GEL',    '젤네일'),
  ('NAIL', 'NAIL_CARE',   '케어'),
  ('NAIL', 'NAIL_REMOVE', '제거')
) as v(cat, code, name)
join public.categories c on c.code = v.cat
on conflict (code) do nothing;
