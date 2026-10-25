"""TAGO 클라이언트의 응답 해석·페이지 순회·재시도·요청 예산·인증키 보호를 검증한다.

모든 응답은 테스트에서 만든 최소한의 합성 데이터이며 실제 TAGO 수집 결과가 아니다.
"""

import json
import logging
import unittest
from urllib.parse import quote, quote_plus

import httpx

from app.integrations import bus_api
from app.integrations.bus_api import TagoClient, TagoConfigError, field_state, text_value

# 합성 인증키(실제 키 아님). +, /, = 를 넣어 인코딩 횟수를 확인한다.
FAKE_KEY = "Synthetic+Key/For==Tests0123456789"
KEY_FORMS = (FAKE_KEY, quote(FAKE_KEY, safe=""), quote_plus(FAKE_KEY))
AUTO = object()


def synthetic_body(items, *, total=None, page=1, rows=10, code="00", container=AUTO):
    """합성 TAGO JSON 본문. items가 1개면 item을 단일 객체로, 없으면 items를 빈 문자열로 만든다."""
    if container is AUTO:
        if not items:
            container = ""
        elif len(items) == 1:
            container = {"item": items[0]}
        else:
            container = {"item": items}
    body = {"items": container, "numOfRows": rows, "pageNo": page, "totalCount": len(items) if total is None else total}
    return {"response": {"header": {"resultCode": code, "resultMsg": "NORMAL SERVICE."}, "body": body}}


def gateway_xml(code, message):
    """포털 게이트웨이 오류 형식의 합성 XML."""
    return (
        "<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>SERVICE ERROR</errMsg>"
        f"<returnAuthMsg>{message}</returnAuthMsg><returnReasonCode>{code}</returnReasonCode>"
        "</cmmMsgHeader></OpenAPI_ServiceResponse>"
    )


def stations(start, count):
    return [
        {"nodeid": f"SYN{index:04d}", "nodenm": f"합성정류소{index}", "nodeno": 40000 + index,
         "gpslati": 36.3 + index / 1000, "gpslong": 127.3}
        for index in range(start, start + count)
    ]


class Queue:
    """요청을 기록하고 준비한 응답을 순서대로 돌려준다. 호출 가능한 항목은 요청을 받아 실행한다."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        response = self.responses.pop(0)
        return response(request) if callable(response) else response


def make_client(handler, **options):
    sleeps = []
    options = {"request_interval": 0, "retry_backoff": 0, **options}
    client = TagoClient(FAKE_KEY, transport=httpx.MockTransport(handler), sleep=sleeps.append, **options)
    return client, sleeps


def ok(items, **kwargs):
    return httpx.Response(200, json=synthetic_body(items, **kwargs))


class ResponseParsingTest(unittest.TestCase):
    def test_single_item_object_becomes_list_and_ids_stay_strings(self):
        client, _ = make_client(Queue(ok(stations(1, 1))))
        with client:
            result = client.station_list(rows=10, max_pages=1)
        self.assertEqual(result.status, "success")
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0]["nodeno"], 40001, "원본 필드 값은 그대로 보존")
        self.assertEqual(text_value(result.items[0], "nodeno"), "40001")

    def test_multiple_items(self):
        client, _ = make_client(Queue(ok(stations(1, 3))))
        with client:
            result = client.station_list(rows=10)
        self.assertEqual((result.status, len(result.items), result.total_count), ("success", 3, 3))

    def test_normal_empty_results_are_not_errors(self):
        for container in ("", None, {}, {"item": []}, {"item": ""}):
            with self.subTest(container=container):
                client, _ = make_client(Queue(ok([], total=0, container=container)))
                with client:
                    result = client.arrivals("SYN0001")
                self.assertEqual(result.status, "empty")
                self.assertIsNone(result.error)
                self.assertEqual(client.stats[bus_api.ARRIVALS.path]["empty"], 1)

    def test_unparseable_items_structure_is_failure_with_raw_body(self):
        for container in ("unexpected", {"item": "text"}, {"other": 1}, {"item": [1, 2]}):
            with self.subTest(container=container):
                client, _ = make_client(Queue(ok([], total=2, container=container)))
                with client:
                    result = client.arrivals("SYN0001")
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.error.category, "unexpected_response")
                self.assertIsNotNone(result.pages[0].raw)

    def test_provider_result_code_error_is_not_empty_success(self):
        handler = Queue(httpx.Response(200, json=synthetic_body([], code="99")))
        client, _ = make_client(handler)
        with client:
            result = client.route_stops("SYNROUTE")
        self.assertEqual(result.status, "failed")
        self.assertEqual((result.error.category, result.error.code, result.error.source), ("provider", "99", "provider"))
        self.assertEqual(len(handler.requests), 1, "제공기관 오류 99는 재시도하지 않음")

    def test_gateway_xml_errors_are_classified_without_retry(self):
        cases = {"30": "auth", "31": "auth", "20": "auth", "22": "rate_limit", "23": "rate_limit", "10": "bad_request"}
        for code, category in cases.items():
            with self.subTest(code=code):
                handler = Queue(httpx.Response(200, text=gateway_xml(code, "SYNTHETIC_ERROR")))
                client, _ = make_client(handler, max_retries=3)
                with client:
                    result = client.bus_locations("SYNROUTE")
                self.assertEqual((result.error.category, result.error.source), (category, "gateway"))
                self.assertFalse(result.error.retryable)
                self.assertEqual(len(handler.requests), 1)

    def test_http_status_and_non_json_bodies(self):
        cases = [
            (httpx.Response(401, text="Unauthorized"), "auth"),
            (httpx.Response(429, text="Too Many Requests"), "rate_limit"),
            (httpx.Response(404, text="Not Found"), "bad_request"),
            (httpx.Response(200, text="plain text"), "unexpected_response"),
            (httpx.Response(200, text="<response><header><resultCode>00</resultCode></header></response>"),
             "unexpected_response"),
            (httpx.Response(200, json={"unexpected": True}), "unexpected_response"),
        ]
        for response, category in cases:
            with self.subTest(status=response.status_code, category=category):
                client, _ = make_client(Queue(response))
                with client:
                    result = client.route_info("SYNROUTE")
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.error.category, category)

    def test_field_state_distinguishes_missing_blank_and_zero(self):
        item = {"blank": " ", "none": None, "zero_number": 0, "zero_text": "0", "value": 7, "text": "A"}
        expected = {
            "absent": "missing", "blank": "empty", "none": "null", "zero_number": "zero", "zero_text": "zero",
            "value": "present", "text": "present",
        }
        for name, state in expected.items():
            self.assertEqual(field_state(item, name), state, name)
        self.assertEqual(text_value(item, "zero_number"), "0")
        self.assertIsNone(text_value(item, "blank"))


class PagingTest(unittest.TestCase):
    def paged_handler(self, items, rows):
        def respond(request):
            page = int(request.url.params["pageNo"])
            chunk = items[(page - 1) * rows: page * rows]
            return ok(chunk, total=len(items), page=page, rows=rows)
        return respond

    def test_reads_all_pages_by_total_count(self):
        handler = Queue(*[self.paged_handler(stations(1, 25), 10)] * 3)
        client, _ = make_client(handler)
        with client:
            result = client.station_list(rows=10)
        self.assertEqual((result.status, len(result.items), len(result.pages)), ("success", 25, 3))
        self.assertEqual([r.url.params["pageNo"] for r in handler.requests], ["1", "2", "3"])

    def test_page_limit_is_partial(self):
        client, _ = make_client(Queue(self.paged_handler(stations(1, 25), 10)))
        with client:
            result = client.station_list(rows=10, max_pages=1)
        self.assertEqual((result.status, result.stop_reason, len(result.items)), ("partial", "page_limit", 10))

    def test_repeated_page_stops(self):
        page = ok(stations(1, 10), total=30, rows=10)
        client, _ = make_client(Queue(page, ok(stations(1, 10), total=30, page=2, rows=10)))
        with client:
            result = client.station_list(rows=10)
        self.assertEqual((result.status, result.stop_reason, len(result.items)), ("partial", "repeated_page", 10))

    def test_empty_page_before_total_stops(self):
        client, _ = make_client(Queue(ok(stations(1, 10), total=30, rows=10), ok([], total=30, page=2, rows=10)))
        with client:
            result = client.station_list(rows=10)
        self.assertEqual((result.status, result.stop_reason), ("partial", "empty_page_before_total"))

    def test_short_page_before_total_stops(self):
        client, _ = make_client(Queue(ok(stations(1, 5), total=30, rows=10)))
        with client:
            result = client.station_list(rows=10)
        self.assertEqual((result.status, result.stop_reason), ("partial", "short_page"))

    def test_missing_total_count_stops_after_first_page(self):
        body = synthetic_body(stations(1, 10), rows=10)
        del body["response"]["body"]["totalCount"]
        client, _ = make_client(Queue(httpx.Response(200, json=body)))
        with client:
            result = client.station_list(rows=10)
        self.assertEqual((result.status, result.stop_reason), ("partial", "total_count_missing"))

    def test_non_paged_endpoint_sends_no_paging_params(self):
        handler = Queue(ok([{"routeid": "SYNROUTE", "routeno": 5}]))
        client, _ = make_client(handler)
        with client:
            result = client.route_info("SYNROUTE")
        params = handler.requests[0].url.params
        self.assertNotIn("pageNo", params)
        self.assertNotIn("numOfRows", params)
        self.assertEqual(result.status, "success")
        self.assertIsNone(result.rows)


class RetryAndBudgetTest(unittest.TestCase):
    def test_transient_error_retries_then_succeeds(self):
        handler = Queue(httpx.Response(503, text="busy"), ok(stations(1, 1)))
        client, sleeps = make_client(handler, max_retries=2, retry_backoff=1.5)
        with client:
            result = client.station_list(rows=10)
        stats = client.stats[bus_api.STATION_LIST.path]
        self.assertEqual(result.status, "success")
        self.assertEqual((stats["http_attempts"], stats["retries"], stats["succeeded"], stats["failed"]), (2, 1, 1, 0))
        self.assertEqual([attempt.outcome for attempt in result.pages[0].attempts], ["http", "ok"])
        self.assertTrue(sleeps and sleeps[0] > 1.0, "재시도 전 대기")

    def test_timeouts_are_retried_a_limited_number_of_times(self):
        def timeout(request):
            raise httpx.ReadTimeout("timed out", request=request)

        handler = Queue(timeout, timeout, timeout, timeout)
        client, _ = make_client(handler, max_retries=2)
        with client:
            result = client.arrivals("SYN0001")
        self.assertEqual(len(handler.requests), 3)
        self.assertEqual((result.status, result.error.category), ("failed", "timeout"))
        self.assertIsNone(result.pages[0].received_at, "응답을 받지 못한 시도는 수신 시각이 없음")

    def test_network_error_and_gateway_01_are_retryable(self):
        def disconnect(request):
            raise httpx.ConnectError("connection refused", request=request)

        handler = Queue(disconnect, httpx.Response(200, text=gateway_xml("01", "APPLICATION_ERROR")), ok(stations(1, 1)))
        client, _ = make_client(handler, max_retries=2)
        with client:
            result = client.station_list(rows=10)
        self.assertEqual(result.status, "success")
        self.assertEqual([a.outcome for a in result.pages[0].attempts], ["network", "provider", "ok"])

    def test_budget_counts_retries_and_blocks_further_requests(self):
        handler = Queue(httpx.Response(503), httpx.Response(503), httpx.Response(503))
        client, _ = make_client(handler, max_retries=5, max_requests=2)
        with client:
            first = client.route_stops("SYNROUTE")
            second = client.route_stops("SYNROUTE2")
        stats = client.stats[bus_api.ROUTE_STOPS.path]
        self.assertEqual(len(handler.requests), 2)
        self.assertEqual((first.status, first.stop_reason), ("failed", "budget_exhausted"))
        self.assertEqual((second.error.category, second.pages[0].attempts), ("budget", []))
        self.assertEqual((stats["http_attempts"], stats["retries"], stats["failed"], stats["not_sent"]), (2, 1, 1, 1))
        self.assertEqual(client.remaining_requests, 0)

    def test_budget_mid_pagination_keeps_partial_items(self):
        handler = Queue(ok(stations(1, 10), total=30, rows=10), ok(stations(11, 10), total=30, page=2, rows=10))
        client, _ = make_client(handler, max_requests=2)
        with client:
            result = client.station_list(rows=10)
        self.assertEqual((result.status, result.stop_reason, len(result.items)), ("partial", "budget_exhausted", 20))


class SecretHandlingTest(unittest.TestCase):
    def test_key_is_encoded_once_and_kept_out_of_results(self):
        handler = Queue(ok(stations(1, 1)))
        client, _ = make_client(handler)
        with client:
            result = client.station_list(rows=10)
        raw_query = handler.requests[0].url.query.decode()
        self.assertIn(f"serviceKey={quote(FAKE_KEY, safe='')}", raw_query)
        self.assertNotIn("%25", raw_query, "이중 인코딩 없음")
        self.assertEqual(handler.requests[0].url.params["serviceKey"], FAKE_KEY)
        self.assertNotIn("serviceKey", result.pages[0].params)

    def test_echoed_key_is_redacted_from_errors_and_bodies(self):
        echo_error = httpx.Response(500, text=f"bad request serviceKey={quote(FAKE_KEY, safe='')} key={FAKE_KEY}")
        echo_body = synthetic_body([], total=1, container={"item": f"echo {FAKE_KEY}"})
        client, _ = make_client(Queue(echo_error, httpx.Response(200, json=echo_body)), max_retries=1)
        with client:
            result = client.arrivals("SYN0001")
        dumped = json.dumps(
            [page.__dict__ for page in result.pages], default=lambda value: value.__dict__, ensure_ascii=False
        )
        for form in KEY_FORMS:
            self.assertNotIn(form, dumped)
        self.assertIn("***", dumped)

    def test_http_library_logs_do_not_contain_key(self):
        records = []
        handler = logging.Handler()
        handler.emit = lambda record: records.append(record.getMessage())
        root = logging.getLogger()
        previous = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            client, _ = make_client(Queue(ok(stations(1, 1))))
            with client:
                client.station_list(rows=10)
        finally:
            root.removeHandler(handler)
            root.setLevel(previous)
        self.assertFalse(any(form in message for message in records for form in KEY_FORMS))

    def test_encoded_or_blank_key_is_rejected_without_echoing_it(self):
        with self.assertRaises(TagoConfigError) as raised:
            TagoClient("Encoded%2BKey%3D%3D")
        self.assertNotIn("Encoded", str(raised.exception))
        with self.assertRaises(TagoConfigError):
            TagoClient("   ")


if __name__ == "__main__":
    unittest.main()
