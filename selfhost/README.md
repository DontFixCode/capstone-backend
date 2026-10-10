# Supabase 셀프호스팅 (팀 서버)

[공식 가이드: Self-Hosting with Docker](https://supabase.com/docs/guides/self-hosting/docker)의 절차대로 팀 서버에 Supabase 전체(DB + 로그인 + Studio)를 설치합니다.
공식 Docker 파일은 레포에 복사하지 않고 `setup.sh`가 설치 시점에 공식 저장소(`self-hosted/v0.8.2`)에서 받아옵니다.

| 파일 | 역할 |
| --- | --- |
| `setup.sh` | 공식 가이드 2~5단계 자동 실행 + 캡스톤 스키마 적용 (최초 1회) |
| `migrate.sh` | 새 마이그레이션만 골라서 적용 (스키마 변경 시마다) |
| `create-api-user.sh` | FastAPI용 DB 계정 `api_server` 생성 + 접속 주소 출력 |
| `docker-compose.capstone.yml` | 서버 포트 설정 (DB는 서버 내부 5000번, 풀러 외부 포트 닫음) |

## 설치 (서버에서, 최초 1회)

```bash
cd ~/capstone-backend
git pull
bash selfhost/setup.sh
```
- 처음엔 이미지 다운로드로 5~10분 걸립니다.
- 끝나면 Studio 주소와 아이디/비밀번호가 출력됩니다. 비밀번호는 `~/supabase-project/.env`의 `DASHBOARD_PASSWORD`에도 있습니다.
- 예전에 띄운 단독 PostgreSQL(`capstone-db`)은 자동으로 중지됩니다. 데이터 볼륨은 남습니다.
- 포트가 이미 사용 중이면 `GW_PORT=8080 bash selfhost/setup.sh` 처럼 바꿔 실행합니다.

## 접속

| 대상 | 주소 | 비고 |
| --- | --- | --- |
| Studio (관리 화면) | `http://<서버IP>:8000` | 아이디 `capstone`, 비밀번호는 `.env`의 `DASHBOARD_PASSWORD` |
| 프론트 로그인용 API | `http://<서버IP>:8000` + 공개 키 | `cd ~/supabase-project && sh run.sh secrets` → `SUPABASE_PUBLISHABLE_KEY` |
| DB (백엔드) | `127.0.0.1:5000` | 서버 내부 전용. 외부에서는 SSH 터널 |

```bash
# 내 컴퓨터에서 DB 터널 (맥은 5000번을 AirPlay가 쓰므로 15000으로)
ssh -L 15000:127.0.0.1:5000 <계정>@<서버IP> -p 65522
```

## 자주 쓰는 명령 (서버, `~/supabase-project` 에서)

```bash
sh run.sh status            # 서비스 상태 (모두 healthy인지)
sh run.sh logs auth         # 특정 서비스 로그
sh run.sh stop / start      # 중지 / 시작
sh run.sh secrets           # 비밀번호·API 키 보기
```
스키마 변경 반영: `cd ~/capstone-backend && git pull && bash selfhost/migrate.sh`

## 보안 주의
- `~/supabase-project/.env`에는 모든 비밀키가 있습니다. 절대 GitHub·단톡방에 올리지 않습니다.
- `SUPABASE_SECRET_KEY`는 서버(FastAPI)에서만 사용하고, 프론트에는 `SUPABASE_PUBLISHABLE_KEY`만 줍니다.
- Studio는 8000번에서 비밀번호로 보호되지만 HTTP입니다. 실제 서비스 전에는 HTTPS 리버스 프록시를 붙입니다 ([공식 문서](https://supabase.com/docs/guides/self-hosting/self-hosted-proxy-https)).
- `sh reset.sh`는 DB를 포함한 모든 데이터를 삭제합니다.
