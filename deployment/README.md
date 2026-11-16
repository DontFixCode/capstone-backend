# 서버 배포 설정 인수인계

2026-11-13 실행 중인 버스 서버에서 읽어 온 배포 설정입니다. 이번 동기화에서는 서버 파일 변경, 서비스 재시작, 자동 배포, Git 커밋·푸시를 수행하지 않았습니다. 동기화 전 서버·로컬의 Git 관리 대상 204개 파일 내용을 비교해 모두 일치했고, `utils/generate-keys.sh`의 실행 권한만 서버와 맞췄습니다. 실행 중인 API 컨테이너의 Python 소스 79개도 서버 저장소와 같았으며 API·워커는 같은 이미지를 사용합니다. 이후 배포 파일과 이 문서, 환경변수 예시·Git 제외 규칙을 로컬에 반영했습니다.

## 원본 경로

| 이 저장소의 경로 | 서버에서 대응하는 원본 |
| --- | --- |
| 백엔드 소스·`Dockerfile`·`compose.server.yaml` | `/home/gmlrud/capstone-backend/` |
| `.env.api` | `/home/gmlrud/capstone-backend/.env.api` |
| `.env` | `/home/gmlrud/bus-transfer-supabase/deployment/.env` |
| `deployment/supabase/docker-compose.yml`, `volumes/` | `/home/gmlrud/bus-transfer-supabase/deployment/` 아래 동일 상대 경로 |
| `deployment/public-auth/compose.yaml`, `nginx.conf`, `studio.nginx.conf`, `renew.sh` | `/home/gmlrud/bus-transfer-supabase/deployment/public-auth/` 아래 동일 파일명 |
| `deployment/public-auth/bus-auth-renew.cron` | `/etc/cron.d/bus-auth-renew` |
| `deployment/public-auth/acme-nginx.conf` | `/etc/nginx/sites-available/zz-bus-public-ip.conf` |

루트 `.env`는 **실제로 가동 중인 Supabase 스택의 환경변수**입니다. 서버 백엔드 디렉터리에 있던 비활성·기존 `.env`는 가져오지 않았습니다. 루트 `docker-compose.yml`은 기존 Supabase 템플릿이며, 이번에 보관한 실제 서버 구성은 `deployment/supabase/docker-compose.yml`입니다. 두 구성을 혼동하지 않습니다.

`deployment/supabase/volumes/`에는 Compose에서 참조하는 설정·초기화 SQL·기본 함수 소스만 보관합니다. DB 데이터, DB 백업, 스토리지 데이터, 스니펫은 복사하지 않았습니다. 공개 프록시의 절대 경로는 서버 배포본 그대로 유지했습니다.

## 현재 운영 구성

| 용도 | 주소·구성 |
| --- | --- |
| 앱 API·Supabase Auth | `https://121.153.148.189:18443` — `/api/v1/*`, `/auth/v1/*` |
| 앱의 `SUPABASE_URL` | `https://121.153.148.189:18443` — `/auth/v1`을 붙이지 않음 |
| Auth 토큰 issuer | `https://121.153.148.189:18443/auth/v1` |
| Studio 관리 접속 | `https://121.153.148.189:18444` — 별도 HTTP Basic 관리자 인증 |
| 앱 인증 복귀 | `busro://auth-callback` |
| 백엔드 Compose 프로젝트 | `capstone-backend` — API·워커 |
| Supabase Compose 프로젝트 | `bus-transfer-supabase` — 현재 `db`, `meta`, `studio`, `auth`, `rest`, `api-gw` 가동 |
| 공개 프록시 Compose 프로젝트 | `bus-public-auth` — `proxy`, `studio-proxy` |

Supabase Compose에는 Storage·Realtime 등 추가 서비스 정의도 있지만 현재 가동 중이지 않습니다. 전체 서비스를 새로 실행하는 명령은 현재 운영 상태를 재현하는 명령이 아닙니다. 기존 서버 워커가 운영 DB를 처리하고 있으므로 로컬 워커를 같은 DB에 추가 실행하지 않습니다.

Studio는 기존 서버의 동일한 Studio·DB를 사용합니다. 별도 HTTP Basic 관리 계정은 앱의 Supabase Auth 회원이나 PostgreSQL 사용자와 다르며, 관리 화면에서 DB 수정·관리 키 열람 등의 권한을 가집니다. 이번 인수인계 파일에는 Studio 관리자 자격 증명 파일을 복사하지 않았습니다.

## 환경변수와 별도 운영 데이터

루트 `.env`, `.env.api`는 각각 위 서버 원본과 내용이 같고 로컬 파일 권한은 `600`입니다. 두 파일은 Git 제외 대상이며 담당 개발자에게 비공개로 별도 전달합니다. GitHub에는 실제 환경변수를 포함하지 않고 `.env.example` 계열 예시만 커밋합니다. `deployment/supabase/.env.example`은 공개 URL을 반영한 예시이며 운영 비밀값을 제공하는 파일이 아닙니다.

기존 루트 `.env.example`에는 서버 `.env`와 같은 OpenAI API 키 값이 있어 공유 예제에서 제거했습니다. 기존 Git 이력에는 값이 남아 있으므로 실제 사용 중인 키라면 담당자가 교체해야 합니다. 이번 동기화에서는 서버의 키를 변경하지 않았습니다.

서버 전용 인증서·개인키(`/var/lib/bus-auth/certificates`), 관리자 비밀번호 해시(`/var/lib/bus-auth/admin/studio.htpasswd`), Certbot 작업·로그·ACME 데이터는 복사하지 않았습니다. `public-auth`의 Compose와 갱신 스크립트는 이 운영 경로를 참조하므로 로컬에서 그대로 시작하는 용도가 아닙니다. 인증서 갱신은 서버 Cron이 처리하며, 인증서 변경 시 앱·Studio 프록시를 각각 검사하고 reload합니다.

## 구성만 확인하기

다음 명령은 저장소 루트에서 실행하며 컨테이너를 시작하거나 서버에 배포하지 않습니다. `-q`는 환경변수의 실제 값이 출력되지 않게 합니다.

```sh
SUPABASE_ENV_FILE=./.env docker compose --env-file .env.api -f compose.server.yaml config -q
docker compose --env-file .env -p bus-transfer-supabase -f deployment/supabase/docker-compose.yml config -q
docker compose -f deployment/public-auth/compose.yaml config -q
```

구성 검사 성공은 경로·인증서·Docker 네트워크·DB 접속이 로컬에서 준비됐다는 뜻이 아닙니다. `compose.server.yaml`의 서버 네트워크와 DB 주소, `public-auth`의 절대 경로를 개발 PC 설정으로 자동 변경하지 않았습니다. 서버 환경변수를 인수한 상태에서 서비스나 워커를 자동 실행하지 않습니다.

세 Compose 구성 검사, 갱신 스크립트의 두 프록시 처리·부분 실패 재시도 검사, 셸 구문 검사와 `git diff --check`를 통과했습니다. Git에서 보이는 파일의 비밀값 검사와 실제 환경변수 파일의 제외·권한도 확인했습니다.
