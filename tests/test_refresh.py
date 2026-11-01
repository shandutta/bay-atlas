"""Refresh contract tests. All network access is mocked; no API quota is used."""
from datetime import date, datetime, timezone
import io
import json
from pathlib import Path
import stat
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import refresh


TODAY = date(2026, 10, 29)
FAKE_KEY = 'test_only_key_never_a_real_provider_credential'


def property_record(zpid='12345'):
    return {
        'zpid': zpid, 'status': 'RECENTLY_SOLD', 'price': 2000000,
        'zestimate': 2100000,
        'address': {'street': '100 Example Rd', 'city': 'Oakland', 'state': 'CA', 'zip': '94611'},
        'location': {'lat': 37.83, 'lng': -122.23},
        'facts': {'homeType': 'SingleFamily', 'propertySubType': 'Detached', 'livingArea': 2500, 'bedrooms': 3},
        'priceHistory': [{'event': 'Sold', 'date': '2026-10-01', 'price': 1500000}],
    }


def search_result(zpid='12345'):
    return {'searchResultCounts': {'totalMatchingCount': 1}, 'searchResults': [
        {'resultType': 'property', 'property': {'zpid': zpid, 'livingArea': 2500, 'bedrooms': 3}}
    ]}


def response(value):
    return io.BytesIO(json.dumps(value).encode())


class SaleNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch('refresh.today_utc', return_value=TODAY)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def test_uses_latest_actual_sold_event_ignoring_list_and_estimate(self):
        record = property_record()
        record['priceHistory'] = [
            {'event': 'Listed for sale', 'date': '2026-10-28', 'price': 2200000},
            {'event': 'Pending sale', 'date': '2026-10-20', 'price': 2100000},
            {'event': 'Sold', 'date': '2026-10-01', 'price': 1500000},
            {'event': 'Sold', 'date': '2025-01-01', 'price': 1000000},
        ]
        row = refresh.normalize_property(record, '12345')
        self.assertEqual(row['price'], 1500000)
        self.assertEqual(row['sold_date'], '2026-10-01')
        self.assertNotIn('zestimate', row)

    def test_top_level_price_or_sold_status_cannot_substitute_for_sale_event(self):
        for history in [[], [{'event': 'Listed for sale', 'date': '2026-10-01', 'price': 1500000}],
                        [{'event': 'Pending sale', 'date': '2026-10-01', 'price': 1500000}],
                        [{'event': 'Sold', 'date': '2026-10-01', 'price': None}]]:
            with self.subTest(history=history):
                record = property_record()
                record['priceHistory'] = history
                self.assertIsNone(refresh.normalize_property(record))

    def test_two_calendar_year_cutoff_is_inclusive_and_future_excluded(self):
        for sold, accepted in [('2024-10-29', True), ('2024-10-28', False),
                               ('2026-10-29', True), ('2026-10-30', False)]:
            with self.subTest(sold=sold):
                record = property_record()
                record['priceHistory'][0]['date'] = sold
                self.assertEqual(refresh.normalize_property(record) is not None, accepted)

    def test_identity_must_equal_requested_zpid(self):
        self.assertIsNone(refresh.normalize_property(property_record(), '99999'))
        self.assertIsNone(refresh.normalize_property(property_record('not-an-id')))
        self.assertIsNotNone(refresh.normalize_property({'property': property_record()}, 12345))

    def test_detached_subtype_is_allowed_but_attached_homes_excluded(self):
        record = property_record()
        self.assertIsNotNone(refresh.normalize_property(record))
        for subtype in ['Attached', 'Single Family Attached', 'Townhouse', 'Condominium']:
            with self.subTest(subtype=subtype):
                record['facts']['propertySubType'] = subtype
                self.assertIsNone(refresh.normalize_property(record))
        record['facts']['propertySubType'] = 'Detached'
        record['facts']['isAttached'] = True
        self.assertIsNone(refresh.normalize_property(record))

    def test_malformed_records_and_events_are_not_invented_or_fatal(self):
        for value in [None, [], {'property': []}, {'facts': []}]:
            self.assertIsNone(refresh.normalize_property(value))
        for field in ['facts', 'address', 'location']:
            record = property_record()
            record[field] = ['unexpected']
            self.assertIsNone(refresh.normalize_property(record))
        record = property_record()
        record['priceHistory'][:0] = [None, {'event': 'Sold', 'time': float('inf'), 'price': 1},
                                    {'event': 'Sold', 'time': 10**100, 'price': 1},
                                    {'event': 'Sold', 'date': '2026-10-28', 'price': float('inf')}]
        self.assertEqual(refresh.normalize_property(record)['price'], 1500000)

    def test_seconds_and_milliseconds_dates_work_and_invalid_values_fail(self):
        seconds = datetime(2026, 10, 1, 12, tzinfo=timezone.utc).timestamp()
        self.assertEqual(refresh.sale_date(seconds), '2026-10-01')
        self.assertEqual(refresh.sale_date(seconds * 1000), '2026-10-01')
        for value in [True, float('nan'), float('inf'), -10**100, 'not-a-date']:
            self.assertIsNone(refresh.sale_date(value))


class RefreshManagerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.manager = refresh.RefreshManager(self.root)
        self.manager.run_calls = 0
        self.cfg = {'api_key': FAKE_KEY, 'scope': 'east_bay', 'max_calls_per_refresh': 10, 'monthly_call_limit': 20}
        for target, replacement in [('refresh.today_utc', TODAY), ('model.today_utc', TODAY)]:
            mock = patch(target, return_value=replacement)
            mock.start()
            self.addCleanup(mock.stop)
        pause = patch('refresh.time.sleep')
        pause.start()
        self.addCleanup(pause.stop)
        self.net = patch('refresh.urlopen', side_effect=AssertionError('Unexpected network call'))
        self.http = self.net.start()
        self.addCleanup(self.net.stop)

    def save_original(self):
        row = refresh.normalize_property(property_record('99999'))
        row['address'] = '99 Original Rd, Oakland, CA 94611'
        self.original = json.dumps({'sales': [row], 'source': 'original snapshot'}, indent=3).encode()
        self.manager.data.mkdir(parents=True, exist_ok=True)
        (self.manager.data / 'sales.json').write_bytes(self.original)

    def test_no_configuration_makes_no_network_call_or_usage_reservation(self):
        self.assertEqual(self.manager.start()['state'], 'unconfigured')
        self.assertIsNone(self.manager.worker)
        self.http.assert_not_called()
        self.assertFalse((self.manager.data / 'provider-usage.json').exists())
        with self.assertRaises(refresh.ProviderError):
            self.manager.request({}, '/search')
        self.http.assert_not_called()

    def test_housedata_refresh_preserves_market_quarantine_and_filters_exact_event(self):
        from test_market_quarantine import quarantine
        event = refresh.normalize_property(property_record())
        metadata = quarantine(event)
        self.manager.data.mkdir(parents=True, exist_ok=True)
        path = self.manager.data / 'sales.json'
        path.write_text(json.dumps({'sales': [event], 'market_sale_quarantine': [metadata]}))
        def request(cfg, endpoint, **kwargs):
            if endpoint == '/search':
                return search_result()
            return property_record()
        with patch.object(refresh, 'EAST_BAY', ['Oakland, CA']), patch.object(self.manager, 'request', side_effect=request):
            self.manager.run(self.cfg)
        saved = json.loads(path.read_text())
        self.assertEqual(saved['sales'], [])
        self.assertEqual(saved['market_sale_quarantine'], [metadata])
        self.assertEqual(saved['coverage']['merge_counts']['excluded_quarantined'], 1)
        self.http.assert_not_called()

    def test_key_is_private_and_never_returned_in_status(self):
        configured = self.manager.configure({'api_key': FAKE_KEY})
        self.assertTrue(configured['configured'])
        self.assertNotIn(FAKE_KEY, json.dumps(configured))
        self.assertNotIn(FAKE_KEY, json.dumps(self.manager.status()))
        self.assertEqual(stat.S_IMODE(self.manager.config_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.manager.config_path.parent.stat().st_mode), 0o700)
        self.assertEqual(json.loads(self.manager.config_path.read_text())['api_key'], FAKE_KEY)
        self.assertFalse(list(self.manager.config_path.parent.glob('*.tmp')))

    def test_per_refresh_budget_stops_before_dispatch(self):
        self.cfg['max_calls_per_refresh'] = 2
        self.http.side_effect = lambda *_args, **_kwargs: response({'ok': True})
        self.manager.request(self.cfg, '/search')
        self.manager.request(self.cfg, '/search')
        with self.assertRaises(refresh.BudgetReached):
            self.manager.request(self.cfg, '/search')
        self.assertEqual(self.http.call_count, 2)
        self.assertEqual(len(self.manager.usage()), 2)

    def test_monthly_budget_survives_manager_restart(self):
        self.cfg['monthly_call_limit'] = 2
        refresh.atomic_json(self.manager.data / 'provider-usage.json', [{'at': time.time()}], True)
        self.http.side_effect = lambda *_args, **_kwargs: response({'ok': True})
        self.manager.request(self.cfg, '/search')
        restarted = refresh.RefreshManager(self.root)
        restarted.run_calls = 0
        with self.assertRaises(refresh.BudgetReached):
            restarted.request(self.cfg, '/search')
        self.assertEqual(self.http.call_count, 1)
        self.assertEqual(len(restarted.usage()), 2)

    def test_corrupt_usage_ledger_fails_closed_without_resetting_budget(self):
        self.manager.configure({'api_key': FAKE_KEY})
        self.manager.data.mkdir(parents=True, exist_ok=True)
        ledger = self.manager.data / 'provider-usage.json'
        for invalid in ['{broken', '{}', '[{"at": "not-a-time"}]']:
            with self.subTest(invalid=invalid):
                ledger.write_text(invalid)
                self.assertEqual(self.manager.start()['state'], 'error')
                self.assertIn('budget', self.manager.current['message'])
                self.assertEqual(ledger.read_text(), invalid)
                self.assertIsNone(self.manager.provider_status()['calls_used'])
                self.http.assert_not_called()

    def test_failed_request_is_reserved_first_and_not_retried_or_echoed(self):
        def refused(*_args, **_kwargs):
            self.assertEqual(len(self.manager.usage()), 1)
            raise HTTPError('https://example.invalid/', 401, FAKE_KEY, {}, None)
        self.http.side_effect = refused
        with self.assertRaises(refresh.ProviderError) as caught:
            self.manager.request(self.cfg, '/property')
        self.assertNotIn(FAKE_KEY, str(caught.exception))
        self.assertEqual(self.http.call_count, 1)
        self.assertEqual(self.manager.run_calls, 1)

    def test_provider_failure_after_valid_sale_preserves_original_bytes(self):
        self.save_original()
        self.http.side_effect = [response(search_result()), response(property_record()), URLError(FAKE_KEY)]
        with patch('refresh.EAST_BAY', ['Oakland, CA', 'Berkeley, CA']):
            self.manager.run(self.cfg)
        self.assertEqual((self.manager.data / 'sales.json').read_bytes(), self.original)
        self.assertEqual(self.manager.current['state'], 'error')
        self.assertFalse(self.manager.current.get('live_verified', False))
        self.assertNotIn(FAKE_KEY, json.dumps(self.manager.current))
        self.assertFalse((self.manager.data / 'snapshots').exists())
        self.assertEqual(self.http.call_count, 3)

    def test_json_response_without_valid_sale_does_not_claim_live_verification(self):
        self.save_original()
        record = property_record()
        record['priceHistory'] = []
        self.http.side_effect = [response(search_result()), response(record)]
        with patch('refresh.EAST_BAY', ['Oakland, CA']):
            self.manager.run(self.cfg)
        self.assertEqual((self.manager.data / 'sales.json').read_bytes(), self.original)
        self.assertEqual(self.manager.current['state'], 'error')
        self.assertFalse(self.manager.current.get('live_verified', False))

    def test_budget_limited_success_keeps_previous_sales_and_marks_partial(self):
        self.save_original()
        self.cfg['max_calls_per_refresh'] = 2
        self.http.side_effect = [response(search_result()), response(property_record())]
        with patch('refresh.EAST_BAY', ['Oakland, CA', 'Berkeley, CA']):
            self.manager.run(self.cfg)
        published = json.loads((self.manager.data / 'sales.json').read_text())
        self.assertEqual({row['id'] for row in published['sales']}, {'12345', '99999'})
        self.assertEqual(published['coverage']['status'], 'partial')
        self.assertTrue(published['coverage']['request_limit_reached'])
        self.assertEqual(self.manager.current['state'], 'success')
        self.assertTrue(self.manager.current['live_verified'])
        self.assertEqual(self.http.call_count, 2)
        self.assertEqual(len(list((self.manager.data / 'snapshots').glob('sales-*.json'))), 1)

    def test_changed_search_schema_preserves_original(self):
        self.save_original()
        self.http.side_effect = [response({'newSearchSchema': []})]
        with patch('refresh.EAST_BAY', ['Oakland, CA']):
            self.manager.run(self.cfg)
        self.assertEqual((self.manager.data / 'sales.json').read_bytes(), self.original)
        self.assertIn('format changed', self.manager.current['message'])


if __name__ == '__main__':
    unittest.main()
