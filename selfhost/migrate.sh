#!/usr/bin/env bash
# 셀프호스팅 Supabase DB에 아직 적용되지 않은 캡스톤 마이그레이션과 시드를 적용합니다.
#   bash selfhost/migrate.sh
# 새 마이그레이션 파일을 추가한 뒤 git pull 하고 다시 실행하면 새 것만 적용됩니다.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="${SUPABASE_DIR:-$HOME/supabase-project}"
[ -f "$TARGET/docker-compose.yml" ] || { echo "[migrate] $TARGET 에 Supabase가 없습니다. 먼저 setup.sh를 실행하세요." >&2; exit 1; }
cd "$TARGET"

# Studio(SQL Editor)와 같은 관리 계정으로 실행
PSQL=(docker compose exec -T db psql -U supabase_admin -d postgres -v ON_ERROR_STOP=1 -q)

"${PSQL[@]}" <<'SQL'
create schema if not exists app_meta;
create table if not exists app_meta.schema_migrations (
  version    text primary key,
  applied_at timestamptz not null default now()
);
SQL

applied=0
for f in $(ls "$REPO_DIR"/supabase/migrations/*.sql | sort); do
  v="$(basename "$f" .sql)"
  done_count="$("${PSQL[@]}" -tA -c "select count(*) from app_meta.schema_migrations where version = '$v'")"
  if [ "$done_count" = "0" ]; then
    echo "[migrate] 적용: $v"
    { echo "begin;"; cat "$f"; echo; echo "insert into app_meta.schema_migrations (version) values ('$v');"; echo "commit;"; } \
      | "${PSQL[@]}"
    applied=$((applied + 1))
  fi
done
echo "[migrate] 새로 적용한 마이그레이션: ${applied}개"

echo "[migrate] 시드 적용 (중복은 건너뜀)"
"${PSQL[@]}" < "$REPO_DIR/supabase/seed.sql"

echo "[migrate] public 테이블 수: $("${PSQL[@]}" -tA -c "select count(*) from information_schema.tables where table_schema = 'public'")"
echo "[migrate] 완료"
