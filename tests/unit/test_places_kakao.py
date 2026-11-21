"""카카오 로컬 API 장소 검색 어댑터 단위 시험(네트워크 없음, httpx.MockTransport)."""

import logging
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.dependencies import active_user
from app.core.security import AuthUser
from app.integrations import places_api

KEY = "unit-test-kakao-key-not-real"
RECT = (127.2466, 36.1765, 127.5628, 36.5003)

PLACE_DOC = {"id": "8118221", "place_name": "대전역", "category_name": "교통,수송 > 기차,철도 > 기차역",
             "category_group_code": "", "phone": "1544-7788", "address_name": "대전 동구 정동 1-1",
             "road_address_name": "대전 동구 중앙로 215", "x": "127.43436", "y": "36.33227",
             "place_url": "http://place.map.kakao.com/8118221", "distance": ""}
ADDRESS_DOC = {"address_name": "대전 서구 둔산로 100", "address_type": "ROAD_ADDR", "x": "127.3849", "y": "36.3504",
               "address": {"address_name": "대전 서구 둔산동 1420", "x": "127.3849", "y": "36.3504"},
               "road_address": {"address_name": "대전 서구 둔산로 100", "building_name": "대전광역시청",
                                "x": "127.3849", "y": "36.3504"}}
REGION_DOC = {"address_name": "서울 중구 태평로1가", "address_type": "REGION", "x": "126.9779", "y": "37.5663",
              "address": {"address_name": "서울 중구 태평로1가"}, "road_address": None}


def documents(*docs):
    return {"meta": {"total_count": len(docs), "pageable_count": len(docs), "is_end": True}, "documents": list(docs)}


class KakaoProviderTest(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.requests = []

    def provider(self, keyword=None, address=None, *, rect=None):
        """경로별 응답: (HTTP 상태, 본문) 또는 예외. 본문이 dict면 JSON, str이면 그대로 보낸다."""
        answers = {places_api.KAKAO_KEYWORD_PATH: keyword or (200, documents()),
                   places_api.KAKAO_ADDRESS_PATH: address or (200, documents())}

        def handler(request):
            self.requests.append(request)
            answer = answers[request.url.path]
            if isinstance(answer, Exception):
                raise answer
            status, body = answer
            return httpx.Response(status, json=body) if isinstance(body, dict) else httpx.Response(status, text=body)

        provider = places_api.KakaoPlacesProvider(KEY, rect=rect, transport=httpx.MockTransport(handler))
        self.addCleanup(provider.close)
        return provider

    def test_merges_addresses_before_places_and_maps_fields(self):
        provider = self.provider((200, documents(PLACE_DOC)), (200, documents(ADDRESS_DOC)))
        self.assertEqual(provider.search("대전"), [
            places_api.PlaceItem("대전 서구 둔산로 100", "대전 서구 둔산로 100", 36.3504, 127.3849, "kakao", None),
            places_api.PlaceItem("대전역", "대전 동구 중앙로 215", 36.33227, 127.43436, "kakao", "8118221"),
        ])
        by_path = {r.url.path: r for r in self.requests}
        self.assertEqual(set(by_path), {places_api.KAKAO_KEYWORD_PATH, places_api.KAKAO_ADDRESS_PATH})
        for request in self.requests:
            self.assertEqual((request.method, request.url.host), ("GET", "dapi.kakao.com"))
            self.assertEqual(request.headers["Authorization"], f"KakaoAK {KEY}")
            self.assertEqual(request.url.params["query"], "대전")
        self.assertEqual(by_path[places_api.KAKAO_KEYWORD_PATH].url.params["size"], "15")
        self.assertNotIn("rect", by_path[places_api.KAKAO_KEYWORD_PATH].url.params)

    def test_address_fallbacks(self):
        lot_only = {"place_name": "이름만", "id": "1", "address_name": "대전 동구 정동 1-1", "road_address_name": "",
                    "x": "127.4", "y": "36.3"}
        no_address = {"place_name": "주소 없음", "id": "", "address_name": "", "road_address_name": "",
                      "x": "127.4", "y": "36.3"}
        items = self.provider((200, documents(lot_only, no_address)), (200, documents(REGION_DOC))).search("x")
        self.assertEqual([(i.name, i.address, i.external_id) for i in items], [
            ("서울 중구 태평로1가", "서울 중구 태평로1가", None),
            ("이름만", "대전 동구 정동 1-1", "1"),
            ("주소 없음", None, None),
        ])

    def test_rect_limits_keyword_request_and_filters_addresses(self):
        provider = self.provider((200, documents(PLACE_DOC)), (200, documents(REGION_DOC, ADDRESS_DOC)), rect=RECT)
        self.assertEqual([i.name for i in provider.search("시청")], ["대전 서구 둔산로 100", "대전역"])
        keyword = next(r for r in self.requests if r.url.path == places_api.KAKAO_KEYWORD_PATH)
        self.assertEqual(keyword.url.params["rect"], "127.2466,36.1765,127.5628,36.5003")

    def test_empty_result_is_empty_list(self):
        self.assertEqual(self.provider().search("없는 장소"), [])

    def test_query_longer_than_100_characters_is_rejected_without_calls(self):
        provider = self.provider()
        self.assertEqual(provider.search("가" * 100), [])
        self.assertEqual(len(self.requests), 2)
        with self.assertRaises(places_api.PlacesInvalidQuery) as ctx:
            provider.search("가" * 101)
        self.assertEqual((ctx.exception.status_code, ctx.exception.code), (422, "VALIDATION_ERROR"))
        self.assertEqual(len(self.requests), 2, "길이 초과 검색어로는 카카오를 호출하지 않는다")

    def test_error_mapping(self):
        cases = [
            ((429, {"errorType": "RequestThrottled", "message": "API limit has been exceeded."}),
             places_api.PlacesRateLimited),
            ((400, {"code": -10, "msg": "quota exceeded"}), places_api.PlacesRateLimited),
            ((401, {"errorType": "AccessDeniedError", "message": "wrong appKey(invalid-key) format"}),
             places_api.PlacesNotConfigured),
            ((403, {"errorType": "NotAuthorizedError", "message": "App disabled OPEN_MAP_AND_LOCAL service."}),
             places_api.PlacesNotConfigured),
            ((503, {"code": -9798, "msg": "maintenance"}), places_api.PlacesUnavailable),
            ((500, "<html>error</html>"), places_api.PlacesUnavailable),
            ((400, {"code": -7, "msg": "maintenance"}), places_api.PlacesUnavailable),
            ((400, {"errorType": "ValidationError", "message": "Request validation is failed",
                    "details": [{"field": "query.size", "error": "should be at most 15"}]}),
             places_api.PlacesUpstreamError),
            ((400, {"code": -2, "msg": "Max (query) length 100"}), places_api.PlacesUpstreamError),
            ((200, "not json"), places_api.PlacesUpstreamError),
            ((200, {"meta": {}}), places_api.PlacesUpstreamError),
            ((200, {"documents": ["text"]}), places_api.PlacesUpstreamError),
            (httpx.ReadTimeout("slow"), places_api.PlacesTimeout),
            (httpx.ConnectError(f"refused {KEY}"), places_api.PlacesUnavailable),
        ]
        for answer, expected in cases:
            for target in ("keyword", "address"):
                with self.subTest(answer=answer, target=target):
                    with self.assertRaises(places_api.PlacesError) as ctx:
                        self.provider(**{target: answer}).search("대전역")
                    self.assertIs(type(ctx.exception), expected)
                    self.assertNotIn(KEY, ctx.exception.message)

    def test_unreadable_coordinates_are_upstream_error(self):
        for target, doc in (("keyword", {**PLACE_DOC, "x": ""}), ("address", {**ADDRESS_DOC, "y": None}),
                            ("keyword", {**PLACE_DOC, "place_name": ""})):
            with self.subTest(target=target, doc=doc), self.assertRaises(places_api.PlacesUpstreamError):
                places_api.validate_items(self.provider(**{target: (200, documents(doc))}, rect=RECT).search("x"))


class KakaoSettingsTest(unittest.TestCase):
    def settings(self, **values):
        return Settings(_env_file=None, **values)

    def test_provider_from_settings(self):
        provider = places_api.provider_from_settings(self.settings(
            places_provider=" Kakao ", KAKAO_REST_API_KEY=KEY, places_search_rect="127.2466,36.1765,127.5628,36.5003"))
        self.addCleanup(provider.close)
        self.assertIsInstance(provider, places_api.KakaoPlacesProvider)
        self.assertEqual(provider._rect, RECT)

    def test_missing_key_or_bad_rect_is_not_configured(self):
        for values in ({"places_provider": "kakao"}, {"places_provider": "kakao", "KAKAO_REST_API_KEY": "  "},
                       {"places_provider": "naver", "KAKAO_REST_API_KEY": KEY}, {"KAKAO_REST_API_KEY": KEY}):
            with self.subTest(values=values), self.assertRaises(places_api.PlacesNotConfigured):
                places_api.provider_from_settings(self.settings(**values))
        for rect in ("127.2,36.1,127.5", "a,b,c,d", "127.5,36.1,127.2,36.5", "127.2,36.5,127.5,36.1",
                     "127.2,36.1,127.5,91", "nan,36.1,127.5,36.5"):
            with self.subTest(rect=rect), self.assertRaises(places_api.PlacesNotConfigured):
                places_api.provider_from_settings(self.settings(places_provider="kakao", KAKAO_REST_API_KEY=KEY,
                                                                places_search_rect=rect))

    def test_key_is_hidden_in_settings_repr(self):
        self.assertNotIn(KEY, repr(self.settings(KAKAO_REST_API_KEY=KEY)))


class PlacesEndpointProviderCacheTest(unittest.TestCase):
    """설정으로 만든 제공처는 app.state에 한 번만 만들어 다시 쓰고, 미설정이면 저장하지 않는다."""

    def setUp(self):
        from app import main

        self.app = main.app
        self.app.dependency_overrides[active_user] = lambda: AuthUser(uuid4(), "authenticated",
                                                                      datetime.now(timezone.utc) + timedelta(hours=1))
        self.addCleanup(self.app.dependency_overrides.clear)
        saved = (self.app.state.settings, self.app.state.places_provider, dict(places_api.PROVIDERS))
        self.addCleanup(lambda: (setattr(self.app.state, "settings", saved[0]),
                                 setattr(self.app.state, "places_provider", saved[1]),
                                 places_api.PROVIDERS.update(saved[2])))
        self.app.state.places_provider = None
        self.client = TestClient(self.app)

    def test_provider_is_created_once_and_serves_requests(self):
        created = []

        def factory(settings):
            transport = httpx.MockTransport(lambda request: httpx.Response(200, json=documents(
                *([PLACE_DOC] if request.url.path == places_api.KAKAO_KEYWORD_PATH else []))))
            created.append(places_api.KakaoPlacesProvider(settings.kakao_rest_api_key.get_secret_value(),
                                                          transport=transport))
            return created[-1]

        places_api.PROVIDERS[places_api.KAKAO] = factory
        self.app.state.settings = Settings(_env_file=None, places_provider="kakao", KAKAO_REST_API_KEY=KEY)
        for _ in range(2):
            r = self.client.get("/api/v1/places/search", params={"q": " 대전역 "})
            self.assertEqual((r.status_code, r.json()), (200, {"items": [{
                "name": "대전역", "address": "대전 동구 중앙로 215", "latitude": 36.33227, "longitude": 127.43436,
                "provider": "kakao", "externalId": "8118221"}]}))
        self.assertEqual(len(created), 1)
        self.assertIs(self.app.state.places_provider, created[0])
        created[0].close()

    def test_too_long_query_is_422(self):
        self.app.state.places_provider = places_api.KakaoPlacesProvider(
            KEY, transport=httpx.MockTransport(lambda request: httpx.Response(200, json=documents())))
        self.addCleanup(self.app.state.places_provider.close)
        r = self.client.get("/api/v1/places/search", params={"q": "가" * 101})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (422, "VALIDATION_ERROR"))

    def test_not_configured_is_not_cached(self):
        self.app.state.settings = Settings(_env_file=None, places_provider="kakao")
        r = self.client.get("/api/v1/places/search", params={"q": "대전역"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (503, "PLACES_NOT_CONFIGURED"))
        self.assertIsNone(self.app.state.places_provider)


if __name__ == "__main__":
    unittest.main()
