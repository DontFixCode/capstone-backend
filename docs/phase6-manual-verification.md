# Phase 6 수동 검증 절차 — 기록 관리·회원탈퇴·현장 검증

자동 시험(`tests/unit/test_phase6.py`, `tests/integration/test_phase6_api.py`)은 임시 PostGIS DB·합성 TAGO/TMAP·가짜 Auth 관리자로
API·DB 규칙을 확인한다. 이 문서는 **실제 기기·대전 버스 주행·배포 서버**가 필요해 자동 시험으로 대신할 수 없는 항목의 절차와 예상 결과다.
수행 전에는 어떤 항목도 통과로 표시하지 않는다.

- Phase 5(이동 중 재평가·대안 추천)가 없으므로 `대안 계산 → 수락 → 안내 경로 변경`은 **보류**한다. `proposedRoute`는 항상 `null`이다.
- 탈퇴(M-6·M-7)는 되돌리는 API가 없다. 시험 전용 계정만 쓴다.
- 호출 예시 값은 [API 예시](api-examples.md)를 따른다.

## 0. 배포 전 확인

| 항목 | 방법 | 기대 |
|---|---|---|
| profiles 탈퇴 컬럼 | DB에서 `\d app.profiles` | `deleted_at`, `auth_blocked_at`, CHECK `profiles_auth_blocked_check`가 있다. 없으면 `migrations/09_profiles_soft_delete.sql` 적용(재실행 안전) |
| 재평가 검색 FK | `SELECT conname FROM pg_constraint WHERE conrelid = 'app.route_searches'::regclass AND contype = 'f'` | journeys를 가리키는 FK가 있다(2026-11-07 카탈로그에서 확인된 `route_searches_journey_id_fkey`). 없을 때만 `11_route_searches_journey_fk.sql`이 추가하며, 있으면 아무것도 바꾸지 않는다 |
| Auth 관리자 주소 | 서버 `.env.api`에 `BACKEND_AUTH_ADMIN_URL` 설정. API·워커 컨테이너에서 `curl -s -o /dev/null -w '%{http_code}' http://auth:9999/health`(서비스 이름은 `docker compose ps`로 확인) | 200. 비워 두면 탈퇴는 되지만 `authBlockStatus: "pending"`으로 남는다 |
| service_role 키 | 키를 따로 넣지 않으면 `/app/.env`(Supabase `.env`)의 `SERVICE_ROLE_KEY`를 쓴다 | 키 값을 로그·채팅·문서에 남기지 않는다 |

## 1. 앱 연동 호출 순서

```
signUp / signInWithPassword (Supabase Auth SDK)
→ GET  /api/v1/me                          프로필 초기화
→ POST /api/v1/route-searches              202 searchId
→ GET  /api/v1/route-searches/{searchId}   status가 processing이 아닐 때까지
→ POST /api/v1/journeys                    201 journeyId, transferResults[](환승별 고정 예측)
→ WS   /api/v1/journeys/{id}/stream        안내 수신(끊기면 재접속 또는 GET 상세)
→ PUT  /api/v1/journeys/{id}/location      주기적 위치
→ POST /api/v1/journeys/{id}/events        board → alight → (환승) board → alight → completed
→ POST /api/v1/journeys/{id}/events        transfer_result(환승마다, 이동 중 하차 뒤 또는 종료 뒤)
→ POST /api/v1/journeys/{id}/events        correction(결과를 바꿀 때, 가장 최근 응답의 eventId 대상)
→ GET  /api/v1/journeys?status=completed   기록 목록(nextCursor로 다음 페이지)
→ GET  /api/v1/journeys/{id}               기록 상세(검색이 정리돼도 selectedRoute 복원)
→ DELETE /api/v1/journeys/{id}             204 기록 삭제(종료된 이동만)
→ DELETE /api/v1/me                        202 탈퇴 접수(마지막)
```

앱 재실행 시: `GET /api/v1/journeys?status=active` → 있으면 상세 조회·스트림 재접속. 이벤트는 직전 응답의 `stateVersion`과 새 `Idempotency-Key`로
보내고, 응답을 못 받았으면 **같은 키·같은 본문**으로 재전송한다(같은 결과, `replayed: true`).

## 2. 수동 검증 항목

각 항목에 수행 일시·환경(서버/로컬)·계정(시험 계정 식별용 별칭)·요청/응답 요약·DB 확인 결과를 남긴다.

### M-1. 배포 환경 기동·재시작 후 상태 보존
1. 배포 서버에서 활성 이동 1건을 만든다(실제 토큰).
2. `docker compose -f compose.server.yaml restart api`, 이어서 `restart worker`.
3. 재시작 전후 `GET /api/v1/journeys/{id}`와 DB `state_version`, `next_recalc_at`, `guidance->>'base_state_version'`을 비교한다.

기대: 이동은 active 그대로이고 `next_recalc_at`이 유지된다. 워커가 다시 계산하면 `resultVersion`이 오른다. 재시작 중 보낸 이벤트는 실패하거나
같은 키 재전송으로 한 번만 반영된다.

### M-2. 실제 Supabase 토큰으로 기록 조회·삭제
1. 실제 로그인 토큰으로 이동 2건 이상을 완료·취소한다.
2. `GET /api/v1/journeys`, `?status=completed`, `?status=cancelled`, 상세를 조회한다. 다른 시험 계정 토큰으로 같은 ID를 조회한다.
3. 하나를 `DELETE`하고 목록·상세·`GET /api/v1/route-searches/{searchId}`를 확인한다. 진행 중 이동에 `DELETE`를 보낸다.

기대: 본인 기록만 시작 시각 내림차순, 다른 계정은 404, 삭제 후 204·목록에서 사라지고 시작 검색도 404(다른 이동이 참조하면 남음),
진행 중 이동은 409 `JOURNEY_ACTIVE`. DB의 `api_fetches`·`transit_observations`·`transfer_samples` 행 수는 삭제 전후 같다.

### M-3. 대전 실제 구간 주행 — 환승 결과와 당시 예측 대조
1. 환승 1회 이상인 실제 경로로 이동을 시작하고 `transferResults[]`의 `prediction`(확률·여유·연결 상태·`fixedAt`)을 기록한다.
2. 실제로 승하차하며 `board`/`alight`를 보낸다(차량번호를 알면 `vehicleId`).
3. 각 환승 직후 실제 결과로 `transfer_result`를 보낸다. 계획과 다른 버스를 탔으면 `actualConnection`에 `nextRouteId` 또는 `usedPlanned: false`.
4. 도착 뒤 `completed`, 상세의 `transferResults[].evaluationStatus`를 확인한다. 하나는 일부러 `unknown`으로 보낸 뒤 correction으로 고쳐 본다.

기대: 결과의 `prediction.fixedAt`은 이동 시작 시각이고 결과 입력으로 바뀌지 않는다. 계획대로 환승했으면 `counted`, 다른 연결은
`excluded`, `unknown`·미응답은 집계되지 않는다. 사용자 결과가 `transfer_samples.time_feasible`을 바꾸지 않는다(사후 판정은 워커 관측 기준).
현재 실데이터에서 순위 없는(`data_limited`) 후보로 시작하면 예측 확률·여유가 null일 수 있다 — 이때는 대조 자료 없음으로 기록하고
**예측 정확도 검증 완료로 표시하지 않는다**.

### M-4. 위치 미수신·오래된 위치·백그라운드
1. 이동 중 앱을 백그라운드로 보내거나 위치 권한을 끈 채 5분 이상 둔다.
2. 상세 `guidance.reasonCodes`, DB `lifecycle_status`·`next_recalc_at`·`location_observed_at`을 확인한다. 다시 켜서 위치를 보낸다.

기대: 이동은 active·재평가 예약 유지, `location_not_received`/오래된 위치 사유가 붙고, 새 위치가 오면 즉시 재평가가 예약된다.
이 단계에서 결과 입력·기록 조회는 위치와 무관하게 동작한다.

### M-5. WebSocket 재접속
1. 스트림 연결 중 비행기 모드 전환·Wi-Fi↔LTE 전환을 한다.
2. 재접속 직후 첫 메시지의 `stateVersion`·`resultVersion`을 상세 API와 비교한다.

기대: 끊긴 동안에도 워커가 계산을 계속하고(`connectionState: disconnected`), 재접속하면 최신 상태가 한 번에 온다.

### M-6. 회원탈퇴 — 실제 Supabase Auth 차단
1. 시험 계정으로 활성 이동을 만든 뒤 스트림을 연결한 상태에서 `DELETE /api/v1/me`.
2. 응답 `authBlockStatus`, 스트림 종료 코드, 같은 토큰으로 `GET /api/v1/me`·`GET /api/v1/journeys`·WS 재연결을 확인한다.
3. 앱에서 같은 이메일로 다시 로그인한다. Studio(Authentication → Users)에서 사용자가 남아 있고 차단(banned until) 표시가 있는지 본다.
4. DB `app.profiles`의 `deleted_at`·`auth_blocked_at`, 이동·검색·이벤트 행이 남아 있는지 확인한다.

기대: 202·`displayUserId`가 `<UUID>_deleted`, 스트림 4403, API 403 `ACCOUNT_DELETED`, WS 4403, 새 로그인 실패(정확한 오류 문구를 기록),
Auth 사용자·이메일·UUID 유지, `auth_blocked_at ≥ deleted_at`, 활성 이동은 `cancelled`(시스템 이벤트 `account_deleted`).
이미 발급된 액세스 토큰은 만료 전까지 Supabase 자체에는 유효할 수 있으나 이 서버의 API는 모두 거부한다.

### M-7. Auth 관리자 차단 실패와 재시도
운영 Auth를 멈추지 않는다. 스테이징이나 잠시 잘못된 `BACKEND_AUTH_ADMIN_URL`(예: 존재하지 않는 포트)로 API·워커를 띄워 시험한다.
1. 시험 계정 `DELETE /api/v1/me` → `authBlockStatus: "pending"`, DB `auth_blocked_at` NULL, API 403 확인.
2. 주소를 바로잡고 워커를 재시작한다. `BACKEND_AUTH_BLOCK_RETRY_INTERVAL_SECONDS` 이내에 워커 로그 `Auth 차단 재시도`를 확인한다.

기대: 재시작 뒤 첫 작업 주기에 재시도해 `auth_blocked_at`이 기록되고 로그인이 막힌다. 로그에 키·이메일이 없다.

### M-8. 중복·동시 요청
1. 앱에서 탑승·결과 버튼을 빠르게 두 번 누른다(같은 키 재전송)와 다른 키로 같은 환승 결과를 두 번 보낸다.
2. 두 기기(또는 두 탭)에서 같은 결과의 correction을 동시에 보낸다.

기대: 같은 키는 한 건(`replayed: true`), 다른 키의 두 번째 결과는 409 `TRANSFER_RESULT_EXISTS`, 동시 correction은 하나만 200이고
나머지는 409 `CORRECTION_TARGET_SUPERSEDED`. 상세에서 환승마다 유효 결과는 하나다.

### M-9. 외부 API 실패·응답 누락
TAGO·대전시·TMAP 키를 잠시 비우거나 호출 한도에 걸린 상태에서 이동·기록 기능을 확인한다.

기대: 안내는 제한 사유와 함께 계산되거나 대기(`guidanceStatus: pending`)지만, 이벤트·결과 입력·기록 조회/삭제·탈퇴는 외부 API와 무관하게 동작한다.

## 3. 보류 항목 (Phase 5·7 이후)

| 항목 | 이유 |
|---|---|
| 버스 놓침·목적지 변경 → 대안 계산 → 앱 전달 → 수락 → 안내 경로 변경 | Phase 5 미구현. `missed_bus`·`destination_changed`·`route_accepted`·`route_rejected`는 422 `UNSUPPORTED_EVENT_TYPE` |
| 수락 후 새 선택 버전(`selectionVersion` 2 이상)의 예측 스냅샷과 결과 연결 | Phase 5가 선택을 바꿀 때 `evaluation_snapshots()`로 새 버전 스냅샷을 고정해야 한다. 결과 API는 이미 선택 버전으로 구분한다 |
| 재평가 검색(`source=worker`)이 있는 실제 이동의 기록 삭제 | 삭제 규칙은 DB에 직접 넣은 재평가 검색으로만 시험했다. Phase 5가 실제로 만든 검색·제안 후보로 다시 확인한다 |
| 자동 탑승 식별·하차/환승 알림 | Phase 7 |
