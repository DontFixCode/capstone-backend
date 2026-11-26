"""대전시 노선 위치 조회(getBusPosByRtid) 클라이언트·해석 단위 시험(DB·네트워크 없음).

XML_POSITIONS는 2026-11-27 06:07 KST 실응답(608번, busRouteId 30300130, 차량 4대), XML_NO_BUSES는 같은 날 05:32 KST
실응답(운행 차량 없음)의 형식이다.
"""

import unittest

import httpx
from pydantic import ValidationError

from app.core.config import Settings
from app.integrations.daejeon_bus_api import DaejeonBusClient, RawResponse, parse_arrivals, parse_positions

FAKE_KEY = "unit-test-daejeon-key+/=="

XML_POSITIONS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?><ServiceResult><msgHeader>
<currentPage>1</currentPage><headerCd>0</headerCd><headerMsg>정상적으로 처리되었습니다.</headerMsg><itemCnt>4</itemCnt>
<itemPageCnt>1</itemPageCnt></msgHeader><msgBody>
<itemList><ARR_TIME>20261127060612</ARR_TIME><BUS_NODE_ID>8002487</BUS_NODE_ID><BUS_STOP_ID>11360</BUS_STOP_ID><DIR>0</DIR>
<EVT_CD>1</EVT_CD><GPS_LATI>36.335552</GPS_LATI><GPS_LONG>127.449017</GPS_LONG><PLATE_NO>대전75자2719</PLATE_NO>
<ROUTE_CD>30300130</ROUTE_CD><STRE_DT>20261127060612</STRE_DT><TOTAL_DIST>9004</TOTAL_DIST><ud_type>0</ud_type></itemList>
<itemList><ARR_TIME>20261127060602</ARR_TIME><BUS_NODE_ID>8001940</BUS_NODE_ID><BUS_STOP_ID>21840</BUS_STOP_ID><DIR>0</DIR>
<EVT_CD>1</EVT_CD><GPS_LATI>36.318982</GPS_LATI><GPS_LONG>127.400012</GPS_LONG><PLATE_NO>대전75자2113</PLATE_NO>
<ROUTE_CD>30300130</ROUTE_CD><STRE_DT>20261127060605</STRE_DT><TOTAL_DIST>3329</TOTAL_DIST><ud_type>0</ud_type></itemList>
<itemList><ARR_TIME>20261127060522</ARR_TIME><BUS_NODE_ID>8001042</BUS_NODE_ID><BUS_STOP_ID>10390</BUS_STOP_ID><DIR>1</DIR>
<EVT_CD>1</EVT_CD><GPS_LATI>36.309898</GPS_LATI><GPS_LONG>127.455337</GPS_LONG><PLATE_NO>대전75자2110</PLATE_NO>
<ROUTE_CD>30300130</ROUTE_CD><STRE_DT>20261127060525</STRE_DT><TOTAL_DIST>19992</TOTAL_DIST><ud_type>1</ud_type></itemList>
<itemList><ARR_TIME>20261127060550</ARR_TIME><BUS_NODE_ID>8002358</BUS_NODE_ID><BUS_STOP_ID>10060</BUS_STOP_ID><DIR>1</DIR>
<EVT_CD>1</EVT_CD><GPS_LATI>36.327906</GPS_LATI><GPS_LONG>127.434694</GPS_LONG><PLATE_NO>대전75자2146</PLATE_NO>
<ROUTE_CD>30300130</ROUTE_CD><STRE_DT>20261127060551</STRE_DT><TOTAL_DIST>28425</TOTAL_DIST><ud_type>1</ud_type></itemList>
</msgBody></ServiceResult>"""

XML_NO_BUSES = ("<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?><ServiceResult><msgHeader>"
                "<currentPage>1</currentPage><headerCd>4</headerCd><headerMsg>결과가 없습니다.</headerMsg>"
                "<itemCnt>0</itemCnt><itemPageCnt>1</itemPageCnt></msgHeader><msgBody/></ServiceResult>")

XML_GATEWAY = ("<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>SERVICE ERROR serviceKey=" + FAKE_KEY + "</errMsg>"
               "<returnAuthMsg>LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR</returnAuthMsg>"
               "<returnReasonCode>22</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>")

PARAMS = {"busRouteId": "30300130"}


def raw(body: str, status: int = 200) -> RawResponse:
    return RawResponse(requested_at="2026-11-26T21:07:49+00:00", received_at="2026-11-26T21:07:50+00:00",
                       http_status=status, content_type="application/xml;charset=UTF-8", body=body.encode("utf-8"))


class ParsePositionsTest(unittest.TestCase):
    def test_real_response_shape(self):
        page = parse_positions(raw(XML_POSITIONS), PARAMS)
        self.assertEqual((page.status, page.provider_code, page.item_count, page.error), ("success", "0", 4, None))
        self.assertEqual(len(page.items), 4, "노선당 요청 1회에 운행 중인 차량이 모두 온다")
        first = page.items[0]
        self.assertEqual(set(first), {"ARR_TIME", "BUS_NODE_ID", "BUS_STOP_ID", "DIR", "EVT_CD", "GPS_LATI", "GPS_LONG",
                                      "PLATE_NO", "ROUTE_CD", "STRE_DT", "TOTAL_DIST", "ud_type"})
        self.assertEqual((first["PLATE_NO"], first["BUS_NODE_ID"], first["ARR_TIME"], first["EVT_CD"]),
                         ("대전75자2719", "8002487", "20261127060612", "1"))
        self.assertEqual(page.params, PARAMS)

    def test_no_running_bus_is_an_empty_result_not_an_error(self):
        page = parse_positions(raw(XML_NO_BUSES), PARAMS)
        self.assertEqual((page.status, page.provider_code, page.item_count, page.items, page.error),
                         ("empty", "4", 0, [], None))
        arrivals = parse_arrivals(raw(XML_NO_BUSES), {"BusStopID": "8001488"})
        self.assertEqual((arrivals.status, arrivals.provider_code), ("error", "4"), "도착정보 해석은 그대로 둔다")
        with_items = XML_POSITIONS.replace("<headerCd>0</headerCd>", "<headerCd>4</headerCd>")
        self.assertEqual(parse_positions(raw(with_items), PARAMS).status, "error", "항목이 있는 결과 없음 코드는 오류")

    def test_partial_and_errors_follow_the_arrivals_rules(self):
        partial = XML_POSITIONS.replace("<itemPageCnt>1</itemPageCnt>", "<itemPageCnt>2</itemPageCnt>")
        self.assertEqual(parse_positions(raw(partial), PARAMS).status, "partial", "남은 페이지가 있으면 공개하지 않음")
        failed = XML_POSITIONS.replace("<headerCd>0</headerCd>", "<headerCd>9</headerCd>")
        page = parse_positions(raw(failed), PARAMS)
        self.assertEqual((page.status, page.provider_code), ("error", "9"))
        gateway = parse_positions(raw(XML_GATEWAY, 429), PARAMS)
        self.assertEqual((gateway.status, gateway.provider_code), ("error", "22"))
        self.assertTrue(gateway.error.startswith("gateway"))
        self.assertTrue(parse_positions(raw("oops", 500), PARAMS).error.startswith("http"))
        self.assertTrue(parse_positions(raw("not xml"), PARAMS).error.startswith("parse"))
        timeout = RawResponse(requested_at="t", error={"category": "timeout", "message": "slow"})
        self.assertEqual(parse_positions(timeout, PARAMS).status, "timeout")


class PositionsClientTest(unittest.TestCase):
    def client(self, handler):
        self.requests: list[httpx.Request] = []

        def record(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return handler(request)
        return DaejeonBusClient(FAKE_KEY, transport=httpx.MockTransport(record))

    def test_request_goes_to_the_position_service_with_the_route_code(self):
        with self.client(lambda r: httpx.Response(200, text=XML_POSITIONS,
                                                  headers={"content-type": "application/xml;charset=UTF-8"})) as client:
            page = client.fetch_route_positions("30300130")
        self.assertEqual((page.status, len(page.items)), ("success", 4))
        self.assertEqual(len(self.requests), 1, "요청 1회, 재시도 없음")
        sent = self.requests[0]
        self.assertEqual(sent.url.path, "/6300000/busposinfo/getBusPosByRtid")
        self.assertEqual((sent.url.params["busRouteId"], sent.url.params["serviceKey"]), ("30300130", FAKE_KEY))
        self.assertNotIn("serviceKey", page.params, "호출 기록에 남는 요청 조건에는 인증키가 없다")
        self.assertTrue(page.requested_at and page.received_at)

    def test_arrivals_still_use_the_arrival_service(self):
        with self.client(lambda r: httpx.Response(200, text=XML_NO_BUSES)) as client:
            client.fetch_arrivals("8001488")
        self.assertEqual(self.requests[0].url.path, "/6300000/arrive/getArrInfoByStopID")

    def test_key_is_masked_in_error_bodies_and_timeouts_are_reported(self):
        with self.client(lambda r: httpx.Response(429, text=XML_GATEWAY)) as client:
            page = client.fetch_route_positions("30300130")
            raw_response = client.positions_by_route("30300130")
        self.assertEqual((page.status, page.provider_code), ("error", "22"))
        for text in (page.body, page.error, raw_response.body.decode("utf-8")):
            self.assertNotIn(FAKE_KEY, text)
            self.assertNotIn("unit-test-daejeon-key%2B", text)
        self.assertIn("serviceKey=***", page.body)

        def timeout(request):
            raise httpx.ReadTimeout("slow", request=request)
        with self.client(timeout) as client:
            page = client.fetch_route_positions("30300130")
        self.assertEqual((page.status, page.http_status, page.body), ("timeout", None, None))


class ArrTimeMeaningSettingTest(unittest.TestCase):
    def test_default_is_unverified(self):
        self.assertEqual(Settings(_env_file=None).daejeon_position_arr_time_meaning, "unverified")
        for value in ("arrival", "departure"):
            self.assertEqual(Settings(_env_file=None, daejeon_position_arr_time_meaning=value)
                             .daejeon_position_arr_time_meaning, value)
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, daejeon_position_arr_time_meaning="guess")


if __name__ == "__main__":
    unittest.main()
