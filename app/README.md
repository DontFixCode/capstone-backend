# FastAPI 실행 골격

Python 3.11 이상을 지원하며 Docker는 Python 3.11을 사용한다. 저장소의 기존 uv 방식을 따른다.

저장소 루트에서 실행한다.

```powershell
uv sync --python 3.11
# 선택 사항: 로그 수준 변경이나 TAGO 인증키 입력이 필요할 때 복사한다. 기존 .env.api는 덮어쓰지 않는다.
if (-not (Test-Path .env.api)) { Copy-Item .env.api.example .env.api }
uv run python -m uvicorn app.main:app --host 127.0.0.1 --port 8001 --reload
```

`GET http://127.0.0.1:8001/health`는 외부 서비스 없이 `{"status":"ok"}`를 반환한다.
API 문서는 `http://127.0.0.1:8001/docs`에서 확인한다. 기존 Supabase의 8000번 포트를 피해 8001번을 사용한다.

```powershell
uv run python -m app.workers.main
```

워커는 실제 작업이 미구현임을 로그로 알리고 정상 종료한다.

```powershell
docker compose -f compose.api.yaml config --quiet
docker compose -f compose.api.yaml up --build -d api
docker compose -f compose.api.yaml --profile worker run --build --rm worker
docker compose -f compose.api.yaml down
```

API Compose 프로젝트는 `capstone-backend`이고 기본 서비스는 API뿐이다. 워커는 `worker` profile에 있으며 자동 재시작하지 않는다.
기존 Supabase Compose와 서비스·네트워크를 공유하거나 변경하지 않는다. DB 초기화와 마이그레이션은 실행하지 않는다.

앱 설정은 실행 디렉터리와 관계없이 저장소 루트의 `.env.api`를 읽는다. 환경 변수 `BACKEND_LOG_LEVEL`이 파일보다 우선하며 기본값은 `INFO`다.
지원 값은 `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`이다. 이 설정은 애플리케이션 로그에 적용된다.
TAGO 인증키는 실제 TAGO 호출에만 필요하다(아래 Phase 1 참고). 그 밖의 외부 서비스 설정 예시는 주석으로만 제공한다.
기존 Supabase의 `.env`와 `.env.example`은 유지하며 앱 설정으로 읽지 않는다.

`/db-test`는 Phase 2의 `app/db/session.py`로 현재 대상 DB(아래 "DB 접속")에 연결해 `SELECT 1`을 실행한다.
실제 DB 연결은 해당 엔드포인트를 호출할 때만 수행하며, 설정·연결 실패는 503으로 반환한다.
Docker 이미지에는 로컬 환경 파일을 포함하지 않으므로 `/health`는 DB 자격증명 없이 실행된다.
신규 실행 골격에 추가한 의존성은 `pydantic-settings`이고, Phase 1 TAGO 호출을 위해 `httpx`를 추가했다.

## 서버 배포 (DB가 있는 서버)

`compose.server.yaml`은 API·워커를 DB와 같은 서버에서 띄운다. 두 서비스가 Supabase 스택의 Docker 네트워크에 붙어 DB(`db:5432`)에
직접 접속하므로 SSH 터널이 필요 없고, 둘 다 `restart: unless-stopped`다. 이미지(Python 3.13)에는 코드와 `data/service_area`만 들어가고
비밀값은 넣지 않는다.

```bash
# 서버에서 (저장소를 ~/capstone-backend에 둔 경우)
docker network ls                      # Supabase 네트워크 이름 확인 (기본값 bus-transfer-supabase_default)
cp <로컬>/.env.api ~/capstone-backend/  # 실행 설정·TAGO·대전시·TMAP 키 (커밋하지 않음)
SUPABASE_NETWORK=<네트워크 이름> SUPABASE_ENV_FILE=~/bus-transfer-supabase/deployment/.env \
  docker compose -f compose.server.yaml up -d --build
curl http://127.0.0.1:8001/health      # 서버 안에서 확인
docker compose -f compose.server.yaml logs -f worker
```

- `.env.api`의 로컬 값 중 `BACKEND_DB_HOST`·`BACKEND_DB_PORT`(터널 주소)·`BACKEND_ENV`·`ENABLE_DEVTOOLS`는 compose가 덮어쓴다
  (DB `db:5432`, `production`, devtools 끔). `BACKEND_AUTH_JWT_ISSUER`는 서버 GoTrue 발급자(`https://121.153.148.189:18443/auth/v1`)와 같아야 한다.
- DB 이름·비밀번호·JWT 비밀값은 `SUPABASE_ENV_FILE`(Supabase 스택 `.env`)을 컨테이너 `/app/.env`에 읽기 전용으로 연결해 읽는다.
- API 공개 주소는 `API_PUBLISH`(기본 `0.0.0.0:8001`). 서버 방화벽·공유기에서 그 포트를 열어야 외부(프론트 개발자)에서 접근한다.
  HTTPS·도메인은 앞단 리버스 프록시에서 붙인다. 모바일 앱은 HTTP를 기본 차단하므로(Android cleartext, iOS ATS) HTTP로 쓸 때는 개발 빌드에서 예외를 둔다.
- 앱의 회원가입·로그인은 Supabase Auth(`/auth/v1`)에 직접 접속한다. 내부 게이트웨이 `127.0.0.1:18080` 앞에 버스 전용 HTTPS 프록시를 두어
  앱 API·Auth를 `https://121.153.148.189:18443`으로 공개했다. 앱의 Supabase URL에는 `/auth/v1`을 붙이지 않는다. Studio 관리 접속은 별도 Basic 인증을 사용하는 `https://121.153.148.189:18444`다.
  앱에는 Supabase 주소와 anon key만 주고 JWT 비밀값·service_role key는 주지 않는다. 실제 배포본과 비공개 환경변수 인수인계는 [서버 배포 설정](../deployment/README.md)을 참고한다.
- 워커는 서버에 하나만 둔다. 로컬 PC 워커를 같은 DB에 동시에 띄우지 않는다. 서버 워커는 정적 정보 하루 1회 갱신도 수행한다.
- 코드 갱신: `git pull` 뒤 같은 `up -d --build`. 중지: `docker compose -f compose.server.yaml down`(DB·Supabase 스택에는 영향 없음).

## 구성과 책임

- `core/`: 실행 설정·로그, 추후 보안·공통 예외 처리.
- `db/`: 추후 DB 연결·모델 기반.
- `modules/users/`: 회원정보·탈퇴·사용자 접근 권한.
- `modules/transit/`: 정류장·노선·방향·경유 순서, 정보 정규화·외부 식별자·최신성.
- `modules/journeys/`: 이동 세션·탑승 식별·사용자 확인·상태 전환·이용 결과와 기록.
- `modules/routing/`: 장소 검색 연동·직행 및 최대 1회 환승 후보 생성·실행 불가능 후보 제외.
- `modules/evaluation/`: 예상 도착·대기시간·환승 여유·성공 확률.
- `modules/recommendation/`: 후보 비교·추천 유지 및 변경·추천 이유.
- `workflows/`: 최초 탐색 → 평가 → 추천, 이동 상태 반영 → 필요 시 재탐색 → 재평가 → 추천 갱신.
- `integrations/`: 외부 API 호출·응답 해석.
- `devtools/`: Phase 1 개발용 TAGO 확인 URL(`/dev/transit/...`). 개발 환경에서 명시적으로 켠 경우에만 등록한다.
- `realtime/`: 갱신 결과와 알림 전달.
- `workers/`: 주기 작업 실행 시점과 대상 관리. 업무 규칙은 도메인·워크플로에서 공유할 예정.
- 루트 `migrations/`, `scripts/import_transit.py`, `tests/unit/`, `tests/integration/`, `tests/fixtures/`: 후속 개발 위치.
- 루트 `scripts/inspect_transit.py`(+ `inspect_transit_report.py`): Phase 1 TAGO 응답 확인 CLI.

도메인 파일은 한국어 책임 설명만 포함하며 API 라우터는 빈 상태로 등록했다.
새 기능·WebSocket·인증·DB 모델·알고리즘·수집 작업은 구현하지 않았다.

참고한 현재 문서:
[Introduction](https://www.notion.so/3f6eeac83dec80858f75e770d536adfe),
[Function](https://www.notion.so/3f6eeac83dec801eaa94e003ce22654d).

후속 결정 항목은 DB 스키마·접근 권한, 인증 토큰 검증, 외부 API 공급자와 호출 한도,
이동 상태와 데이터 계약, 평가·추천 기준, 수집 주기와 실시간 전달 계약이다.

## 교통 데이터 확인 (Phase 1)

TAGO 버스 API의 실제 응답·식별자·누락 정보를 DB 없이 확인하는 수동 실행 도구다.
`app/integrations/bus_api.py`가 정류소·노선·위치·도착 7개 상세기능을 호출하고 `scripts/inspect_transit.py`가 계획·저장·요약을 맡는다.
DB 저장·주기 수집·경로 알고리즘은 구현하지 않았다.

### 인증키

```powershell
if (-not (Test-Path .env.api)) { Copy-Item .env.api.example .env.api }
```

`.env.api`의 `TAGO_SERVICE_KEY=` 뒤에 공공데이터포털 마이페이지의 **일반 인증키**(Decoding)를 입력한다.
Encoding 키(`%2B` 등 포함)는 이중 인코딩되므로 실행 전에 거부한다. 키는 httpx가 요청할 때 한 번만 URL 인코딩하며 로그·오류·저장 결과에 남기지 않는다.
설정 우선순위는 프로세스 환경변수 → 저장소 루트 `.env.api` → 기본값이다. 요청 제한시간·간격·재시도는 `BACKEND_TAGO_*` 항목으로 바꾼다.
인증키가 없어도 앱·`/health`·도움말·계획 출력·`analyze`는 실행되며, `--execute`만 키를 확인한다.

### 실행

저장소 루트에서 실행한다. **`--execute`가 없으면 대상·엔드포인트·예상 요청 수만 출력하고 API를 호출하지 않는다.**

```powershell
uv run python -m scripts.inspect_transit --help
uv run python -m scripts.inspect_transit stations                     # 계획만 출력
uv run python -m scripts.inspect_transit stations --execute           # 첫 페이지
uv run python -m scripts.inspect_transit stations --all-pages --max-requests 60 --execute
uv run python -m scripts.inspect_transit routes --all-pages --execute
uv run python -m scripts.inspect_transit route-detail --route-id DJB30300004 --execute
# 아래 경로는 예시다. routes --all-pages로 저장된 실제 실행 디렉터리로 바꾼다.
uv run python -m scripts.inspect_transit route-detail --all-routes --routes-from artifacts\transit-inspection\20261026T010203Z_routes --max-requests 800 --execute
uv run python -m scripts.inspect_transit nearby --lat 36.3504 --lon 127.3845 --execute
uv run python -m scripts.inspect_transit locations --route-id DJB30300004 --execute
uv run python -m scripts.inspect_transit arrivals --node-id DJB8001793 --execute
uv run python -m scripts.inspect_transit analyze
uv run python -m unittest discover -s tests/unit                     # 네트워크 없는 테스트
```

| 모드 | 상세기능 | 범위 |
|---|---|---|
| `stations` / `routes` | getSttnNoList / getRouteNoList | 기본 첫 페이지, 전체 목록은 `--all-pages` |
| `route-detail` | getRouteInfoIem + getRouteAcctoThrghSttnList | `--route-id` 지정 노선, 전체 노선은 `--all-routes` |
| `nearby` | getCrdntPrxmtSttnList | 지정 좌표 |
| `locations` / `arrivals` | getRouteAcctoBusLcList / getSttnAcctoArvlPrearngeInfoList | 지정 노선·정류소만 (전체 실시간 순회 없음) |
| `analyze` | 없음 | 저장된 실행 결과만 사용 |

- `--max-requests`(기본 30)는 재시도를 포함한 실행당 최대 HTTP 요청 수다. 소진되면 호출을 멈추고 확보한 결과와 미완료 대상을 기록한다.
- 요청은 순차 실행한다. 시간초과·연결 오류·HTTP 5xx·게이트웨이 01/05만 재시도하고, 인증 실패·잘못된 요청·호출 한도 초과는 재시도하지 않으며 인증·한도 오류 뒤에는 남은 대상을 호출하지 않는다.
- 전체 페이지 수는 첫 응답의 `totalCount`로 정해지므로 계획에는 확정값 대신 최소 요청 수와 예산을 표시한다.
- `--all-routes`는 노선 목록을 끝까지 조회한 뒤 상세를 호출한다. `--routes-from`에 `routes --all-pages` 실행 디렉터리를 주면 저장된 노선 ID를 쓴다.

### 출력과 요약

결과는 실행마다 `artifacts/transit-inspection/<UTC시각>_<모드>/`에 새로 저장하며 Git에서 제외된다.

- `run.json`: 옵션(인증키 제외), 상태(success·partial·failed·aborted·interrupted), 서비스별 HTTP 시도·재시도·성공·실패, 예산, 미완료 대상, 오류 요약.
- `responses/*.json`: 원본 항목(`items`)과 페이지별 조회 조건·요청 시작/응답 수신 시각(UTC)·`totalCount`·반환 개수·상태·중단 사유.
  응답에 원천 관측 시각이 없으므로 `source_observed_at`은 `null`(미제공)이며 수신 시각으로 채우지 않는다.
- `summary.json`: 아래 검사 결과. `analyze`는 여러 실행을 합쳐 `<UTC시각>_analyze/summary.json`을 만든다.

| 항목 | 해석 |
|---|---|
| `fields` | 필드별 값 있음·0·빈값·null·누락 개수, JSON 타입, 명세 외 필드 |
| `stop_station_linkage` | 경유 정류소 ID와 정류소 목록의 연결. 목록이 일부거나 시외 정류소일 수 있어 미연결을 오류로 단정하지 않음 |
| `route_stop_order` | nodeord 중복·연속성, updowncd 분포, 반복 경유(정류소와 routeId+nodeord 경유 위치를 구분) |
| `bus_locations` | 응답에 routeid가 없어 요청 routeId(조회 맥락)로만 노선·경유 정류소와 연결 |
| `arrivals` | arrtime(초)·남은 정류장 수의 정수 변환. 차량번호가 없어 위치정보 차량과 연결하지 않음 |
| `coordinates` / `nearby` | 좌표 누락·0·범위 밖, 근접 결과의 직선거리(도보 경로·시간 아님)와 citycode |
| `call_volume` | 실제 호출 집계, 정적 하루 1회 갱신·실시간 고정 전체 수집·실시간 필요 대상 수집의 일일 호출량 |

실시간 호출량은 대상별 `ceil(활성 분 / 10)`회(24시간이면 최초 조회 포함 144회) × 대상 수 × 관측 페이지 수다.
대상 수·페이지 수·계정 한도를 모르면 계산식과 미확인 항목만 남긴다. 필요 대상과 한도는 `analyze --location-targets N --arrival-targets M --active-hours H --daily-limit L`로 입력한다.

### 확인 상태

공식 포털 명세와 참고문서를 기준으로 구현했고, 2026-10-26 02시경(KST, 미운행 시간) 실제 호출 12회로 다음을 확인했다.

- 대전(cityCode=25)의 totalCount는 정류소 3,032개, 노선 123개이며 numOfRows=100이 동작한다. 정류소 목록에는 세종 지역 정류소(예: 해밀동)도 있다.
- `nodeno`·`routeno`는 숫자와 문자열이 섞여 온다(예: `"031056"`, `"61-1"`). 앞자리 0은 문자열일 때만 남으므로 ID·번호는 문자열로 다룬다.
  첫차 시각은 `"0540"`(문자열), 막차 시각은 `2235`(숫자)로 형식이 다르다.
- 조회한 2개 노선의 경유 정류소는 nodeord가 1부터 연속이고 updowncd가 모두 있으며 회차 지점에서 한 번 바뀐다.
  61-1번은 정류소 4곳을 같은 nodeid로 상·하행 두 번 경유한다. nodeno는 일부 누락된다.
- 근접 조회에는 대전(25) 외 세종(12) 등 다른 도시 정류소가 섞이고, 같은 실제 정류소가 도시별로 다른 ID(`DJB8001378`, `SJB186005227`)로 나온다.
  명세에 없는 `nodeno`도 응답에 있다.
- 존재하지 않는 routeId도 오류 없이 빈 결과로 온다. 빈 결과만으로는 미운행과 잘못된 ID를 구분할 수 없다.

남은 확인 항목은 운행 시간대 위치·도착 응답(vehicleno·arrtime 형식, 관측 시각 부재), 전체 정류소 목록과 경유 정류소의 ID 연결,
실제 오류 응답 형식, 계정의 실제 일일 한도다.

## 개발용 확인 URL (Phase 1)

브라우저 주소창이나 Swagger에서 조회 조건을 넣고 실제 TAGO 응답을 확인한다. CLI와 같은 `bus_api.py` 클라이언트를 쓴다.

### 활성화와 실행

`.env.api`에 인증키(위 "인증키" 참고)를 넣은 뒤, 실행 환경이 `development`이고 `ENABLE_DEVTOOLS=true`일 때만 URL이 등록된다.
기본값은 `BACKEND_ENV=production`, `ENABLE_DEVTOOLS=false`라 아무것도 설정하지 않으면 등록되지 않는다.
`.env.api`에 두 줄을 넣거나, 현재 터미널에서만 환경변수로 켠다.

```powershell
$env:BACKEND_ENV = "development"; $env:ENABLE_DEVTOOLS = "true"
uv run python -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

Swagger는 `http://127.0.0.1:8001/docs`의 `devtools: TAGO 확인` 태그에 있다. 인증키가 없으면 앱과 `/health`는 실행되고 확인 URL만 503을 반환한다.

### URL

대전 도시코드 `cityCode=25`는 서버가 넣는다. 목록형 URL은 `page_no`(1 이상, 기본 1)와 `num_of_rows`(1~100, 기본 10)를 받는다.
노선별 경유 정류소만 노선 하나를 한 번에 보도록 `num_of_rows`가 1~500, 기본 200이다.

| URL | 용도·조회 조건 | 예시 |
|---|---|---|
| `/dev/transit/stops` | 정류소 목록. 선택 `stop_name`(nodeNm), `stop_no`(nodeNo) | http://127.0.0.1:8001/dev/transit/stops?stop_name=시청 |
| `/dev/transit/stops/nearby` | 좌표 주변(반경 500m) 정류소. 필수 `lat`, `lon` | http://127.0.0.1:8001/dev/transit/stops/nearby?lat=36.3504&lon=127.3845 |
| `/dev/transit/routes` | 노선 목록. 선택 `route_no` | http://127.0.0.1:8001/dev/transit/routes?route_no=102 |
| `/dev/transit/routes/{route_id}` | 노선 기본정보(페이지 없음) | http://127.0.0.1:8001/dev/transit/routes/DJB30300037 |
| `/dev/transit/routes/{route_id}/stops` | 노선별 경유 정류소(기본 200, 최대 500) | http://127.0.0.1:8001/dev/transit/routes/DJB30300037/stops |
| `/dev/transit/routes/{route_id}/positions` | 노선별 버스 위치 | http://127.0.0.1:8001/dev/transit/routes/DJB30300037/positions |
| `/dev/transit/stops/{stop_id}/arrivals` | 정류소 도착예정정보 | http://127.0.0.1:8001/dev/transit/stops/DJB8001378/arrivals |
| `/dev/daejeon/stops/{bus_stop_id}/arrivals` | 대전시 버스 도착정보(`apis.data.go.kr/6300000/arrive/getArrInfoByStopID`, `BusStopID`) | http://127.0.0.1:8001/dev/daejeon/stops/8001378/arrivals |

대전시 확인 URL은 수집과 같은 `DAEJEON_BUS_SERVICE_KEY`(비우면 `TAGO_SERVICE_KEY`)로 1회 호출하고 제공기관 응답을 그대로 돌려준다(본문 바이트·
HTTP 상태·Content-Type 유지, 본문에 인증키가 되돌아오면 그 부분만 `***`). 요청·수신 시각(UTC)은 응답 헤더 `X-Upstream-Requested-At`·
`X-Upstream-Received-At`에만 붙는다. 응답을 받지 못한 경우만 JSON 오류(시간초과 504, 연결 오류 502), 키 미설정은 503이다.
`BusStopID`가 TAGO `nodeid`의 `DJB` 뒤 숫자와 같은지는 확인 전이다(Notion Backend > 검증 대기 항목 2번).

ID는 목록 응답에서 얻는다. `routes?route_no=102`의 `raw.response.body.items.item[].routeid`를 노선 URL에,
노선 경유 정류소나 `stops?stop_name=...` 응답의 `nodeid`를 도착정보 URL에 넣는다. 위치·도착은 운행 시간이 아니면 빈 결과이고,
존재하지 않는 ID도 오류 없이 빈 결과로 오므로 ID는 목록에서 복사해 쓴다.

### 응답과 호출 방식

- 응답은 `meta`(TAGO 경로, 인증키를 뺀 조회 조건, 요청·수신 시각(UTC), 제공된 페이지·전체 개수, 외부 HTTP 시도 횟수·상태, resultCode)와
  인증키를 가린 원본 `raw`로 나뉜다. `received_at`은 서버 수신 시각이며 원천 관측 시각이 아니다.
- 요청 한 번에 지정한 대상의 한 페이지만 TAGO에 1회 요청하고 재시도하지 않는다. 다음 페이지는 `page_no`를 올려 다시 요청한다.
- 새로고침·재요청마다 외부 API가 다시 호출되어 개발계정 트래픽이 차감된다. 자동 갱신·저장은 하지 않는다.
- 상태 코드: 입력 오류 422(TAGO 호출 전), 인증키 미설정 503, 제공기관·인증·응답 해석 오류 502, 호출 한도 초과 429, 시간초과 504.

### 비활성화

`ENABLE_DEVTOOLS=false`로 바꾸거나 줄을 지우고(또는 `BACKEND_ENV=production`) 서버를 재시작한다. 환경변수로 켰다면 터미널을 새로 열거나
`Remove-Item Env:ENABLE_DEVTOOLS`를 실행한다. 비활성화하면 URL은 404이고 Swagger·OpenAPI에서도 사라진다.

## 교통정보 수집·저장 (Phase 2)

정적 정보(정류소·노선·노선 기본정보·경유 순서)를 대전 전체로 적재·하루 1회 갱신하고, 개발 설정으로 지정한 노선 위치·정류소 도착만
단일 워커가 대상별 600초 간격으로 공유 수집한다. 경로 후보·평가·추천, 이동 세션, TMAP 도보 호출은 후속 단계다.

| 위치 | 책임 |
|---|---|
| `app/db/session.py` | psycopg 3 연결·트랜잭션, 트랜잭션 단위 advisory 잠금. ORM·마이그레이션 도구는 도입하지 않음 |
| `app/integrations/bus_api.py` | 기존 TAGO 클라이언트. 선택형 `on_attempt`(HTTP 시도별 가린 원문)·`session_retries` 추가, 기존 반환 형식 유지 |
| `app/modules/transit/` | `models.py` 행 형식, `repository.py` SQL, `service.py` 정규화·검증·원자 공개·실시간 수집·최신 조회, `schemas.py` 모듈 간 형식 |
| `app/workers/` | `collection.py` 대상 취합, `scheduler.py` 위상 슬롯·하루 1회 판단, `main.py` 워커 루프 |
| `scripts/import_transit.py` | 정적 초기 적재·상태·사후 대조·호출량·인근 정류소 CLI(워커와 같은 서비스) |
| `migrations/01_transit_core_supabase.sql` | 대상 DB에 적용된 교통 5개 테이블 DDL의 사본(실행 도구 아님) |

### DB 접속

현재 대상은 서버의 `bus-transfer-supabase` 스택이다. 풀러(Supavisor)가 없고 DB 포트를 외부에 열지 않으므로 SSH 터널로 DB 컨테이너에 직접 접속한다
(컨테이너 IP는 재시작 시 바뀔 수 있다).

- 접속 위치: `.env.api`의 `BACKEND_DB_HOST`·`BACKEND_DB_PORT`(터널의 로컬 주소, 필수)
- 계정: `BACKEND_DB_USER`, 비우면 `postgres`
- DB 이름·비밀번호: 루트 `.env`의 `POSTGRES_DB`·`POSTGRES_PASSWORD`(서버 배포 `.env`와 같은 값)
- 루트 `.env`의 `POSTGRES_HOST`(=`db`, 컨테이너 내부 이름)·`POOLER_TENANT_ID`는 쓰지 않는다. 이전 주제의 Supavisor 경로는 제거했다.

```powershell
ssh -p <SSH 포트> -N -L 127.0.0.1:15432:<bus-transfer-supabase-db-1 IP>:5432 <계정>@<서버>
# 컨테이너 IP 확인(서버에서): docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' bus-transfer-supabase-db-1
```

### 정적 적재·확인

```powershell
uv run python -m scripts.import_transit                                    # 계획만 출력(호출·쓰기 없음)
uv run python -m scripts.import_transit run --execute --max-requests 400   # 실제 TAGO 호출 + 적재
uv run python -m scripts.import_transit status                             # 공개 버전·건수·보류 여부
uv run python -m scripts.import_transit verify                             # 저장 원문 재해석 ↔ 활성 행 대조
uv run python -m scripts.import_transit usage --hours 24                   # 서비스별 실제 HTTP 시도 ↔ 승인 한도
uv run python -m scripts.import_transit nearby --lat 36.3504 --lon 127.3845 --radius-m 300
```

- 모든 HTTP 시도(페이지·실패·재시도 포함)는 즉시 `app.api_fetches`에 커밋되어 공개 트랜잭션을 되돌려도 남는다. 인증키는 가린다.
- 전체 페이지·참조·경유 순서를 검증한 뒤에만 `stops`·`routes`·`route_stops`를 같은 `data_version`으로 한 트랜잭션에서 공개한다.
  한 노선이라도 실패하면 공개하지 않고(이후 호출도 멈춤) 마지막 정상본을 유지하며 실패 행에 보류 사유를 남긴다.
- 정류소·노선은 원천 ID(문자열)로 기존 내부 ID를 유지한다. 원천에서 사라진 행은 전체 검증이 끝난 공개에서만 비활성화한다.
- 정류소 목록에 없는 경유 정류소(시외 경유 등)는 경유 목록 값으로 보존한다. 반복 경유는 서로 다른 `seq`로 모두 남긴다.
- 노선 ID당 패턴 하나(`pattern_key`=노선·정류소 순서·updowncd 서명)이며, `direction_code`에는 행별 `updowncd` 원값을 둔다.
- 좌표가 누락·비숫자·범위 밖·(0, 0)이면 `location`은 NULL이다. 인근 정류소는 저장 좌표의 측지 거리(`ST_DWithin`) 결과이며 도보 가능 확정이 아니다.
- 제공기관이 `resultCode 99: 가용한 세션이 존재하지 않습니다`를 반환하면 정적 적재에서만 90초 기다려 최대 3회 다시 요청한다.
- 노선 기본정보의 첫차·막차·배차간격은 단위·형식 미확인 상태로 원값과 함께 `service_info`에 둔다.

### 실시간 수집 워커

```powershell
uv run python -m app.workers.main --targets          # 대상·최신 공개값·다음 예정 시각(호출 없음)
uv run python -m app.workers.main --once             # 만기 작업 한 번 처리
uv run python -m app.workers.main                    # 상시 실행(정적 하루 1회 갱신 포함, --no-static으로 제외)
```

- 대상은 `.env.api`의 `BACKEND_REALTIME_POSITION_ROUTE_IDS`(노선 ID)·`BACKEND_REALTIME_ARRIVAL_STOP_IDS`(정류소 ID)이다.
  워커가 매 회 다시 읽으므로 목록을 고치면 등록·해제된다. 같은 조회 조건은 하나로 합치고, DB 잠금으로 동시 실행에서도 한 번만 호출한다.
- 공개값이 없으면 즉시 최초 조회하고, 이후 대상별 고정 위상 슬롯에서 600초(`BACKEND_REALTIME_INTERVAL_SECONDS`) 간격으로 갱신한다.
- 배치의 모든 페이지를 정규화한 뒤 관측과 `published_at`을 함께 공개한다. 정상 빈 응답은 빈 스냅샷으로 공개하고, 부분 수집·실패는 직전 공개본을 유지한다.
  최신 조회는 `fresh`/`stale`/`no_data`, 내용 `items`/`empty`, 마지막 시도 실패 여부, 원천 미제공 항목을 따로 반환한다.
- 위치의 경유 위치는 nodeid·nodeord가 현재 패턴의 같은 행과 맞을 때만, 도착의 경유 위치는 현재 패턴에서 한 번만 경유할 때만 확정한다.
  도착정보는 대전시 버스 도착정보 API(`getArrInfoByStopID`, 2026-11-09부터 TAGO 도착정보 대체)에서 받는다. 조회 조건 `BusStopID`는 정적 정류소의
  `stop_no`이고, 응답 `BUS_NODE_ID`·`ROUTE_CD`에 `DJB`를 붙여 TAGO 정류소·노선에 잇는다. 차량번호(`CAR_REG_NO`)를 그대로 저장하고 잔여초(`EXTIME_SEC`)는
  원값, 기준 시각은 항목별 `INFO_OFFER_TM`(KST, 사용자 결정·검증 전, 출처 `daejeon_info_offer_tm`)으로 절대 ETA를 함께 저장한다. 운행 회차 키는 없다.
  수집 키는 `daejeon:getArrInfoByStopID?nodeId=<정류소 ID>`, 위치정보는 TAGO 그대로다. `collected_at`은 서버 수신 시각이다.

### 시험

```powershell
uv run python -m unittest discover -s tests/unit
# 임시 PostGIS(예: docker run --rm -d -e POSTGRES_PASSWORD=<임의값> -p 127.0.0.1:55432:5432 postgis/postgis:16-3.4)
$env:TEST_POSTGIS_DSN = "postgresql://postgres:<임의값>@127.0.0.1:55432/postgres"
uv run python -m unittest discover -s tests/integration
```

격리 시험은 시험마다 새 데이터베이스에 `migrations/01_transit_core_supabase.sql`을 적용하고 `tests/fixtures/tago_world.py`의 합성 응답만 쓴다.
`TEST_POSTGIS_DSN`이 없으면 건너뛴다. fixture 통과는 실제 데이터 연결·예측 품질 검증을 뜻하지 않는다.

## 경로 검색 연동 (Phase 3A~3C)

좌표 입력 → 정적 후보(기존 `routing.generate_candidates`) → TMAP 도보 → 실시간 공유 수집 → 평가(`evaluation.evaluate_candidates`)
→ 추천(`recommendation.recommend_candidates`) → `route_searches`·`route_candidates` 저장을 잇는다. 핵심 함수는 바꾸지 않았다.
장소·주소 검색 API는 제공처 지정 후 연동하며, 지금은 좌표와 선택적 이름·주소·제공기관·외부 ID만 `place_info`에 보존한다.

| 위치 | 책임 |
|---|---|
| `app/workflows/search.py` | 검색 흐름·설정 검증·도보 중복 제거·실시간 입력 변환·legs/evaluation JSON·한 트랜잭션 저장 |
| `app/integrations/walking_api.py` | TMAP 보행자 경로(POST `/tmap/routes/pedestrian?version=1`, 헤더 `appKey`) 요청·응답 해석 |
| `app/modules/routing/repository.py` | `route_searches`(processing → 확정)·`route_candidates` 저장·조회, 검색 실시간 대상 조회 |
| `app/modules/evaluation/samples.py`, `repository.py` | 환승 표본·공동 관측 묶음 생성 키·평가 입력 변환·사후 판정 규칙과 SQL |
| `app/workflows/labeling.py` | 워커의 사후 판정(같은 패턴·경유 위치·회차의 도착 점 이벤트만 사용) |
| `app/workers/collection.py` | 개발 설정 대상 + 유효기간 안의 검색 대상 취합(만료 시 해제) |
| `scripts/search_routes.py` | 좌표 검색 CLI(`run`, `show`) |

### 실행

```powershell
# 설정: .env.api의 BACKEND_SEARCH_*·BACKEND_EVAL_*·BACKEND_TMAP_*·BACKEND_SAMPLE_*(기본값 없음, 누락 시 설정 오류), SK_OPEN_API_KEY
uv run python -m scripts.search_routes run --origin 36.3315,127.4335 --destination 36.3504,127.3845            # 계획만(호출·쓰기 없음)
uv run python -m scripts.search_routes run --origin 36.3315,127.4335 --destination 36.3504,127.3845 --execute  # 실제 TMAP·TAGO·DB 저장
uv run python -m scripts.search_routes show <search_id>                                                        # 저장 결과 다시 읽기
```

- DFS 중에는 외부 API를 호출하지 않는다. 도보는 후보 생성 뒤 방향 있는 구간·좌표·옵션별로 한 번만 조회하며 검색당 한도를 넘으면 미조회다.
  legs의 도보 `status`: `available`(TMAP 경로·거리 m·시간 초) / `error`(조회 실패·응답 해석 불가) / `unknown`(미조회·좌표 없음, 시간 미확인) /
  `same_stop`(같은 정류소 환승, 조회 없음·준비시간은 평가에서 별도). TMAP 오류 코드의 의미가 확인되지 않아 `no_path`로 단정하지 않는다.
- 실시간 대상(후보의 승하차 정류소 도착·노선 위치)은 검색 행의 `summary.realtime_targets`·`expires_at`으로 등록되어 워커가 공유 수집하고,
  검색 시점에는 Phase 2 수집기로 600초 이내 공개 배치를 재사용·부족한 대상만 조회한다. 평가의 최신성 한도는 `BACKEND_EVAL_MAX_DATA_AGE_SECONDS`로 따로 적용한다.
- 도착정보(대전시)는 차량번호와 `INFO_OFFER_TM` 기준 절대 ETA가 있어 승차 ETA를 만든다. 하차 ETA는 같은 차량번호의 하차 정류소 예측을 쓰되,
  운행 회차 키가 없으므로 같은 패턴이고 남은 정류장 수(`STATUS_POS`) 차이가 승하차 경유 순번 차이와 같으며 하차가 승차보다 늦을 때만 잇는다
  (사유 코드 `alight_linked_by_vehicle_without_run_key`, 검증 전 연결). 모든 탑승 구간이 이어진 후보는 nominal 순위를 받는다. 회차 키가 없어
  환승 표본·확률은 계속 null이다. 같은 노선 차량이 승하차 정류소 사이에 더 있으면 하차 정류소 목록에 우리 차량이 없어 평가 제한으로 남는다.
- ETA 기준 시각 정책 `BACKEND_TAGO_ETA_BASIS_POLICY`: 기본 `unconfirmed`. `server_received_at`은 응답 수신 시각 + 잔여초로 절대 ETA를
  근사한다(기준 출처 `server_received_at_approx`, 정책 버전 `tago-arrival-basis-server-received-at/1`이 검색 `params`·`summary`·후보 ETA·
  안내에 남고 표본 조건 키도 분리된다). Notion DB-C04는 검증한 근사 정책만 허용하지만 2026-11-09 사용자 결정으로 검증 전에 켜 둔다
  (`.env.api.example` 값 `server_received_at`, 코드 기본값은 `unconfirmed`; 정확도 검증은 Notion Backend > 검증 대기 항목 1번). 이 정책을 켜도 하차 ETA는
  같은 차량 ETA(차량번호 필요) 또는 확인된 도착 이벤트의 구간시간이 있어야 하므로 TAGO만으로는 순위가 생기지 않는다.
- 결과 저장: 검색 확정(상태·비교 모드)과 후보·표본·묶음을 한 트랜잭션에서 저장하고 `processing` 검색만 확정한다. 순위 후보의 모드는 검색과 같고
  제한 후보의 순위·모드는 NULL이다. 버스 구간 지도 형상은 검증된 제공처가 없어 `geometry_status=unavailable`(`shape_provider_not_configured`)이다.

## 앱 API·이동 세션 (Phase 4A~4C)

인증된 사용자가 검색을 접수하고(API) 워커가 계산한 후보를 비교해 이동을 시작한 뒤, 수동 탑승·하차로 도착까지 진행한다.
API 서버는 TAGO·TMAP을 호출하지 않고 DB만 읽고 쓴다. 검색 계산·공유 수집·안내 재계산은 워커가, 앱 전달은 API 서버의 WebSocket이
DB에 저장된 버전을 확인해 수행한다(프로세스 간 메모리 공유 없음).

| 위치 | 책임 |
|---|---|
| `app/core/security.py`, `dependencies.py`, `exceptions.py` | Supabase 액세스 토큰(HS256, iss·aud·exp·sub·role) 검증, 사용자·탈퇴 확인, 오류 응답 형식 |
| `app/modules/users/` | `GET/PATCH /api/v1/me`: 프로필 1회 생성(동시 요청 1행), 표시 이름 변경, 탈퇴 계정 403 |
| `app/modules/transit/router.py`, `views.py` | `/stops`·`/routes`·`/static-data/version`: 정적 공개본·공유 수집 최신 배치 조회, 형상 상태·ETag |
| `app/modules/routing/router.py`, `views.py` | `/places/search`(제공처 어댑터), `POST/GET /route-searches`: 접수(202)·후보·평가·순위·제한 사유 |
| `app/workers/search_jobs.py` | 접수된 검색을 임대해 Phase 3 흐름(`SearchWorkflow.run_queued`)으로 계산·저장, 중단·만료 시 error |
| `app/modules/journeys/` | `/journeys`: 시작·조회·이벤트(멱등·상태 버전)·위치. `state.py`가 전이 규칙 |
| `app/workflows/guidance.py`, `app/workers/recalculation.py` | 선택 경로 남은 구간을 기존 평가 엔진으로 재계산(1분 + 입력 즉시), 상태 버전 조건부 저장, 예측 스냅샷 |
| `app/realtime/` | `WS /api/v1/journeys/{id}/stream`: 인증·소유권 확인 후 최신 상태 전송, 버전 변경 동기화 |
| `app/integrations/places_api.py`, `route_shapes_api.py` | 장소 검색·버스 형상 제공처 계약(현재 설정된 제공처 없음 → 503 / 미확보 사유) |
| `migrations/09_profiles_soft_delete.sql` | `profiles.deleted_at`·`auth_blocked_at`·CHECK를 재실행 가능하게 추가(02로 만든 DB에는 변화 없음) |

### 실행

```powershell
uv run python -m uvicorn app.main:app --host 127.0.0.1 --port 8001   # API (토큰 검증값: .env.api BACKEND_AUTH_JWT_* 또는 루트 .env)
uv run python -m app.workers.main                                    # 워커: 공유 수집 루프 + 앱 작업 스레드(검색·안내)
```

- 인증: `Authorization: Bearer <Supabase access token>`. places·stops·routes도 Phase 4에서는 인증을 요구한다. 탈퇴(`deleted_at`) 계정은
  유효한 토큰이어도 403 `ACCOUNT_DELETED`이며 프로필을 다시 만들지 않는다. `DELETE /me`·Auth 차단 작업은 Phase 6 범위다.
- 검색: `POST /api/v1/route-searches` → 202 `{searchId, status: processing}` → 워커 계산 → `GET`으로 `ready`/`data_limited`/`no_path`/
  `out_of_scope`/`error` 확인. 후보의 `startMode`: 순위 후보는 `ranked`, 자료 부족으로 순위가 없는 후보는 `data_limited`(시간·확률은
  null과 사유로 안내, 이동 상세 `selectedRoute.startMode`·`startLimitReasons`). 기준 ETA상 연결 불가(`negative_slack`)·실행 불가 확인
  (`confirmed_infeasible`, 도보 경로 없음 포함) 후보는 409 `CANDIDATE_NOT_STARTABLE`, 만료 검색은 409 `SEARCH_EXPIRED`.
- 이동: `POST /journeys`(사용자당 활성 1개, 409 `ACTIVE_JOURNEY_EXISTS`), `POST /journeys/{id}/events`(헤더 `Idempotency-Key` 필수,
  `stateVersion` 불일치 409, 지원: `board`·`alight`·`completed`·`cancelled`·`correction`; `missed_bus`·`destination_changed`·
  `route_accepted/rejected`는 Phase 5, `transfer_result`는 Phase 6이라 422 `UNSUPPORTED_EVENT_TYPE`), `PUT /journeys/{id}/location`(역순 무시).
- WebSocket: 헤더 인증이 기본이며, 헤더를 보낼 수 없으면 연결 후 첫 메시지 `{"type":"auth","accessToken":"..."}`(제한 시간 내).
  종료 코드 4401(인증)·4403(탈퇴)·4404(없음/타인). 토큰을 URL에 넣지 않는다.

### 시험

```powershell
uv run python -m unittest discover -s tests/unit
$env:TEST_POSTGIS_DSN = "postgresql://postgres:<임의값>@127.0.0.1:55437/postgres"   # 임시 PostGIS 컨테이너
uv run python -m unittest discover -s tests/integration                  # Phase 2·3·4 DB 격리 시험
uv run python tests/e2e/phase4_flow.py [--real-tmap]                     # API·워커 별도 프로세스 E2E(HTTP·WebSocket)
```

### 지원 범위(out_of_scope) 판정

앱 검색은 워커가 후보 생성·외부 호출 전에 출발지·목적지를 각각 판정한다([service_area.py](modules/routing/service_area.py)).

1. 대전광역시 행정경계 안 또는 경계선 위(`ST_Covers`)면 지원 범위. 주변 정류소가 없어도 포함한다.
2. 아니면 탐색과 같은 정적 공개본의 활성 노선이 경유하는 정류소(시외 경유 포함)가 출발지=접근 반경, 목적지=도착 반경 안에
   있으면(`ST_DWithin`, 기존 `nearby_stops` 재사용) 지원 범위.
3. 경계 밖이고 반경 안 정류소가 없으면 범위 밖 → `out_of_scope`, `reasonCodes`: `origin_out_of_scope`·`destination_out_of_scope`,
   후보 `[]`, `recommendedCandidateId`·`rankingMode` null, `serviceArea.outOfScopePoints`. TAGO·TMAP 호출과 수집 대상 등록 없음.
4. 경계 자료·정적 공개본이 없어 3을 확정할 수 없으면 `data_limited`(`service_area_unavailable`). 모두 범위 안이면 기존 흐름(경로가
   없으면 `no_path`). CLI(`scripts.search_routes`)와 이동 세션은 판정하지 않는다.

경계 자료: `data/service_area/daejeon.geojson`(출처·변환·검증 기록은 `daejeon.source.json`). 사용자 제공 `N3A_G0010000.zip`
(시·도 17건, Korea 2000 통합좌표계)에서 BJCD `3000000000` 1건을 GDAL 3.10.3으로 EPSG:4326 변환(`-makevalid`)했다. 원본 압축 해제
폴더 `N3A_G0010000/`은 Git에서 제외한다. 다른 파일을 쓰려면 `BACKEND_SERVICE_AREA_GEOJSON`에 경로를 지정한다.

```powershell
# 재생성(작업 디렉터리에 N3A_G0010000/ 압축 해제 후, GDAL 3.10.3 이미지 사용)
docker run --rm -v ${PWD}:/work -w /work ghcr.io/osgeo/gdal:ubuntu-small-3.10.3 ogr2ogr -f GeoJSON -oo ENCODING=CP949 `
  -where "BJCD = '3000000000'" -select BJCD,NAME -t_srs EPSG:4326 -makevalid -nlt MULTIPOLYGON -nln daejeon `
  -lco RFC7946=YES -lco COORDINATE_PRECISION=9 "daejeon.geojson" "N3A_G0010000/N3A_G0010000.shp"
```

### 화면 조회 기반 실시간 수집 (Phase 4A 후속 보완)

`GET /api/v1/routes/{routeId}/positions`·`GET /api/v1/stops/{stopId}/arrivals`가 인증·ID 확인을 통과한 **활성** 노선·정류소면
화면 수집 수요를 `app.realtime_view_targets`(마이그레이션 10: 수집 대상 키 + 만료 시각)에 등록하고, 저장된 최신 공개본을 바로 반환한다.

- 키: 수집기·`api_fetches.target_key`와 같은 `tago:<상세기능>?cityCode=25&routeId|nodeId=<원천 ID>`. 내부 ID → 원천 ID 해석은 기존 조회 그대로.
- 만료: DB 시계 `now() + BACKEND_REALTIME_VIEW_TARGET_TTL_SECONDS`(기본 1800초). 재조회하면 연장하고, 동시 조회는 한 행에서 더 늦은 만료를 유지한다.
- 워커는 유효한 화면 수요를 개발 설정·검색·활성 이동 대상과 같은 키로 합친다. 최신 공개본이 없으면 다음 회차(최대 `BACKEND_WORKER_POLL_SECONDS`)에
  최초 수집하고 이후 600초 간격으로 갱신한다. 재조회는 갱신 시각을 바꾸지 않고 외부 호출을 일으키지 않는다. 만료된 행은 워커가 정리한다.
- 비활성 대상·인증 실패·형식 오류·없는 ID는 등록·연장하지 않는다. 첫 조회는 공개본이 없으면 `unavailable`, 이후 조회에서 워커 공개본을 읽는다.
