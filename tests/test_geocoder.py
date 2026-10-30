"""Submitted-address search regressions; all remote responses are mocked."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

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
