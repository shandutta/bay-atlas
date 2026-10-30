"""Submitted-address search regressions; all remote responses are mocked."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

from address_suggest import AddressSuggestions
from server import Geocoder


class GeocoderTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'geocode-cache.json'
        self.service = Geocoder(self.path)
        self.match = {'matchedAddress': '100 Example Street, Oakland, CA',
                      'coordinates': {'y': 37.8, 'x': -122.25}}

    def response(self, payload):
        return io.BytesIO(json.dumps(payload).encode())

    def suggestions(self, rows, remote_status='ok', cached=False):
        self.service.suggestions = Mock()
        self.service.suggestions.search.return_value = {
            'results': rows, 'remote_status': remote_status, 'cached': cached}

    def precise(self, **changes):
        return dict({'lat': 37.8, 'lng': -122.25,
                     'display_name': '100 Example Street, Oakland, CA 94612',
                     'street_address': '100 Example Street', 'address_level': True,
                     'source': 'Photon / OpenStreetMap', 'precision': 'Mapped street address'}, **changes)

    def test_submit_reuses_precise_suggestion_without_city_or_census(self):
        self.suggestions([self.precise()], cached=True)
        # A prior Census miss must not hide a successful autocomplete result.
        self.service.cache['100 example st'] = {'saved_at': 1000, 'results': []}
        with patch('server.time.time', return_value=1001), \
                patch('server.urlopen', side_effect=AssertionError('precise suggestion needs no Census request')):
            result = self.service.search('100 Example St')
        self.assertEqual(result['status'], 'matched')
        self.assertEqual(result['provider'], 'Photon / OpenStreetMap')
        self.assertEqual(result['results'][0]['lat'], 37.8)
        self.assertTrue(result['cached'])
        self.assertFalse(self.path.exists(), 'Photon suggestions remain memory-only')
        self.assertGreater(self.service.suggestions.search.call_args.kwargs['wait_timeout'], 8)

    def test_exact_saved_address_resolves_locally_without_remote_services(self):
        self.service.suggestions = AddressSuggestions(lambda: {'sales': [
            {'address': '100 Example Street, Oakland, CA 94612', 'lat': 37.8, 'lng': -122.25}]},
            opener=Mock(side_effect=AssertionError('complete local address needs no external query')))
        with patch('server.urlopen', side_effect=AssertionError('local match needs no Census request')):
            result = self.service.search('100 Example Street, Oakland, CA 94612')
        self.assertEqual(result['provider'], 'Local sale records')
        self.assertEqual(len(result['results']), 1)

    def test_approximate_or_wrong_address_suggestions_never_resolve_a_house(self):
        rejected = [self.precise(address_level=False, precision='Approximate street or place location'),
                    self.precise(street_address='1009 Example Street'),
                    self.precise(street_address='100 Example Court'),
                    self.precise(lat=40.7, lng=-73.9)]
        for row in rejected:
            with self.subTest(row=row):
                self.service.cache.clear()
                self.suggestions([row])
                with patch('server.urlopen', return_value=self.response({'result': {'addressMatches': []}})), \
                        patch('server.time.sleep'):
                    result = self.service.search('100 Example Street')
                self.assertEqual(result['results'], [])
                self.assertEqual(result['status'], 'no_match')
                self.assertIn('city or ZIP', result['message'])

    def test_city_qualifier_and_complete_street_name_are_required(self):
        self.suggestions([self.precise()])
        for query in ('100 Example', '100 Example Street, Berkeley, CA', '100 Example Street 99999'):
            with self.subTest(query=query), \
                    patch('server.urlopen', return_value=self.response({'result': {'addressMatches': []}})), \
                    patch('server.time.sleep'):
                self.assertEqual(self.service.search(query)['results'], [])

    def test_city_name_cannot_match_a_subsequence_of_another_city(self):
        self.suggestions([self.precise(display_name='100 Example Street, South San Francisco, CA 94080')])
        with patch('server.urlopen', return_value=self.response({'result': {'addressMatches': []}})):
            self.assertEqual(self.service.search('100 Example Street, San Francisco, CA')['results'], [])

    def test_state_or_zip_qualifiers_can_omit_the_city(self):
        self.suggestions([self.precise()])
        with patch('server.urlopen', side_effect=AssertionError('exact qualifiers should resolve')):
            for query in ('100 Example St, CA', '100 Example Street 94612',
                          '100 Example Street, Oakland, California',
                          '100 Example Street, Oakland 94612'):
                with self.subTest(query=query):
                    self.assertEqual(len(self.service.search(query)['results']), 1)

    def test_same_street_address_in_two_cities_remains_ambiguous(self):
        self.suggestions([self.precise(), self.precise(display_name='100 Example Street, Berkeley, CA 94704')])
        with patch('server.urlopen', side_effect=AssertionError('show both precise suggestions')):
            self.assertEqual(len(self.service.search('100 Example Street')['results']), 2)

    def test_provider_failure_is_not_cached_as_no_match(self):
        for status in ('unavailable', 'busy'):
            self.suggestions([], remote_status=status)
            with self.subTest(status=status), \
                    patch('server.urlopen', return_value=self.response({'result': {'addressMatches': []}})), \
                    patch('server.time.sleep'):
                with self.assertRaisesRegex(RuntimeError, 'temporarily unavailable'):
                    self.service.search('100 Example Street')
            self.assertFalse(self.service.cache)
            self.assertFalse(self.service.lock.locked())

    def test_census_still_resolves_when_suggestions_are_unavailable(self):
        self.suggestions([], remote_status='unavailable')
        with patch('server.urlopen', return_value=self.response({'result': {'addressMatches': [self.match]}})):
            result = self.service.search('100 Example Street')
        self.assertEqual(result['provider'], 'U.S. Census Geocoder')
        self.assertEqual(result['status'], 'matched')

    def test_census_network_failure_has_no_false_no_match_response(self):
        self.suggestions([])
        with patch('server.urlopen', side_effect=URLError('private query omitted')):
            with self.assertRaisesRegex(RuntimeError, 'temporarily unavailable'):
                self.service.search('100 Example Street')
        self.assertFalse(self.service.cache)

    def test_bad_provider_shapes_fail_cleanly_and_release_lock(self):
        for payload in ([], None, {'result': []}, {'result': {'addressMatches': None}}):
            with self.subTest(payload=payload), patch('server.urlopen', return_value=self.response(payload)), patch('server.time.sleep'):
                with self.assertRaisesRegex(RuntimeError, 'temporarily unavailable'):
                    self.service.search('100 Example Street')
                self.assertFalse(self.service.lock.locked())
                self.assertFalse(self.service.cache)

    def test_malformed_cache_entry_is_refreshed_and_private_file_is_written(self):
        self.service.cache['100 example street'] = {'saved_at': 'broken', 'results': []}
        payload = {'result': {'addressMatches': [None, self.match]}}
        with patch('server.urlopen', return_value=self.response(payload)), patch('server.time.sleep'):
            result = self.service.search('100 Example Street')
        self.assertFalse(result['cached'])
        self.assertEqual(len(result['results']), 1)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        with patch('server.urlopen', side_effect=AssertionError('cache hit must not call provider')):
            self.assertTrue(self.service.search('100 EXAMPLE STREET')['cached'])

    def test_negative_cache_expires_quickly_and_total_entries_are_bounded(self):
        self.service.cache['100 example street'] = {'saved_at': 1000, 'results': []}
        payload = {'result': {'addressMatches': [self.match]}}
        with patch('server.time.time', return_value=1301), patch('server.time.sleep'), \
                patch('server.urlopen', side_effect=lambda *a, **kw: self.response(payload)), \
                patch.object(self.service, 'MAX_CACHE_ENTRIES', 2):
            self.assertFalse(self.service.search('100 Example Street')['cached'])
            self.service.search('200 Example Street')
            self.service.search('300 Example Street')
        self.assertEqual(len(self.service.cache), 2)
        self.assertNotIn('100 example street', self.service.cache)

    def test_invalid_queries_never_call_provider(self):
        with patch('server.urlopen', side_effect=AssertionError('invalid input must stay local')):
            for query in (None, '100 Example\nStreet', '100 Example\x7fStreet', 'abc', 'x' * 251):
                with self.subTest(query=query), self.assertRaises(ValueError):
                    self.service.search(query)
