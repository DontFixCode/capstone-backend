# 서버 배포 가이드

> **현재 팀 방식: 팀 서버에 Supabase 셀프호스팅** → [`selfhost/README.md`](selfhost/README.md)
> 서버에서 `bash selfhost/setup.sh` 한 번이면 DB·로그인(Auth)·Studio가 함께 설치됩니다.
> 아래 내용은 Supabase 없이 PostgreSQL만 띄우던 이전 방식(B)과 공통 운영 팁입니다.

## 먼저 정할 것: DB를 어디서 돌릴까?

| 방식 | 설명 | 추천 상황 |
| --- | --- | --- |
| **A. Supabase** | DB는 이미 Supabase 클라우드 서버에서 돌아감. 서버에는 FastAPI만 배포 | 팀 설계 문서대로 Supabase Auth를 쓸 때 (권장) |
| **B. 자체 서버 + Docker** | 이 레포의 `docker-compose.yml`로 서버에 PostgreSQL을 직접 띄움 | Supabase 없이 서버 하나에서 다 돌려야 할 때, 팀 개발·시연용 공용 DB |

> B 방식에는 Supabase Auth가 없습니다. `auth.users`는 최소 구조로만 만들어지므로 회원가입·로그인은 FastAPI가 직접 처리하거나 Supabase Auth와 별도로 연결해야 합니다.

A 방식이면 `supabase/README.md`대로 SQL을 한 번 적용하면 끝입니다. 아래는 **B 방식** 절차입니다.

---

## 1. 서버 준비 (Ubuntu 22.04/24.04 기준, 최초 1회)

```bash
# 내 PC에서 서버 접속
ssh ubuntu@<서버 IP>

# Docker 설치
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
exit                      # 그룹 적용을 위해 재접속
ssh ubuntu@<서버 IP>
docker run --rm hello-world   # 동작 확인
```

## 2. 코드 받기

```bash
cd ~
git clone https://github.com/DontFixCode/capstone-backend.git
cd capstone-backend
```
비공개 레포라면 비밀번호 대신 GitHub Personal Access Token을 입력하거나, 서버에서 `ssh-keygen`으로 만든 공개키를 레포 **Settings → Deploy keys**에 등록한 뒤 `git@github.com:DontFixCode/capstone-backend.git`으로 clone 합니다.

## 3. 환경변수 설정

```bash
cp .env.example .env
nano .env        # POSTGRES_PASSWORD를 긴 랜덤 값으로 변경
# 랜덤 비밀번호 만들기: openssl rand -base64 24
```

## 4. 실행

```bash
docker compose up -d          # 백그라운드 실행. 처음 실행 시 마이그레이션·시드 자동 적용
docker compose logs -f db     # "[migrate] 완료"가 보이면 성공 (Ctrl+C로 로그 종료)
docker compose ps             # STATUS가 healthy인지 확인

# 테이블 확인
docker compose exec db psql -U postgres -d capstone -c '\dt public.*'
```

`restart: unless-stopped` 설정이라 서버가 재부팅돼도 자동으로 다시 켜지고, 데이터는 `db-data` 볼륨에 남습니다. SSH 창을 닫아도 계속 실행됩니다.

## 5. 코드 업데이트 (새 마이그레이션 추가 후)

```bash
cd ~/capstone-backend
git pull
docker compose up -d
docker compose exec db bash /docker/migrate.sh   # 아직 적용 안 된 마이그레이션만 적용
```
⚠️ 이미 적용된 마이그레이션 파일은 수정하지 말고, 변경 사항은 **새 파일**(예: `20261020000000_add_xxx.sql`)로 추가하세요.

GitHub Actions 자동 배포(`.github/workflows/deploy.yml`)를 켜면 main에 push할 때마다 위 과정이 자동으로 실행됩니다. 레포 Settings → Secrets에 `SERVER_HOST`, `SERVER_USER`, `SERVER_SSH_KEY`를 등록하세요.

## 6. 팀원이 DB에 접속하는 방법

DB 포트(5432)는 기본으로 **서버 내부에서만** 열려 있습니다. 인터넷에 DB 포트를 열지 말고 SSH 터널로 접속하세요.

```bash
# 내 PC에서 (터널을 연 상태로 유지)
ssh -L 5432:127.0.0.1:5432 ubuntu@<서버 IP>
# 다른 터미널이나 DBeaver/DataGrip에서
#   host=localhost port=5432 db=capstone user=postgres password=<.env 값>
```

FastAPI를 같은 서버의 compose에 넣으면 `db:5432`로 바로 접속하므로 포트를 열 필요가 없습니다 (`docker-compose.yml`의 `api` 주석 참고). 외부에는 FastAPI 포트(8000 또는 80/443)만 클라우드 보안그룹/방화벽에서 열면 됩니다.

## 7. 백업

```bash
# 백업
docker compose exec -T db pg_dump -U postgres -d capstone -Fc > backup_$(date +%F).dump
# 복원
docker compose exec -T db pg_restore -U postgres -d capstone --clean < backup_2026-10-10.dump
```
매일 자동 백업: `crontab -e`에 `0 4 * * * cd ~/capstone-backend && docker compose exec -T db pg_dump -U postgres -d capstone -Fc > ~/backups/db_$(date +\%F).dump`

## 문제 해결

| 증상 | 해결 |
| --- | --- |
| `POSTGRES_PASSWORD를 설정하세요` | `.env` 파일이 없음 → 3단계 |
| 마이그레이션이 다시 안 돌아감 | 초기화 스크립트는 빈 볼륨일 때만 실행됨 → `docker compose exec db bash /docker/migrate.sh` |
| 처음부터 DB를 다시 만들고 싶음 | `docker compose down -v` (**데이터 전부 삭제**) 후 `docker compose up -d` |
| `permission denied ... docker.sock` | 1단계의 `usermod` 후 재접속 안 함 |
| 포트 5432 사용 중 | `.env`의 `DB_PORT`를 5433 등으로 변경 |
