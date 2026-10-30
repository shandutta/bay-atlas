"""Autocomplete tests use local fixtures, never a public geocoder."""
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse

import address_suggest as subject
from server import AtlasServer


def feature(**changes):
    value = {'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': [-122.25, 37.8]},
             'properties': {'countrycode': 'US', 'state': 'California', 'housenumber': '1',
                            'street': 'Synthetic Civic Plaza', 'city': 'Oakland', 'postcode': '94612',
                            'name': 'Synthetic Civic Building', 'type': 'house'}}
    return dict(value, **changes)


class Clock:
    def __init__(self):
        self.at, self.sleeps = 100.0, []

    def now(self):
        return self.at

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.at += seconds


class SuggestionsTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.calls, self.sales = [], []
        self.payload = {'features': [feature()]}
        self.fail = False

        def opener(request, timeout):
            self.calls.append((request, timeout, self.clock.at))
            if self.fail:
                raise URLError('private query must not escape through an exception')
            return io.BytesIO(json.dumps(self.payload).encode())

        self.service = subject.AddressSuggestions(lambda: {'sales': self.sales}, opener=opener,
                                                  clock=self.clock.now, sleep=self.clock.sleep)

    def test_public_results_have_geocode_shape_and_attribution(self):
        result = self.service.search('1 Synthetic Civic')
        row = result['results'][0]
        self.assertEqual(row['display_name'], '1 Synthetic Civic Plaza, Oakland, CA 94612')
        self.assertEqual(row['lat'], 37.8)
        self.assertEqual(row['lng'], -122.25)
        self.assertEqual(row['precision'], 'Mapped street address')
        self.assertIn('OpenStreetMap', result['attribution']['label'])
        self.assertIn('Photon (komoot)', result['notice'])

    def test_query_uses_fixed_https_host_country_bbox_layers_and_limit(self):
        self.service.search('1 Synthetic Civic')
        request, timeout, _ = self.calls[0]
        parsed = urlparse(request.full_url)
        self.assertEqual((parsed.scheme, parsed.hostname), ('https', 'photon.komoot.io'))
        params = parse_qs(parsed.query)
        self.assertEqual(params['q'], ['1 Synthetic Civic'])
        self.assertEqual(params['bbox'], ['-123.6,36.8,-121.15,38.9'])
        self.assertEqual(params['layer'], ['house', 'street'])
        self.assertEqual(params['limit'], ['5'])
        self.assertEqual(params['countrycode'], ['US'])
        self.assertEqual(timeout, 8)

    def test_invalid_queries_never_send_network_requests(self):
        for query in ['', 'abcde', 'x' * 121, '123 Main\nOakland', '123 Main\x00', None, '------']:
            with self.subTest(query=query):
                with self.assertRaises(ValueError):
                    self.service.search(query)
        self.assertFalse(self.calls)

    def test_cache_normalizes_case_and_whitespace_and_expires(self):
        self.service.search('1 Synthetic Civic')
        result = self.service.search('  1   SYNTHETIC CIVIC  ')
        self.assertTrue(result['cached'])
        self.assertEqual(len(self.calls), 1)
        self.clock.at += subject.CACHE_TTL_SECONDS + 1
        self.service.search('1 Synthetic Civic')
        self.assertEqual(len(self.calls), 2)

    def test_cache_has_bounded_entries(self):
        with patch.object(subject, 'MAX_CACHE_ENTRIES', 3):
            for index in range(5):
                self.service.search('123 Test ' + str(index))
        self.assertEqual(len(self.service._cache), 3)
        self.assertNotIn('123 test 0', self.service._cache)

    def test_all_outbound_requests_are_spaced_over_one_second(self):
        self.service.search('1 Synthetic Civic')
        self.service.search('125 Example Oakland')
        self.service.search('123 Main Berkeley')
        self.assertEqual(len(self.calls), 3)
        self.assertGreaterEqual(self.calls[1][2] - self.calls[0][2], 1.0)
        self.assertGreaterEqual(self.calls[2][2] - self.calls[1][2], 1.0)

    def test_local_prefix_matching_omits_suffix_and_resolves_coordinates(self):
        self.sales.extend([
            {'address': '100 Example View Drive, Lafayette, CA 94549', 'lat': 37.9, 'lng': -122.1},
            {'address': '1009 Example View Drive, Lafayette, CA 94549', 'lat': 37.91, 'lng': -122.1},
        ])
        result = self.service.search('100 Exam View')
        local = result['results'][0]
        self.assertEqual(local['source'], 'Local sale records')
        self.assertEqual(local['lat'], 37.9)
        self.assertTrue(local['display_name'].startswith('100 Example'))
        self.assertFalse(any(row['display_name'].startswith('1009') for row in result['results']))

    def test_exact_local_address_needs_no_external_query(self):
        address = '100 Example View Drive, Lafayette, CA 94549'
        self.sales.append({'address': address, 'lat': 37.9, 'lng': -122.1})
        result = self.service.search(address)
        self.assertEqual(result['remote_status'], 'not_needed')
        self.assertFalse(self.calls)

    def test_local_index_handles_word_order_prefixes_and_dataset_replacement(self):
        self.sales = [{'address': '100 Example View Drive, Oakland, CA', 'lat': 37.8, 'lng': -122.25},
                      {'address': '1009 Example View Drive, Oakland, CA', 'lat': 37.81, 'lng': -122.25},
                      {'address': '100 Different Street, Berkeley, CA', 'lat': 37.82, 'lng': -122.25}]
        for query in ('100 Exa View', 'View 100 Exa', '100 Oakland Dr'):
            with self.subTest(query=query):
                results = self.service._local(query)
                self.assertEqual(len(results), 1)
                self.assertTrue(results[0]['display_name'].startswith('100 Example'))
        self.assertEqual(self.service._local('100 Berk')[0]['lat'], 37.82)
        self.sales = [{'address': '100 New Street, Oakland, CA', 'lat': 37.83, 'lng': -122.25}]
        self.assertFalse(self.service._local('100 Exa View'))
        self.assertEqual(self.service._local('100 New')[0]['lat'], 37.83)

    def test_failed_remote_keeps_local_matches_without_echoing_error(self):
        self.sales.append({'address': '100 Example View Drive, Lafayette, CA 94549', 'lat': 37.9, 'lng': -122.1})
        self.fail = True
        result = self.service.search('100 Example')
        self.assertEqual(result['remote_status'], 'unavailable')
        self.assertEqual(len(result['results']), 1)
        self.assertNotIn('private query', json.dumps(result))
        self.assertFalse(self.service._cache)

    def test_non_address_results_are_marked_approximate(self):
        item = feature()
        del item['properties']['housenumber']
        item['properties']['name'] = 'Synthetic Civic Plaza'
        self.payload = {'features': [item]}
        result = self.service.search('Synthetic Civic')
        self.assertIn('Approximate street', result['results'][0]['precision'])

    def test_malformed_outside_and_nonfinite_results_are_rejected(self):
        outside = feature(geometry={'type': 'Point', 'coordinates': [-73.9, 40.7]})
        nonfinite = feature(geometry={'type': 'Point', 'coordinates': [-122.2, float('nan')]})
        foreign = feature()
        foreign['properties']['countrycode'] = 'DE'
        self.payload = {'features': [None, {}, outside, nonfinite, foreign, feature(), feature()]}
        result = self.service.search('1 Synthetic Civic')
        self.assertEqual(len(result['results']), 1)

    def test_oversized_or_changed_schema_fails_with_safe_fallback(self):
        self.payload = {'wrong': 'schema'}
        self.assertEqual(self.service.search('123 Main Street')['remote_status'], 'unavailable')
        self.payload = {'features': [], 'padding': 'x' * subject.MAX_RESPONSE_BYTES}
        self.assertEqual(self.service.search('123 Other Street')['remote_status'], 'unavailable')


class SuggestionsHTTPTests(unittest.TestCase):
    def test_suggest_route_accepts_homes_prefix_and_rejects_short_input(self):
        with tempfile.TemporaryDirectory() as folder:
            server = AtlasServer(('127.0.0.1', 0), data_path=Path(folder) / 'missing.json')
            server.suggestions = subject.AddressSuggestions(lambda: {'sales': []},
                opener=lambda *args, **kwargs: io.BytesIO(json.dumps({'features': [feature()]}).encode()))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for path, expected in [('/homes/api/suggest?q=1%20Synthetic%20Civic', 200),
                                       ('/api/suggest?q=Oak', 400)]:
                    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
                    connection.request('GET', path)
                    response = connection.getresponse()
                    payload = json.loads(response.read())
                    self.assertEqual(response.status, expected)
                    if expected == 200:
                        self.assertEqual(payload['results'][0]['source'], subject.PROVIDER)
                    connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__':
    unittest.main()
