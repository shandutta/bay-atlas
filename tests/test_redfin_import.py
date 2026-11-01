"""Cross-provider identity and transactional manual-import regressions."""
from datetime import date
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import hashlib

from model import merge_sales, prepare_sales
from refresh import ImportBusyError, RefreshManager
from server import AtlasServer, DataStore, MAX_IMPORT_BYTES
from test_redfin_csv import csv_bytes, home, parse
from redfin_csv import URL_HEADER

TODAY = date(2026, 10, 29)


def zillow(**changes):
    row = dict(parse([home()])['sales'][0])
    row.update(id='1234', source='Zillow', source_url='https://www.zillow.com/homedetails/1234_zpid/')
    for key in ('street_address', 'source_id', 'export_sha256', 'imported_at', 'export_filename'):
        row.pop(key, None)
    row.update(changes)
    return row


class MergeTests(unittest.TestCase):
    def test_full_and_street_addresses_merge_across_providers_without_id_collision(self):
        old = zillow(address='100 Example Street, Berkeley, CA 94707')
        same = parse([home()])['sales'][0]
        other = dict(same, address='200 Other Rd', street_address='200 Other Rd')
        merged, stats = merge_sales([old], [same], TODAY)
        self.assertEqual(len(merged), 1)
        self.assertEqual(stats['duplicates'], 1)
        self.assertEqual(merged[0]['source'], 'Zillow')
        merged, stats = merge_sales([old], [other], TODAY)
        self.assertEqual({row['id'] for row in merged}, {'1234', 'redfin:1234'})
        self.assertEqual(stats['added'], 1)

    def test_omitted_suffix_requires_matching_city_and_near_identical_location(self):
        old = zillow(address='100 Synthetic Example St, Berkeley, CA 94707')
        incoming = dict(parse([home()])['sales'][0], address='100 Synthetic Example', street_address='100 Synthetic Example')
        self.assertEqual(merge_sales([old], [incoming], TODAY)[1]['duplicates'], 1)
        for changes in ({'city': 'Oakland'}, {'lat': 37.92}):
            merged, stats = merge_sales([old], [dict(incoming, **changes)], TODAY)
            self.assertEqual(len(merged), 2)
            self.assertEqual(stats['added'], 1)
        explicit_different_suffix = dict(incoming, address='100 Synthetic Example Rd', street_address='100 Synthetic Example Rd')
        self.assertEqual(merge_sales([old], [explicit_different_suffix], TODAY)[1]['added'], 1)

    def test_newest_sale_wins_and_same_date_conflict_preserves_saved_values(self):
        old = zillow(sold_date='2026-10-01', price=1_900_000)
        latest = parse([home()])['sales'][0]
        merged, stats = merge_sales([old], [latest], TODAY)
        self.assertEqual(stats['updated'], 1)
        self.assertEqual(merged[0]['price'], 2_000_000)
        result, stale = merge_sales(merged, [old], TODAY)
        self.assertEqual(stale['stale'], 1)
        self.assertEqual(result[0]['sold_date'], '2026-10-28')
        result, conflict = merge_sales([zillow()], [dict(latest, price=3_000_000)], TODAY)
        self.assertEqual(conflict['conflicts'], 1)
        self.assertEqual(result[0]['price'], 2_000_000)
        self.assertEqual(result[0]['source'], 'Zillow')

    def test_allowlisted_source_labels_cannot_be_spoofed(self):
        good = parse([home()])['sales'][0]
        accepted, _ = prepare_sales([dict(good, source='Zillow')], TODAY)
        self.assertEqual(accepted[0]['source'], 'Redfin')
        for url in ('https://redfin.com.evil.test/a', 'https://user:pass@redfin.com/a',
                    'http://redfin.com/a', 'https://redfin.com:444/a'):
            self.assertFalse(prepare_sales([dict(good, source_url=url)], TODAY)[0])


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = RefreshManager(self.root)
        self.path = self.root / 'data' / 'sales.json'
        self.path.parent.mkdir()
        self.original = json.dumps({'sales': [zillow(address='999 Original Rd, Berkeley, CA 94707')],
                                    'source': 'Zillow snapshot', 'coverage': {'complete': False}}, indent=2).encode()
        self.path.write_bytes(self.original)
        clock = patch('refresh.today_utc', return_value=TODAY)
        clock.start()
        self.addCleanup(clock.stop)
        network = patch('refresh.urlopen', side_effect=AssertionError('Import must not call the network'))
        self.net = network.start()
        self.addCleanup(network.stop)

    def payload(self, rows=None):
        return {'csv': csv_bytes(rows if rows is not None else [home()]).decode(), 'filename': '/private/downloads/export.csv'}

    def retained_exclusion(self, **changes):
        row = dict(zillow(), reason='explicit_attached_home',
                   attachment_evidence='Zillow resoFacts.hasAttachedProperty true')
        row.update(changes)
        return row

    def test_known_attached_cross_provider_home_is_excluded_with_raw_provenance_preserved(self):
        prior = json.loads(self.path.read_text())
        evidence = self.retained_exclusion()
        prior['excluded_attached_properties'] = [evidence]
        self.path.write_text(json.dumps(prior))
        good = home(**{'ADDRESS': '200 Different St', URL_HEADER:
                       'https://www.redfin.com/CA/Berkeley/200-Different-St-94707/home/5678'})
        payload = self.payload([home(), good])
        result = self.manager.import_redfin(payload)
        self.assertEqual(result['counts']['added'], 1)
        self.assertEqual(result['counts']['excluded_attached'], 1)
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved['excluded_attached_properties'], [evidence])
        self.assertEqual(saved['last_import']['export_sha256'], hashlib.sha256(payload['csv'].encode()).hexdigest())
        self.assertNotIn('redfin:1234', {row['id'] for row in saved['sales']})
        self.assertIn('redfin:5678', {row['id'] for row in saved['sales']})

    def test_attached_suffix_matching_does_not_exclude_distinct_nearby_homes(self):
        prior = json.loads(self.path.read_text())
        prior['excluded_attached_properties'] = [self.retained_exclusion()]
        self.path.write_text(json.dumps(prior))
        rows = [home(**{'ADDRESS': '100 Example'}),
                home(**{'ADDRESS': '100 Example Road', URL_HEADER:
                         'https://www.redfin.com/CA/Berkeley/100-Example-Rd-94707/home/5678'}),
                home(**{'ADDRESS': '102 Example St', URL_HEADER:
                         'https://www.redfin.com/CA/Berkeley/102-Example-St-94707/home/9012'})]
        result = self.manager.import_redfin(self.payload(rows))
        self.assertEqual(result['counts']['excluded_attached'], 1)
        self.assertEqual(result['counts']['added'], 2)

    def test_only_known_attached_import_preserves_exact_prior_bytes(self):
        prior = json.loads(self.path.read_text())
        prior['excluded_attached_properties'] = [self.retained_exclusion()]
        self.path.write_text(json.dumps(prior))
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'known to be attached'):
            self.manager.import_redfin(self.payload())
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.manager.operation_lock.locked())

    def test_unverified_exclusion_cannot_remove_a_home(self):
        prior = json.loads(self.path.read_text())
        prior['excluded_attached_properties'] = [self.retained_exclusion(attachment_evidence='unverified')]
        self.path.write_text(json.dumps(prior))
        result = self.manager.import_redfin(self.payload())
        self.assertEqual(result['counts']['excluded_attached'], 0)
        self.assertEqual(result['counts']['added'], 1)

    def test_success_publishes_mixed_source_provenance_and_safe_counts(self):
        prior = json.loads(self.path.read_text())
        prior['coverage'].update(label='East Bay starter snapshot', counts={'Oakland': 257})
        prior['warnings'] = ['Partial starter snapshot. Live API refresh needs a RapidAPI connection.',
                             'A relevant saved limitation.', 'Kept existing values for 1 conflicting sale records.',
                             'Kept existing values for 2 conflicting sale records.']
        self.path.write_text(json.dumps(prior))
        result = self.manager.import_redfin(self.payload())
        self.assertEqual(result['counts']['added'], 1)
        self.assertEqual(result['counts']['total'], 2)
        self.assertEqual(result['refresh_mode'], 'manual_csv')
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved['sources'], ['Redfin', 'Zillow'])
        self.assertFalse(saved['coverage']['complete'])
        self.assertEqual(saved['coverage']['label'], 'Imported sale snapshots')
        self.assertEqual(saved['coverage']['counts'], {'Berkeley': 2})
        self.assertEqual(saved['coverage']['source_counts'], {'Redfin': 1, 'Zillow': 1})
        self.assertIn('A relevant saved limitation.', saved['warnings'])
        self.assertFalse(any('starter snapshot' in warning for warning in saved['warnings']))
        self.assertEqual(sum('conflict' in warning for warning in saved['warnings']), 1)
        self.assertEqual(saved['last_import']['export_filename'], 'export.csv')
        self.assertEqual(saved['last_import']['export_sha256'], result['export_sha256'])
        self.assertNotIn('csv', saved['last_import'])
        served = DataStore(self.path).snapshot()
        self.assertEqual(served['source'], 'Redfin + Zillow')
        self.assertEqual(served['metadata']['last_import']['counts']['total'], 2)
        self.assertEqual(len(list(self.path.parent.glob('snapshots/*.json'))), 1)
        self.net.assert_not_called()

    def test_invalid_empty_and_oversized_imports_preserve_exact_original_bytes(self):
        for payload in ({'csv': 'PRICE\n1000', 'filename': 'bad.csv'}, self.payload([]),
                        self.payload([home(**{'SOLD DATE': ''})]),
                        {'csv': 'a' * (5 * 1024 * 1024 + 1), 'filename': 'huge.csv'}):
            with self.subTest(length=len(payload['csv'])), self.assertRaises(ValueError):
                self.manager.import_redfin(payload)
            self.assertEqual(self.path.read_bytes(), self.original)
        self.assertFalse((self.path.parent / 'snapshots').exists())
        self.assertFalse(self.manager.operation_lock.locked())

    def test_conflicting_only_import_and_corrupt_existing_data_fail_closed(self):
        self.path.write_text(json.dumps({'sales': [zillow()]}))
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.manager.import_redfin(self.payload([home(**{'PRICE': '2100000'})]))
        self.assertEqual(self.path.read_bytes(), before)
        self.path.write_text('broken JSON')
        with self.assertRaises(RuntimeError):
            self.manager.import_redfin(self.payload())
        self.assertEqual(self.path.read_text(), 'broken JSON')

    def test_failed_atomic_publication_preserves_previous_dataset(self):
        from refresh import atomic_json
        def fail_main(path, value, *args):
            if Path(path) == self.path:
                raise OSError('simulated write failure')
            return atomic_json(path, value, *args)
        with patch('refresh.atomic_json', side_effect=fail_main), self.assertRaises(RuntimeError):
            self.manager.import_redfin(self.payload())
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertFalse(self.manager.operation_lock.locked())

    def test_shared_operation_lock_blocks_both_import_and_refresh(self):
        self.manager.operation_lock.acquire()
        try:
            with self.assertRaises(ImportBusyError):
                self.manager.import_redfin(self.payload())
            self.assertEqual(self.manager.start()['state'], 'busy')
            self.assertEqual(self.path.read_bytes(), self.original)
        finally:
            self.manager.operation_lock.release()

    def test_http_origin_limits_busy_and_live_snapshot_contract(self):
        server = AtlasServer(('127.0.0.1', 0), data_path=self.path, refresh_manager=self.manager)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host = '127.0.0.1:' + str(server.server_port)
        def request(payload, *, origin=None, length=None):
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            headers = {'Origin': origin or 'http://' + host, 'Content-Type': 'application/json'}
            if length is not None:
                headers['Content-Length'] = str(length)
            conn.request('POST', '/homes/api/import/redfin', body=json.dumps(payload), headers=headers)
            response = conn.getresponse()
            result = response.status, json.loads(response.read())
            conn.close()
            return result
        try:
            self.assertEqual(request(self.payload(), origin='https://evil.test')[0], 403)
            self.assertEqual(request({}, length=MAX_IMPORT_BYTES + 1)[0], 413)
            self.manager.operation_lock.acquire()
            try:
                self.assertEqual(request(self.payload())[0], 409)
            finally:
                self.manager.operation_lock.release()
            status, body = request(self.payload())
            self.assertEqual(status, 200)
            self.assertEqual(body['counts']['total'], 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
