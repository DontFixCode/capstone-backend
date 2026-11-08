# Phase 4 API 예시 데이터 — Swagger 순서

Swagger(`http://127.0.0.1:8001/docs`)에 보이는 순서(1 me → 7 journeys) 그대로 위에서부터 실행하면 된다.
각 단계에서 `기록` 표시한 값을 다음 단계 입력에 넣는다.

## 0. 준비

1. API: `uv run python -m uvicorn app.main:app --host 127.0.0.1 --port 8001`
2. 워커(6번 경로 검색 계산에 필요): `uv run python -m app.workers.main`
3. Swagger 오른쪽 위 **Authorize** → Supabase 로그인 세션의 `access_token` 값만 입력(앞에 `Bearer `를 붙이지 않음).
   모든 API에 자물쇠가 붙어 있고, 한 번 넣으면 끝까지 쓴다.
4. 실제 DB를 쓰려면 SSH 터널(127.0.0.1:15432)이 열려 있어야 한다. 닫혀 있으면 DB를 쓰는 API는 503 `DB_UNAVAILABLE`.

좌표(공식 대전 경계로 안/밖 확인함)

| 이름 | latitude | longitude | 판정 |
|---|---|---|---|
| 대전역 | 36.3315 | 127.4335 | 대전 안 |
| 대전시청 | 36.3504 | 127.3845 | 대전 안 |
| 유성온천역 | 36.3536 | 127.3412 | 대전 안 |
| 정부대전청사 | 36.3593 | 127.3858 | 대전 안 |
| 대전 경계 꼭짓점 | 36.211769695 | 127.410119983 | 경계선 위(지원 범위) |
| 서울시청 | 37.5665 | 126.9780 | 범위 밖 |
| 서울역 | 37.5547 | 126.9707 | 범위 밖 |

---

## 1. me

### 1-1. `GET /api/v1/me`
입력 없음 → **200** `userId`, `displayName: null`, `createdAt`, `updatedAt` (첫 호출에서 프로필 생성)

### 1-2. `PATCH /api/v1/me`
Request body:
```json
{"displayName": "대전버스 이용자"}
```
→ **200** `displayName: "대전버스 이용자"`

다른 본문으로 다시 실행해 보기:

| 본문 | 결과 |
|---|---|
| `{}` | 200, 이름 유지·`updatedAt` 그대로 |
| `{"displayName": null}` | 200, 이름 지움 |
| `{"displayName": "   "}` | 400 `INVALID_DISPLAY_NAME` |
| `{"displayName": "a", "nickname": "b"}` | 422 |

---

## 2. static-data

### 2-1. `GET /api/v1/static-data/version`
입력 없음 → **200** `dataVersion`(현재 정적 공개본 UUID), `publishedAt`, `mixedVersions: false`

---

## 3. stops

### 3-1. `GET /api/v1/stops/nearby`
| 파라미터 | 값 |
|---|---|
| latitude | `36.3504` |
| longitude | `127.3845` |
| radiusM | (비움 → 300 m) |

→ **200** 대전시청 주변 정류소가 거리순. **기록** `items[0].id` → `stopId`, `items[0].externalId` → `stopExternalId`

같이 해 보기: `radiusM` = `5000` → 400 `RADIUS_TOO_LARGE`, `latitude` = `91` → 422

### 3-2. `GET /api/v1/stops`
| 파라미터 | 값 |
|---|---|
| provider | `tago` |
| externalId | 3-1의 `stopExternalId` |

→ **200** 같은 정류소(`id`가 `stopId`와 같음). `externalId` = `NOPE` → 404

### 3-3. `GET /api/v1/stops/{stopId}`
stopId = 3-1의 `stopId` → **200** 이름·번호·좌표·`isActive`. stopId = `999999999` → 404, `abc` → 422

### 3-4. `GET /api/v1/stops/{stopId}/routes`
stopId = 3-1의 `stopId` → **200** 지나는 노선 목록.
**기록** `items[0].route.id` → `routeId`, `items[0].route.routeNo` → `routeNo`, `items[0].patternKey` → `patternKey`,
`items[0].seq` → `seq`. 같은 노선이 두 번 지나면 `seq`가 다른 항목이 따로 나온다.

### 3-5. `GET /api/v1/stops/{stopId}/arrivals`
stopId = 3-1의 `stopId` → **200** `items`, `dataStatus`
- 처음 조회하면 `availability: "unavailable"`(아직 공개본 없음)이고, 이 조회로 그 정류소가 30분간 수집 대상이 된다.
  워커(`uv run python -m app.workers.main --no-static`)가 떠 있으면 30초 안팎에 수집하므로 잠시 뒤 다시 조회하면 `available`
  (운행 차량이 없는 시간이면 `empty`)이 된다.
- `predictedArrivalAt`·`etaBasisAt`·`etaBasisSource`는 null이 정상이다(TAGO 기준 시각 미확인, 잔여초 `remainingSeconds`만 원값).
  `.env.api`에 `BACKEND_TAGO_ETA_BASIS_POLICY=server_received_at`을 넣고 API를 다시 띄우면(검증 실행용) `etaBasisAt` = `collectedAt`,
  `etaBasisSource: "server_received_at_approx"`, `predictedArrivalAt` = `collectedAt` + `remainingSeconds`가 나온다. 현재 남은 시간은
  `predictedArrivalAt` − 현재 시각이다(5분 전 수집한 "600초"는 지금 300초).

---

## 4. routes

### 4-1. `GET /api/v1/routes`
| 파라미터 | 값 |
|---|---|
| routeNo | 3-4의 `routeNo` |

→ **200** 같은 번호의 노선 목록(`routeId` 포함). 파라미터를 모두 비우면 422.
원천 ID로도 찾기: provider `tago`, externalId = 목록의 `externalId`.

### 4-2. `GET /api/v1/routes/{routeId}`
routeId = 3-4의 `routeId` → **200** 기점·종점, `serviceInfo`(배차간격 `UNIT_UNCONFIRMED`·`seconds: null`), `shapes[]`

### 4-3. `GET /api/v1/routes/{routeId}/patterns`
routeId = 3-4의 `routeId` → **200** 패턴별 첫·마지막 정류소, `stopCount`, `shapeStatus: "UNAVAILABLE"`

### 4-4. `GET /api/v1/routes/{routeId}/patterns/{patternKey}/stops`
| 파라미터 | 값 |
|---|---|
| routeId | 3-4의 `routeId` |
| patternKey | 3-4의 `patternKey` |
| afterSeq | 3-4의 `seq` |

→ **200** 그 정류소 다음 경유 정류소들(`seq` 오름차순). 마지막 seq를 넣으면 `items: []`, patternKey = `nope` → 404

### 4-5. `GET /api/v1/routes/{routeId}/shapes/{patternKey}`
routeId·patternKey = 3-4 값 → **200** `status: "UNAVAILABLE"`, `geometry: null`, `reason: "shape_provider_not_configured"`,
응답 헤더 `ETag`. (`If-None-Match` 헤더에 ETag를 넣어 다시 보내면 304 — Swagger에는 헤더 칸이 없어 curl로 확인)

### 4-6. `GET /api/v1/routes/{routeId}/positions`
routeId = 3-4의 `routeId` → **200** `items`(차량번호·위치), `dataStatus`. 3-5와 같이 첫 조회는 `unavailable`이고 노선이 30분간 수집 대상이 되며, 워커 수집 뒤 다시 조회하면 `available`/`empty`

---

## 5. places

### 5-1. `GET /api/v1/places/search`
q = `대전역` → **503** `PLACES_NOT_CONFIGURED`(장소 검색 제공처 미정, 정상). q = `   ` → 422

---

## 6. route-searches

### 6-1. `POST /api/v1/route-searches`
Request body (대전역 → 대전시청):
```json
{
  "origin": {"latitude": 36.3315, "longitude": 127.4335, "name": "대전역"},
  "destination": {"latitude": 36.3504, "longitude": 127.3845, "name": "대전시청"}
}
```
→ **202** `searchId`, `status: "processing"`. **기록** `searchId`

### 6-2. `GET /api/v1/route-searches/{searchId}`
searchId = 6-1 값. 워커가 계산하는 몇 초 동안은 `processing`, 이후 다시 실행.
- 실제 TAGO 자료: **200** `status: "data_limited"`, `reasonCodes`에 `no_rankable_candidate`. 후보(`candidates`)는 있지만
  `rank: null`(ETA 기준 시각 미확인 → `evaluation.reasonCodes`에 `eta_basis_unconfirmed`). 자료 부족일 뿐인 후보는
  `startable: true`, `startMode: "data_limited"`이다. `evaluation.connectionStatus`가 `confirmed_infeasible`(도보 경로 없음 등)·
  `negative_slack`인 후보는 `startable: false`, `startBlockedReason: "CANDIDATE_NOT_STARTABLE"`.
- **기록** `startMode`가 `data_limited`(또는 `ranked`)인 후보의 `candidateId` → `candidateId` (7-1에서 사용)

6-1 본문을 바꿔 다시 접수 → 6-2로 확인:

| 본문 | 6-2 결과 |
|---|---|
| destination 서울시청 `{"latitude": 37.5665, "longitude": 126.9780}` | `out_of_scope`, `reasonCodes: ["destination_out_of_scope"]`, `candidates: []` |
| origin 서울역 `{"latitude": 37.5547, "longitude": 126.9707}` | `out_of_scope`, `["origin_out_of_scope"]` |
| origin 서울역 + destination 서울시청 | `out_of_scope`, `["origin_out_of_scope", "destination_out_of_scope"]` |
| origin 대전 경계 꼭짓점 `{"latitude": 36.211769695, "longitude": 127.410119983}` | `out_of_scope`가 아님(`serviceArea.points[0].insideBoundary: true`). 주변 정류소가 없으면 `no_path` |
| 위 기본 본문에 `"startAt": "2026-11-08T09:00:00+09:00"` 추가 | 출발 기준 시각 지정 |
| origin `latitude` = `91` | 6-1에서 422 |
| `"startAt": "2026-11-08T09:00:00"`(시간대 없음) | 6-1에서 422 |

---

## 7. journeys

### 7-1. `POST /api/v1/journeys`
Request body:
```json
{"searchId": "<6-1 searchId>", "candidateId": "<6-2 candidateId>"}
```
- **201** `journeyId`, `stateVersion: 1`, `movementState: "walking"`. **기록** `journeyId`
- 실제 TAGO 자료(6-2가 `data_limited`)에서는 `selectedRoute.startMode: "data_limited"`, `rank: null`,
  `startLimitReasons`에 `eta_basis_unconfirmed` 등. 시간·확률은 null이고 수동 탑승·하차·도착 안내는 그대로 동작한다.
  순위 후보로 시작하면 `startMode: "ranked"`.
- 그 밖에: `startable: false` 후보 409 `CANDIDATE_NOT_STARTABLE`, 30분 지난 검색 409 `SEARCH_EXPIRED`,
  진행 중 이동이 있으면 409 `ACTIVE_JOURNEY_EXISTS`, 없는 searchId(`00000000-0000-4000-8000-000000000000`) 404

### 7-2. `GET /api/v1/journeys`
status = `active` → **200** 진행 중 이동(없으면 `items: []`). status = `done` → 422

### 7-3. `GET /api/v1/journeys/{journeyId}`
journeyId = 7-1 값 → **200** `selectedRoute`(구간·`startMode`), `currentProgress`, `guidance`(워커 계산 전엔 null·`guidanceStatus: "pending"`).
제한 시작한 이동은 워커 계산 뒤 `guidance.status: "data_limited"`, `nextBoarding.predictedArrivalAt: null`,
`reasonCodes`에 `next_boarding_eta_unavailable`·`location_not_received` 등이 나온다(`guidance.etaBasisPolicy`로 적용 정책 확인).
없는 ID `00000000-0000-4000-8000-000000000000` → 404

### 7-4. `POST /api/v1/journeys/{journeyId}/events`
journeyId = 7-1 값. 헤더 칸 `Idempotency-Key`에 아래 키를, `stateVersion`에는 직전 응답의 `stateVersionAfter`를 넣는다.
`legIndex`는 7-3 `selectedRoute.legs`에서 `kind: "bus"`인 구간 번호(보통 1).

| 순서 | Idempotency-Key | Request body | 결과 |
|---|---|---|---|
| ① 탑승 | `board-1` | `{"eventType": "board", "stateVersion": 1, "payload": {"legIndex": 1, "vehicleId": null}}` | 200, `stateVersionAfter: 2`. **기록** `eventId` |
| ② 같은 요청 재전송 | `board-1` | ①과 같음 | 200, 같은 `eventId`, `replayed: true` |
| ③ 같은 키 다른 본문 | `board-1` | ①에서 `"vehicleId": "X"` | 409 `IDEMPOTENCY_KEY_REUSED` |
| ④ 탑승 정보 수정 | `fix-1` | `{"eventType": "correction", "stateVersion": 2, "payload": {"supersedesEventId": "<①eventId>", "vehicleId": "대전75자1234"}}` | 200, `stateVersionAfter: 3` |
| ⑤ 하차 | `alight-1` | `{"eventType": "alight", "stateVersion": 3, "payload": {"legIndex": 1}}` | 200, `stateVersionAfter: 4` |
| ⑥ 도착 | `done-1` | `{"eventType": "completed", "stateVersion": 4, "payload": {}}` | 200, 이동 종료 |

오류 확인: `stateVersion`을 틀리게 → 409 `STATE_VERSION_CONFLICT`, 탑승 중 다시 `board` → 409 `INVALID_TRANSITION`,
`{"eventType": "missed_bus", "stateVersion": 1, "payload": {}}` → 422 `UNSUPPORTED_EVENT_TYPE`, 종료 뒤 `board` → 409 `JOURNEY_ENDED`.

### 7-5. `PUT /api/v1/journeys/{journeyId}/location`
journeyId = 7-1 값 (⑥ 도착 전에 실행). Request body:
```json
{"latitude": 36.3504, "longitude": 127.3845, "observedAt": "2026-11-08T09:05:00+09:00", "accuracyM": 12}
```
→ **204**. `observedAt`을 더 이전(`2026-11-08T09:00:00+09:00`)으로 보내도 204이지만 위치는 바뀌지 않는다(7-3 `location`으로 확인).
현재보다 1시간 뒤 시각 → 422 `OBSERVED_AT_IN_FUTURE`, 종료된 이동 → 409.

---

## 8. Swagger에 없는 것: WebSocket

`ws://127.0.0.1:8001/api/v1/journeys/{journeyId}/stream` — Postman의 WebSocket 요청 등으로 연결한다.
- 헤더 `Authorization: Bearer <access_token>`, 또는 헤더 없이 연결 후 10초 안에 `{"type": "auth", "accessToken": "<access_token>"}`
- 연결 직후 현재 상태(`journey_state`)를 받고, 7-4 이벤트를 보내면 바뀐 상태가 온다. `{"type": "ping"}` → `pong`
- 종료 코드: 4401 인증, 4403 탈퇴 계정, 4404 없거나 다른 사용자의 이동

## 9. default

`GET /health` → 200 `{"status": "ok"}`, `GET /db-test` → DB 연결 확인(터널 필요).
`devtools` 그룹(`/dev/transit/...`)은 개발 설정에서만 보이며 요청마다 실제 TAGO를 호출한다.
`devtools: 대전시 버스 확인` 그룹의 `GET /dev/daejeon/stops/{bus_stop_id}/arrivals`는 대전시 도착정보 API를 1회 호출하고 응답을
그대로 보여준다(인증키만 가림). `bus_stop_id`에 TAGO 정류소 ID의 숫자 부분(예: `DJB8001378` → `8001378`)을 넣어 보고 차량번호 필드가
있는지 확인한다(대응 관계는 미확인). 본문에 `returnReasonCode` `30`(`SERVICE_KEY_IS_NOT_REGISTERED_ERROR`)이 있으면 인증키가 이 API에 등록되지 않은 것이다.
