"""대전시 버스 도착정보 확인 URL(/dev/daejeon) 단위 시험. 실제 외부 호출 없이 httpx.MockTransport로 응답을 흉내 낸다.

확인 URL은 제공기관 응답(본문 바이트·HTTP 상태·Content-Type)을 그대로 돌려준다. 본문은 형식 처리만 검증하는 가상 입력이며,
필드 이름(CAR_REG_NO 등)은 Notion Phase 3 - Algorithms 1.2.2 ①의 활용 후보 표기를 썼다.
"""

import unittest

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.devtools.daejeon_router import router

FAKE_KEY = "unit-test-daejeon-key+/=="

XML_OK = ("<?xml version=\"1.0\" encoding=\"EUC-KR\"?><ServiceResult><msgHeader><headerCd>0</headerCd>"
          "<headerMsg>정상</headerMsg></msgHeader><msgBody><itemList><ROUTE_NO>102</ROUTE_NO>"
          "<CAR_REG_NO>대전75자0001</CAR_REG_NO><EXTIME_SEC>180</EXTIME_SEC></itemList></msgBody></ServiceResult>")

XML_GATEWAY = ("<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>SERVICE ERROR serviceKey=" + FAKE_KEY + "</errMsg>"
               "<returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</returnAuthMsg><returnReasonCode>30</returnReasonCode>"
               "</cmmMsgHeader></OpenAPI_ServiceResponse>")


class DaejeonDevtoolsTest(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(router)
        self.app.state.settings = Settings(_env_file=None, DAEJEON_BUS_SERVICE_KEY=FAKE_KEY)
        self.requests: list[httpx.Request] = []
        self.client = TestClient(self.app)

    def serve(self, handler):
        def record(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return handler(request)
        self.app.state.daejeon_bus_transport = httpx.MockTransport(record)

    def get(self, stop="8001793"):
        return self.client.get(f"/dev/daejeon/stops/{stop}/arrivals")

    def assert_no_key(self, response):
        for variant in (FAKE_KEY, "unit-test-daejeon-key%2B"):
            self.assertNotIn(variant.encode(), response.content)

    def test_body_status_and_content_type_pass_through_unchanged(self):
        raw = XML_OK.encode("euc-kr")
        self.serve(lambda r: httpx.Response(200, content=raw, headers={"content-type": "text/xml;charset=EUC-KR"}))
        r = self.get()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, raw, "본문 바이트를 다시 인코딩하지 않음")
        self.assertEqual(r.headers["content-type"], "text/xml;charset=EUC-KR")
        self.assertTrue(r.headers["x-upstream-requested-at"] and r.headers["x-upstream-received-at"])
        self.assertEqual(len(self.requests), 1, "요청당 외부 호출 1회")
        sent = self.requests[0]
        self.assertEqual(sent.url.path, "/6300000/arrive/getArrInfoByStopID")
        self.assertEqual((sent.url.params["BusStopID"], sent.url.params["serviceKey"]), ("8001793", FAKE_KEY))

    def test_provider_errors_pass_through_with_key_masked(self):
        self.serve(lambda r: httpx.Response(401, text=XML_GATEWAY, headers={"content-type": "text/xml"}))
        r = self.get()
        self.assertEqual(r.status_code, 401, "제공기관 HTTP 상태 그대로")
        self.assertIn(b"<returnReasonCode>30</returnReasonCode>", r.content)
        self.assertIn(b"serviceKey=***", r.content)
        self.assert_no_key(r)
        self.serve(lambda r: httpx.Response(200, json={"response": {"header": {"resultCode": "00"}}}))
        r = self.get()
        self.assertEqual((r.headers["content-type"], r.json()), ("application/json",
                                                                 {"response": {"header": {"resultCode": "00"}}}))

    def test_no_response_and_config_errors(self):
        def timeout(request):
            raise httpx.ReadTimeout("slow", request=request)
        self.serve(timeout)
        r = self.get()
        self.assertEqual((r.status_code, r.json()["error"]["category"]), (504, "timeout"))
        self.serve(lambda r: httpx.Response(200, text="ok"))
        self.app.state.settings = Settings(_env_file=None, TAGO_SERVICE_KEY=FAKE_KEY)
        self.assertEqual(self.get().status_code, 200)
        self.assertEqual(self.requests[-1].url.params["serviceKey"], FAKE_KEY, "비우면 TAGO 키 사용")
        self.app.state.settings = Settings(_env_file=None)
        self.assertEqual(self.get().status_code, 503)
        self.app.state.settings = Settings(_env_file=None, DAEJEON_BUS_SERVICE_KEY="abc%2Bdef")
        self.assertEqual(self.get().status_code, 503, "URL 인코딩된 키는 거부")


if __name__ == "__main__":
    unittest.main()
