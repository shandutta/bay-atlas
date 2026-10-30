"""No live calls or account usage: sold evidence, API bounds and completion."""
from datetime import date
from pathlib import Path
import io
import json
import unittest
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

import apify_source as source

TODAY = date(2026, 10, 29)
KEY = "apify_api_fake_test_key_not_a_credential"


def sale(zpid="12345"):
    return {"zpid": zpid, "propertyUrl": "https://www.zillow.com/homedetails/" + zpid + "_zpid/",
            "listingStatus": "sold", "listingSoldPrice": {"amount": 1500000, "currency": "USD"},
            "listingPrice": {"amount": 1700000, "currency": "USD"}, "zestimate": 1800000,
            "dateSold": "2026-10-01", "homeType": "SINGLE_FAMILY", "livingArea": 2500,
            "bedrooms": 3, "listingAddress": {"street": "10 Example Rd", "city": "Oakland",
            "state": "CA", "zipCode": "94611", "unit": None},
            "coordinates": {"latitude": 37.83, "longitude": -122.23}}


def run(status="SUCCEEDED", **kwargs):
    return {"data": {"id": "Run12345", "actId": "Actor12345", "defaultDatasetId": "Data12345",
            "status": status, "exitCode": 0 if status == "SUCCEEDED" else None,
            "options": {"maxTotalChargeUsd": 0.10, "timeoutSecs": 600, "restartOnError": False},
            "stats": {"resurrectCount": 0}, "usageTotalUsd": 0.04, **kwargs}}


def metadata(count):
    return {"data": {"id": "Data12345", "actRunId": "Run12345", "actId": "Actor12345",
            "itemCount": count, "modifiedAt": "2026-10-29T01:02:03Z",
            "urlSigningSecretKey": "secret_must_not_escape"}}


class Response(io.BytesIO):
    def __init__(self, value, headers=None, url="https://api.apify.com/v2/test"):
        super().__init__(json.dumps(value).encode())
        self.headers, self.url = headers or {}, url

    def geturl(self):
        return self.url


def page(items, *, offset=0, total=None, limit=500):
    return Response(items, {"X-Apify-Pagination-Offset": str(offset),
        "X-Apify-Pagination-Limit": str(limit), "X-Apify-Pagination-Count": str(len(items)),
        "X-Apify-Pagination-Total": str(len(items) if total is None else total)})


class Queue:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, request, **kwargs):
        self.calls.append(request)
        if not self.responses:
            raise AssertionError("Unexpected request")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class NormalizeTests(unittest.TestCase):
    def test_uses_actual_sold_evidence_only(self):
        row = source.normalize_property(sale(), as_of=TODAY)
        self.assertEqual(row["price"], 1500000)
        self.assertEqual(row["sold_date"], "2026-10-01")
        self.assertEqual(row["id"], "12345")
        self.assertNotIn("listingPrice", row)
        self.assertIn("may be rounded", row["price_evidence"])
        for field in ("listingSoldPrice", "dateSold", "listingStatus"):
            item = sale()
            item.pop(field)
            self.assertIsNone(source.normalize_property(item, as_of=TODAY))

    def test_synthetic_detailed_fixture_recovers_exact_latest_sale_prices(self):
        # Entirely synthetic records preserve exact/rounded price, older sale,
        # rental history, attached-home, and attached-garage distinctions.
        fixtures = json.loads((Path(__file__).parent / "fixtures" / "apify-detailed-sales.json").read_text())
        expected = {"900001": (1501000, "2026-10-26", 2400, 3),
                    "900002": (1702000, "2026-10-23", 2600, 4)}
        for item in fixtures:
            with self.subTest(zpid=item["zpid"]):
                row = source.normalize_property(item, as_of=TODAY)
                if item["zpid"] == "900003":
                    # Single-family category does not override explicit attached evidence.
                    self.assertTrue(item["resoFacts"]["hasAttachedProperty"])
                    self.assertIsNone(row)
                    continue
                self.assertEqual((row["price"], row["sold_date"], row["sqft"], row["beds"]), expected[item["zpid"]])
                self.assertNotEqual(row["price"], item["listingSoldPrice"]["amount"])
                self.assertIn("exact Sold event price matching dateSold", row["price_evidence"])
                self.assertIn("hasAttachedProperty false", row["detachment_evidence"])
                self.assertNotIn("listingPriceHistory", row)

    def test_matching_sold_event_wins_without_using_listing_or_rental_prices(self):
        item = sale()
        item["lastSoldPrice"] = 999999  # No date: cannot override the event.
        item["listingPriceHistory"] = [
            {"date": "2026-10-01", "event": "Sold", "price": 1499000, "sourceType": "other"},
            {"date": "2026-10-01", "event": "Sold", "price": 3000, "postingIsRental": True},
            {"date": "2026-10-02", "event": "Listed for sale", "price": 1900000},
            {"date": "2026-10-01", "event": "Pending sale", "price": 1700000},
            {"date": "2025-10-01", "event": "Sold", "price": 700000}]
        row = source.normalize_property(item, as_of=TODAY)
        self.assertEqual(row["price"], 1499000)
        self.assertEqual(row["sold_date"], "2026-10-01")

    def test_older_history_and_undated_last_price_cannot_supply_latest_price(self):
        item = sale()
        item["lastSoldPrice"] = 700000
        item["listingPriceHistory"] = [{"date": "2025-10-01", "event": "Sold", "price": 700000}]
        for last_date in (None, "2025-10-01"):
            item["lastSoldDate"] = last_date
            row = source.normalize_property(item, as_of=TODAY)
            self.assertEqual(row["price"], 1500000)
            self.assertIn("may be rounded", row["price_evidence"])
        item["listingSoldPrice"]["amount"] = None
        self.assertIsNone(source.normalize_property(item, as_of=TODAY))

    def test_last_sold_price_requires_explicit_matching_date(self):
        item = sale()
        item.update(lastSoldDate="2026-10-01T00:00:00.000Z", lastSoldPrice=1499000)
        row = source.normalize_property(item, as_of=TODAY)
        self.assertEqual(row["price"], 1499000)
        self.assertIn("paired lastSoldDate matches dateSold", row["price_evidence"])
        item["lastSoldDate"] = "2026-10-02"
        self.assertIsNone(source.normalize_property(item, as_of=TODAY))

    def test_conflicting_or_invalid_detailed_sale_evidence_is_rejected(self):
        histories = [
            [{"date": "2026-10-02", "event": "Sold", "price": 1499000}],
            [{"date": "2026-10-01", "event": "Sold", "price": 1499000},
             {"date": "2026-10-01", "event": "Sold", "price": 1500000}],
            [{"date": "2026-10-01", "event": "Sold", "price": None}],
            [{"date": "2026-10-01", "event": "Sold", "price": True}],
            [{"date": "2026-10-01", "event": "Sold", "price": -100}],
            [{"date": "invalid", "event": "Sold", "price": 1499000}],
            "invalid", [None]]
        for history in histories:
            with self.subTest(history=history):
                item = sale()
                item["listingPriceHistory"] = history
                self.assertIsNone(source.normalize_property(item, as_of=TODAY))
        item = sale()
        item.update(lastSoldDate="2026-10-01", lastSoldPrice=1500000,
                    listingPriceHistory=[{"date": "2026-10-01", "event": "Sold", "price": 1499000}])
        self.assertIsNone(source.normalize_property(item, as_of=TODAY))

    def test_rejects_fabricated_or_malformed_fields(self):
        alterations = [("listingSoldPrice", {"amount": True, "currency": "USD"}),
            ("listingSoldPrice", {"amount": 100, "currency": "CAD"}),
            ("listingSoldPrice", {"amount": float("inf"), "currency": "USD"}),
            ("listingSoldPrice", 1000000), ("dateSold", 1790812800000),
            ("dateSold", "2026-10-01 not a date"), ("dateSold", "2026-02-30"),
            ("dateSold", "2026-10-01T99:99:00Z"),
            ("listingStatus", "forSale"), ("isValid", False), ("homeType", "TOWNHOUSE"),
            ("livingArea", 3001), ("bedrooms", 1), ("resoFacts", [])]
        for key, value in alterations:
            with self.subTest(key=key, value=value):
                item = sale()
                item[key] = value
                self.assertIsNone(source.normalize_property(item, as_of=TODAY))

    def test_boundaries_two_calendar_years(self):
        for value, accepted in (("2024-10-29", True), ("2024-10-28", False),
                                ("2026-10-29", True), ("2026-10-30", False),
                                ("2026-10-01T07:00:00Z", True)):
            item = sale()
            item["dateSold"] = value
            self.assertEqual(source.normalize_property(item, as_of=TODAY) is not None, accepted)

    def test_attached_and_unit_evidence_is_rejected(self):
        for item in ({**sale(), "isAttached": True}, {**sale(), "isDetached": False},
                     {**sale(), "hasAttachedProperty": True},
                     {**sale(), "resoFacts": {"hasAttachedProperty": True}},
                     {**sale(), "propertySubType": "Single Family Attached"},
                     {**sale(), "resoFacts": {"propertySubType": ["Condominium"]}}):
            self.assertIsNone(source.normalize_property(item, as_of=TODAY))
        item = sale()
        item["listingAddress"]["unit"] = "Unit A"
        self.assertIsNone(source.normalize_property(item, as_of=TODAY))
        item["listingAddress"]["unit"] = None
        item["listingAddress"]["street"] = "10 Example Rd #2"
        self.assertIsNone(source.normalize_property(item, as_of=TODAY))

    def test_explicit_attachment_exclusions_require_otherwise_verified_sale(self):
        fixtures = json.loads((Path(__file__).parent / "fixtures" / "apify-detailed-sales.json").read_text())
        result = source.normalize_records(fixtures, as_of=TODAY)
        exclusions = result["diagnostics"]["explicit_attached_exclusions"]
        self.assertEqual(len(exclusions), 1)
        self.assertEqual(exclusions[0]["source_id"], "900003")
        self.assertEqual(exclusions[0]["reason"], "explicit_attached_home")
        self.assertEqual(exclusions[0]["attachment_evidence"], "Zillow resoFacts.hasAttachedProperty true")
        self.assertEqual(exclusions[0]["street_address"], "300 Synthetic Example Ave")
        self.assertEqual(exclusions[0]["city"], "Berkeley")
        self.assertTrue(exclusions[0]["source_url"].endswith("/900003_zpid/"))
        self.assertNotIn("price", exclusions[0])
        self.assertEqual(len(result["sales"]), 2)
        for field, value in (("propertyUrl", "https://evil.test/12345"),
                             ("dateSold", None), ("livingArea", 4000),
                             ("listingSoldPrice", None), ("coordinates", {})):
            item = sale()
            item["resoFacts"] = {"hasAttachedProperty": True}
            item[field] = value
            result = source.normalize_records([item], as_of=TODAY)
            self.assertEqual(result["diagnostics"]["explicit_attached_exclusions"], [])

    def test_missing_attachment_evidence_does_not_authorize_removal(self):
        for facts in ({}, {"hasAttachedProperty": None}, {"hasAttachedProperty": False},
                      {"hasAttachedGarage": True, "parkingFeatures": ["Attached"]}):
            item = sale()
            item["resoFacts"] = facts
            result = source.normalize_records([item], as_of=TODAY)
            self.assertEqual(len(result["sales"]), 1)
            self.assertEqual(result["diagnostics"]["explicit_attached_exclusions"], [])
        item = sale()
        item["listingAddress"]["unit"] = "A"
        result = source.normalize_records([item], as_of=TODAY)
        self.assertEqual(result["sales"], [])
        self.assertEqual(result["diagnostics"]["explicit_attached_exclusions"], [])

    def test_identity_and_url_allowlist(self):
        bad = ["http://www.zillow.com/homedetails/12345_zpid/",
               "https://zillow.com.evil.test/homedetails/12345_zpid/",
               "https://www.zillow.com/homedetails/99999_zpid/",
               "https://u:p@www.zillow.com/homedetails/12345_zpid/",
               "https://www.zillow.com:444/homedetails/12345_zpid/",
               "https://www.zillow.com/homedetails/12345_zpid/?token=secret", None]
        for value in bad:
            item = sale()
            item["propertyUrl"] = value
            self.assertIsNone(source.normalize_property(item, as_of=TODAY))

    def test_repeated_sales_keep_latest_and_count_exclusions(self):
        older = sale()
        older["dateSold"] = "2025-01-01"
        result = source.normalize_records([older, sale(), None], as_of=TODAY)
        self.assertEqual(len(result["sales"]), 1)
        self.assertEqual(result["sales"][0]["sold_date"], "2026-10-01")
        self.assertEqual(result["diagnostics"]["excluded"], {"invalid_record": 1, "duplicate_home": 1})

    def test_all_counties_are_requested_with_correct_filters(self):
        params = source.build_input()
        self.assertEqual(len(params["searchLocation"]), 9)
        self.assertIn("Sonoma County, CA", params["searchLocation"])
        self.assertIn("San Francisco County, CA", params["searchLocation"])
        self.assertEqual(params["daysOnZillow"], "24m")
        self.assertEqual(params["extractionMethod"], "PAGINATION_WITH_ZOOM_IN")
        self.assertFalse(params["compact"])
        self.assertFalse(params["includeDetails"])
        self.assertTrue(source.build_input(include_details=True)["includeDetails"])
        for invalid in (None, "true", 1, 0):
            with self.assertRaises(ValueError):
                source.build_input(include_details=invalid)
        for locations in ([], "Oakland, CA", ["Austin, TX"], ["Bad\nPlace, CA"]):
            with self.assertRaises(ValueError):
                source.build_input(locations)


class ClientTests(unittest.TestCase):
    def client(self, *responses, **kwargs):
        queue = Queue(*responses)
        client = source.ApifyClient(KEY, max_total_charge_usd=0.10, opener=queue,
                                    sleep=lambda seconds: None, **kwargs)
        return client, queue

    def test_start_sets_provider_cap_and_never_places_key_in_url(self):
        before = []
        client, queue = self.client(Response(run("READY")), before_request=lambda method, path: before.append((method, path)))
        result = client.start(source.build_input(["Berkeley, CA"], max_results=50))
        self.assertEqual(result["id"], "Run12345")
        request = queue.calls[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + KEY)
        self.assertNotIn(KEY, request.full_url)
        self.assertNotIn(KEY, json.dumps(result))
        query = parse_qs(urlparse(request.full_url).query)
        self.assertEqual(float(query["maxTotalChargeUsd"][0]), 0.10)
        self.assertEqual(query["restartOnError"], ["false"])
        self.assertEqual(len(before), 1)

    def test_unconfirmed_or_excessive_provider_cap_fails_with_id(self):
        for options in ({}, {"maxTotalChargeUsd": 5}):
            client, _ = self.client(Response(run("READY", options=options)))
            with self.assertRaises(source.ApifyError) as caught:
                client.start(source.build_input())
            self.assertEqual(caught.exception.code, "unconfirmed_cap")
            self.assertEqual(caught.exception.run_id, "Run12345")

    def test_detailed_input_keeps_same_run_cap_and_rejects_other_modifications(self):
        client, queue = self.client(Response(run("READY")))
        params = source.build_input(["Berkeley, CA"], max_results=10, include_details=True)
        client.start(params)
        request = queue.calls[0]
        self.assertTrue(json.loads(request.data)["includeDetails"])
        self.assertEqual(parse_qs(urlparse(request.full_url).query)["maxTotalChargeUsd"], ["0.10000000"])
        params["minBeds"] = 1
        with self.assertRaises(ValueError):
            client.start(params)
        self.assertEqual(len(queue.calls), 1)

    def test_success_wait_rechecks_settled_run(self):
        client, queue = self.client(Response(run("RUNNING")), Response(run()), Response(run()))
        final = client.wait("Run12345")
        self.assertFalse(final["costs_preliminary"])
        self.assertEqual(len(queue.calls), 3)

    def test_start_requires_confirmed_timeout_and_no_automatic_restart(self):
        for changes in ({'timeoutSecs': 0}, {'timeoutSecs': 601}, {'timeoutSecs': None},
                        {'restartOnError': True}, {'restartOnError': None}):
            options = dict(run()['data']['options'], **changes)
            client, queue = self.client(Response(run('READY', options=options)))
            with self.assertRaises(source.ApifyError) as caught:
                client.start(source.build_input())
            self.assertEqual(caught.exception.code, 'unconfirmed_timeout')
            self.assertEqual(caught.exception.run_id, 'Run12345')
            self.assertEqual(len(queue.calls), 1)

    def test_same_run_resurrection_keeps_cumulative_cap_and_storages(self):
        result = run('RUNNING', options={'maxTotalChargeUsd': .1, 'timeoutSecs': 21600,
                                       'restartOnError': False}, stats={'resurrectCount': 1})
        client, queue = self.client(Response(result), wait_timeout=21600, poll_interval=300)
        original = client._run(run('TIMED-OUT'))
        recovered = client.resurrect_timeout(original, timeout_seconds=21600)
        self.assertEqual(recovered['id'], original['id'])
        self.assertEqual(recovered['defaultDatasetId'], original['defaultDatasetId'])
        self.assertEqual(recovered['resurrect_count'], 1)
        request = queue.calls[0]
        self.assertEqual(request.method, 'POST')
        self.assertEqual(urlparse(request.full_url).path, '/v2/actor-runs/Run12345/resurrect')
        self.assertEqual(parse_qs(urlparse(request.full_url).query),
                         {'timeout': ['21600'], 'maxTotalChargeUsd': ['0.10000000'], 'restartOnError': ['false']})

    def test_resurrection_rejects_non_timeout_repeated_or_changed_cap_before_post(self):
        client, queue = self.client()
        original = client._run(run('TIMED-OUT'))
        for change in ({'status': 'FAILED'}, {'status': 'SUCCEEDED'}, {'resurrect_count': 1},
                       {'max_total_charge_usd': .09}, {'max_total_charge_usd': .11},
                       {'defaultDatasetId': '../other'}):
            with self.subTest(change=change), self.assertRaises(source.ApifyError):
                client.resurrect_timeout(dict(original, **change), timeout_seconds=21600)
        self.assertFalse(queue.calls)

    def test_resurrection_response_cannot_change_identity_or_limit(self):
        base = run('RUNNING', options={'maxTotalChargeUsd': .1, 'timeoutSecs': 21600,
                                     'restartOnError': False}, stats={'resurrectCount': 1})['data']
        for change in ({'id': 'Other12345'}, {'defaultDatasetId': 'Other12345'}, {'actId': 'Other12345'},
                       {'options': {'maxTotalChargeUsd': .09, 'timeoutSecs': 21600, 'restartOnError': False}},
                       {'options': {'maxTotalChargeUsd': .1, 'timeoutSecs': 21601, 'restartOnError': False}},
                       {'options': {'maxTotalChargeUsd': .1, 'timeoutSecs': 21600, 'restartOnError': True}}):
            client, queue = self.client(Response({'data': dict(base, **change)}))
            with self.subTest(change=change), self.assertRaises(source.ApifyError):
                client.resurrect_timeout(client._run(run('TIMED-OUT')), timeout_seconds=21600)
            self.assertEqual(len(queue.calls), 1)

    def test_operational_bounds_allow_six_hours_and_five_minute_polling(self):
        self.client(wait_timeout=21600, poll_interval=300)
        for kwargs in ({'wait_timeout': 21601}, {'poll_interval': 301}):
            with self.assertRaises(source.ApifyError):
                self.client(**kwargs)

    def test_terminal_failure_never_downloads_dataset(self):
        for state in ("FAILED", "ABORTED", "TIMED-OUT"):
            client, queue = self.client(Response(run(state)))
            with self.assertRaises(source.ApifyError) as caught:
                client.wait("Run12345")
            self.assertTrue(caught.exception.code.startswith("run_"))
            self.assertEqual(caught.exception.run_id, "Run12345")
            self.assertEqual(len(queue.calls), 1)

    def test_timeout_is_resumable_and_never_restarts(self):
        times = iter([0, 0, 0, 2])
        client, queue = self.client(Response(run("RUNNING")), Response(run("RUNNING")),
                                    wait_timeout=1, clock=lambda: next(times))
        with self.assertRaises(source.ApifyError) as caught:
            client.wait("Run12345")
        self.assertEqual(caught.exception.code, "wait_timeout")
        self.assertTrue(all(request.method == "GET" for request in queue.calls))

    def test_complete_dataset_requires_every_page_and_stable_count(self):
        client, queue = self.client(Response(metadata(3)), page([sale(), sale("23456")], total=3, limit=2),
            page([sale("34567")], offset=2, total=3, limit=2), Response(metadata(3)))
        final = client._run(run())
        result = client.dataset(final, page_size=2, max_results=100)
        self.assertEqual(result["item_count"], 3)
        self.assertEqual(result["pages"], 2)
        self.assertTrue(result["dataset_complete"])
        self.assertFalse(result["coverage_complete"])
        self.assertFalse(result["cap_reached"])
        self.assertGreater(result["read_bytes"], 0)
        self.assertNotIn("secret_must_not_escape", json.dumps(result))
        self.assertEqual(parse_qs(urlparse(queue.calls[2].full_url).query)["offset"], ["2"])
        fields = parse_qs(urlparse(queue.calls[1].full_url).query)["fields"][0].split(",")
        self.assertTrue({"listingPriceHistory", "lastSoldPrice", "lastSoldDate", "detailsSource"}.issubset(fields))
        self.assertNotIn("photos", fields)

    def test_even_zero_count_is_confirmed_by_items_request(self):
        client, queue = self.client(Response(metadata(0)), page([]), Response(metadata(0)))
        result = client.dataset(client._run(run()))
        self.assertEqual(result["items"], [])
        self.assertEqual(len(queue.calls), 3)

    def test_truncated_or_changed_dataset_fails_closed(self):
        for pages in ((Response(metadata(2)), page([sale()], total=1)),
                      (Response(metadata(1)), page([sale()]), Response(metadata(2))),
                      (Response(metadata(1)), Response([sale()])),
                      (Response(metadata(2)), page([], total=2))):
            client, _ = self.client(*pages)
            with self.assertRaises(source.ApifyError):
                client.dataset(client._run(run()))

    def test_cap_detection_never_claims_full_coverage(self):
        client, _ = self.client(Response(metadata(1)), page([sale()]), Response(metadata(1)))
        result = client.dataset(client._run(run()), max_results=1)
        self.assertTrue(result["cap_reached"])
        self.assertFalse(result["coverage_complete"])

    def test_detail_cost_proximity_is_conservatively_flagged(self):
        client, _ = self.client(Response(metadata(1)), page([sale()]), Response(metadata(1)))
        result = client.dataset(client._run(run(usageTotalUsd=0.098)), max_results=100)
        self.assertTrue(result["cap_reached"])
        self.assertFalse(result["coverage_complete"])

    def test_unsafe_paths_and_resource_ids_make_no_request(self):
        client, queue = self.client()
        for value in ("https://evil.test/v2/a", "/v2/../token", "/v2/a?token=x"):
            with self.assertRaises(source.ApifyError):
                client._request("GET", value)
        with self.assertRaises(source.ApifyError):
            client.get_run("../../secret")
        self.assertEqual(queue.calls, [])

    def test_redirect_handler_refuses_all_redirects(self):
        with self.assertRaises(source.ApifyError):
            source._NoRedirect().redirect_request(None, None, 302, None, {}, "https://evil.test")

    def test_network_errors_hide_provider_body_and_never_retry(self):
        for error in (URLError(KEY), HTTPError("https://api.apify.com/?token=" + KEY, 403, KEY, {}, None)):
            client, queue = self.client(error)
            with self.assertRaises(source.ApifyError) as caught:
                client.start(source.build_input())
            self.assertNotIn(KEY, str(caught.exception))
            self.assertEqual(len(queue.calls), 1)

    def test_http_byte_and_item_limits(self):
        client, queue = self.client(Response(run()), max_http_calls=1)
        client.get_run("Run12345")
        with self.assertRaises(source.ApifyError) as caught:
            client.get_run("Run12345")
        self.assertEqual(caught.exception.code, "http_limit")
        self.assertEqual(len(queue.calls), 1)
        client, _ = self.client(Response({"text": "x" * 2048}), max_total_response_bytes=1024)
        with self.assertRaises(source.ApifyError) as caught:
            client.get_run("Run12345")
        self.assertEqual(caught.exception.code, "response_limit")
        client, _ = self.client(Response(metadata(11)), max_dataset_items=10)
        with self.assertRaises(source.ApifyError):
            client.dataset(client._run(run()))

    def test_before_request_can_fail_closed_without_dispatch(self):
        def stop(*args):
            raise RuntimeError("Budget exhausted")
        client, queue = self.client(before_request=stop)
        with self.assertRaisesRegex(RuntimeError, "Budget exhausted"):
            client.start(source.build_input())
        self.assertEqual(queue.calls, [])

    def test_response_is_reserved_before_io_and_settled_after_complete_read(self):
        events = []
        response = Response(run())
        length = len(response.getvalue())
        queue = Queue(response)
        def dispatch(request, **kwargs):
            self.assertEqual(events, [('reserve', source.MAX_METADATA_RESPONSE_BYTES + 1)])
            return queue(request, **kwargs)
        client = source.ApifyClient(KEY, max_total_charge_usd=0.1, opener=dispatch,
            reserve_response=lambda size: events.append(('reserve', size)),
            settle_response=lambda reserved, actual: events.append(('settle', reserved, actual)))
        client.get_run('Run12345')
        self.assertEqual(events[-1], ('settle', source.MAX_METADATA_RESPONSE_BYTES + 1, length))
        self.assertEqual(client.budget_bytes, length)

    def test_failed_or_interrupted_body_keeps_reservation_and_reduces_next_allowance(self):
        class BrokenBody(Response):
            def read(self, size):
                raise OSError('connection dropped after unknown partial transfer')
        reserved, settled = [], []
        client, queue = self.client(BrokenBody(run()), Response(run()), max_total_response_bytes=300000,
            reserve_response=reserved.append, settle_response=lambda *args: settled.append(args))
        with self.assertRaises(source.ApifyError):
            client.get_run('Run12345')
        self.assertEqual(reserved, [source.MAX_METADATA_RESPONSE_BYTES + 1])
        self.assertEqual(settled, [])
        client.get_run('Run12345')
        self.assertEqual(reserved[-1], 300000 - reserved[0])
        self.assertGreater(client.budget_bytes, reserved[0])

    def test_overflow_detection_byte_is_reserved_even_without_content_length(self):
        reservations, settlements = [], []
        client, queue = self.client(Response({'text': 'x' * 2000}), max_total_response_bytes=1024,
            reserve_response=reservations.append, settle_response=lambda *args: settlements.append(args))
        with self.assertRaises(source.ApifyError):
            client.get_run('Run12345')
        self.assertEqual(reservations, [1024])
        self.assertEqual(settlements, [(1024, 1024)])
        self.assertEqual(client.budget_bytes, 1024)
        with self.assertRaises(source.ApifyError):
            client.get_run('Run12345')
        self.assertEqual(len(queue.calls), 1)

    def test_failed_reservation_persistence_prevents_dispatch(self):
        def failed_save(size):
            raise OSError('disk unavailable')
        client, queue = self.client(reserve_response=failed_save)
        with self.assertRaises(OSError):
            client.get_run('Run12345')
        self.assertEqual(queue.calls, [])


if __name__ == "__main__":
    unittest.main()
